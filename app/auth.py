import hashlib
import hmac
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select, update

from app.config import get_settings
from app.db import DbSession
from app.errors import ApiError
from app.models import Drone

API_KEY_HEADER = "X-API-Key"

_api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)

ApiKeyHeaderValue = Annotated[str | None, Security(_api_key_scheme)]


def hash_api_key(raw_key: str) -> str:
    """Digest used both to store and to look up a drone key.

    SHA-256 rather than bcrypt or argon2, deliberately. Those exist to
    slow down brute force against low-entropy human passwords; an API
    key is 32 random bytes, which is not brute-forceable regardless of
    digest speed. Their per-call salt would also make the indexed
    lookup below impossible, forcing a scan of every drone row.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def authenticate_machine(
    api_key: ApiKeyHeaderValue,
    db: DbSession,
) -> Drone:
    """Resolve the calling drone from its API key.

    The drone identity always comes from the key, never from the
    request body, so a drone cannot submit runs on behalf of another.
    """
    if not api_key:
        raise ApiError(401, "missing_api_key", f"{API_KEY_HEADER} header is required")

    drone = db.scalar(select(Drone).where(Drone.api_key_hash == hash_api_key(api_key)))
    if drone is None:
        raise ApiError(401, "invalid_api_key", "unknown or revoked API key")

    # Fleet visibility, and cheap: one narrow update per request, and
    # requests are batched by design (a run of 18 000 files makes ~20
    # calls, not 18 000).
    db.execute(
        update(Drone)
        .where(Drone.id == drone.id)
        .values(last_seen_at=datetime.now(timezone.utc))
    )
    db.commit()
    return drone


AuthedMachine = Annotated[Drone, Depends(authenticate_machine)]


def require_backoffice_key(api_key: ApiKeyHeaderValue) -> None:
    """Guard for endpoints the web app and operators call.

    A separate credential from the drone keys, because the two sides
    have opposite shapes: a drone writes only its own runs, the web
    app reads across every site. Reusing one key would mean a single
    compromised drone could read the whole fleet.

    Compared in constant time, so a wrong key cannot be recovered one
    byte at a time from response timings.
    """
    expected = get_settings().backoffice_api_key
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise ApiError(401, "invalid_api_key", "a valid backoffice key is required")
