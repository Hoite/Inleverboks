"""Append-only audit log with a hash chain per exam plus one system chain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from inleverboks.db.base import Base
from inleverboks.db.types import UTCDateTime, new_id

SYSTEM_CHAIN = "system"
GENESIS_HASH = "0" * 64


class AuditEvent(Base):
    """One audit entry. There are no update or delete paths in the application, and
    database triggers reject UPDATE and DELETE on this table.

    ``chain`` is ``"system"`` or the exam id as text (no foreign key, so the log
    survives a definitive deletion of the exam).
    """

    __tablename__ = "audit_event"
    __table_args__ = (UniqueConstraint("chain", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    chain: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | student | system
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_label: Mapped[str] = mapped_column(String(200))
    ip: Mapped[str | None] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class DeniedAttemptBucket(Base):
    """Per-minute aggregate of refused student attempts, to keep the audit log readable."""

    __tablename__ = "denied_attempt_bucket"
    __table_args__ = (UniqueConstraint("exam_id", "ip", "minute", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    exam_id: Mapped[uuid.UUID] = mapped_column()
    ip: Mapped[str] = mapped_column(String(64))
    minute: Mapped[datetime] = mapped_column(UTCDateTime)
    kind: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(Integer, default=0)
    flushed: Mapped[bool] = mapped_column(default=False)
