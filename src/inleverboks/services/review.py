"""Teacher actions on a submission: undo (cancel) and declare invalid (spec §6.3).

Files are always kept: a cancelled or invalid attempt stays stored until retention.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import Exam, Submission, User
from inleverboks.policy import ExamAction, ExamRef, can_exam
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError

MAX_REASON = 1000


class ReviewService:
    def __init__(self, *, clock: Clock, audit: AuditService) -> None:
        self.clock = clock
        self.audit = audit

    def _load(self, db: Session, user: User, exam: Exam, submission_id: uuid.UUID) -> Submission:
        if not can_exam(
            user, ExamAction.MANAGE_SUBMISSIONS, ExamRef(exam.owner_id, exam.second_reviewer_id)
        ):
            raise PermissionDeniedError()
        submission = db.get(Submission, submission_id)
        if submission is None or submission.exam_id != exam.id:
            raise NotFoundError()
        if submission.status != "active":
            raise DomainError("not_active")
        if exam.finalized_at is not None:
            raise DomainError("exam_finalized")
        return submission

    @staticmethod
    def _reason(reason: str) -> str:
        reason = reason.strip()
        if not reason:
            raise DomainError("reason_required")
        if len(reason) > MAX_REASON:
            raise DomainError("reason_too_long")
        return reason

    def cancel(
        self, db: Session, user: User, exam: Exam, submission_id: uuid.UUID, reason: str,
        *, ip: str | None,
    ) -> Submission:  # fmt: skip
        """Undo at the student's request: the student may hand in again (new attempt)."""
        submission = self._load(db, user, exam, submission_id)
        reason = self._reason(reason)
        submission.status = "cancelled"
        submission.may_resubmit = True
        self._stamp(submission, user, reason)
        self.audit.append(
            db, "submission.cancelled", actor=Actor.for_user(user), chain=exam.id, ip=ip,
            details=self._details(submission, reason),
        )  # fmt: skip
        db.commit()
        return submission

    def invalidate(
        self, db: Session, user: User, exam: Exam, submission_id: uuid.UUID, reason: str,
        *, may_resubmit: bool, ip: str | None,
    ) -> Submission:  # fmt: skip
        submission = self._load(db, user, exam, submission_id)
        reason = self._reason(reason)
        submission.status = "invalid"
        submission.may_resubmit = may_resubmit
        self._stamp(submission, user, reason)
        self.audit.append(
            db, "submission.invalidated", actor=Actor.for_user(user), chain=exam.id, ip=ip,
            details={**self._details(submission, reason), "may_resubmit": may_resubmit},
        )  # fmt: skip
        db.commit()
        return submission

    def _stamp(self, submission: Submission, user: User, reason: str) -> None:
        submission.status_reason = reason
        submission.status_changed_by_id = user.id
        submission.status_changed_at = self.clock.now()

    @staticmethod
    def _details(submission: Submission, reason: str) -> dict[str, object]:
        return {
            "submission_id": str(submission.id),
            "student_number": submission.student_number,
            "attempt": submission.attempt,
            "receipt_code": submission.receipt_code,
            "reason": reason,
        }
