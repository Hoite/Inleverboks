"""The SBB qualification structure: qualifications (uitstroom), core tasks, work processes.

Filled by the import of the SBB XML. Rows are added per dossier version and never
deleted while templates refer to them, so an old template keeps its context.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inleverboks.db.base import Base
from inleverboks.db.types import UTCDateTime, new_id


class Qualification(Base):
    """One qualification ("uitstroom") with its own crebo, in one dossier version."""

    __tablename__ = "qualification"
    __table_args__ = (UniqueConstraint("crebo", "dossier_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    crebo: Mapped[str] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(300))
    level: Mapped[int | None] = mapped_column(Integer)
    dossier_crebo: Mapped[str | None] = mapped_column(String(10))
    dossier_name: Mapped[str | None] = mapped_column(String(300))
    dossier_version: Mapped[str] = mapped_column(String(40))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    # Order of the versions of one crebo (0 = oldest), from the SBB version chain:
    # versions often share their start date, so this orders them.
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Crebo of the previous version when a revision gave this outflow a new crebo.
    previous_crebo: Mapped[str | None] = mapped_column(String(10), index=True)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime)

    core_tasks: Mapped[list[CoreTask]] = relationship(
        order_by="CoreTask.position", cascade="all, delete-orphan"
    )


class CoreTask(Base):
    """A core task ("kerntaak"), coded like ``B1-K1`` (basisdeel) or ``P1-K1`` (profieldeel)."""

    __tablename__ = "core_task"
    __table_args__ = (UniqueConstraint("qualification_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    qualification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qualification.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer)

    work_processes: Mapped[list[WorkProcess]] = relationship(
        order_by="WorkProcess.position", cascade="all, delete-orphan"
    )


class WorkProcess(Base):
    """A work process ("werkproces"), coded like ``B1-K1-W1``."""

    __tablename__ = "work_process"
    __table_args__ = (UniqueConstraint("core_task_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    core_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_task.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer)
