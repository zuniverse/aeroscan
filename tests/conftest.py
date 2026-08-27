"""Test fixtures.

Two choices worth knowing about.

Tests run against a dedicated database whose schema is built by the
real Alembic revisions, not by `metadata.create_all`. A schema created
a different way from production is a schema free to drift from it, and
the migration is itself part of what deserves testing.

Each test runs inside a transaction that is rolled back at the end. The
session joins that transaction with `create_savepoint`, so the commits
the application makes internally behave exactly as they do in
production while still being undone. Tests share one migrated database
without sharing any state, and none of them needs a cleanup step.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_ROOT = Path(__file__).resolve().parent.parent

_source_url = make_url(
    os.environ.get("DATABASE_URL", "postgresql+psycopg://aeroscan:aeroscan@db:5432/aeroscan")
)
_test_url = _source_url.set(database=f"{_source_url.database}_test")


def _ensure_test_database() -> None:
    """Create the test database if it is not there yet.

    AUTOCOMMIT because CREATE DATABASE cannot run inside a transaction.
    """
    admin = create_engine(_source_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": _test_url.database},
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{_test_url.database}"'))
    admin.dispose()


_ensure_test_database()

# Set before importing anything from app: the engine and the Alembic
# environment both read this at import time.
os.environ["DATABASE_URL"] = _test_url.render_as_string(hide_password=False)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth import hash_api_key  # noqa: E402
from app.db import engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Drone, Site  # noqa: E402

TEST_API_KEY = "test-drone-key"


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema() -> None:
    command.upgrade(Config(str(_ROOT / "alembic.ini")), "head")


@pytest.fixture
def db() -> Session:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db: Session) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    # raise_server_exceptions=False so a 500 shows up as a failed
    # assertion on the status code rather than as an exception, which
    # keeps the failure output about the endpoint and not the plumbing.
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def site(db: Session) -> Site:
    site = Site(name="Test Site")
    db.add(site)
    db.flush()
    return site


@pytest.fixture
def drone(db: Session) -> Drone:
    """A drone, with no site attached.

    That separation is the point: an aircraft inspects a viaduct one
    day and a wind farm the next, so the site travels with the run.
    """
    drone = Drone(
        serial_number="TEST-0001",
        model="Falcon 4",
        api_key_hash=hash_api_key(TEST_API_KEY),
    )
    db.add(drone)
    db.flush()
    return drone


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def bucket(monkeypatch) -> set[str]:
    """Stand-in for the run's S3 prefix.

    Tests add keys to this set to mean "this frame really landed",
    which is what lets them drive the two states the real bucket can
    be in and the drone's confirmations cannot distinguish: a frame
    confirmed and present, and a frame confirmed but absent.
    """
    contents: set[str] = set()
    monkeypatch.setattr(
        "app.routers.runs.list_uploaded_keys", lambda run_id: set(contents)
    )
    return contents


def manifest(count: int, with_sizes: bool = True) -> list[dict]:
    return [
        {"key": f"frame_{i:05d}.jpg", **({"size_bytes": 2_200_000} if with_sizes else {})}
        for i in range(1, count + 1)
    ]
