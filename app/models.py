from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RunStatus(str, enum.Enum):
    """Lifecycle of a run. See the state diagram in DESIGN.md.

    INCOMPLETE is transitory, not terminal. Re-flying an inspection
    means a pilot back on site, a weather window and sometimes an
    overflight clearance, and the structure may have changed in the
    meantime, so the capture is not reproducible in any useful sense.
    Late uploads are therefore accepted and can still promote a run to
    COMPLETED.
    """

    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    ABANDONED = "ABANDONED"


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)

    runs: Mapped[list[Run]] = relationship(back_populates="site")


class Drone(Base):
    __tablename__ = "drones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    # SHA-256 of the API key. The key is 32 random bytes, so a fast
    # digest is appropriate: bcrypt and friends exist to slow down
    # brute force on low-entropy human passwords, and their per-call
    # salt would make an indexed lookup impossible.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    drone_id: Mapped[int] = mapped_column(ForeignKey("drones.id"), nullable=False)

    # Declared by the drone at creation and never updated afterwards.
    # The site belongs to the mission, not to the aircraft: a drone
    # inspects a viaduct one day and a wind farm the next, so there is
    # no standing drone-to-site link to derive this from. It is also a
    # point-in-time fact - the run happened here, on this date - so
    # nothing should ever propagate a later change back onto it.
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)

    # Client-generated identifier, the idempotency key for run creation.
    drone_run_uid: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RunStatus.UPLOADING.value
    )
    operator: Mapped[str | None] = mapped_column(String(120))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Counters maintained in the same transaction as each batch, so
    # reconciliation never has to COUNT(*) over ~18 000 rows.
    expected_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Analysis results, written once at the first completion and never
    # overwritten. On regulated data a value that silently changes after
    # the fact is worse than a missing file.
    defect_detected: Mapped[bool | None] = mapped_column(Boolean)
    defect_count: Mapped[int | None] = mapped_column(Integer)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    drone: Mapped[Drone] = relationship()
    site: Mapped[Site] = relationship(back_populates="runs")

    __table_args__ = (
        UniqueConstraint("drone_id", "drone_run_uid", name="uq_runs_drone_uid"),
        CheckConstraint(
            "status IN ('UPLOADING', 'COMPLETED', 'INCOMPLETE', 'ABANDONED')",
            name="ck_runs_status",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="ck_runs_confidence_range",
        ),
        # The hot list query: defective runs, one site, a date
        # range, sorted by confidence. Partial, so only the minority of
        # rows the query can ever return are indexed. Column order
        # serves equality, then range, then sort, so Postgres reads the
        # rows already ordered and never adds a sort node.
        Index(
            "idx_runs_defective",
            "site_id",
            text("started_at DESC"),
            text("confidence_score DESC"),
            postgresql_where=text("defect_detected"),
        ),
        # Supports the drone filter and the per-drone history view.
        Index("idx_runs_drone_started", "drone_id", text("started_at DESC")),
        # Serves the abandoned-run sweep, which scans by status and idle time.
        Index("idx_runs_status_activity", "status", "last_activity_at"),
    )


class RunFile(Base):
    """One row per expected file, inserted in bulk from the manifest.

    Rows exist from run creation with confirmed_at NULL, which is what
    lets completion report exactly which keys are missing. The composite
    primary key is the only index: the table is written in bulk and read
    as an aggregate, never searched by anything else.
    """

    __tablename__ = "run_files"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    file_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
