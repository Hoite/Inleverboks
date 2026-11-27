"""Exams: create, edit, publish, duplicate, templates, rosters, extensions, ownership."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import distinct, exists, func, or_, select, update
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
    ProcesVerbaal,
    Qualification,
    RetiredSlug,
    Submission,
    SubmissionFile,
    Team,
    TeamCrebo,
    TeamMember,
    TemplateFile,
    User,
)
from inleverboks.policy import (
    ClassListAction,
    ExamAction,
    ExamRef,
    TeamAction,
    TemplateAction,
    TemplateRef,
    can_class_list,
    can_exam,
    can_team,
    can_template,
)
from inleverboks.services import slugs
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.classlists import ref as class_list_ref
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.services.ipranges import parse_cidrs
from inleverboks.services.qualifications import CODE_RE
from inleverboks.services.security import human_code
from inleverboks.services.system_settings import SettingsService, parse_extensions
from inleverboks.services.teams import team_context
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
    work_process_code: str | None = None  # SBB code such as B1-K1-W1


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
    source_template_id: uuid.UUID | None = None  # copy template files per slot
    # A roster given directly (duplicate, resit) instead of a class list.
    roster: list[RosterInput] | None = None
    # Snapshot of the SBB qualification (crebo, name, dossier version, core task).
    qualification: dict[str, Any] | None = None
    source_exam_id: uuid.UUID | None = None  # resit: copy template files from this exam
    # Resit: student numbers from the source exam that make up the roster.
    roster_numbers: list[str] = field(default_factory=list)
    # The link name the form suggested (with the planned date). When the teacher kept it,
    # the server dates it to the chosen start (see ``redate_slug``).
    suggested_slug: str = ""
    # The education team (PLAN B67): the teacher's choice on a new exam. The server decides
    # in ``_resolve_team``; the form value only counts for a team of the owner.
    team_id: uuid.UUID | None = None
    # Set by the server only (duplicate): keep ``team_id`` as it is.
    team_fixed: bool = False


@dataclass(frozen=True)
class RosterInput:
    student_number: str
    first_name: str
    last_name: str
    infix: str | None = None
    class_name: str | None = None


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
    return ExamRef(
        owner_id=exam.owner_id, second_reviewer_id=exam.second_reviewer_id, team_id=exam.team_id
    )


def may(db: Session, user: User, action: ExamAction, exam: Exam) -> bool:
    """``policy.can_exam`` with the user's team context (PLAN B68). Use this everywhere."""
    return can_exam(user, action, _ref(exam), team_context(db, user.id))


def _snapshot(exam: Exam) -> dict[str, Any]:
    """Comparable view of an exam for audit diffs. The access code value is left out."""
    return {
        "name": exam.name,
        "description": exam.description,
        "starts_at": exam.starts_at.isoformat(),
        "ends_at": exam.ends_at.isoformat(),
        "second_reviewer_id": str(exam.second_reviewer_id) if exam.second_reviewer_id else None,
        "team_id": str(exam.team_id) if exam.team_id else None,
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
                "work_process_code": s.work_process_code,
                "template_sha256": s.template_file.sha256 if s.template_file else None,
            }
            for s in exam.expected_files
        ],
    }


TEMPLATE_WINDOW = timedelta(hours=2)
DASH = "\u2013"  # en dash between qualification and core task in suggested names
SLUG_DATE_FORMAT = "%d%m%Y"
_SLUG_DATE_RE = re.compile(r"(?<![0-9])[0-9]{8}(?=(?:-[0-9]+)?$)")
_SLUG_COUNTER_RE = re.compile(r"-[0-9]+$")


def redate_slug(slug: str, day: str) -> str | None:
    """Put ``day`` (``ddmmYYYY``) in place of the date at the end of a suggested slug and
    drop a numeric suffix (``-2``); None when the slug has no date."""
    match = _SLUG_DATE_RE.search(slug)
    if match is None:
        return None
    return slug[: match.start()] + day


def _roster_input(entry: ExamRosterEntry) -> RosterInput:
    return RosterInput(
        student_number=entry.student_number, first_name=entry.first_name,
        last_name=entry.last_name, infix=entry.infix, class_name=entry.class_name,
    )  # fmt: skip


def _slot_label(code: str, title: str) -> str:
    """``B1-K1-W1 Title`` within the 100 characters of a slot label."""
    text = f"{code} {title}".strip()
    return text if len(text) <= 100 else text[:99].rstrip() + "…"


def _copy_file(source: TemplateFile, now: datetime) -> TemplateFile:
    return TemplateFile(
        original_name=source.original_name, storage_key=source.storage_key, size=source.size,
        sha256=source.sha256, uploaded_at=now,
    )  # fmt: skip


def _qualification_snapshot(q: Qualification, core_task_code: str | None) -> dict[str, Any]:
    task = next((t for t in q.core_tasks if t.code == core_task_code), None)
    return {
        "crebo": q.crebo, "name": q.name, "level": q.level,
        "dossier_version": q.dossier_version, "core_task_code": core_task_code,
        "core_task_title": task.title if task else None,
    }  # fmt: skip


def template_ref(template: ExamTemplate) -> TemplateRef:
    return TemplateRef(owner_id=template.owner_id, team_id=template.team_id, shared=template.shared)


