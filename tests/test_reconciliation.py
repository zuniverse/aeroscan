"""Reconciliation, and what happens when a run comes back from the dead.

Re-flying an inspection means a pilot back on site, a weather window
and sometimes an overflight clearance, and the structure may have
changed in the meantime, so an upload that ends a few frames short
must not throw the flight away. That makes INCOMPLETE a transitory
state, which in turn makes completion callable more than once, which
raises the question these tests answer: what does a second completion
change, and what must it never change.
"""
from tests.conftest import manifest


def test_incomplete_run_names_its_missing_files(client, drone, site, headers, bucket):
    """Short of frames, completion reports which ones, not just how many.

    Counting is not enough: the drone has to know what to re-upload.
    """
    created = client.post(
        "/v1/runs",
        json={
            "drone_run_uid": "run-rec-1",
            "site_id": site.id,
            "started_at": "2026-07-25T09:00:00Z",
            "files": manifest(5),
        },
        headers=headers,
    ).json()
    run_id = created["run_id"]

    bucket.update(f["key"] for f in manifest(3))
    client.post(
        f"/v1/runs/{run_id}/file-confirmations",
        json={"files": manifest(3)},
        headers=headers,
    )

    completion = client.post(
        f"/v1/runs/{run_id}/completion",
        json={"defect_detected": True, "defect_count": 42, "confidence_score": 0.871},
        headers=headers,
    ).json()

    assert completion["status"] == "INCOMPLETE"
    assert completion["missing_file_count"] == 2
    assert completion["missing_keys"] == ["frame_00004.jpg", "frame_00005.jpg"]
    assert completion["missing_keys_truncated"] is False
    assert completion["results_recorded"] is True


def test_late_files_promote_the_run_and_never_touch_results(client, drone, site, headers, bucket):
    """The central guarantee of the whole design.

    A drone that recovers can upload what was missing and complete
    again, which promotes INCOMPLETE to COMPLETED. The second call
    carries inspection values, and it must be ignored: results belong
    to the first completion for good. They feed maintenance decisions
    on physical assets, and nothing could arbitrate between two
    contradictory readings after the fact.

    The second payload here is deliberately contradictory, so the test
    fails loudly if the write-once rule ever regresses.
    """
    created = client.post(
        "/v1/runs",
        json={
            "drone_run_uid": "run-rec-2",
            "site_id": site.id,
            "started_at": "2026-07-25T09:00:00Z",
            "files": manifest(5),
        },
        headers=headers,
    ).json()
    run_id = created["run_id"]

    bucket.update(f["key"] for f in manifest(3))
    client.post(
        f"/v1/runs/{run_id}/file-confirmations",
        json={"files": manifest(3)},
        headers=headers,
    )
    client.post(
        f"/v1/runs/{run_id}/completion",
        json={"defect_detected": True, "defect_count": 42, "confidence_score": 0.871},
        headers=headers,
    )

    # The drone comes back and finishes the upload.
    bucket.update(f["key"] for f in manifest(5)[3:])
    client.post(
        f"/v1/runs/{run_id}/file-confirmations",
        json={"files": manifest(5)[3:]},
        headers=headers,
    )
    second = client.post(
        f"/v1/runs/{run_id}/completion",
        json={"defect_detected": False, "defect_count": 0, "confidence_score": 0.100},
        headers=headers,
    ).json()

    assert second["status"] == "COMPLETED"
    assert second["missing_file_count"] == 0
    assert second["results_recorded"] is False

    stored = client.get(
        "/v1/runs",
        params={"drone_id": drone.id},
        headers={"X-API-Key": "dev-backoffice-key"},
    ).json()["items"][0]

    assert stored["defect_detected"] is True
    assert stored["defect_count"] == 42
    assert float(stored["confidence_score"]) == 0.871


def test_sweep_abandons_only_runs_past_their_threshold(client, drone, site, headers, db):
    """Two thresholds, and the sweep must respect the difference.

    A silent UPLOADING run has probably lost its drone mid-transfer.
    An INCOMPLETE run already has its results recorded and may be
    waiting on someone to travel back to the site, so it is given days
    where the other gets hours. A single threshold would either kill
    recoverable runs or leave dead ones forever.
    """
    from datetime import datetime, timedelta, timezone

    import sqlalchemy as sa

    from app.models import Run, RunStatus
    from app.routers.queries import sweep_abandoned_runs

    for uid in ("run-sweep-stale", "run-sweep-fresh"):
        client.post(
            "/v1/runs",
            json={
                "drone_run_uid": uid,
                "site_id": site.id,
            "started_at": "2026-07-25T09:00:00Z",
                "files": manifest(5),
            },
            headers=headers,
        )

    long_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    db.execute(
        sa.update(Run)
        .where(Run.drone_run_uid == "run-sweep-stale")
        .values(last_activity_at=long_ago)
    )

    swept_uploading, swept_incomplete = sweep_abandoned_runs(db)

    assert swept_uploading == 1
    assert swept_incomplete == 0

    statuses = {
        uid: db.scalar(sa.select(Run.status).where(Run.drone_run_uid == uid))
        for uid in ("run-sweep-stale", "run-sweep-fresh")
    }
    assert statuses["run-sweep-stale"] == RunStatus.ABANDONED.value
    assert statuses["run-sweep-fresh"] == RunStatus.UPLOADING.value


def test_completion_does_not_trust_a_confirmation_without_an_upload(
    client, drone, site, headers, bucket
):
    """The guarantee the whole verification step exists for.

    The drone confirms all five frames but only four ever reach the
    bucket, which is what a silently failed PUT looks like from the
    API's side: the confirmation arrives, the bytes do not. Trusting
    the count alone would mark this run COMPLETED and report an asset
    as inspected when part of it was never seen.

    The missing frame is reported twice over, and the distinction
    matters: `missing_keys` says the run is short, `unverified_keys`
    says the drone believes otherwise, which points at the drone
    rather than at the link.
    """
    created = client.post(
        "/v1/runs",
        json={
            "drone_run_uid": "run-rec-3",
            "site_id": site.id,
            "started_at": "2026-07-25T09:00:00Z",
            "files": manifest(5),
        },
        headers=headers,
    ).json()
    run_id = created["run_id"]

    # Four of the five actually land.
    bucket.update(f["key"] for f in manifest(4))

    client.post(
        f"/v1/runs/{run_id}/file-confirmations",
        json={"files": manifest(5)},
        headers=headers,
    )

    completion = client.post(
        f"/v1/runs/{run_id}/completion",
        json={"defect_detected": True, "defect_count": 7, "confidence_score": 0.500},
        headers=headers,
    ).json()

    assert completion["confirmed_file_count"] == 5
    assert completion["verified_file_count"] == 4
    assert completion["status"] == "INCOMPLETE"
    assert completion["missing_keys"] == ["frame_00005.jpg"]
    assert completion["unverified_keys"] == ["frame_00005.jpg"]
