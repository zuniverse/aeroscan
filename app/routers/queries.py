from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import require_backoffice_key
from app.config import get_settings
from app.db import DbSession
from app.errors import ApiError
from app.models import Run, RunStatus
from app.schemas import RunPage, RunSummary, SweepResponse

router = APIRouter(prefix="/v1", tags=["backoffice"])
settings = get_settings()


def _encode_cursor(run: Run) -> str:
    """Opaque cursor carrying the sort key of the last row returned.

    Opaque on purpose: it encodes an implementation detail (the sort
    key), so making it look opaque discourages clients from building
    one by hand and freezing the ordering into their code.
    """
    payload = {
        "c": str(run.confidence_score) if run.confidence_score is not None else None,
        "i": str(run.id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[Decimal | None, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        score = payload["c"]
        return (Decimal(score) if score is not None else None, uuid.UUID(payload["i"]))
    except (KeyError, ValueError, TypeError, binascii.Error, InvalidOperation) as exc:
        raise ApiError(422, "invalid_cursor", "cursor is malformed") from exc


@router.get(
    "/runs", response_model=RunPage, dependencies=[Depends(require_backoffice_key)]
)
def list_runs(
    db: DbSession,
    site_id: int | None = None,
    drone_id: int | None = None,
    defect: bool | None = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: Annotated[
        int, Query(ge=1, le=settings.max_page_size)
    ] = settings.default_page_size,
) -> RunPage:
    """List runs for the web app, ordered by confidence score.

    Backs the query the product cares about: defect detection at one
    site over a window, worst confidence first. `idx_runs_defective`
    is partial on defect_detected and leads with site then
    started_at, so that query narrows to the few hundred rows in the
    window before anything else happens.

    Measured on 500 000 runs, 150 000 of them defective: 2.3 ms for
    the first page. The plan does include a top-N heapsort, because a
    range predicate on started_at means the index cannot also deliver
    rows pre-sorted by confidence. Sorting the ~500 rows the window
    yields is free; the alternative index, leading with confidence to
    avoid the sort, scanned 5 000 rows to discard 4 950 and measured
    eight times slower.

    Pagination is keyset rather than OFFSET. Same dataset, page 100:
    1.4 ms against 48 ms, and OFFSET degrades with depth while keyset
    does not. Postgres also folds the cursor predicate into the index
    condition, so later pages scan less, not more.
    """
    # Typed loosely on purpose: a nullable boolean column is a
    # ColumnElement[bool | None], which does not fit a list of
    # ColumnElement[bool] even though SQL treats it as a predicate.
    conditions: list[Any] = [
        c
        for c in (
            Run.site_id == site_id if site_id is not None else None,
            Run.drone_id == drone_id if drone_id is not None else None,
            Run.status == status if status is not None else None,
            Run.started_at >= started_from if started_from is not None else None,
            Run.started_at <= started_to if started_to is not None else None,
        )
        if c is not None
    ]

    # Written as a bare truth test, not `== True`, so the predicate
    # matches the partial index and the planner can use it.
    if defect is True:
        conditions.append(Run.defect_detected)
    elif defect is False:
        conditions.append(sa.not_(Run.defect_detected))

    if cursor is not None:
        score, last_id = _decode_cursor(cursor)
        if score is None:
            # Already in the NULL-confidence tail, which sorts last.
            conditions.append(sa.and_(Run.confidence_score.is_(None), Run.id < last_id))
        else:
            conditions.append(
                sa.or_(
                    sa.tuple_(Run.confidence_score, Run.id)
                    < sa.tuple_(sa.literal(score), sa.literal(last_id)),
                    Run.confidence_score.is_(None),
                )
            )

    rows = db.scalars(
        sa.select(Run)
        .where(*conditions)
        # NULLS LAST because a run without results is the least
        # interesting, not the most. Costs nothing: the plan sorts
        # anyway, so null placement is not what drives the cost.
        .order_by(Run.confidence_score.desc().nullslast(), Run.id.desc())
        .limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]
    return RunPage(
        items=[RunSummary.model_validate(r) for r in page],
        next_cursor=_encode_cursor(page[-1]) if has_more and page else None,
    )


def sweep_abandoned_runs(db: Session, now: datetime | None = None) -> tuple[int, int]:
    """Mark runs that stopped talking to us as abandoned.

    Two thresholds, because the two situations have different natural
    timescales. A run still UPLOADING and silent for hours has almost
    certainly lost its drone mid-transfer. A run sitting INCOMPLETE
    is a different animal: its results are already recorded, and
    recovering the missing files may need someone to walk onto a
    site floor, so it is given days rather than hours.

    Plain function taking a session, with the route below as a thin
    wrapper: tests call it directly, and in production a scheduler
    calls the route. Which scheduler is a deployment decision, so no
    scheduling library is pulled into the application.
    """
    now = now or datetime.now(timezone.utc)

    def _sweep(from_status: RunStatus, cutoff: datetime) -> int:
        return len(
            db.execute(
                sa.update(Run)
                .where(Run.status == from_status.value, Run.last_activity_at < cutoff)
                .values(status=RunStatus.ABANDONED.value)
                .returning(Run.id)
            ).all()
        )

    uploading = _sweep(
        RunStatus.UPLOADING,
        now - timedelta(hours=settings.uploading_idle_timeout_hours),
    )
    incomplete = _sweep(
        RunStatus.INCOMPLETE,
        now - timedelta(days=settings.incomplete_idle_timeout_days),
    )
    db.commit()
    return uploading, incomplete


@router.post(
    "/admin/sweep-abandoned-runs",
    response_model=SweepResponse,
    dependencies=[Depends(require_backoffice_key)],
)
def run_sweep(db: DbSession) -> SweepResponse:
    uploading, incomplete = sweep_abandoned_runs(db)
    return SweepResponse(swept_uploading=uploading, swept_incomplete=incomplete)
