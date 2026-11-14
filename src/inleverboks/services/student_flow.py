"""The student side before the upload: entry checks, access code, student number.

Students have no account. Their progress lives in a short-lived signed cookie (scoped to
the exam's path) that is cleared after handing in or on "Klaar".
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import DeniedAttemptBucket, Exam, ExamRosterEntry, Submission
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.exams import ExamService, ExamStatus
from inleverboks.services.ipranges import ip_allowed
from inleverboks.services.security import constant_time_equals
from inleverboks.services.system_settings import SettingsService

STATE_MAX_AGE = timedelta(minutes=45)


class Stage(StrEnum):
    CODE = "code"
    NUMBER = "number"
    CONFIRM = "confirm"  # roster match shown, waiting for "Ja, dat ben ik"
    NAMES = "names"  # not on the list or no list: student enters the name
    READY = "ready"  # may choose files and upload


@dataclass
class StudentState:
    exam_id: str
    stage: str
    student_number: str = ""
    first_name: str = ""
    infix: str = ""
    last_name: str = ""
    on_roster: bool = False

    @property
    def display_name(self) -> str:
        parts = [self.first_name, self.infix, self.last_name]
        return " ".join(p for p in parts if p)


class StateCodec:
    def __init__(self, secret_key: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt="student-flow")

    def dumps(self, state: StudentState) -> str:
        return self._serializer.dumps(asdict(state))

    def loads(self, token: str | None, exam: Exam) -> StudentState | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=int(STATE_MAX_AGE.total_seconds()))
        except BadSignature:
            return None
        if not isinstance(data, dict) or data.get("exam_id") != str(exam.id):
            return None
        try:
            return StudentState(**data)
        except TypeError:
            return None


def abbreviate(first_name: str, infix: str | None, last_name: str) -> str:
    """'Jan', 'de', 'Vries' → 'Jan de V.' (shown only after a valid access code)."""
    parts = [first_name.strip()]
    if infix and infix.strip():
        parts.append(infix.strip())
    if last_name.strip():
        parts.append(last_name.strip()[0].upper() + ".")
    return " ".join(p for p in parts if p)


def normalize_code(code: str) -> str:
    return re.sub(r"[\s-]+", "", code).upper()


class Entry(StrEnum):
    OPEN = "open"
    CONCEPT = "concept"
    SCHEDULED = "scheduled"
    CLOSED = "closed"
    IP_DENIED = "ip_denied"


class StudentFlowService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditService,
        exams: ExamService,
        settings: SettingsService,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.exams = exams
        self.settings = settings

    # Entry ---------------------------------------------------------------------------

    def entry(self, db: Session, exam: Exam, ip: str | None, now: datetime | None = None) -> Entry:
        now = now or self.clock.now()
        status = self.exams.status(db, exam, now)
        if status is ExamStatus.CONCEPT:
            return Entry.CONCEPT
        if status is ExamStatus.SCHEDULED:
            return Entry.SCHEDULED
        if status is not ExamStatus.OPEN:
            return Entry.CLOSED
        if exam.ip_restriction_enabled and not ip_allowed(ip, list(exam.ip_cidrs or [])):
            return Entry.IP_DENIED
        return Entry.OPEN

    def is_open_for(self, db: Session, exam: Exam, student_number: str, at: datetime) -> bool:
        return exam.starts_at <= at < self.exams.end_for_student(db, exam, student_number)

    # Denied attempts (aggregated per minute per IP) -------------------------------------

    def record_denied(self, db: Session, exam: Exam, ip: str | None, kind: str) -> None:
        """Count a refused attempt. Only the first one per (exam, IP, minute, kind) goes into
        the audit log right away; the counts are kept for the summary job."""
        now = self.clock.now()
        minute = now.replace(second=0, microsecond=0)
        ip_key = ip or "unknown"
        bucket = db.scalar(
            select(DeniedAttemptBucket).where(
                DeniedAttemptBucket.exam_id == exam.id,
                DeniedAttemptBucket.ip == ip_key,
                DeniedAttemptBucket.minute == minute,
                DeniedAttemptBucket.kind == kind,
            )
        )
        if bucket is not None:
            bucket.count += 1
            db.commit()
            return
        try:
            with db.begin_nested():
                db.add(
                    DeniedAttemptBucket(
                        exam_id=exam.id, ip=ip_key, minute=minute, kind=kind, count=1, flushed=False
                    )
                )
        except IntegrityError:
            db.commit()
            self.record_denied(db, exam, ip, kind)
            return
        self.audit.append(
            db,
            "student.denied",
            actor=Actor.anonymous(),
            chain=exam.id,
            ip=ip,
            details={"kind": kind, "minute": minute.isoformat()},
        )
        db.commit()

    def flush_denied(self, db: Session, before: datetime) -> int:
        """Write one summary audit event per finished minute bucket with more than one
        attempt. Called by the scheduler; returns the number of buckets summarized."""
        buckets = list(
            db.scalars(
                select(DeniedAttemptBucket).where(
                    DeniedAttemptBucket.flushed.is_(False), DeniedAttemptBucket.minute < before
                )
            )
        )
        for bucket in buckets:
            if bucket.count > 1:
                self.audit.append(
                    db,
                    "student.denied_summary",
                    actor=Actor.system(),
                    chain=bucket.exam_id,
                    ip=bucket.ip,
                    details={
                        "kind": bucket.kind,
                        "minute": bucket.minute.isoformat(),
                        "count": bucket.count,
                    },
                )
            bucket.flushed = True
        db.commit()
        return len(buckets)

    # Code and student number ------------------------------------------------------------

    def check_code(self, exam: Exam, code: str) -> bool:
        if not exam.access_code_enabled:
            return True
        return bool(exam.access_code) and constant_time_equals(
            normalize_code(code), normalize_code(exam.access_code or "")
        )

    def number_valid(self, db: Session, number: str) -> bool:
        return re.fullmatch(self.settings.load(db).student_number_regex, number) is not None

    def roster_entry(self, db: Session, exam: Exam, number: str) -> ExamRosterEntry | None:
        return db.scalar(
            select(ExamRosterEntry).where(
                ExamRosterEntry.exam_id == exam.id, ExamRosterEntry.student_number == number
            )
        )

    def has_roster(self, db: Session, exam: Exam) -> bool:
        return self.exams.roster_count(db, exam) > 0

    def blocked_by_previous(self, db: Session, exam: Exam, number: str) -> bool:
        """An active submission, or an invalid one without permission to resubmit, blocks a
        new attempt. The student sees the same message in both cases (PLAN B9)."""
        latest = db.scalar(
            select(Submission)
            .where(Submission.exam_id == exam.id, Submission.student_number == number)
            .order_by(Submission.attempt.desc())
            .limit(1)
        )
        if latest is None:
            return False
        if latest.status == "active":
            return True
        return latest.status == "invalid" and not latest.may_resubmit

    def new_state(self, exam: Exam) -> StudentState:
        stage = Stage.CODE if exam.access_code_enabled else Stage.NUMBER
        return StudentState(exam_id=str(exam.id), stage=stage.value)

    def exam_id(self, state: StudentState) -> uuid.UUID:
        return uuid.UUID(state.exam_id)
