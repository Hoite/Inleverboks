"""Exams, their expected files ("vakjes"), templates, rosters and class lists."""

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
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false

from inleverboks.db.base import Base
from inleverboks.db.models.qualifications import Qualification
from inleverboks.db.types import UTCDateTime, new_id


class Exam(Base):
    """An exam with a hand-in window.

    The lifecycle status (concept/scheduled/open/closed) is derived from
    ``published_at``, the window, extensions and the clock. Only ``finalized_at``
    (files removed by retention) is stored, so the status can never drift.
    """

    __tablename__ = "exam"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"), index=True)
    second_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id"), index=True
    )

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(500))
    slug: Mapped[str] = mapped_column(String(80), unique=True)

    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    access_code_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    access_code: Mapped[str | None] = mapped_column(String(16))
    ip_restriction_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_cidrs: Mapped[list[str]] = mapped_column(JSON, default=list)
    ip_preset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ip_preset.id"))

    allow_unlisted: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_extra_file: Mapped[bool] = mapped_column(Boolean, default=True)
    extensions: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_file_bytes: Mapped[int] = mapped_column(BigInteger)
    max_submission_bytes: Mapped[int] = mapped_column(BigInteger)
    requirements_locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    hold: Mapped[bool] = mapped_column(Boolean, default=False)
    hold_reason: Mapped[str | None] = mapped_column(Text)
    retention_delete_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finalized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Snapshot of the SBB qualification (crebo, name, dossier version, core task), so the
    # archive stays right after SBB replaces a dossier.
    qualification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The template the exam was made from. Its expected files then come from the template
    # and are read-only in the exam form. Deleting the template makes them editable again.
    source_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exam_template.id", ondelete="SET NULL"), index=True
    )
    # The education team of the exam (PLAN B67); NULL when the owner has no team.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("team.id", ondelete="RESTRICT"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    expected_files: Mapped[list[ExpectedFile]] = relationship(
        back_populates="exam", order_by="ExpectedFile.position", cascade="all, delete-orphan"
    )


class ExpectedFile(Base):
    """A slot ("vakje") for one expected file."""

    __tablename__ = "expected_file"
    __table_args__ = (UniqueConstraint("exam_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    exam_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(100))
    expected_name_pattern: Mapped[str | None] = mapped_column(String(255))
    extensions: Mapped[list[str] | None] = mapped_column(JSON)
    work_process_code: Mapped[str | None] = mapped_column(String(30))

    exam: Mapped[Exam] = relationship(back_populates="expected_files")
    template_file: Mapped[TemplateFile | None] = relationship(
        back_populates="expected_file", cascade="all, delete-orphan"
    )


class ExamTemplate(Base):
    """Personal reusable exam configuration (no times, slug, code or submissions)."""

    __tablename__ = "exam_template"
    __table_args__ = (
        CheckConstraint("NOT (shared AND team_id IS NOT NULL)", name="shared_or_team"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Shared templates are visible to every teacher and managed by admins only.
    shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    qualification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("qualification.id", ondelete="SET NULL"), index=True
    )
    core_task_code: Mapped[str | None] = mapped_column(String(20))
    # A team template (PLAN B66). NULL: personal, or a shared one not yet assigned (B69).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("team.id", ondelete="RESTRICT"), index=True
    )

    qualification: Mapped[Qualification | None] = relationship()

    slots: Mapped[list[ExamTemplateSlot]] = relationship(
        order_by="ExamTemplateSlot.position", cascade="all, delete-orphan"
    )


class ExamTemplateSlot(Base):
    __tablename__ = "exam_template_slot"
    __table_args__ = (UniqueConstraint("template_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_template.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(100))
    expected_name_pattern: Mapped[str | None] = mapped_column(String(255))
    extensions: Mapped[list[str] | None] = mapped_column(JSON)
    work_process_code: Mapped[str | None] = mapped_column(String(30))

    template_file: Mapped[TemplateFile | None] = relationship(cascade="all, delete-orphan")


class TemplateFile(Base):
    """An empty template document belonging to a slot of an exam or an exam template."""

    __tablename__ = "template_file"
    __table_args__ = (
        CheckConstraint(
            "(expected_file_id IS NULL) <> (template_slot_id IS NULL)", name="one_owner"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    expected_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("expected_file.id", ondelete="CASCADE"), unique=True
    )
    template_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exam_template_slot.id", ondelete="CASCADE"), unique=True
    )
    original_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(300))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime)

    expected_file: Mapped[ExpectedFile | None] = relationship(back_populates="template_file")


class ClassList(Base):
    """A teacher's reusable class list. Exams get a snapshot (ExamRosterEntry)."""

    __tablename__ = "class_list"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # The education team of the list (PLAN B65); NULL: not yet assigned (B69).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("team.id", ondelete="RESTRICT"), index=True
    )

    entries: Mapped[list[ClassListEntry]] = relationship(cascade="all, delete-orphan")


class ClassListEntry(Base):
    __tablename__ = "class_list_entry"
    __table_args__ = (UniqueConstraint("class_list_id", "student_number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    class_list_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("class_list.id", ondelete="CASCADE")
    )
    student_number: Mapped[str] = mapped_column(String(32))
    first_name: Mapped[str] = mapped_column(String(100))
    infix: Mapped[str | None] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(150))
    class_name: Mapped[str | None] = mapped_column(String(50))


class ExamRosterEntry(Base):
    """Snapshot of a class list for one exam."""

    __tablename__ = "exam_roster_entry"
    __table_args__ = (UniqueConstraint("exam_id", "student_number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    exam_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam.id", ondelete="CASCADE"))
    student_number: Mapped[str] = mapped_column(String(32))
    first_name: Mapped[str] = mapped_column(String(100))
    infix: Mapped[str | None] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(150))
    class_name: Mapped[str | None] = mapped_column(String(50))


class ExtensionGrant(Base):
    """A later end time for one student (no reason is recorded, by design)."""

    __tablename__ = "extension_grant"
    __table_args__ = (UniqueConstraint("exam_id", "student_number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    exam_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam.id", ondelete="CASCADE"))
    student_number: Mapped[str] = mapped_column(String(32))
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RetiredSlug(Base):
    """Slugs of definitively deleted exams; never reused (PLAN B13)."""

    __tablename__ = "retired_slug"

    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    retired_at: Mapped[datetime] = mapped_column(UTCDateTime)
