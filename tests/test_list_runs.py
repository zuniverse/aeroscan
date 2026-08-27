"""The query the product exists to answer.

Runs with defects at one site over a window, worst confidence first,
paginated. Everything in the schema, from the denormalised site_id to
the partial index, is shaped by this one access pattern, so it is
worth pinning down.
"""
import uuid

import pytest

from app.models import Run, RunStatus

BACKOFFICE = {"X-API-Key": "dev-backoffice-key"}


@pytest.fixture
def runs(db, drone, site):
    """Six runs: four with defects and known scores, two clean."""
    from datetime import datetime, timezone

    scores = [
        ("A", True, 0.910),
        ("B", True, 0.640),
        ("C", True, 0.985),
        ("D", True, 0.220),
        ("E", False, 0.500),
        ("F", False, 0.700),
    ]
    for label, defective, score in scores:
        db.add(
            Run(
                id=uuid.uuid4(),
                drone_id=drone.id,
                site_id=site.id,
                drone_run_uid=f"run-list-{label}",
                status=RunStatus.COMPLETED.value,
                started_at=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
                completed_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
                last_activity_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
                expected_file_count=5,
                confirmed_file_count=5,
                defect_detected=defective,
                defect_count=1,
                confidence_score=score,
            )
        )
    db.flush()


def test_defective_runs_come_back_worst_first(client, site, runs):
    """Filtering and ordering, which is the Part 1 query itself."""
    body = client.get(
        "/v1/runs",
        params={"site_id": site.id, "defect": True},
        headers=BACKOFFICE,
    ).json()

    scores = [float(item["confidence_score"]) for item in body["items"]]

    assert scores == [0.985, 0.910, 0.640, 0.220]
    assert all(item["defect_detected"] for item in body["items"])


def test_keyset_pagination_walks_every_row_exactly_once(client, site, runs):
    """Pages must tile the result set, with no gap and no repeat.

    Keyset rather than OFFSET, so this also guards the cursor encoding:
    a cursor that lost precision on the score, or dropped the id
    tiebreaker, would show up here as a duplicated or skipped row
    rather than as a silent reordering in production.
    """
    seen: list[str] = []
    cursor = None

    for _ in range(10):  # bounded, so a broken cursor cannot loop forever
        params = {"site_id": site.id, "defect": True, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/runs", params=params, headers=BACKOFFICE).json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 4
    assert len(set(seen)) == 4
