"""Housekeeping: orphan cleanup, expired tokens and the integrity check over all files."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.adapters.storage import ObjectNotFoundError, StorageBackend
from inleverboks.db.models import (
    Invite,
    PasswordReset,
    ReceiptToken,
    Submission,
    SubmissionFile,
    TemplateFile,
    UserSession,
)
from inleverboks.services.asyncutil import call_async
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.exports import IntegrityReport, check_integrity
from inleverboks.services.student_flow import StudentFlowService

ORPHAN_AGE = timedelta(hours=1)
TOKEN_KEEP = timedelta(days=1)
LINK_KEEP = timedelta(days=30)

log = structlog.get_logger(__name__)


@dataclass
class CleanupResult:
    deleted_objects: list[str] = field(default_factory=list)
    deleted_tokens: int = 0
    flushed_buckets: int = 0


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class MaintenanceService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditService,
        storage: StorageBackend,
        flow: StudentFlowService,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.storage = storage
        self.flow = flow

    # Cleanup -------------------------------------------------------------------------

    async def _all_keys(self) -> list[str]:
        return [key async for key in self.storage.list_keys("")]

    def _older_than(self, key: str, cutoff: datetime) -> bool:
        try:
            return call_async(self.storage.modified, key) < cutoff
        except ObjectNotFoundError:
            return False

    def orphan_keys(self, db: Session) -> list[str]:
        """Objects older than an hour that nothing refers to: leftovers of interrupted
        uploads (tmp/…, or a final key whose database commit never happened) and template
        files no exam or exam template uses any more."""
        cutoff = self.clock.now() - ORPHAN_AGE
        known_files = set(
            db.scalars(
                select(SubmissionFile.storage_key).where(SubmissionFile.storage_key.is_not(None))
            )
        )
        known_templates = set(db.scalars(select(TemplateFile.storage_key)))
        orphans = []
        for key in call_async(self._all_keys):
            parts = key.split("/")
            if parts[0] == "tmp":
                candidate = True
            elif parts[0] == "templates":
                candidate = key not in known_templates
            elif len(parts) == 3 and _is_uuid(parts[0]) and _is_uuid(parts[1]):
                candidate = key not in known_files
            else:
                candidate = False  # pv/…, branding/… and unknown keys are left alone
            if candidate and self._older_than(key, cutoff):
                orphans.append(key)
        return orphans

    def cleanup(self, db: Session) -> CleanupResult:
        result = CleanupResult()
        for key in self.orphan_keys(db):
            call_async(self.storage.delete, key)
            result.deleted_objects.append(key)
        now = self.clock.now()
        statements = [
            delete(ReceiptToken).where(ReceiptToken.expires_at < now - TOKEN_KEEP),
            delete(UserSession).where(
                or_(
                    UserSession.expires_at < now - TOKEN_KEEP,
                    UserSession.revoked_at < now - TOKEN_KEEP,
                )
            ),
            delete(Invite).where(Invite.expires_at < now - LINK_KEEP),
            delete(PasswordReset).where(PasswordReset.expires_at < now - LINK_KEEP),
        ]
        for statement in statements:
            result.deleted_tokens += db.execute(statement).rowcount  # type: ignore[attr-defined]
        db.commit()
        result.flushed_buckets = self.flow.flush_denied(
            db, before=now.replace(second=0, microsecond=0)
        )
        if result.deleted_objects:
            log.info("cleanup.orphans_deleted", count=len(result.deleted_objects))
        return result

    # Integrity check -----------------------------------------------------------------

    def integrity_check(self, db: Session, exam_id: uuid.UUID | None = None) -> IntegrityReport:
        """Re-hash every stored file and compare; record results and audit mismatches."""
        query = select(SubmissionFile).where(SubmissionFile.storage_key.is_not(None))
        if exam_id is not None:
            query = query.join(Submission, Submission.id == SubmissionFile.submission_id).where(
                Submission.exam_id == exam_id
            )
        files = list(db.scalars(query))
        report = call_async(check_integrity, self.storage, files, self.clock.now())
        exam_of = {sid: eid for sid, eid in db.execute(select(Submission.id, Submission.exam_id))}
        for check in report.checks:
            check.file.integrity_checked_at = report.checked_at
            check.file.integrity_ok = check.ok
            if not check.ok:
                self.audit.append(
                    db,
                    "integrity.mismatch",
                    actor=Actor.system(),
                    chain=exam_of.get(check.file.submission_id, "system"),
                    details={
                        "file_id": str(check.file.id),
                        "name": check.file.original_name,
                        "expected": check.file.sha256,
                        "actual": check.actual_sha256,
                        "during": "integrity_check",
                    },
                )
        db.commit()
        return report
