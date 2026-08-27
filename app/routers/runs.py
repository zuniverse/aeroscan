from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Response, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth import AuthedMachine
from app.config import get_settings
from app.db import DbSession
from app.errors import ApiError
from app.models import Drone, Run, RunFile, RunStatus, Site
from app.schemas import (
    CompletionRequest,
    CompletionResponse,
    ConfirmationsRequest,
    ConfirmationsResponse,
    PresignedUpload,
    RunCreate,
    RunCreated,
    UploadUrlsRequest,
    UploadUrlsResponse,
)
from app.storage import list_uploaded_keys, presign_put, run_prefix

router = APIRouter(prefix="/v1/runs", tags=["drone"])
settings = get_settings()


def _load_owned_run(db: Session, drone: Drone, run_id: uuid.UUID) -> Run:
    """Fetch a run, or 404.

    A run belonging to another drone yields 404 rather than 403: a
    distinct status would confirm the run exists to a caller who has no
    business knowing it.
    """
    run = db.get(Run, run_id)
    if run is None or run.drone_id != drone.id:
        raise ApiError(404, "run_not_found", f"no run {run_id} for this drone")
    return run


def _reject_if_abandoned(run: Run) -> None:
    if run.status == RunStatus.ABANDONED.value:
        raise ApiError(
            409,
            "run_abandoned",
            "this run was swept as abandoned and no longer accepts uploads",
            {"run_id": str(run.id), "status": run.status},
        )


def _presign_pending(db: Session, run: Run, limit: int) -> list[PresignedUpload]:
    """Sign URLs for the files this run is still waiting on."""
    keys = db.scalars(
        sa.select(RunFile.file_key)
        .where(RunFile.run_id == run.id, RunFile.confirmed_at.is_(None))
        .order_by(RunFile.file_key)
        .limit(limit)
    ).all()
    return [
        PresignedUpload(
            key=key,
            url=presign_put(run.id, key),
            expires_in=settings.presigned_url_ttl_seconds,
        )
        for key in keys
    ]


@router.post("", response_model=RunCreated)
def create_run(
    payload: RunCreate,
    response: Response,
    drone: AuthedMachine,
    db: DbSession,
) -> RunCreated:
    """Register a run and its file manifest. Safe to retry.

    Idempotency rests on the drone-generated `drone_run_uid` and the
    unique constraint on (drone_id, drone_run_uid): the insert is
    attempted, and a conflict means this is a retry, so the existing run
    is returned instead. Doing it in one statement rather than
    check-then-insert leaves no window for two concurrent retries to
    both create a run.

    A retry returns 200 and freshly signed URLs. Regenerating them is
    correct rather than a workaround: the previous ones may well have
    expired, which is often why the drone is retrying at all.
    """
    # Checked before the insert rather than left to the foreign key,
    # so a bad site_id comes back as a 422 naming the field instead of
    # an IntegrityError surfacing as a 500.
    if not db.scalar(sa.select(sa.literal(1)).where(Site.id == payload.site_id)):
        raise ApiError(
            422,
            "unknown_site",
            f"no site {payload.site_id}",
            {"site_id": payload.site_id},
        )

    now = datetime.now(timezone.utc)
    new_id = uuid.uuid4()

    inserted_id = db.execute(
        pg_insert(Run)
        .values(
            id=new_id,
            drone_id=drone.id,
            # From the mission, then frozen. A drone is not tied to a
            # site, and the run happened here on this date regardless of
            # anything that changes later.
            site_id=payload.site_id,
            drone_run_uid=payload.drone_run_uid,
            status=RunStatus.UPLOADING.value,
            operator=payload.operator,
            started_at=payload.started_at,
            last_activity_at=now,
            expected_file_count=len(payload.files),
            confirmed_file_count=0,
            total_size_bytes=0,
        )
        .on_conflict_do_nothing(constraint="uq_runs_drone_uid")
        .returning(Run.id)
    ).scalar_one_or_none()

    created = inserted_id is not None

    if created:
        # One row per expected file, unconfirmed. Holding the manifest
        # in table form is what lets completion name the missing keys
        # rather than just count them.
        #
        # Passing a list of dicts to execute() batches through
        # SQLAlchemy's insertmanyvalues, which handles ~18 000 rows in
        # one round trip family. COPY would be faster still and is the
        # obvious escalation if manifests grow an order of magnitude.
        db.execute(
            sa.insert(RunFile),
            [
                {"run_id": new_id, "file_key": f.key, "size_bytes": f.size_bytes}
                for f in payload.files
            ],
        )
    # One lookup for both paths: on conflict the insert returned no id,
    # so the run has to be read back anyway, and reading it the same way
    # in both cases keeps a single place where it can be missing.
    run = db.scalar(
        sa.select(Run).where(
            Run.drone_id == drone.id,
            Run.drone_run_uid == payload.drone_run_uid,
        )
    )
    if run is None:
        # Either the insert succeeded or it conflicted with an existing
        # row, so the run exists by construction. Reaching this means
        # something deleted it mid-request, which no path in the service
        # does; better a clear 500 than an unexplained attribute error.
        raise ApiError(500, "run_lookup_failed", "the run vanished during creation")

    if not created:
        _reject_if_abandoned(run)

    db.commit()
    db.refresh(run)

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return RunCreated(
        run_id=run.id,
        status=run.status,
        expected_file_count=run.expected_file_count,
        confirmed_file_count=run.confirmed_file_count,
        upload_prefix=run_prefix(run.id),
        upload_urls=_presign_pending(db, run, settings.max_upload_urls_per_batch),
        already_existed=not created,
    )


