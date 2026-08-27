"""Retries must be free.

Drones on remote sites lose connectivity mid-call and cannot tell a
request that never arrived from a response that never came back. Their
only sane strategy is to resend, so every write endpoint has to make
that harmless. These two tests pin that property down.
"""
from tests.conftest import manifest


def test_recreating_a_run_returns_the_same_run(client, drone, site, headers):
    """A resent creation must not produce a second run.

    The failure this guards against is not cosmetic: two runs for one
    flight would double-count defect findings and split the frame
    manifest across two reconciliations, neither of which could ever
    complete.
    """
    body = {
        "drone_run_uid": "run-idem-1",
        "site_id": site.id,
            "started_at": "2026-07-25T09:00:00Z",
        "operator": "tester",
        "files": manifest(5),
    }

    first = client.post("/v1/runs", json=body, headers=headers)
    second = client.post("/v1/runs", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.json()["already_existed"] is True

    # Fresh URLs on the retry, since the first set may well have
    # expired, which is often why the drone is retrying at all.
    assert second.json()["upload_urls"]


def test_replayed_confirmations_do_not_double_count(client, drone, site, headers):
    """A resent batch must move no counter.

    Without the `confirmed_at IS NULL` guard the count would reach 5 on
    a 5-frame manifest while two frames had never been uploaded, and
    completion would then declare the run COMPLETED. A dropped
    connection would produce a reconciliation that lies.

    Sizes are omitted here on purpose: `size_bytes` is optional, and a
    batch where every row omits it once produced an untyped NULL column
    that Postgres refused to assign to bigint.
    """
    created = client.post(
        "/v1/runs",
        json={
            "drone_run_uid": "run-idem-2",
            "site_id": site.id,
            "started_at": "2026-07-25T09:00:00Z",
            "files": manifest(5),
        },
        headers=headers,
    ).json()
    run_id = created["run_id"]
    batch = {"files": manifest(3, with_sizes=False)}

    first = client.post(f"/v1/runs/{run_id}/file-confirmations", json=batch, headers=headers)
    second = client.post(f"/v1/runs/{run_id}/file-confirmations", json=batch, headers=headers)

    assert first.status_code == 200
    assert first.json()["newly_confirmed"] == 3
    assert first.json()["confirmed_file_count"] == 3

    assert second.status_code == 200
    assert second.json()["newly_confirmed"] == 0
    assert second.json()["confirmed_file_count"] == 3


def test_unknown_site_is_rejected_before_anything_is_written(client, drone, headers, db):
    """A bad site must fail as a 422, not as a foreign key violation.

    The site travels with the mission rather than with the aircraft, so
    it is the one field on run creation the drone can get wrong. Left
    to the foreign key it would surface as an IntegrityError and a 500;
    checked first, it names the field and leaves the manifest unwritten.
    """
    import sqlalchemy as sa

    from app.models import Run

    response = client.post(
        "/v1/runs",
        json={
            "drone_run_uid": "run-bad-site",
            "site_id": 999_999,
            "started_at": "2026-07-25T09:00:00Z",
            "files": manifest(3),
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "unknown_site"
    assert db.scalar(
        sa.select(sa.func.count()).select_from(Run).where(Run.drone_run_uid == "run-bad-site")
    ) == 0
