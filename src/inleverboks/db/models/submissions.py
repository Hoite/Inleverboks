"""Submissions, their files, receipts and the generated official reports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inleverboks.db.base import Base
from inleverboks.db.types import UTCDateTime, new_id

SUBMISSION_STATUSES = ("active", "cancelled", "invalid")


class Submission(Base):
    """One hand-in attempt by a student. At most one ``active`` per (exam, student)."""

    __tablename__ = "submission"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'cancelled', 'invalid')", name="status"),
        UniqueConstraint("exam_id", "student_number", "attempt"),
        Index(
            "uq_submission_one_active",
            "exam_id",
            "student_number",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    exam_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam.id"), index=True)
    student_number: Mapped[str] = mapped_column(String(32))
    first_name: Mapped[str] = mapped_column(String(100))
    infix: Mapped[str | None] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(150))
    on_roster: Mapped[bool] = mapped_column(Boolean)
    attempt: Mapped[int] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(16), default="active")
    may_resubmit: Mapped[bool] = mapped_column(Boolean, default=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_changed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    declaration_accepted: Mapped[bool] = mapped_column(Boolean)
    confirmed_warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    client_ip: Mapped[str | None] = mapped_column(String(64))

    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    receipt_code: Mapped[str] = mapped_column(String(16), unique=True)
    verification_code_hash: Mapped[str] = mapped_column(String(64))

    files: Mapped[list[SubmissionFile]] = relationship(
        back_populates="submission", order_by="SubmissionFile.position"
    )


class SubmissionFile(Base):
    __tablename__ = "submission_file"
    __table_args__ = (UniqueConstraint("submission_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submission.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    expected_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("expected_file.id"))
    original_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str | None] = mapped_column(String(300))  # NULL after retention
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    integrity_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    integrity_ok: Mapped[bool | None] = mapped_column(Boolean)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    submission: Mapped[Submission] = relationship(back_populates="files")


class ReceiptToken(Base):
    """One-time token (15 minutes) for downloading the receipt PDF."""

    __tablename__ = "receipt_token"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submission.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ProcesVerbaal(Base):
    """A generated official report (PDF). ``is_final`` is the one made before deletion."""

    __tablename__ = "proces_verbaal"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    exam_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam.id"), index=True)
    document_id: Mapped[str] = mapped_column(String(32), unique=True)
    storage_key: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    audit_head_hash: Mapped[str] = mapped_column(String(64))
