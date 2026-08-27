"""Seed the development database.

Deliberately lopsided: most runs get a token handful of frame rows,
while two get a full-size manifest. Eighteen thousand rows twice is
enough to show the bulk insert path works and to make the reconciliation
query run against something real, without spending minutes writing a
million rows nobody will read. The list endpoint never touches
run_files anyway.

Re-runnable: it clears its own tables first.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.auth import hash_api_key
from app.db import SessionLocal
from app.models import Drone, Run, RunFile, RunStatus, Site

SITES = ["Bollene HV Line", "Saint-Nazaire Wind Farm", "Millau Viaduct", "Lacq Pipeline"]
MODELS = ["Falcon 4", "Falcon 4", "Skimmer X"]
OPERATORS = ["a.moreau", "j.pires", "l.chen", "s.okafor", None]

FULL_MANIFEST_RUNS = 2
FULL_MANIFEST_SIZE = 18_000
SMALL_MANIFEST_SIZE = 40
RUN_COUNT = 60


def _file_rows(run_id: uuid.UUID, count: int, confirmed: int, now: datetime) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "file_key": f"frame_{i:05d}.jpg",
            "size_bytes": 2_200_000,
            "confirmed_at": now if i <= confirmed else None,
        }
        for i in range(1, count + 1)
    ]


def seed() -> None:
    rng = random.Random(20260725)  # fixed seed, so the data is the same for everyone
    now = datetime.now(timezone.utc)
    db = SessionLocal()

    # TRUNCATE ... RESTART IDENTITY rather than DELETE: sequences would
    # otherwise keep climbing across re-runs, and the site ids in the
    # README would stop matching what the seed actually produced.
    db.execute(sa.text("TRUNCATE run_files, runs, drones, sites RESTART IDENTITY CASCADE"))

    sites = [Site(name=name) for name in SITES]
    db.add_all(sites)
    db.flush()

    drones: list[Drone] = []
    for index in range(1, 9):
        serial = f"DR-{index:04d}"
        drones.append(
            Drone(
                serial_number=serial,
                model=MODELS[index % len(MODELS)],
                api_key_hash=hash_api_key(f"dev-key-{serial}"),
                last_seen_at=now - timedelta(minutes=rng.randint(1, 600)),
            )
        )
    db.add_all(drones)
    db.flush()

    file_rows: list[dict] = []

    for run_index in range(RUN_COUNT):
        drone = rng.choice(drones)
        # Site drawn per run, not per drone: an aircraft moves between
        # missions, which is exactly what the schema now allows.
        site = rng.choice(sites)
        started = now - timedelta(days=rng.uniform(0, 14))
        full = run_index < FULL_MANIFEST_RUNS
        expected = FULL_MANIFEST_SIZE if full else SMALL_MANIFEST_SIZE

        # A realistic spread: mostly clean completions, a few runs still
        # uploading, a few that ended short, a couple long dead.
        status = rng.choices(
            [
                RunStatus.COMPLETED,
                RunStatus.INCOMPLETE,
                RunStatus.UPLOADING,
                RunStatus.ABANDONED,
            ],
            weights=[70, 12, 12, 6],
        )[0]

        if status is RunStatus.COMPLETED:
            confirmed = expected
        elif status is RunStatus.INCOMPLETE:
            confirmed = expected - rng.randint(1, max(2, expected // 20))
        else:
            confirmed = rng.randint(0, expected)

        has_results = status in (RunStatus.COMPLETED, RunStatus.INCOMPLETE)
        defective = rng.random() < 0.35 if has_results else None

        run = Run(
            id=uuid.uuid4(),
            drone_id=drone.id,
            site_id=site.id,
            drone_run_uid=f"seed-{run_index:03d}",
            status=status.value,
            operator=rng.choice(OPERATORS),
            started_at=started,
            completed_at=started + timedelta(hours=4) if has_results else None,
            reconciled_at=started + timedelta(hours=4) if has_results else None,
            last_activity_at=(
                started + timedelta(days=9)
                if status is RunStatus.ABANDONED
                else started + timedelta(hours=4)
            ),
            expected_file_count=expected,
            confirmed_file_count=confirmed,
            total_size_bytes=confirmed * 2_200_000,
            defect_detected=defective,
            defect_count=rng.randint(1, 200) if defective else (0 if has_results else None),
            confidence_score=round(rng.uniform(0.4, 0.999), 3) if has_results else None,
        )
        db.add(run)
        file_rows.extend(_file_rows(run.id, expected, confirmed, now))

    db.flush()
    # Chunked so one statement never carries 36 000 rows of parameters.
    for start in range(0, len(file_rows), 5_000):
        db.execute(sa.insert(RunFile), file_rows[start : start + 5_000])

    db.commit()

    counts = db.execute(
        sa.select(Run.status, sa.func.count()).group_by(Run.status).order_by(Run.status)
    ).all()
    site_ids = db.execute(sa.select(Site.id, Site.name).order_by(Site.id)).all()
    db.close()

    print(f"{len(SITES)} sites, {len(drones)} drones, {RUN_COUNT} runs")
    print(f"{len(file_rows):,} frame rows")
    for status, count in counts:
        print(f"  {status:<11} {count}")
    print()
    for site_id, name in site_ids:
        print(f"  site_id={site_id}  {name}")
    print()
    print("Drone API keys : dev-key-DR-0001 .. dev-key-DR-0008")
    print("Backoffice key : dev-backoffice-key")


if __name__ == "__main__":
    seed()