@router.post("/{run_id}/upload-urls", response_model=UploadUrlsResponse)
def issue_upload_urls(
    run_id: uuid.UUID,
    payload: UploadUrlsRequest,
    drone: AuthedMachine,
    db: DbSession,
) -> UploadUrlsResponse:
    """Hand out presigned URLs for a slice of the manifest.

    Exists because signing all ~18 000 URLs up front would mean roughly
    12 MB of JSON in the creation response, most of it expired by the
    time the drone reached it. Issuing them on demand keeps responses
    small and URLs fresh.

    Only keys present in the manifest are signed, so a URL can never
    authorise an object the run never declared.
    """
    run = _load_owned_run(db, drone, run_id)
    _reject_if_abandoned(run)

    known = set(
        db.scalars(
            sa.select(RunFile.file_key).where(
                RunFile.run_id == run.id, RunFile.file_key.in_(payload.keys)
            )
        ).all()
    )

    return UploadUrlsResponse(
        urls=[
            PresignedUpload(
                key=key,
                url=presign_put(run.id, key),
                expires_in=settings.presigned_url_ttl_seconds,
            )
            for key in payload.keys
            if key in known
        ],
        unknown_keys=[key for key in payload.keys if key not in known],
    )


@router.post("/{run_id}/file-confirmations", response_model=ConfirmationsResponse)
def confirm_files(
    run_id: uuid.UUID,
    payload: ConfirmationsRequest,
    drone: AuthedMachine,
    db: DbSession,
) -> ConfirmationsResponse:
    """Mark a batch of files as uploaded.

    Confirmation is an UPDATE of pre-existing manifest rows, not an
    upsert: a key the run never declared cannot create a row, it is
    reported back in `unknown_keys`. An unknown key does not fail the
    whole batch, since dropping 999 valid confirmations because of one
    bad entry would be a poor trade against a flaky field client.

    Retry safety comes from `confirmed_at IS NULL` in the WHERE clause.
    A replayed batch matches nothing, RETURNING yields no rows, and the
    counters are incremented by zero. That is also why the response
    reports `newly_confirmed` separately from the batch size.
    """
    run = _load_owned_run(db, drone, run_id)
    _reject_if_abandoned(run)

    now = datetime.now(timezone.utc)
    incoming = sa.values(
        sa.column("file_key", sa.String),
        sa.column("size_bytes", sa.BigInteger),
        name="incoming",
    ).data([(f.key, f.size_bytes) for f in payload.files])

    confirmed = db.execute(
        sa.update(RunFile)
        .where(
            RunFile.run_id == run.id,
            RunFile.file_key == incoming.c.file_key,
            RunFile.confirmed_at.is_(None),
        )
        # Cast is load-bearing, not decoration. When every row in a
        # batch omits size_bytes, the VALUES column is a column of bare
        # NULLs, which Postgres types as text and then refuses to assign
        # to a bigint. Casting pins the type regardless of the batch.
        .values(
            confirmed_at=now,
            size_bytes=sa.cast(incoming.c.size_bytes, sa.BigInteger),
        )
        .returning(RunFile.file_key, RunFile.size_bytes)
    ).all()

    newly_confirmed = len(confirmed)
    added_bytes = sum(size or 0 for _, size in confirmed)

    # Incremented in SQL rather than read-modify-write in Python, so two
    # batches landing concurrently cannot lose an increment.
    count, expected, run_status = db.execute(
        sa.update(Run)
        .where(Run.id == run.id)
        .values(
            confirmed_file_count=Run.confirmed_file_count + newly_confirmed,
            total_size_bytes=Run.total_size_bytes + added_bytes,
            last_activity_at=now,
        )
        .returning(Run.confirmed_file_count, Run.expected_file_count, Run.status)
    ).one()
    db.commit()

    submitted = {f.key for f in payload.files}
    known = set(
        db.scalars(
            sa.select(RunFile.file_key).where(
                RunFile.run_id == run.id, RunFile.file_key.in_(submitted)
            )
        ).all()
    )

    return ConfirmationsResponse(
        run_id=run.id,
        status=run_status,
        newly_confirmed=newly_confirmed,
        confirmed_file_count=count,
        expected_file_count=expected,
        unknown_keys=sorted(submitted - known),
    )