def can_use_template(db: Session, user: User, template: ExamTemplate) -> bool:
    """Own templates, the team templates of one's teams and the shared templates not yet
    assigned to a team. Admins also see, use and copy the others (PLAN B61, B66)."""
    return can_template(user, team_context(db, user.id), TemplateAction.USE, template_ref(template))


def can_manage_template(db: Session, user: User, template: ExamTemplate) -> bool:
    """Own personal templates; team templates for the team contacts of the team and for
    admins; shared templates for admins (``policy.TEMPLATE_MATRIX``)."""
    return can_template(
        user, team_context(db, user.id), TemplateAction.EDIT, template_ref(template)
    )


def _qualification_in_team(db: Session, team_id: uuid.UUID, qualification: Qualification) -> bool:
    """The crebo (or the crebo it replaced) is linked to the team (PLAN B66)."""
    crebos = {qualification.crebo} | ({qualification.previous_crebo} - {None})
    query = select(TeamCrebo.crebo).where(TeamCrebo.team_id == team_id, TeamCrebo.crebo.in_(crebos))
    return db.scalar(query.limit(1)) is not None


def _template_config(data: ExamInput) -> dict[str, Any]:
    return {
        "access_code_enabled": data.access_code_enabled,
        "ip_restriction_enabled": data.ip_restriction_enabled,
        "ip_cidrs": list(data.ip_cidrs or []),
        "ip_preset_id": str(data.ip_preset_id) if data.ip_preset_id else None,
        "allow_unlisted": data.allow_unlisted,
        "allow_extra_file": data.allow_extra_file,
        "extensions": list(data.extensions or []),
        "max_file_mb": max(1, data.max_file_mb),
        "max_submission_mb": max(1, data.max_submission_mb),
    }


