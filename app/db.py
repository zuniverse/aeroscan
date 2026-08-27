from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


_settings = get_settings()

# Synchronous engine and session, deliberately. Every endpoint here is
# database-bound, so the async stack would add greenlet plumbing and
# harder-to-read tests without changing where the time is spent.
# FastAPI runs sync path operations in a threadpool, which is enough
# at this scale.
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session.

    Commit is the caller's responsibility, so a route can decide what
    belongs in one transaction. Any escaping exception rolls back.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Declared once and reused by every route, so the injection wiring is
# stated in a single place. Annotated rather than a `Depends()` default:
# the parameter keeps a real `Session` type for the type checker.
DbSession = Annotated[Session, Depends(get_db)]
