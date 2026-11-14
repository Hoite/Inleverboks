"""Exams: create, edit, publish, duplicate, templates, rosters, extensions, ownership."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.config import Settings
from inleverboks.db.models import (
    ClassList,
    Exam,
    ExamRosterEntry,
    ExamTemplate,
    ExamTemplateSlot,
    ExpectedFile,
    ExtensionGrant,
    IpPreset,
    RetiredSlug,
    Submission,
    TemplateFile,
    User,
)
from inleverboks.policy import ExamAction, ExamRef, can_exam
from inleverboks.services import slugs
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.services.ipranges import parse_cidrs
from inleverboks.services.security import human_code
from inleverboks.services.system_settings import SettingsService, parse_extensions
from inleverboks.services.template_files import StoredTemplate
from inleverboks.services.timeutil import default_window

ACCESS_CODE_LENGTH = 6
MB = 1024 * 1024
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,10}$")
_PATTERN_RE = re.compile(r"^[^/\\:\x00-\x1f]{1,255}$")


class ExamStatus(StrEnum):
    CONCEPT = "concept"
    SCHEDULED = "scheduled"
    OPEN = "open"
    CLOSED = "closed"
    FINALIZED = "finalized"


@dataclass
class SlotInput:
    label: str
    pattern: str | None = None
    extensions: list[str] | None = None
    id: uuid.UUID | None = None  # existing slot (keeps its template file)


@dataclass
class ExamInput:
    name: str
    description: str | None
    starts_at: datetime
    ends_at: datetime
    second_reviewer_id: uuid.UUID | None = None
    slug: str = ""
    access_code_enabled: bool = True
    ip_restriction_enabled: bool = False
    ip_cidrs: list[str] = field(default_factory=list)
    ip_preset_id: uuid.UUID | None = None
    allow_unlisted: bool = True
    allow_extra_file: bool = True
    extensions: list[str] = field(default_factory=list)
    max_file_mb: int = 25
    max_submission_mb: int = 100
    slots: list[SlotInput] = field(default_factory=list)
    class_list_id: uuid.UUID | None = None
    source_template_id: uuid.UUID | None = None  # copy template files by slot position


class ExamValidationError(DomainError):
    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__("exam_invalid", problems=problems)
        self.problems = problems


def matches_pattern(filename: str, pattern: str) -> bool:
    """Case-insensitive match with ``*`` as the only wildcard (``?`` and ``[`` literal)."""
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, filename, flags=re.IGNORECASE | re.DOTALL) is not None


def pattern_extension(pattern: str | None) -> str | None:
    if not pattern or "." not in pattern:
        return None
    ext = pattern.rsplit(".", 1)[1].lower()
    return ext if _EXTENSION_RE.fullmatch(ext) else None


def _ref(exam: Exam) -> ExamRef:
    return ExamRef(owner_id=exam.owner_id, second_reviewer_id=exam.second_reviewer_id)


def _snapshot(exam: Exam) -> dict[str, Any]:
    """Comparable view of an exam for audit diffs. The access code value is left out."""
    return {
        "name": exam.name,
        "description": exam.description,
        "starts_at": exam.starts_at.isoformat(),
        "ends_at": exam.ends_at.isoformat(),
        "second_reviewer_id": str(exam.second_reviewer_id) if exam.second_reviewer_id else None,
        "slug": exam.slug,
        "access_code_enabled": exam.access_code_enabled,
        "ip_restriction_enabled": exam.ip_restriction_enabled,
        "ip_cidrs": list(exam.ip_cidrs or []),
        "allow_unlisted": exam.allow_unlisted,
        "allow_extra_file": exam.allow_extra_file,
        "extensions": list(exam.extensions or []),
        "max_file_bytes": exam.max_file_bytes,
        "max_submission_bytes": exam.max_submission_bytes,
        "slots": [
            {
                "label": s.label,
                "pattern": s.expected_name_pattern,
                "extensions": s.extensions,
                "template_sha256": s.template_file.sha256 if s.template_file else None,
            }
            for s in exam.expected_files
        ],
    }


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: {"old": before.get(key), "new": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


REQUIREMENT_KEYS = frozenset(
    {"slots", "extensions", "max_file_bytes", "max_submission_bytes", "allow_extra_file"}
)


class ExamService:
    def __init__(
        self, *, clock: Clock, audit: AuditService, settings: SettingsService, env: Settings
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.settings = settings
        self.env = env

    # Lookups and status --------------------------------------------------------------

    def get(self, db: Session, exam_id: uuid.UUID) -> Exam:
        exam = db.get(Exam, exam_id)
        if exam is None:
            raise NotFoundError("exam_not_found")
        return exam

    def get_for(self, db: Session, user: User, exam_id: uuid.UUID, action: ExamAction) -> Exam:
        """Load an exam and check the policy. Users without any view right get a 404 so
        the existence of other teachers' exams is not revealed."""
        exam = self.get(db, exam_id)
        if not can_exam(user, ExamAction.VIEW_SUBMISSIONS, _ref(exam)):
            raise NotFoundError("exam_not_found")
        if not can_exam(user, action, _ref(exam)):
            raise PermissionDeniedError()
        return exam

    def by_slug(self, db: Session, slug: str) -> Exam | None:
        return db.scalar(select(Exam).where(Exam.slug == slug))

    def latest_extension(self, db: Session, exam: Exam) -> datetime | None:
        value: datetime | None = db.scalar(
            select(func.max(ExtensionGrant.ends_at)).where(ExtensionGrant.exam_id == exam.id)
        )
        if value is not None and value.tzinfo is None:  # defensive: aggregate on SQLite
            value = value.replace(tzinfo=UTC)
        return value

    def effective_end(self, db: Session, exam: Exam) -> datetime:
        latest = self.latest_extension(db, exam)
        return max(exam.ends_at, latest) if latest else exam.ends_at

    def end_for_student(self, db: Session, exam: Exam, student_number: str) -> datetime:
        grant = db.scalar(
            select(ExtensionGrant).where(
                ExtensionGrant.exam_id == exam.id, ExtensionGrant.student_number == student_number
            )
        )
        return max(exam.ends_at, grant.ends_at) if grant else exam.ends_at

    def status(self, db: Session, exam: Exam, now: datetime | None = None) -> ExamStatus:
        now = now or self.clock.now()
        if exam.finalized_at is not None:
            return ExamStatus.FINALIZED
        if exam.published_at is None:
            return ExamStatus.CONCEPT
        if now < exam.starts_at:
            return ExamStatus.SCHEDULED
        if now < self.effective_end(db, exam):
            return ExamStatus.OPEN
        return ExamStatus.CLOSED

    def has_submissions(self, db: Session, exam: Exam) -> bool:
        return bool(db.scalar(select(exists().where(Submission.exam_id == exam.id))))

    def submission_count(self, db: Session, exam: Exam) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(Submission)
                .where(Submission.exam_id == exam.id, Submission.status == "active")
            )
            or 0
        )

    def roster_count(self, db: Session, exam: Exam) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(ExamRosterEntry)
                .where(ExamRosterEntry.exam_id == exam.id)
            )
            or 0
        )

    # Dashboard -----------------------------------------------------------------------

    def owned(self, db: Session, user: User) -> list[Exam]:
        return list(
            db.scalars(
                select(Exam)
                .where(Exam.owner_id == user.id, Exam.finalized_at.is_(None))
                .order_by(Exam.starts_at.desc())
            )
        )

    def shared_with(self, db: Session, user: User) -> list[Exam]:
        return list(
            db.scalars(
                select(Exam)
                .where(Exam.second_reviewer_id == user.id, Exam.finalized_at.is_(None))
                .order_by(Exam.starts_at.desc())
            )
        )

    def finalized(self, db: Session, user: User) -> list[Exam]:
        query = select(Exam).where(Exam.finalized_at.is_not(None))
        if not user.is_admin:
            query = query.where(or_(Exam.owner_id == user.id, Exam.second_reviewer_id == user.id))
        return list(db.scalars(query.order_by(Exam.finalized_at.desc())))

    def all_exams(self, db: Session) -> list[Exam]:
        return list(db.scalars(select(Exam).order_by(Exam.starts_at.desc())))

    # Input defaults and validation ---------------------------------------------------

    def defaults(self, db: Session, starts_at: datetime, ends_at: datetime) -> ExamInput:
        system = self.settings.load(db)
        return ExamInput(
            name="",
            description=None,
            starts_at=starts_at,
            ends_at=ends_at,
            extensions=list(system.default_extensions),
            max_file_mb=system.default_max_file_mb,
            max_submission_mb=system.default_max_submission_mb,
            slots=[SlotInput(label="")],
        )

    def input_from_exam(self, db: Session, exam: Exam) -> ExamInput:
        return ExamInput(
            name=exam.name,
            description=exam.description,
            starts_at=exam.starts_at,
            ends_at=exam.ends_at,
            second_reviewer_id=exam.second_reviewer_id,
            slug=exam.slug,
            access_code_enabled=exam.access_code_enabled,
            ip_restriction_enabled=exam.ip_restriction_enabled,
            ip_cidrs=list(exam.ip_cidrs or []),
            ip_preset_id=exam.ip_preset_id,
            allow_unlisted=exam.allow_unlisted,
            allow_extra_file=exam.allow_extra_file,
            extensions=list(exam.extensions or []),
            max_file_mb=max(1, exam.max_file_bytes // MB),
            max_submission_mb=max(1, exam.max_submission_bytes // MB),
            slots=[
                SlotInput(
                    label=s.label,
                    pattern=s.expected_name_pattern,
                    extensions=list(s.extensions) if s.extensions else None,
                    id=s.id,
                )
                for s in exam.expected_files
            ],
        )

    def input_from_template(
        self, db: Session, template: ExamTemplate, base: ExamInput
    ) -> ExamInput:
        config = template.config
        base.access_code_enabled = bool(config.get("access_code_enabled", True))
        base.ip_restriction_enabled = bool(config.get("ip_restriction_enabled", False))
        base.ip_cidrs = list(config.get("ip_cidrs", []))
        preset = config.get("ip_preset_id")
        base.ip_preset_id = uuid.UUID(preset) if preset else None
        base.allow_unlisted = bool(config.get("allow_unlisted", True))
        base.allow_extra_file = bool(config.get("allow_extra_file", True))
        base.extensions = list(config.get("extensions", base.extensions))
        base.max_file_mb = int(config.get("max_file_mb", base.max_file_mb))
        base.max_submission_mb = int(config.get("max_submission_mb", base.max_submission_mb))
        base.slots = [
            SlotInput(label=s.label, pattern=s.expected_name_pattern, extensions=s.extensions)
            for s in template.slots
        ]
        base.source_template_id = template.id
        return base

    def validate(
        self, db: Session, owner_id: uuid.UUID, data: ExamInput, *, exam: Exam | None = None
    ) -> ExamInput:
        """Normalize and validate; raises ExamValidationError with a code per field."""
        problems: dict[str, str] = {}
        self._validate_basics(db, owner_id, data, exam, problems)
        self._validate_access(db, data, problems)
        self._validate_limits(data, problems)
        self._validate_slots(db, data, problems)
        if data.class_list_id is not None:
            class_list = db.get(ClassList, data.class_list_id)
            if class_list is None or class_list.owner_id != owner_id:
                problems["class_list_id"] = "invalid"
        if problems:
            raise ExamValidationError(problems)
        return data

    def _validate_basics(
        self,
        db: Session,
        owner_id: uuid.UUID,
        data: ExamInput,
        exam: Exam | None,
        problems: dict[str, str],
    ) -> None:
        data.name = data.name.strip()
        data.description = (data.description or "").strip() or None
        if not data.name:
            problems["name"] = "required"
        elif len(data.name) > 200:
            problems["name"] = "too_long"
        if data.description and len(data.description) > 500:
            problems["description"] = "too_long"
        if data.ends_at <= data.starts_at:
            problems["ends_at"] = "end_before_start"
        data.slug = data.slug.strip().lower()
        if not data.slug:
            data.slug = slugs.suggest(db, data.name) if data.name else slugs.unique_random(db)
        try:
            slugs.check_available(db, data.slug, exclude_exam_id=exam.id if exam else None)
        except DomainError as exc:
            problems["slug"] = exc.code
        if data.second_reviewer_id is not None:
            reviewer = db.get(User, data.second_reviewer_id)
            if reviewer is None or not reviewer.is_active or reviewer.id == owner_id:
                problems["second_reviewer_id"] = "invalid"

    def _validate_access(self, db: Session, data: ExamInput, problems: dict[str, str]) -> None:
        try:
            data.ip_cidrs = parse_cidrs(data.ip_cidrs)
        except DomainError as exc:
            problems["ip_cidrs"] = exc.code
        if data.ip_preset_id is not None:
            preset = db.get(IpPreset, data.ip_preset_id)
            if preset is None:
                problems["ip_preset_id"] = "invalid"
            else:
                data.ip_cidrs = list(dict.fromkeys([*data.ip_cidrs, *preset.cidrs]))
        if data.ip_restriction_enabled and not data.ip_cidrs and "ip_cidrs" not in problems:
            problems["ip_cidrs"] = "ip_required"

    def _validate_limits(self, data: ExamInput, problems: dict[str, str]) -> None:
        data.extensions = parse_extensions(data.extensions)
        if not data.extensions or not all(_EXTENSION_RE.fullmatch(e) for e in data.extensions):
            problems["extensions"] = "invalid"
        if not 1 <= data.max_file_mb <= self.env.max_file_mb:
            problems["max_file_mb"] = "range"
        if not 1 <= data.max_submission_mb <= self.env.max_submission_mb:
            problems["max_submission_mb"] = "range"
        elif data.max_file_mb > data.max_submission_mb and "max_file_mb" not in problems:
            problems["max_file_mb"] = "exceeds_submission"

    def _validate_slots(self, db: Session, data: ExamInput, problems: dict[str, str]) -> None:
        for index, slot in enumerate(data.slots):
            slot.label = slot.label.strip()
            slot.pattern = (slot.pattern or "").strip() or None
            if slot.extensions is not None:
                slot.extensions = parse_extensions(slot.extensions) or None
            if not slot.label:
                problems[f"slot_{index}_label"] = "required"
            elif len(slot.label) > 100:
                problems[f"slot_{index}_label"] = "too_long"
            if slot.pattern and not _PATTERN_RE.fullmatch(slot.pattern):
                problems[f"slot_{index}_pattern"] = "invalid"
            if slot.extensions and not all(_EXTENSION_RE.fullmatch(e) for e in slot.extensions):
                problems[f"slot_{index}_extensions"] = "invalid"
        if len(data.slots) > self.settings.load(db).max_files_per_submission:
            problems["slots"] = "too_many"
        if not data.slots and not data.allow_extra_file:
            problems["slots"] = "no_files_possible"

    # Create / update -----------------------------------------------------------------

    def _apply(self, exam: Exam, data: ExamInput) -> None:
        exam.name = data.name
        exam.description = data.description
        exam.starts_at = data.starts_at
        exam.ends_at = data.ends_at
        exam.second_reviewer_id = data.second_reviewer_id
        exam.slug = data.slug
        exam.access_code_enabled = data.access_code_enabled
        if data.access_code_enabled and not exam.access_code:
            exam.access_code = human_code(ACCESS_CODE_LENGTH)
        exam.ip_restriction_enabled = data.ip_restriction_enabled
        exam.ip_cidrs = data.ip_cidrs
        exam.ip_preset_id = data.ip_preset_id
        exam.allow_unlisted = data.allow_unlisted
        exam.allow_extra_file = data.allow_extra_file
        exam.extensions = data.extensions
        exam.max_file_bytes = data.max_file_mb * MB
        exam.max_submission_bytes = data.max_submission_mb * MB

    def _apply_slots(self, db: Session, exam: Exam, slots: list[SlotInput]) -> None:
        existing = {s.id: s for s in exam.expected_files}
        kept: list[ExpectedFile] = []
        for slot in slots:
            row = existing.pop(slot.id, None) if slot.id else None
            if row is None:
                row = ExpectedFile(exam_id=exam.id)
            row.label = slot.label
            row.expected_name_pattern = slot.pattern
            row.extensions = slot.extensions
            kept.append(row)
        for removed in existing.values():
            exam.expected_files.remove(removed)
        db.flush()
        # Positions are unique per exam: renumber through a temporary offset.
        for index, row in enumerate(kept):
            row.position = 10_000 + index
        exam.expected_files = kept
        db.flush()
        for index, row in enumerate(kept, start=1):
            row.position = index
        db.flush()

    def _copy_template_files(
        self, db: Session, exam: Exam, source: list[TemplateFile | None]
    ) -> None:
        now = self.clock.now()
        for slot, template in zip(exam.expected_files, source, strict=False):
            if template is None:
                continue
            slot.template_file = TemplateFile(
                original_name=template.original_name,
                storage_key=template.storage_key,
                size=template.size,
                sha256=template.sha256,
                uploaded_at=now,
            )
        db.flush()

    def create(self, db: Session, owner: User, data: ExamInput, *, ip: str | None) -> Exam:
        data = self.validate(db, owner.id, data)
        now = self.clock.now()
        exam = Exam(owner_id=owner.id, created_at=now, updated_at=now)
        self._apply(exam, data)
        db.add(exam)
        db.flush()
        self._apply_slots(db, exam, data.slots)
        if data.source_template_id is not None:
            template = db.get(ExamTemplate, data.source_template_id)
            if template is not None and template.owner_id == owner.id:
                self._copy_template_files(db, exam, [s.template_file for s in template.slots])
        if data.class_list_id is not None:
            self._set_roster(db, exam, db.get(ClassList, data.class_list_id))
        self.audit.append(
            db,
            "exam.created",
            actor=Actor.for_user(owner),
            chain=exam.id,
            ip=ip,
            details={
                "exam": _snapshot(exam),
                "class_list_id": str(data.class_list_id) if data.class_list_id else None,
            },
        )
        db.commit()
        return exam

    def update(
        self, db: Session, user: User, exam: Exam, data: ExamInput, *, ip: str | None
    ) -> dict[str, dict[str, Any]]:
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        if exam.finalized_at is not None:
            raise DomainError("exam_finalized")
        data = self.validate(db, exam.owner_id, data, exam=exam)
        before = _snapshot(exam)
        locked = self.has_submissions(db, exam)
        if locked:
            # File requirements are fixed once a student has handed in (spec §4.2).
            data.slots = [
                SlotInput(
                    label=s.label, pattern=s.expected_name_pattern, extensions=s.extensions, id=s.id
                )
                for s in exam.expected_files
            ]
            data.extensions = list(exam.extensions)
            data.max_file_mb = exam.max_file_bytes // MB
            data.max_submission_mb = exam.max_submission_bytes // MB
            data.allow_extra_file = exam.allow_extra_file
        if exam.slug != data.slug and exam.published_at is not None:
            # A published link may already be on a beamer or in a QR code; the old slug
            # stays reserved so it never points at another exam.
            db.add(RetiredSlug(slug=exam.slug, retired_at=self.clock.now()))
        self._apply(exam, data)
        if not locked:
            self._apply_slots(db, exam, data.slots)
        exam.updated_at = self.clock.now()
        db.flush()
        db.refresh(exam)
        changes = diff(before, _snapshot(exam))
        if locked and REQUIREMENT_KEYS & set(changes):  # pragma: no cover - defensive
            raise DomainError("requirements_locked")
        if changes:
            self.audit.append(
                db,
                "exam.updated",
                actor=Actor.for_user(user),
                chain=exam.id,
                ip=ip,
                details={"changes": changes},
            )
        db.commit()
        return changes

    def publish(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> None:
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        if exam.published_at is not None:
            return
        exam.published_at = self.clock.now()
        self.audit.append(db, "exam.published", actor=Actor.for_user(user), chain=exam.id, ip=ip)
        db.commit()

    def unpublish(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> None:
        """Back to concept; only while nobody has handed in."""
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        if self.has_submissions(db, exam):
            raise DomainError("has_submissions")
        if exam.published_at is None:
            return
        exam.published_at = None
        self.audit.append(db, "exam.unpublished", actor=Actor.for_user(user), chain=exam.id, ip=ip)
        db.commit()

    def delete_concept(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> list[str]:
        """Delete a concept without submissions. Returns template storage keys that may
        now be unreferenced (the caller removes them from storage)."""
        if not can_exam(user, ExamAction.DELETE_CONCEPT, _ref(exam)):
            raise PermissionDeniedError()
        if exam.published_at is not None:
            raise DomainError("not_concept")
        if self.has_submissions(db, exam):
            raise DomainError("has_submissions")
        keys = [s.template_file.storage_key for s in exam.expected_files if s.template_file]
        details = {"exam_id": str(exam.id), "name": exam.name, "slug": exam.slug}
        self.audit.append(
            db, "exam.deleted", actor=Actor.for_user(user), chain=exam.id, ip=ip, details=details
        )
        self.audit.append(db, "exam.deleted", actor=Actor.for_user(user), ip=ip, details=details)
        db.add(RetiredSlug(slug=exam.slug, retired_at=self.clock.now()))
        db.query(ExamRosterEntry).filter_by(exam_id=exam.id).delete()
        db.query(ExtensionGrant).filter_by(exam_id=exam.id).delete()
        db.delete(exam)
        db.commit()
        return keys

    def regenerate_code(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> str:
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        new = human_code(ACCESS_CODE_LENGTH)
        while new == exam.access_code:
            new = human_code(ACCESS_CODE_LENGTH)
        exam.access_code = new
        self.audit.append(
            db, "exam.access_code_regenerated", actor=Actor.for_user(user), chain=exam.id, ip=ip
        )
        db.commit()
        return new

    def duplicate(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> Exam:
        """Copy configuration, slots, template files and roster; new times, slug and code."""
        if not can_exam(user, ExamAction.DUPLICATE, _ref(exam)):
            raise PermissionDeniedError()
        data = self.input_from_exam(db, exam)
        for slot in data.slots:
            slot.id = None
        data.name = f"{exam.name} (kopie)"[:200]
        data.slug = ""
        data.ip_preset_id = None
        now = self.clock.now()
        duration = exam.ends_at - exam.starts_at
        data.starts_at, _ = default_window(now, self.env.timezone)
        data.ends_at = data.starts_at + duration
        copy = self.create(db, user, data, ip=ip)
        self._copy_template_files(db, copy, [s.template_file for s in exam.expected_files])
        roster = list(db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id)))
        for entry in roster:
            db.add(
                ExamRosterEntry(
                    exam_id=copy.id,
                    student_number=entry.student_number,
                    first_name=entry.first_name,
                    infix=entry.infix,
                    last_name=entry.last_name,
                    class_name=entry.class_name,
                )
            )
        self.audit.append(
            db,
            "exam.duplicated",
            actor=Actor.for_user(user),
            chain=copy.id,
            ip=ip,
            details={"source_exam_id": str(exam.id)},
        )
        db.commit()
        return copy

    # Template files ------------------------------------------------------------------

    def attach_template_file(
        self,
        db: Session,
        user: User,
        exam: Exam,
        slot_id: uuid.UUID,
        stored: StoredTemplate,
        original_name: str,
        *,
        ip: str | None,
    ) -> str | None:
        """Attach an uploaded template to a slot. Returns the storage key of a replaced
        template that may now be unreferenced."""
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        if self.has_submissions(db, exam):
            raise DomainError("requirements_locked")
        slot = next((s for s in exam.expected_files if s.id == slot_id), None)
        if slot is None:
            raise NotFoundError()
        replaced = slot.template_file.storage_key if slot.template_file else None
        if slot.template_file is not None:
            db.delete(slot.template_file)
            db.flush()
        slot.template_file = TemplateFile(
            original_name=original_name[:255],
            storage_key=stored.storage_key,
            size=stored.size,
            sha256=stored.sha256,
            uploaded_at=self.clock.now(),
        )
        self.audit.append(
            db,
            "exam.template_file_uploaded",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={
                "slot": slot.label,
                "name": original_name,
                "sha256": stored.sha256,
                "size": stored.size,
            },
        )
        db.commit()
        return replaced if replaced != stored.storage_key else None

    def remove_template_file(
        self, db: Session, user: User, exam: Exam, slot_id: uuid.UUID, *, ip: str | None
    ) -> str | None:
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        if self.has_submissions(db, exam):
            raise DomainError("requirements_locked")
        slot = next((s for s in exam.expected_files if s.id == slot_id), None)
        if slot is None or slot.template_file is None:
            return None
        key = slot.template_file.storage_key
        self.audit.append(
            db,
            "exam.template_file_removed",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={"slot": slot.label, "sha256": slot.template_file.sha256},
        )
        db.delete(slot.template_file)
        db.commit()
        return key

    # Exam templates ------------------------------------------------------------------

    def save_as_template(
        self, db: Session, user: User, exam: Exam, name: str, *, ip: str | None
    ) -> ExamTemplate:
        if not can_exam(user, ExamAction.DUPLICATE, _ref(exam)):
            raise PermissionDeniedError()
        name = name.strip() or exam.name
        template = ExamTemplate(
            owner_id=user.id,
            name=name[:200],
            created_at=self.clock.now(),
            config={
                "access_code_enabled": exam.access_code_enabled,
                "ip_restriction_enabled": exam.ip_restriction_enabled,
                "ip_cidrs": list(exam.ip_cidrs or []),
                "ip_preset_id": str(exam.ip_preset_id) if exam.ip_preset_id else None,
                "allow_unlisted": exam.allow_unlisted,
                "allow_extra_file": exam.allow_extra_file,
                "extensions": list(exam.extensions or []),
                "max_file_mb": max(1, exam.max_file_bytes // MB),
                "max_submission_mb": max(1, exam.max_submission_bytes // MB),
            },
        )
        now = self.clock.now()
        for slot in exam.expected_files:
            template_slot = ExamTemplateSlot(
                position=slot.position,
                label=slot.label,
                expected_name_pattern=slot.expected_name_pattern,
                extensions=slot.extensions,
            )
            if slot.template_file is not None:
                template_slot.template_file = TemplateFile(
                    original_name=slot.template_file.original_name,
                    storage_key=slot.template_file.storage_key,
                    size=slot.template_file.size,
                    sha256=slot.template_file.sha256,
                    uploaded_at=now,
                )
            template.slots.append(template_slot)
        db.add(template)
        db.flush()
        self.audit.append(
            db,
            "exam_template.saved",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={"template_id": str(template.id), "name": template.name},
        )
        db.commit()
        return template

    def templates(self, db: Session, user: User) -> list[ExamTemplate]:
        return list(
            db.scalars(
                select(ExamTemplate)
                .where(ExamTemplate.owner_id == user.id)
                .order_by(ExamTemplate.name)
            )
        )

    def get_template(self, db: Session, user: User, template_id: uuid.UUID) -> ExamTemplate:
        template = db.get(ExamTemplate, template_id)
        if template is None or template.owner_id != user.id:
            raise NotFoundError("template_not_found")
        return template

    def delete_template(
        self, db: Session, user: User, template_id: uuid.UUID, *, ip: str | None
    ) -> list[str]:
        template = self.get_template(db, user, template_id)
        keys = [s.template_file.storage_key for s in template.slots if s.template_file]
        self.audit.append(
            db,
            "exam_template.deleted",
            actor=Actor.for_user(user),
            ip=ip,
            details={"template_id": str(template.id), "name": template.name},
        )
        db.delete(template)
        db.commit()
        return keys

    # Roster --------------------------------------------------------------------------

    def _set_roster(self, db: Session, exam: Exam, class_list: ClassList | None) -> int:
        db.query(ExamRosterEntry).filter_by(exam_id=exam.id).delete()
        if class_list is None:
            return 0
        for entry in class_list.entries:
            db.add(
                ExamRosterEntry(
                    exam_id=exam.id,
                    student_number=entry.student_number,
                    first_name=entry.first_name,
                    infix=entry.infix,
                    last_name=entry.last_name,
                    class_name=entry.class_name,
                )
            )
        db.flush()
        return len(class_list.entries)

    def set_roster(
        self,
        db: Session,
        user: User,
        exam: Exam,
        class_list_id: uuid.UUID | None,
        *,
        ip: str | None,
    ) -> int:
        if not can_exam(user, ExamAction.EDIT_SETTINGS, _ref(exam)):
            raise PermissionDeniedError()
        class_list = None
        if class_list_id is not None:
            class_list = db.get(ClassList, class_list_id)
            if class_list is None or class_list.owner_id != user.id:
                raise NotFoundError("class_list_not_found")
        count = self._set_roster(db, exam, class_list)
        self.audit.append(
            db,
            "exam.roster_set",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={
                "class_list_id": str(class_list.id) if class_list else None,
                "class_list_name": class_list.name if class_list else None,
                "entries": count,
            },
        )
        db.commit()
        return count

    def roster(self, db: Session, exam: Exam) -> list[ExamRosterEntry]:
        return list(
            db.scalars(
                select(ExamRosterEntry)
                .where(ExamRosterEntry.exam_id == exam.id)
                .order_by(ExamRosterEntry.last_name, ExamRosterEntry.first_name)
            )
        )

    # Extensions ----------------------------------------------------------------------

    def extensions(self, db: Session, exam: Exam) -> list[ExtensionGrant]:
        return list(
            db.scalars(
                select(ExtensionGrant)
                .where(ExtensionGrant.exam_id == exam.id)
                .order_by(ExtensionGrant.student_number)
            )
        )

    def grant_extension(
        self,
        db: Session,
        user: User,
        exam: Exam,
        student_number: str,
        ends_at: datetime,
        *,
        ip: str | None,
    ) -> ExtensionGrant:
        if not can_exam(user, ExamAction.MANAGE_SUBMISSIONS, _ref(exam)):
            raise PermissionDeniedError()
        student_number = student_number.strip()
        if not re.fullmatch(self.settings.load(db).student_number_regex, student_number):
            raise DomainError("student_number_invalid")
        if ends_at <= exam.ends_at:
            raise DomainError("extension_not_later")
        grant = db.scalar(
            select(ExtensionGrant).where(
                ExtensionGrant.exam_id == exam.id, ExtensionGrant.student_number == student_number
            )
        )
        old = grant.ends_at.isoformat() if grant else None
        if grant is None:
            grant = ExtensionGrant(
                exam_id=exam.id,
                student_number=student_number,
                created_by_id=user.id,
                created_at=self.clock.now(),
                ends_at=ends_at,
            )
            db.add(grant)
        else:
            grant.ends_at = ends_at
        self.audit.append(
            db,
            "exam.extension_granted",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={"student_number": student_number, "old": old, "new": ends_at.isoformat()},
        )
        db.commit()
        return grant

    def revoke_extension(
        self, db: Session, user: User, exam: Exam, grant_id: uuid.UUID, *, ip: str | None
    ) -> None:
        if not can_exam(user, ExamAction.MANAGE_SUBMISSIONS, _ref(exam)):
            raise PermissionDeniedError()
        grant = db.get(ExtensionGrant, grant_id)
        if grant is None or grant.exam_id != exam.id:
            raise NotFoundError()
        self.audit.append(
            db,
            "exam.extension_revoked",
            actor=Actor.for_user(user),
            chain=exam.id,
            ip=ip,
            details={"student_number": grant.student_number, "old": grant.ends_at.isoformat()},
        )
        db.delete(grant)
        db.commit()

    # Ownership -----------------------------------------------------------------------

    def transfer_ownership(
        self, db: Session, admin: User, exam: Exam, new_owner_id: uuid.UUID, *, ip: str | None
    ) -> None:
        if not can_exam(admin, ExamAction.TRANSFER_OWNERSHIP, _ref(exam)):
            raise PermissionDeniedError()
        new_owner = db.get(User, new_owner_id)
        if new_owner is None or not new_owner.is_active:
            raise DomainError("owner_invalid")
        if new_owner.id == exam.owner_id:
            return
        old_owner_id = exam.owner_id
        exam.owner_id = new_owner.id
        if exam.second_reviewer_id == new_owner.id:
            exam.second_reviewer_id = None
        details = {
            "old_owner_id": str(old_owner_id),
            "new_owner_id": str(new_owner.id),
            "new_owner": new_owner.email,
        }
        self.audit.append(
            db,
            "exam.ownership_transferred",
            actor=Actor.for_user(admin),
            chain=exam.id,
            ip=ip,
            details=details,
        )
        self.audit.append(
            db,
            "exam.ownership_transferred",
            actor=Actor.for_user(admin),
            ip=ip,
            details={**details, "exam_id": str(exam.id)},
        )
        db.commit()