def _template_snapshot(template: ExamTemplate) -> dict[str, Any]:
    return {
        "name": template.name,
        **template.config,
        "shared": template.shared,
        "team_id": str(template.team_id) if template.team_id else None,
        "slots": [
            {
                "label": s.label,
                "pattern": s.expected_name_pattern,
                "extensions": s.extensions,
                "work_process_code": s.work_process_code,
            }
            for s in template.slots
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
        if not may(db, user, ExamAction.VIEW_SUBMISSIONS, exam):
            raise NotFoundError("exam_not_found")
        if not may(db, user, action, exam):
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

    def handed_in_count(self, db: Session, exam: Exam) -> int:
        """Students who handed in, for the presentation counter: one per student with an
        active or invalid submission. A cancelled one does not count: that student hands
        in again. Invalidating work therefore never lowers the count."""
        return (
            db.scalar(
                select(func.count(distinct(Submission.student_number))).where(
                    Submission.exam_id == exam.id, Submission.status.in_(("active", "invalid"))
                )
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

    def all_exams(self, db: Session, owner_id: uuid.UUID | None = None) -> list[Exam]:
        query = select(Exam)
        if owner_id is not None:
            query = query.where(Exam.owner_id == owner_id)
        return list(db.scalars(query.order_by(Exam.starts_at.desc())))

    def contact_teams(self, db: Session, user: User) -> list[Team]:
        """The teams where the user is team contact (PLAN B68)."""
        contact_of = team_context(db, user.id).contact_of
        teams = db.scalars(select(Team).where(Team.id.in_(contact_of)))
        return sorted(teams, key=lambda t: t.name.lower())

    def team_exams(
        self, db: Session, user: User, *, team_id: uuid.UUID | None = None
    ) -> list[Exam]:
        """Every exam (also finalized) of the teams where the user is team contact."""
        contact_of = team_context(db, user.id).contact_of
        ids = {team_id} & contact_of if team_id is not None else contact_of
        query = select(Exam).where(Exam.team_id.in_(ids)).order_by(Exam.starts_at.desc())
        return list(db.scalars(query))

    def exam_owner_ids(self, db: Session) -> set[uuid.UUID]:
        return set(db.scalars(select(Exam.owner_id).distinct()))

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
                    work_process_code=s.work_process_code,
                )
                for s in exam.expected_files
            ],
            qualification=dict(exam.qualification) if exam.qualification else None,
            source_template_id=exam.source_template_id,
            team_id=exam.team_id,
        )

    def input_from_template(
        self, db: Session, template: ExamTemplate, base: ExamInput,
        work_processes: list[str] | None = None,
    ) -> ExamInput:  # fmt: skip
        """Fill the new-exam form from a template. ``work_processes`` limits the slots of
        a template with SBB codes to a part exam (for example only B1-K1-W3)."""
        config = template.config
        qualification = template.qualification
        if qualification is not None:
            base.name, base.slug = self._suggest_names(
                db, template, qualification, work_processes or [], base.starts_at
            )
            base.suggested_slug = base.slug
            base.qualification = _qualification_snapshot(qualification, template.core_task_code)
        base.name = base.name or template.name
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
        # The template slot ids travel along, so template files follow their slot even
        # when the teacher adds or removes slots before saving.
        selected = set(work_processes or [])
        base.slots = [
            SlotInput(
                label=s.label, pattern=s.expected_name_pattern, extensions=s.extensions, id=s.id,
                work_process_code=s.work_process_code,
            )
            for s in template.slots
            if not selected or s.work_process_code in selected
        ]  # fmt: skip
        base.source_template_id = template.id
        return base

    def slots_template(
        self, db: Session, owner_id: uuid.UUID, data: ExamInput, exam: Exam | None = None
    ) -> ExamTemplate | None:
        """The template whose expected files an exam uses (read-only in the form), or None.

        A new exam: the template it is made from, when the owner may use it (not for a
        resit, which keeps its own selection). An existing exam: the stored source template.
        """
        if exam is not None:
            if exam.source_template_id is None:
                return None
            return db.get(ExamTemplate, exam.source_template_id)
        if data.source_template_id is None or data.source_exam_id is not None:
            return None
        template = db.get(ExamTemplate, data.source_template_id)
        owner = db.get(User, owner_id)
        if template is None or owner is None or not can_use_template(db, owner, template):
            return None
        return template

    def _lock_template_slots(
        self, db: Session, owner_id: uuid.UUID, data: ExamInput, exam: Exam | None
    ) -> None:
        """Expected files of a template-based exam never come from the form: a new exam
        takes the template's slots (limited to the chosen work processes, a part exam);
        an existing one keeps its slots. Slot changes in the POST are ignored."""
        template = self.slots_template(db, owner_id, data, exam)
        if template is None:
            return
        if exam is not None:
            data.slots = [
                SlotInput(
                    label=s.label, pattern=s.expected_name_pattern,
                    extensions=list(s.extensions) if s.extensions else None, id=s.id,
                    work_process_code=s.work_process_code,
                )
                for s in exam.expected_files
            ]  # fmt: skip
            return
        codes = {s.work_process_code for s in template.slots if s.work_process_code}
        chosen = {s.work_process_code for s in data.slots if s.work_process_code} & codes
        data.slots = [
            SlotInput(
                label=s.label, pattern=s.expected_name_pattern,
                extensions=list(s.extensions) if s.extensions else None, id=s.id,
                work_process_code=s.work_process_code,
            )
            for s in template.slots
            if not chosen or s.work_process_code in chosen
        ]  # fmt: skip

    def _resolve_sources(self, db: Session, owner: User, data: ExamInput) -> None:
        """The qualification snapshot and a resit roster come from the server, never from
        hidden form fields."""
        if data.source_template_id is not None:
            template = db.get(ExamTemplate, data.source_template_id)
            usable = template is not None and can_use_template(db, owner, template)
            if usable and template is not None and template.qualification is not None:
                data.qualification = _qualification_snapshot(
                    template.qualification, template.core_task_code
                )
        if data.source_exam_id is not None:
            source = db.get(Exam, data.source_exam_id)
            if source is None or not may(db, owner, ExamAction.DUPLICATE, source):
                data.source_exam_id = None
                return
            data.qualification = dict(source.qualification) if source.qualification else None
            if data.roster_numbers:
                data.roster = self.resit_roster(db, source, data.roster_numbers)
                data.class_list_id = None

    def _suggest_names(
        self, db: Session, template: ExamTemplate, qualification: Qualification,
        selected: list[str], starts_at: datetime,
    ) -> tuple[str, str]:  # fmt: skip
        """``27016 ICT System Engineer, dash, B1-K1 Title`` and ``27016-b1-k1-23112026``.
        A part exam names its work processes instead of the core task."""
        task_code = template.core_task_code or ""
        task = next((t for t in qualification.core_tasks if t.code == task_code), None)
        codes = [s.work_process_code for s in template.slots if s.work_process_code]
        chosen = [c for c in codes if c in selected]
        prefix = f"{qualification.crebo} {qualification.name}"
        if not chosen or len(chosen) == len(codes):
            name = f"{prefix} {DASH} {task_code} {task.title if task else ''}".strip()
            part = task_code
        else:
            name = f"{prefix} {DASH} {', '.join(chosen)}"
            part = task_code + "".join(c.removeprefix(task_code) for c in chosen)
        day = starts_at.astimezone(self.env.timezone).strftime(SLUG_DATE_FORMAT)
        slug = slugs.suggest(db, f"{qualification.crebo}-{part}-{day}")
        return name[:200], slug

    def validate(
        self, db: Session, owner_id: uuid.UUID, data: ExamInput, *, exam: Exam | None = None
    ) -> ExamInput:
        """Normalize and validate; raises ExamValidationError with a code per field."""
        problems: dict[str, str] = {}
        self._lock_template_slots(db, owner_id, data, exam)
        self._validate_basics(db, owner_id, data, exam, problems)
        self._validate_access(db, data, problems)
        self._validate_limits(data, problems)
        self._validate_slots(db, data, problems)
        self._resolve_team(db, owner_id, data, exam, problems)
        if data.class_list_id is not None:
            # The owner must be allowed to use the list (PLAN B65); the policy decides. A
            # team list only fits an exam of the same team.
            class_list = db.get(ClassList, data.class_list_id)
            if class_list is None or not can_class_list(
                db.get(User, owner_id), team_context(db, owner_id), ClassListAction.USE,
                class_list_ref(class_list),
            ):  # fmt: skip
                problems["class_list_id"] = "invalid"
            elif class_list.team_id is not None and class_list.team_id != data.team_id:
                problems["class_list_id"] = "other_team"
        if problems:
            raise ExamValidationError(problems)
        return data

    def exam_teams(self, db: Session, user: User) -> list[Team]:
        """The teams a teacher can put a new exam in: his own (an admin: every team)."""
        query = select(Team)
        if not user.is_admin:
            query = query.where(Team.id.in_(team_context(db, user.id).member_of))
        return sorted(db.scalars(query), key=lambda t: t.name.lower())

    def _resolve_team(
        self, db: Session, owner_id: uuid.UUID, data: ExamInput, exam: Exam | None,
        problems: dict[str, str],
    ) -> None:  # fmt: skip
        """PLAN B67: an existing exam keeps its team (only an admin changes it, elsewhere);
        a new one takes the team of its team template or resit source, otherwise one of
        the owner's teams. Without a team of his own the exam has none."""
        if exam is not None:
            data.team_id = exam.team_id
            return
        if data.team_fixed:
            return
        template = (
            db.get(ExamTemplate, data.source_template_id) if data.source_template_id else None
        )
        if template is not None and template.team_id is not None:
            data.team_id = template.team_id
            return
        source = db.get(Exam, data.source_exam_id) if data.source_exam_id else None
        owner = db.get(User, owner_id)
        if (
            source is not None
            and owner is not None
            and may(db, owner, ExamAction.DUPLICATE, source)
        ):
            data.team_id = source.team_id
            return
        allowed = {t.id for t in self.exam_teams(db, owner)} if owner else set()
        if data.team_id is not None:
            if data.team_id not in allowed:
                problems["team_id"] = "invalid"
            return
        if len(allowed) == 1:
            data.team_id = next(iter(allowed))
        elif allowed and not (owner and owner.is_admin):
            problems["team_id"] = "required"

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
        suggested = data.suggested_slug.strip().lower()
        if exam is None and suggested and data.slug == suggested:
            # The teacher kept the suggested link name: date it to the chosen start.
            day = data.starts_at.astimezone(self.env.timezone).strftime(SLUG_DATE_FORMAT)
            dated = redate_slug(data.slug, day)
            if dated is not None:
                data.slug = slugs.suggest(db, dated)
            data.suggested_slug = data.slug
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
            slot.work_process_code = (slot.work_process_code or "").strip().upper() or None
            if slot.work_process_code and not CODE_RE.fullmatch(slot.work_process_code):
                slot.work_process_code = None  # hidden field from the form: drop, not an error
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
            row.work_process_code = slot.work_process_code
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
        self._resolve_sources(db, owner, data)
        if data.class_list_id is None and not data.roster:
            data.allow_unlisted = True  # without a class list nobody is "on the list"
        now = self.clock.now()
        template_source = self.slots_template(db, owner.id, data)
        exam = Exam(
            owner_id=owner.id, created_at=now, updated_at=now, qualification=data.qualification,
            source_template_id=template_source.id if template_source else None,
            team_id=data.team_id,
        )  # fmt: skip
        self._apply(exam, data)
        db.add(exam)
        db.flush()
        self._apply_slots(db, exam, data.slots)
        if data.source_template_id is not None:
            template = db.get(ExamTemplate, data.source_template_id)
            if template is not None and can_use_template(db, owner, template):
                files = {s.id: s.template_file for s in template.slots}
                if any(slot.id in files for slot in data.slots):
                    source = [files.get(slot.id) if slot.id else None for slot in data.slots]
                else:  # a client without slot ids: match by position
                    source = [s.template_file for s in template.slots]
                self._copy_template_files(db, exam, source)
        if data.source_exam_id is not None:
            source_exam = db.get(Exam, data.source_exam_id)
            if source_exam is not None and may(db, owner, ExamAction.DUPLICATE, source_exam):
                files = {s.id: s.template_file for s in source_exam.expected_files}
                self._copy_template_files(
                    db, exam, [files.get(slot.id) if slot.id else None for slot in data.slots]
                )
        if data.class_list_id is not None:
            self._set_roster(db, exam, db.get(ClassList, data.class_list_id))
        elif data.roster:
            for entry in data.roster:
                db.add(
                    ExamRosterEntry(
                        exam_id=exam.id, student_number=entry.student_number,
                        first_name=entry.first_name, infix=entry.infix,
                        last_name=entry.last_name, class_name=entry.class_name,
                    )
                )  # fmt: skip
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
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
            raise PermissionDeniedError()
        if exam.finalized_at is not None:
            raise DomainError("exam_finalized")
        data = self.validate(db, exam.owner_id, data, exam=exam)
        if self.roster_count(db, exam) == 0:
            data.allow_unlisted = True  # without a class list nobody is "on the list"
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
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
            raise PermissionDeniedError()
        if exam.published_at is not None:
            return
        exam.published_at = self.clock.now()
        self.audit.append(db, "exam.published", actor=Actor.for_user(user), chain=exam.id, ip=ip)
        db.commit()

    def unpublish(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> None:
        """Back to concept; only while nobody has handed in."""
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
            raise PermissionDeniedError()
        if self.has_submissions(db, exam):
            raise DomainError("has_submissions")
        if exam.published_at is None:
            return
        exam.published_at = None
        self.audit.append(db, "exam.unpublished", actor=Actor.for_user(user), chain=exam.id, ip=ip)
        db.commit()

    def delete_concept(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> list[str]:
        """Delete a concept without submissions. Returns storage keys the caller removes:
        template files that may now be unreferenced, and the PDFs of its proces-verbalen.

        A proces-verbaal can exist when one was downloaded while the exam was published.
        Without submissions it records no hand-ins, so it goes with the exam; the audit
        event keeps its document ids.
        """
        if not may(db, user, ExamAction.DELETE_CONCEPT, exam):
            raise PermissionDeniedError()
        if exam.published_at is not None:
            raise DomainError("not_concept")
        if self.has_submissions(db, exam):
            raise DomainError("has_submissions")
        reports = list(db.scalars(select(ProcesVerbaal).where(ProcesVerbaal.exam_id == exam.id)))
        if any(pv.is_final for pv in reports):  # pragma: no cover - a concept is never final
            raise DomainError("not_concept")
        keys = [s.template_file.storage_key for s in exam.expected_files if s.template_file]
        keys += [pv.storage_key for pv in reports]
        details: dict[str, Any] = {"exam_id": str(exam.id), "name": exam.name, "slug": exam.slug}
        if reports:
            details["proces_verbaal"] = sorted(pv.document_id for pv in reports)
        self.audit.append(
            db, "exam.deleted", actor=Actor.for_user(user), chain=exam.id, ip=ip, details=details
        )
        self.audit.append(db, "exam.deleted", actor=Actor.for_user(user), ip=ip, details=details)
        db.add(RetiredSlug(slug=exam.slug, retired_at=self.clock.now()))
        db.query(ExamRosterEntry).filter_by(exam_id=exam.id).delete()
        db.query(ExtensionGrant).filter_by(exam_id=exam.id).delete()
        for pv in reports:
            db.delete(pv)
        db.flush()  # the proces-verbalen refer to the exam: remove them first
        db.delete(exam)
        db.commit()
        return keys

    def regenerate_code(self, db: Session, user: User, exam: Exam, *, ip: str | None) -> str:
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
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
        if not may(db, user, ExamAction.DUPLICATE, exam):
            raise PermissionDeniedError()
        data = self.input_from_exam(db, exam)
        # The copy keeps the slots of this exam (not the template's current ones), but stays
        # linked to the template, so its expected files stay read-only.
        data.source_template_id = None
        for slot in data.slots:
            slot.id = None
        data.name = f"{exam.name} (kopie)"[:200]
        data.slug = ""
        data.team_fixed = True  # PLAN B67: a copy stays in the team of the exam
        data.ip_preset_id = None
        now = self.clock.now()
        duration = exam.ends_at - exam.starts_at
        data.starts_at, _ = default_window(now, self.env.timezone)
        data.ends_at = data.starts_at + duration
        data.roster = [_roster_input(e) for e in self.roster(db, exam)]
        copy = self.create(db, user, data, ip=ip)
        copy.source_template_id = exam.source_template_id
        self._copy_template_files(db, copy, [s.template_file for s in exam.expected_files])
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
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
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
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
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
        if not may(db, user, ExamAction.DUPLICATE, exam):
            raise PermissionDeniedError()
        name = name.strip() or exam.name
        template = ExamTemplate(
            owner_id=user.id,
            name=name[:200],
            created_at=self.clock.now(),
            config=_template_config(self.input_from_exam(db, exam)),
        )
        now = self.clock.now()
        for slot in exam.expected_files:
            template_slot = ExamTemplateSlot(
                position=slot.position,
                label=slot.label,
                expected_name_pattern=slot.expected_name_pattern,
                extensions=slot.extensions,
                work_process_code=slot.work_process_code,
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

    def template_input(self, template: ExamTemplate) -> ExamInput:
        """The template as form input. Times, slug and class list do not apply."""
        now = self.clock.now()
        data = ExamInput(
            name=template.name, description=None, starts_at=now, ends_at=now + TEMPLATE_WINDOW
        )
        config = template.config
        data.access_code_enabled = bool(config.get("access_code_enabled", True))
        data.ip_restriction_enabled = bool(config.get("ip_restriction_enabled", False))
        data.ip_cidrs = list(config.get("ip_cidrs", []))
        preset = config.get("ip_preset_id")
        data.ip_preset_id = uuid.UUID(preset) if preset else None
        data.allow_unlisted = bool(config.get("allow_unlisted", True))
        data.allow_extra_file = bool(config.get("allow_extra_file", True))
        data.extensions = list(config.get("extensions", []))
        data.max_file_mb = int(config.get("max_file_mb", 25))
        data.max_submission_mb = int(config.get("max_submission_mb", 100))
        data.slots = [
            SlotInput(
                label=s.label, pattern=s.expected_name_pattern, extensions=s.extensions, id=s.id,
                work_process_code=s.work_process_code,
            )
            for s in template.slots
        ]  # fmt: skip
        return data

    def validate_template(self, db: Session, data: ExamInput) -> ExamInput:
        problems: dict[str, str] = {}
        data.name = data.name.strip()
        if not data.name:
            problems["name"] = "required"
        elif len(data.name) > 200:
            problems["name"] = "too_long"
        self._validate_access(db, data, problems)
        self._validate_limits(data, problems)
        self._validate_slots(db, data, problems)
        if problems:
            raise ExamValidationError(problems)
        return data

    def create_template(
        self, db: Session, user: User, data: ExamInput, *, ip: str | None
    ) -> ExamTemplate:
        data = self.validate_template(db, data)
        template = ExamTemplate(
            owner_id=user.id, name=data.name, config=_template_config(data),
            created_at=self.clock.now(),
        )  # fmt: skip
        for index, slot in enumerate(data.slots, start=1):
            template.slots.append(
                ExamTemplateSlot(
                    position=index, label=slot.label, expected_name_pattern=slot.pattern,
                    extensions=slot.extensions, work_process_code=slot.work_process_code,
                )
            )  # fmt: skip
        db.add(template)
        db.flush()
        self.audit.append(
            db, "exam_template.created", actor=Actor.for_user(user), ip=ip,
            details={"template_id": str(template.id), "template": _template_snapshot(template)},
        )  # fmt: skip
        db.commit()
        return template

    def update_template(
        self, db: Session, user: User, template: ExamTemplate, data: ExamInput, *, ip: str | None
    ) -> list[str]:
        """Save the editor; returns storage keys of template files of removed slots."""
        self._check_manage(db, user, template)
        data = self.validate_template(db, data)
        before = _template_snapshot(template)
        template.name = data.name
        template.config = _template_config(data)
        existing = {s.id: s for s in template.slots}
        kept: list[ExamTemplateSlot] = []
        for slot in data.slots:
            row = existing.pop(slot.id, None) if slot.id else None
            if row is None:
                row = ExamTemplateSlot(template_id=template.id)
            row.label = slot.label
            row.expected_name_pattern = slot.pattern
            row.extensions = slot.extensions
            row.work_process_code = slot.work_process_code
            kept.append(row)
        removed_keys = [s.template_file.storage_key for s in existing.values() if s.template_file]
        for removed in existing.values():
            template.slots.remove(removed)
        db.flush()
        # Positions are unique per template: renumber through a temporary offset.
        for index, row in enumerate(kept):
            row.position = 10_000 + index
        template.slots = kept
        db.flush()
        for index, row in enumerate(kept, start=1):
            row.position = index
        db.flush()
        changes = diff(before, _template_snapshot(template))
        if changes:
            self.audit.append(
                db, "exam_template.updated", actor=Actor.for_user(user), ip=ip,
                details={"template_id": str(template.id), "changes": changes},
            )  # fmt: skip
        db.commit()
        return removed_keys

    def attach_template_slot_file(
        self, db: Session, user: User, template: ExamTemplate, slot_id: uuid.UUID,
        stored: StoredTemplate, original_name: str, *, ip: str | None,
    ) -> str | None:  # fmt: skip
        """Returns the storage key of a replaced file that may now be unreferenced."""
        self._check_manage(db, user, template)
        slot = next((s for s in template.slots if s.id == slot_id), None)
        if slot is None:
            raise NotFoundError()
        replaced = slot.template_file.storage_key if slot.template_file else None
        if slot.template_file is not None:
            db.delete(slot.template_file)
            db.flush()
        slot.template_file = TemplateFile(
            original_name=original_name[:255], storage_key=stored.storage_key,
            size=stored.size, sha256=stored.sha256, uploaded_at=self.clock.now(),
        )  # fmt: skip
        self.audit.append(
            db, "exam_template.file_uploaded", actor=Actor.for_user(user), ip=ip,
            details={"template_id": str(template.id), "slot": slot.label, "sha256": stored.sha256},
        )  # fmt: skip
        db.commit()
        return replaced

    def remove_template_slot_file(
        self, db: Session, user: User, template: ExamTemplate, slot_id: uuid.UUID,
        *, ip: str | None,
    ) -> str | None:  # fmt: skip
        self._check_manage(db, user, template)
        slot = next((s for s in template.slots if s.id == slot_id), None)
        if slot is None or slot.template_file is None:
            return None
        key = slot.template_file.storage_key
        self.audit.append(
            db, "exam_template.file_removed", actor=Actor.for_user(user), ip=ip,
            details={"template_id": str(template.id), "slot": slot.label},
        )  # fmt: skip
        db.delete(slot.template_file)
        db.commit()
        return key

    def templates(self, db: Session, user: User) -> list[ExamTemplate]:
        """The user's own personal templates, the team templates of his teams (an admin:
        of every team) and the shared templates not yet assigned to a team, by name."""
        teams = team_context(db, user.id)
        own = (ExamTemplate.owner_id == user.id) & ExamTemplate.team_id.is_(None)
        # Admins see the templates of every team, as with the class lists.
        team = (
            ExamTemplate.team_id.is_not(None)
            if user.is_admin
            else ExamTemplate.team_id.in_(teams.member_of)
        )
        return list(
            db.scalars(
                select(ExamTemplate)
                .where(or_(ExamTemplate.shared.is_(True), own, team))
                .order_by(ExamTemplate.name)
            )
        )

    def template_teams(self, db: Session, user: User) -> list[Team]:
        """The teams the user may create templates for or copy templates into."""
        teams = team_context(db, user.id)
        return [
            team
            for team in sorted(db.scalars(select(Team)), key=lambda t: t.name.lower())
            if can_team(user, teams, TeamAction.MANAGE_TEMPLATES, team.id)
        ]

    def qualification_teams(
        self, db: Session, user: User, qualification: Qualification
    ) -> list[Team]:
        """The teams the user may make templates for from this qualification."""
        return [
            team
            for team in self.template_teams(db, user)
            if _qualification_in_team(db, team.id, qualification)
        ]

    def _check_template_team(self, db: Session, user: User, team_id: uuid.UUID) -> Team:
        team = db.get(Team, team_id)
        if team is None or not can_team(
            user, team_context(db, user.id), TeamAction.MANAGE_TEMPLATES, team.id
        ):
            raise PermissionDeniedError()
        return team

    def all_templates(self, db: Session) -> list[ExamTemplate]:
        """Every template, shared and personal; for the admin view (PLAN B61)."""
        return list(db.scalars(select(ExamTemplate).order_by(ExamTemplate.name)))

    def get_template(self, db: Session, user: User, template_id: uuid.UUID) -> ExamTemplate:
        """A template the user may see and use; 404 otherwise."""
        template = db.get(ExamTemplate, template_id)
        if template is None or not can_use_template(db, user, template):
            raise NotFoundError("template_not_found")
        return template

    def get_managed_template(self, db: Session, user: User, template_id: uuid.UUID) -> ExamTemplate:
        template = self.get_template(db, user, template_id)
        self._check_manage(db, user, template)
        return template

    @staticmethod
    def _check_manage(db: Session, user: User, template: ExamTemplate) -> None:
        if not can_use_template(db, user, template):
            raise NotFoundError("template_not_found")
        if not can_manage_template(db, user, template):
            raise PermissionDeniedError()

    def copy_template(
        self, db: Session, user: User, template: ExamTemplate, *, ip: str | None,
        team_id: uuid.UUID | None = None,
    ) -> ExamTemplate:  # fmt: skip
        """A personal copy that the user can change, or a copy in a team the user manages
        templates for (team contacts and admins, PLAN B66)."""
        if not can_template(
            user, team_context(db, user.id), TemplateAction.COPY, template_ref(template)
        ):
            raise NotFoundError("template_not_found")
        if team_id is not None:
            self._check_template_team(db, user, team_id)
        now = self.clock.now()
        copy = ExamTemplate(
            owner_id=user.id, team_id=team_id, name=f"{template.name} (kopie)"[:200],
            config=dict(template.config),
            created_at=now, qualification_id=template.qualification_id,
            core_task_code=template.core_task_code,
        )  # fmt: skip
        for slot in template.slots:
            row = ExamTemplateSlot(
                position=slot.position, label=slot.label,
                expected_name_pattern=slot.expected_name_pattern, extensions=slot.extensions,
                work_process_code=slot.work_process_code,
            )  # fmt: skip
            if slot.template_file is not None:
                row.template_file = _copy_file(slot.template_file, now)
            copy.slots.append(row)
        db.add(copy)
        db.flush()
        self.audit.append(
            db, "exam_template.copied", actor=Actor.for_user(user), ip=ip,
            details={
                "template_id": str(copy.id), "source_template_id": str(template.id),
                "source_owner_id": str(template.owner_id),
                "team_id": str(team_id) if team_id else None,
            },
        )  # fmt: skip
        db.commit()
        return copy

    def templates_from_qualification(
        self, db: Session, user: User, qualification: Qualification, core_task_codes: list[str],
        *, team_id: uuid.UUID | None, ip: str | None,
    ) -> list[ExamTemplate]:  # fmt: skip
        """One template per chosen core task, with a slot per work process: personal, or
        for a team whose crebos include this qualification (PLAN B66). The teacher uploads
        the template files afterwards (they are protected material)."""
        if team_id is not None:
            self._check_template_team(db, user, team_id)
            if not _qualification_in_team(db, team_id, qualification):
                raise DomainError("crebo_not_in_team")
        tasks = [t for t in qualification.core_tasks if t.code in set(core_task_codes)]
        if not tasks:
            raise DomainError("no_core_task_selected")
        now = self.clock.now()
        start, end = default_window(now, self.env.timezone)
        defaults = self.defaults(db, start, end)
        created: list[ExamTemplate] = []
        for task in tasks:
            name = f"{qualification.crebo} {qualification.name} {DASH} {task.code} {task.title}"
            template = ExamTemplate(
                owner_id=user.id, team_id=team_id, created_at=now,
                config=_template_config(defaults),
                name=name[:200],
                qualification_id=qualification.id, core_task_code=task.code,
            )  # fmt: skip
            for index, wp in enumerate(task.work_processes, start=1):
                template.slots.append(
                    ExamTemplateSlot(
                        position=index, label=_slot_label(wp.code, wp.title),
                        work_process_code=wp.code,
                    )
                )  # fmt: skip
            db.add(template)
            db.flush()
            created.append(template)
        self.audit.append(
            db, "exam_template.created_from_qualification", actor=Actor.for_user(user), ip=ip,
            details={
                "crebo": qualification.crebo, "dossier_version": qualification.dossier_version,
                "core_tasks": [t.code for t in tasks],
                "team_id": str(team_id) if team_id else None,
                "template_ids": [str(t.id) for t in created],
            },
        )  # fmt: skip
        db.commit()
        return created

    def delete_template(
        self, db: Session, user: User, template_id: uuid.UUID, *, ip: str | None
    ) -> list[str]:
        template = self.get_managed_template(db, user, template_id)
        keys = [s.template_file.storage_key for s in template.slots if s.template_file]
        # Also done by ON DELETE SET NULL; explicit, so it never depends on the database.
        db.execute(
            update(Exam)
            .where(Exam.source_template_id == template.id)
            .values(source_template_id=None)
        )
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
        if not may(db, user, ExamAction.EDIT_SETTINGS, exam):
            raise PermissionDeniedError()
        class_list = None
        if class_list_id is not None:
            class_list = db.get(ClassList, class_list_id)
            if class_list is None or not can_class_list(
                user, team_context(db, user.id), ClassListAction.USE, class_list_ref(class_list)
            ):
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

    # Resit ---------------------------------------------------------------------------

    def handed_in_slots(self, db: Session, exam: Exam) -> dict[str, set[uuid.UUID]]:
        """Per student number: the slots of the active submission that hold a file."""
        rows = db.execute(
            select(Submission.student_number, SubmissionFile.expected_file_id)
            .join(SubmissionFile, SubmissionFile.submission_id == Submission.id)
            .where(Submission.exam_id == exam.id, Submission.status == "active")
        )
        result: dict[str, set[uuid.UUID]] = {}
        for number, slot_id in rows:
            if slot_id is not None:
                result.setdefault(number, set()).add(slot_id)
        return result

    def resit_roster(self, db: Session, exam: Exam, numbers: list[str]) -> list[RosterInput]:
        """Roster entries for a resit: from the class list, or from the student's own
        submission when the student was not on the list. Unknown numbers are dropped."""
        roster = {e.student_number: e for e in self.roster(db, exam)}
        result: list[RosterInput] = []
        for number in dict.fromkeys(numbers):
            if number in roster:
                result.append(_roster_input(roster[number]))
                continue
            submission = db.scalar(
                select(Submission)
                .where(Submission.exam_id == exam.id, Submission.student_number == number)
                .order_by(Submission.attempt.desc())
                .limit(1)
            )
            if submission is not None:
                result.append(
                    RosterInput(
                        student_number=number, first_name=submission.first_name,
                        infix=submission.infix, last_name=submission.last_name,
                    )
                )  # fmt: skip
        return result

    def resit_input(
        self, db: Session, user: User, exam: Exam, numbers: list[str], *, name_suffix: str
    ) -> ExamInput:
        """A new exam for the selected students, with only the slots (work processes) that
        are still open for at least one of them. One configuration for the whole group."""
        if not may(db, user, ExamAction.DUPLICATE, exam):
            raise PermissionDeniedError()
        roster = self.resit_roster(db, exam, numbers)
        if not roster:
            raise DomainError("no_students_selected")
        handed_in = self.handed_in_slots(db, exam)
        chosen = [entry.student_number for entry in roster]
        open_slots = {
            slot.id for slot in exam.expected_files
            if any(slot.id not in handed_in.get(number, set()) for number in chosen)
        }  # fmt: skip
        data = self.input_from_exam(db, exam)
        data.source_template_id = None  # a resit keeps its own selection of expected files
        if open_slots:
            data.slots = [slot for slot in data.slots if slot.id in open_slots]
        data.name = f"{exam.name} {DASH} {name_suffix}"[:200]
        data.slug = ""
        duration = exam.ends_at - exam.starts_at
        data.starts_at, _ = default_window(self.clock.now(), self.env.timezone)
        data.ends_at = data.starts_at + duration
        data.allow_unlisted = False
        data.class_list_id = None
        data.source_exam_id = exam.id
        data.roster_numbers = chosen
        data.roster = roster
        return data

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
        if not may(db, user, ExamAction.MANAGE_SUBMISSIONS, exam):
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
        if not may(db, user, ExamAction.MANAGE_SUBMISSIONS, exam):
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

    def transfer_candidate_ids(self, db: Session, exam: Exam) -> set[uuid.UUID]:
        """The members of the exam's team: those a team contact may transfer it to."""
        if exam.team_id is None:
            return set()
        query = select(TeamMember.user_id).where(TeamMember.team_id == exam.team_id)
        return set(db.scalars(query))

    def set_team(
        self, db: Session, admin: User, exam: Exam, team_id: uuid.UUID | None, *, ip: str | None
    ) -> None:
        """Only an admin moves an exam to another team (PLAN B67), in its audit log."""
        if not admin.is_admin:
            raise PermissionDeniedError()
        if team_id is not None and db.get(Team, team_id) is None:
            raise DomainError("team_not_found")
        if team_id == exam.team_id:
            return
        details = {
            "old_team_id": str(exam.team_id) if exam.team_id else None,
            "new_team_id": str(team_id) if team_id else None,
        }
        exam.team_id = team_id
        self.audit.append(
            db, "exam.team_changed", actor=Actor.for_user(admin), chain=exam.id, ip=ip,
            details=details,
        )  # fmt: skip
        db.commit()

    def transfer_ownership(
        self, db: Session, admin: User, exam: Exam, new_owner_id: uuid.UUID, *, ip: str | None
    ) -> None:
        if not may(db, admin, ExamAction.TRANSFER_OWNERSHIP, exam):
            raise PermissionDeniedError()
        new_owner = db.get(User, new_owner_id)
        if new_owner is None or not new_owner.is_active:
            raise DomainError("owner_invalid")
        if not admin.is_admin and new_owner.id not in self.transfer_candidate_ids(db, exam):
            raise DomainError("owner_not_in_team")  # PLAN B68: a TCP stays within the team
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
