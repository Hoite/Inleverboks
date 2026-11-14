"""Handing in: verify the uploaded files, store them atomically and issue a receipt.

The route streams the request to temporary files (hashing while it reads), then:
1. ``prepare`` checks everything again on the server and plans storage keys;
2. the route moves the files into storage (temporary key first, then the final key);
3. ``commit`` writes the submission in one transaction. If that fails (for example a
   second device won the race for the single active submission) the route removes the
   stored objects again.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import Exam, ReceiptToken, Submission, SubmissionFile
from inleverboks.db.types import new_id
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.checks import (
    CheckResult,
    FileCandidate,
    Rules,
    SlotRule,
    check_files,
    sanitize_filename,
)
from inleverboks.services.errors import DomainError, NotFoundError
from inleverboks.services.security import hash_token, human_code, keyed_hash, new_token
from inleverboks.services.student_flow import StudentFlowService, StudentState
from inleverboks.services.system_settings import SettingsService

RECEIPT_TOKEN_LIFETIME = timedelta(minutes=15)
_VERIFICATION_PURPOSE = "receipt-verification"


@dataclass(frozen=True)
class ReceivedFile:
    """A file part as received and hashed by the server."""

    filename: str
    size: int
    sha256: str
    path: Path


@dataclass
class PlannedFile:
    position: int
    name: str
    size: int
    sha256: str
    slot_id: uuid.UUID | None
    slot_label: str | None
    warnings: list[str]
    path: Path
    storage_key: str
    temp_key: str


@dataclass
class Plan:
    exam_id: uuid.UUID
    submission_id: uuid.UUID
    student: StudentState
    attempt: int
    started_at: datetime
    idempotency_key: str
    files: list[PlannedFile]
    warnings: list[dict[str, str]]
    client_ip: str | None
    existing: Submission | None = None  # idempotent replay of a finished submission


@dataclass
class Receipt:
    submission: Submission
    exam: Exam
    verification_code: str | None
    token: str
    files: list[dict[str, Any]] = field(default_factory=list)


def format_receipt_code(raw: str) -> str:
    return f"IB-{raw[:4]}-{raw[4:8]}"


class SubmissionService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditService,
        settings: SettingsService,
        flow: StudentFlowService,
        secret_key: str,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.settings = settings
        self.flow = flow
        self._secret_key = secret_key

    # Rules -------------------------------------------------------------------------

    def rules_for(self, db: Session, exam: Exam) -> Rules:
        return Rules(
            slots=[
                SlotRule(
                    id=s.id,
                    label=s.label,
                    pattern=s.expected_name_pattern,
                    extensions=list(s.extensions) if s.extensions else None,
                )
                for s in exam.expected_files
            ],
            extensions=list(exam.extensions or []),
            allow_extra_file=exam.allow_extra_file,
            max_file_bytes=exam.max_file_bytes,
            max_submission_bytes=exam.max_submission_bytes,
            max_files=self.settings.load(db).max_files_per_submission,
            template_hashes=frozenset(
                s.template_file.sha256 for s in exam.expected_files if s.template_file
            ),
        )

    # Prepare -------------------------------------------------------------------------

    def find_idempotent(self, db: Session, exam: Exam, key: str) -> Submission | None:
        return db.scalar(
            select(Submission).where(
                Submission.exam_id == exam.id, Submission.idempotency_key == key
            )
        )

    def prepare(
        self,
        db: Session,
        exam: Exam,
        state: StudentState,
        *,
        manifest_json: str,
        received: list[ReceivedFile],
        idempotency_key: str,
        declaration: bool,
        warnings_confirmed: bool,
        started_at: datetime,
        ip: str | None,
    ) -> Plan:
        if not idempotency_key or len(idempotency_key) > 64:
            raise DomainError("bad_request")
        existing = self.find_idempotent(db, exam, idempotency_key)
        if existing is not None:
            if existing.student_number != state.student_number:
                raise DomainError("bad_request")
            return Plan(
                exam_id=exam.id, submission_id=existing.id, student=state, attempt=existing.attempt,
                started_at=existing.started_at, idempotency_key=idempotency_key, files=[],
                warnings=[], client_ip=ip, existing=existing,
            )  # fmt: skip
        if not self.flow.is_open_for(db, exam, state.student_number, started_at):
            raise DomainError("closed")
        if self.flow.blocked_by_previous(db, exam, state.student_number):
            raise DomainError("already_submitted")
        if not declaration:
            raise DomainError("declaration_required")

        manifest = self._parse_manifest(manifest_json, len(received))
        candidates: list[FileCandidate] = []
        for entry, file in zip(manifest, received, strict=True):
            if entry["sha256"] != file.sha256 or entry["size"] != file.size:
                raise DomainError("hash_mismatch")
            candidates.append(
                FileCandidate(
                    name=sanitize_filename(file.filename),
                    size=file.size,
                    sha256=file.sha256,
                    slot_id=entry["slot_id"],
                )
            )
        rules = self.rules_for(db, exam)
        result = check_files(candidates, rules)
        if result.blocked:
            raise DomainError(self._first_error(result))
        if result.has_warnings and not warnings_confirmed:
            raise DomainError("warnings_not_confirmed")

        submission_id = new_id()
        labels = {s.id: s.label for s in rules.slots}
        files = []
        for index, (candidate, file) in enumerate(zip(candidates, received, strict=True)):
            object_id = uuid.uuid4().hex
            files.append(
                PlannedFile(
                    position=index + 1,
                    name=candidate.name,
                    size=candidate.size,
                    sha256=candidate.sha256,
                    slot_id=candidate.slot_id,
                    slot_label=labels.get(candidate.slot_id) if candidate.slot_id else None,
                    warnings=result.file_warnings.get(index, []),
                    path=file.path,
                    storage_key=f"{exam.id}/{submission_id}/{object_id}",
                    temp_key=f"tmp/upload-{object_id}",
                )
            )
        attempt = (
            db.scalar(
                select(func.max(Submission.attempt)).where(
                    Submission.exam_id == exam.id, Submission.student_number == state.student_number
                )
            )
            or 0
        ) + 1
        return Plan(
            exam_id=exam.id,
            submission_id=submission_id,
            student=state,
            attempt=attempt,
            started_at=started_at,
            idempotency_key=idempotency_key,
            files=files,
            warnings=result.summary(candidates),
            client_ip=ip,
        )

    @staticmethod
    def _first_error(result: CheckResult) -> str:
        if result.errors:
            return result.errors[0]
        return next(codes[0] for codes in result.file_errors.values() if codes)

    @staticmethod
    def _parse_manifest(raw: str, count: int) -> list[dict[str, Any]]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DomainError("bad_request") from exc
        if not isinstance(data, list) or len(data) != count:
            raise DomainError("bad_request")
        manifest: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                raise DomainError("bad_request")
            slot = item.get("slot_id")
            try:
                slot_id = uuid.UUID(slot) if slot else None
                size = int(item["size"])
                sha = str(item["sha256"]).lower()
            except (KeyError, ValueError, TypeError) as exc:
                raise DomainError("bad_request") from exc
            manifest.append({"slot_id": slot_id, "size": size, "sha256": sha})
        return manifest

    # Commit --------------------------------------------------------------------------

    def commit(self, db: Session, exam: Exam, plan: Plan) -> Receipt:
        if plan.existing is not None:
            existing = db.get(Submission, plan.existing.id)  # reload in this session
            if existing is None:  # pragma: no cover - defensive
                raise DomainError("bad_request")
            return self._issue_receipt(db, exam, existing, verification_code=None)
        now = self.clock.now()
        verification_code = human_code(8)
        state = plan.student
        for _ in range(5):
            receipt_code = format_receipt_code(human_code(8))
            if (
                db.scalar(select(Submission.id).where(Submission.receipt_code == receipt_code))
                is None
            ):
                break
        submission = Submission(
            id=plan.submission_id,
            exam_id=exam.id,
            student_number=state.student_number,
            first_name=state.first_name,
            infix=state.infix or None,
            last_name=state.last_name,
            on_roster=state.on_roster,
            attempt=plan.attempt,
            status="active",
            started_at=plan.started_at,
            completed_at=now,
            declaration_accepted=True,
            confirmed_warnings=plan.warnings,
            client_ip=plan.client_ip,
            idempotency_key=plan.idempotency_key,
            receipt_code=receipt_code,
            verification_code_hash=keyed_hash(
                self._secret_key, _VERIFICATION_PURPOSE, verification_code
            ),
        )
        submission.files = [
            SubmissionFile(
                position=f.position,
                expected_file_id=f.slot_id,
                original_name=f.name,
                storage_key=f.storage_key,
                size=f.size,
                sha256=f.sha256,
                warnings=f.warnings,
            )
            for f in plan.files
        ]
        try:
            db.add(submission)
            if exam.requirements_locked_at is None:
                exam.requirements_locked_at = now
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            replay = self.find_idempotent(db, exam, plan.idempotency_key)
            if replay is not None and replay.student_number == state.student_number:
                return self._issue_receipt(db, exam, replay, verification_code=None)
            raise DomainError("already_submitted") from exc
        self.audit.append(
            db,
            "submission.completed",
            actor=Actor.student(state.student_number),
            chain=exam.id,
            ip=plan.client_ip,
            details={
                "submission_id": str(submission.id),
                "receipt_code": receipt_code,
                "attempt": plan.attempt,
                "on_roster": state.on_roster,
                "started_at": plan.started_at.isoformat(),
                "files": [
                    {"name": f.name, "size": f.size, "sha256": f.sha256, "slot": f.slot_label}
                    for f in plan.files
                ],
                "warnings": plan.warnings,
            },
        )
        return self._issue_receipt(db, exam, submission, verification_code=verification_code)

    def _issue_receipt(
        self, db: Session, exam: Exam, submission: Submission, *, verification_code: str | None
    ) -> Receipt:
        token = new_token()
        db.add(
            ReceiptToken(
                submission_id=submission.id,
                token_hash=hash_token(token),
                expires_at=self.clock.now() + RECEIPT_TOKEN_LIFETIME,
            )
        )
        db.commit()
        return Receipt(
            submission=submission,
            exam=exam,
            verification_code=verification_code,
            token=token,
            files=self.receipt_files(exam, submission),
        )

    @staticmethod
    def receipt_files(exam: Exam, submission: Submission) -> list[dict[str, Any]]:
        labels = {s.id: s.label for s in exam.expected_files}
        return [
            {
                "name": f.original_name,
                "slot": labels.get(f.expected_file_id) if f.expected_file_id else None,
                "size": f.size,
                "sha256": f.sha256,
                "warnings": list(f.warnings or []),
            }
            for f in sorted(submission.files, key=lambda f: f.position)
        ]

    def record_started(
        self, db: Session, exam: Exam, state: StudentState, *, ip: str | None, started_at: datetime
    ) -> None:
        self.audit.append(
            db, "submission.started", actor=Actor.student(state.student_number), chain=exam.id,
            ip=ip, details={"started_at": started_at.isoformat()},
        )  # fmt: skip
        db.commit()

    def record_failed(
        self, db: Session, exam: Exam, state: StudentState | None, reason: str, *, ip: str | None
    ) -> None:
        actor = (
            Actor.student(state.student_number)
            if state and state.student_number
            else Actor.anonymous()
        )
        self.audit.append(
            db, "submission.failed", actor=actor, chain=exam.id, ip=ip,
            details={"reason": reason},
        )  # fmt: skip
        db.commit()

    # Receipt PDF token -----------------------------------------------------------------

    def use_receipt_token(self, db: Session, token: str) -> Submission:
        row = db.scalar(select(ReceiptToken).where(ReceiptToken.token_hash == hash_token(token)))
        now = self.clock.now()
        if row is None or row.used_at is not None or row.expires_at <= now:
            raise NotFoundError("receipt_token_invalid")
        row.used_at = now
        submission = db.get(Submission, row.submission_id)
        if submission is None:  # pragma: no cover - defensive
            raise NotFoundError("receipt_token_invalid")
        db.commit()
        return submission

    def verification_matches(self, submission: Submission, code: str) -> bool:
        normalized = code.strip().upper().replace("-", "").replace(" ", "")
        return (
            keyed_hash(self._secret_key, _VERIFICATION_PURPOSE, normalized)
            == submission.verification_code_hash
        )
