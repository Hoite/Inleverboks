"""Retention, hold flag, finalized exams and permanent deletion (spec §10).

Counting: day 1 is the calendar day after the effective end date of the exam (the last
end time including extensions, in APP_TIMEZONE). After day N has passed the files are
deleted, so deletion is due from local midnight at the start of day N + 1.
Example: end Friday 13 Nov 2026, 60 days → day 1 = 14 Nov, day 60 = 12 Jan 2027, due from
13 Jan 2027 00:00 (Amsterdam); the dashboard warns from 6 Jan 2027.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.adapters.storage import StorageBackend
from inleverboks.db.models import (
    DeniedAttemptBucket,
    Exam,
    ExamRosterEntry,
    ExtensionGrant,
    ProcesVerbaal,
    ReceiptToken,
    RetiredSlug,
    Submission,
    SubmissionFile,
    User,
)
from inleverboks.policy import ExamAction, ExamRef, can_exam
from inleverboks.services.asyncutil import call_async
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.errors import DomainError, PermissionDeniedError
from inleverboks.services.exams import ExamService, ExamStatus
from inleverboks.services.pv import ProcesVerbaalService, RenderPv, generate
from inleverboks.services.system_settings import SettingsService
from inleverboks.services.template_files import unreferenced

WARNING_PERIOD = timedelta(days=7)
GRACE_AFTER_HOLD = timedelta(days=7)
MAX_REASON = 1000

log = structlog.get_logger(__name__)


def deletion_due(effective_end: datetime, retention_days: int, tz: ZoneInfo) -> datetime:
    """Local midnight at the start of day ``retention_days + 1`` after the end date."""
    end_date: date = effective_end.astimezone(tz).date()
    due_local = datetime.combine(end_date + timedelta(days=retention_days + 1), time(0), tzinfo=tz)
    return due_local.astimezone(UTC)


@dataclass
class RetentionInfo:
    due: datetime | None  # None: never (hold flag, concept, finalized)
    warn_from: datetime | None
    held: bool

    def warning(self, now: datetime) -> bool:
        return (
            self.due is not None and self.warn_from is not None and self.warn_from <= now < self.due
        )


@dataclass
class RunResult:
    finalized: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)


def _reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise DomainError("reason_required")
    if len(reason) > MAX_REASON:
        raise DomainError("reason_too_long")
    return reason


class LifecycleService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditService,
        settings: SettingsService,
        exams: ExamService,
        pv: ProcesVerbaalService,
        storage: StorageBackend,
        tz: ZoneInfo,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.settings = settings
        self.exams = exams
        self.pv = pv
        self.storage = storage
        self.tz = tz

    # Retention dates -----------------------------------------------------------------

    def retention(self, db: Session, exam: Exam) -> RetentionInfo:
        if exam.finalized_at is not None or exam.published_at is None:
            return RetentionInfo(due=None, warn_from=None, held=exam.hold)
        if exam.hold:
            return RetentionInfo(due=None, warn_from=None, held=True)
        due = deletion_due(
            self.exams.effective_end(db, exam), self.settings.load(db).retention_days, self.tz
        )
        if exam.retention_delete_after is not None:  # grace period after a lifted hold
            due = max(due, exam.retention_delete_after)
        return RetentionInfo(due=due, warn_from=due - WARNING_PERIOD, held=False)

    def due_exams(self, db: Session, now: datetime | None = None) -> list[Exam]:
        now = now or self.clock.now()
        candidates = db.scalars(
            select(Exam).where(
                Exam.finalized_at.is_(None), Exam.published_at.is_not(None), Exam.hold.is_(False)
            )
        )
        due = []
        for exam in candidates:
            if self.exams.status(db, exam, now) is not ExamStatus.CLOSED:
                continue
            info = self.retention(db, exam)
            if info.due is not None and info.due <= now:
                due.append(exam)
        return due

    # Hold flag -----------------------------------------------------------------------

    def set_hold(self, db: Session, user: User, exam: Exam, reason: str, *, ip: str | None) -> None:
        if not can_exam(user, ExamAction.SET_HOLD, ExamRef(exam.owner_id, exam.second_reviewer_id)):
            raise PermissionDeniedError()
        if exam.finalized_at is not None:
            raise DomainError("exam_finalized")
        reason = _reason(reason)
        exam.hold = True
        exam.hold_reason = reason
        self.audit.append(
            db,
            "exam.hold_set",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={"reason": reason},
        )
        db.commit()

    def remove_hold(
        self, db: Session, user: User, exam: Exam, reason: str, *, ip: str | None
    ) -> None:
        if not can_exam(user, ExamAction.SET_HOLD, ExamRef(exam.owner_id, exam.second_reviewer_id)):
            raise PermissionDeniedError()
        if not exam.hold:
            return
        reason = _reason(reason)
        exam.hold = False
        old_reason = exam.hold_reason
        exam.hold_reason = None
        now = self.clock.now()
        info = self.retention(db, exam)
        if info.due is not None and info.due <= now + GRACE_AFTER_HOLD:
            # The period has (almost) passed: delete at the earliest 7 days from now.
            exam.retention_delete_after = now + GRACE_AFTER_HOLD
        self.audit.append(
            db,
            "exam.hold_removed",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={
                "reason": reason,
                "hold_reason": old_reason,
                "not_before": exam.retention_delete_after.isoformat()
                if exam.retention_delete_after
                else None,
            },
        )
        db.commit()

    # Finalize ------------------------------------------------------------------------

    def finalize(self, db: Session, exam: Exam, render_pv: RenderPv) -> int:
        """Final proces-verbaal (with integrity check), then delete all files."""
        generate(db, self.pv, self.storage, render_pv, exam, None, final=True, ip=None)
        now = self.clock.now()
        files = list(
            db.scalars(
                select(SubmissionFile)
                .join(Submission, Submission.id == SubmissionFile.submission_id)
                .where(Submission.exam_id == exam.id, SubmissionFile.storage_key.is_not(None))
            )
        )
        for file in files:
            call_async(self.storage.delete, file.storage_key or "")
            file.storage_key = None
            file.deleted_at = now
        roster = db.execute(delete(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id))
        exam.finalized_at = now
        self.audit.append(
            db,
            "retention.deleted",
            actor=Actor.system(),
            chain=exam.id,
            details={"files": len(files), "roster_entries": roster.rowcount},  # type: ignore[attr-defined]
        )
        self.audit.append(db, "exam.finalized", actor=Actor.system(), chain=exam.id)
        db.commit()
        return len(files)

    def run(self, db: Session, render_pv: RenderPv, now: datetime | None = None) -> RunResult:
        result = RunResult()
        for exam in self.due_exams(db, now):
            try:
                deleted = self.finalize(db, exam, render_pv)
            except Exception:
                db.rollback()
                log.exception("retention.failed", exam_id=str(exam.id))
                result.failed.append(exam.id)
                continue
            log.info("retention.finalized", exam_id=str(exam.id), files=deleted)
            result.finalized.append(exam.id)
        return result

    # Permanent deletion of a finalized exam ------------------------------------------------

    def delete_permanently(
        self, db: Session, admin: User, exam: Exam, reason: str, *, ip: str | None
    ) -> None:
        if not can_exam(
            admin, ExamAction.DELETE_FINALIZED, ExamRef(exam.owner_id, exam.second_reviewer_id)
        ):
            raise PermissionDeniedError()
        if exam.finalized_at is None:
            raise DomainError("not_finalized")
        reason = _reason(reason)
        details = {"exam_id": str(exam.id), "name": exam.name, "slug": exam.slug, "reason": reason}
        submission_ids = list(
            db.scalars(select(Submission.id).where(Submission.exam_id == exam.id))
        )
        pv_keys = list(
            db.scalars(select(ProcesVerbaal.storage_key).where(ProcesVerbaal.exam_id == exam.id))
        )
        template_keys = [
            s.template_file.storage_key for s in exam.expected_files if s.template_file
        ]
        for key in pv_keys:
            call_async(self.storage.delete, key)
        db.execute(delete(SubmissionFile).where(SubmissionFile.submission_id.in_(submission_ids)))
        db.execute(delete(ReceiptToken).where(ReceiptToken.submission_id.in_(submission_ids)))
        db.execute(delete(Submission).where(Submission.exam_id == exam.id))
        db.execute(delete(ProcesVerbaal).where(ProcesVerbaal.exam_id == exam.id))
        db.execute(delete(ExtensionGrant).where(ExtensionGrant.exam_id == exam.id))
        db.execute(delete(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id))
        db.execute(delete(DeniedAttemptBucket).where(DeniedAttemptBucket.exam_id == exam.id))
        if db.get(RetiredSlug, exam.slug) is None:
            db.add(RetiredSlug(slug=exam.slug, retired_at=self.clock.now()))
        # The exam's own audit chain stays; the deletion is recorded in both chains.
        self.audit.append(
            db,
            "exam.deleted_permanently",
            actor=Actor.for_user(admin),
            chain=exam.id,
            ip=ip,
            details=details,
        )
        self.audit.append(
            db, "exam.deleted_permanently", actor=Actor.for_user(admin), ip=ip, details=details
        )
        db.delete(exam)
        db.flush()
        for key in template_keys:
            if unreferenced(db, key):
                call_async(self.storage.delete, key)
        db.commit()