@router.post("/{run_id}/completion", response_model=CompletionResponse)
def complete_run(
    run_id: uuid.UUID,
    payload: CompletionRequest,
    drone: AuthedMachine,
    db: DbSession,
) -> CompletionResponse:
    """Declare a run finished, reconcile files, set the final status.

    Reconciliation is against the bucket, not against what the drone
    said. Confirmations arrive over hours and are only a claim: the API
    never sees the bytes, so a drone can report a file it failed to
    upload, and a run marked COMPLETED on that basis would report an
    asset as inspected when part of it was never seen. One listing of
    the run prefix, around 18 paginated calls, settles it against the
    only authority that matters.

    That splits two faults the drone's own count conflates. Files
    never confirmed are an unfinished upload. Files confirmed but
    absent from the bucket are a drone whose view of reality is
    wrong, which is worth naming separately in `unverified_keys`.

    Two rules govern retries, and they pull in opposite directions.

    Inspection results are written once, by the first call, and never
    overwritten. These feed maintenance decisions on physical assets,
    so a value that silently changes after the fact is worse than a
    missing file, and the flight cannot be repeated to arbitrate.

    Reconciliation, on the other hand, runs every time. A run that came
    back INCOMPLETE can have its missing files uploaded later and then
    be completed again, which promotes it to COMPLETED without touching
    the results. That matters because a flight is expensive to repeat:
    declaring a run dead over a handful of files lost to a dropped link
    would throw away hours of transfer and send a crew back on site.

    The row is locked for the duration so two concurrent completions
    cannot both believe they are the first.
    """
    run = db.scalar(sa.select(Run).where(Run.id == run_id).with_for_update())
    if run is None or run.drone_id != drone.id:
        raise ApiError(404, "run_not_found", f"no run {run_id} for this drone")
    _reject_if_abandoned(run)

    now = datetime.now(timezone.utc)
    first_completion = run.completed_at is None

    expected_keys = set(
        db.scalars(sa.select(RunFile.file_key).where(RunFile.run_id == run.id)).all()
    )
    confirmed_keys = set(
        db.scalars(
            sa.select(RunFile.file_key).where(
                RunFile.run_id == run.id, RunFile.confirmed_at.is_not(None)
            )
        ).all()
    )

    if settings.verify_uploads_against_s3:
        present = list_uploaded_keys(run.id) & expected_keys
        verified_count = len(present)
    else:
        # Fallback for environments without a bucket. Degrades to the
        # drone's own account, which is exactly the guarantee this
        # endpoint exists to stop relying on, so it is off by default.
        present = confirmed_keys
        verified_count = None

    missing = expected_keys - present
    unverified = confirmed_keys - present

    new_values: dict = {
        "status": (
            RunStatus.COMPLETED.value if not missing else RunStatus.INCOMPLETE.value
        ),
        "reconciled_at": now,
        "last_activity_at": now,
    }
    if first_completion:
        new_values |= {
            "completed_at": now,
            "defect_detected": payload.defect_detected,
            "defect_count": payload.defect_count,
            "confidence_score": payload.confidence_score,
        }

    db.execute(sa.update(Run).where(Run.id == run.id).values(**new_values))
    db.commit()

    # Bounded samples rather than the full sets: a run that lost 18 000
    # files should not answer with an 18 000-entry body.
    limit = settings.max_missing_keys_reported
    ordered_missing = sorted(missing)

    return CompletionResponse(
        run_id=run.id,
        status=new_values["status"],
        expected_file_count=run.expected_file_count,
        confirmed_file_count=run.confirmed_file_count,
        verified_file_count=verified_count,
        missing_file_count=len(missing),
        missing_keys=ordered_missing[:limit],
        missing_keys_truncated=len(ordered_missing) > limit,
        unverified_keys=sorted(unverified)[:limit],
        results_recorded=first_completion,
        completed_at=now if first_completion else run.completed_at,
    )
