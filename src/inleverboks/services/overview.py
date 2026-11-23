"""Teacher overview of an exam: one row per student, counters and signals (spec §6.2)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from inleverboks.db.models import Exam, ExamRosterEntry, ExtensionGrant, Submission
from inleverboks.services.checks import EMPTY_FILE_SHA256, extension_of


class RowStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    INVALID = "invalid"
    MISSING = "missing"  # on the class list, not handed in


class Signal(StrEnum):
    EMPTY_TEMPLATE = "empty_template"
    TEMP_FILE = "temp_file"
    EMPTY_FILE = "empty_file"
    NAME_MISMATCH = "name_mismatch"
    DUPLICATE_FILE = "duplicate_file"
    IDENTICAL_TO_OTHER = "identical_to_other"
    NOT_ON_LIST = "not_on_list"
    EXTENSION = "extension"
    ZIP = "zip"
    COUNT_MISMATCH = "count_mismatch"


SIGNAL_ORDER = list(Signal)


@dataclass
class Row:
    student_number: str
    first_name: str
    infix: str | None
    last_name: str
    class_name: str | None
    status: RowStatus
    on_roster: bool
    submission: Submission | None = None
    attempts: int = 0
    signals: list[Signal] = field(default_factory=list)
    extension_until: datetime | None = None
    filled_slots: set[uuid.UUID] = field(default_factory=set)  # slots with a file (active)

    @property
    def sort_name(self) -> tuple[str, str, str]:
        return (self.last_name.lower(), self.first_name.lower(), self.student_number)

    @property
    def display_name(self) -> str:
        """``Vries, Jan de`` (last name first, as in the class list and the zip)."""
        name = f"{self.last_name}, {self.first_name}"
        return f"{name} {self.infix}" if self.infix else name

    @property
    def file_count(self) -> int:
        return len(self.submission.files) if self.submission else 0

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.submission.files) if self.submission else 0

    @property
    def submitted_at(self) -> datetime | None:
        return self.submission.started_at if self.submission else None


@dataclass
class Counters:
    active: int = 0
    missing: int | None = None  # only with a class list
    cancelled: int = 0
    invalid: int = 0
    not_on_list: int = 0
    roster: int = 0


@dataclass(frozen=True)
class WorkProcessColumn:
    slot_id: uuid.UUID
    code: str
    label: str


@dataclass
class Overview:
    rows: list[Row]
    counters: Counters
    expected_files: int
    has_roster: bool
    # One column per slot with an SBB work process code (empty for other exams).
    work_processes: list[WorkProcessColumn] = field(default_factory=list)


def load_submissions(db: Session, exam: Exam) -> list[Submission]:
    return list(
        db.scalars(
            select(Submission)
            .where(Submission.exam_id == exam.id)
            .options(selectinload(Submission.files))
            .order_by(Submission.student_number, Submission.attempt)
        )
    )


def other_student_hashes(exam: Exam, submissions: list[Submission]) -> dict[str, set[str]]:
    """sha256 → student numbers with an active submission containing it. Template hashes
    and empty files do not count (spec §6.2)."""
    templates = {s.template_file.sha256 for s in exam.expected_files if s.template_file}
    owners: dict[str, set[str]] = defaultdict(set)
    for submission in submissions:
        if submission.status != "active":
            continue
        for file in submission.files:
            if file.sha256 == EMPTY_FILE_SHA256 or file.sha256 in templates:
                continue
            owners[file.sha256].add(submission.student_number)
    return owners


def submission_signals(
    exam: Exam, submission: Submission, owners: dict[str, set[str]], *, has_roster: bool
) -> list[Signal]:
    found: set[Signal] = set()
    for file in submission.files:
        for code in file.warnings or []:
            try:
                found.add(Signal(code))
            except ValueError:
                continue
        if len(owners.get(file.sha256, set()) - {submission.student_number}) > 0:
            found.add(Signal.IDENTICAL_TO_OTHER)
        if extension_of(file.original_name) == "zip":
            found.add(Signal.ZIP)
    for warning in submission.confirmed_warnings or []:
        if warning.get("code") in {"count_mismatch", "empty_slot"}:
            found.add(Signal.COUNT_MISMATCH)
    if has_roster and not submission.on_roster:
        found.add(Signal.NOT_ON_LIST)
    return [s for s in SIGNAL_ORDER if s in found]


def build_overview(db: Session, exam: Exam) -> Overview:
    submissions = load_submissions(db, exam)
    roster = list(db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id)))
    grants = {
        g.student_number: g.ends_at
        for g in db.scalars(select(ExtensionGrant).where(ExtensionGrant.exam_id == exam.id))
    }
    has_roster = bool(roster)
    owners = other_student_hashes(exam, submissions)

    by_student: dict[str, list[Submission]] = defaultdict(list)
    for submission in submissions:
        by_student[submission.student_number].append(submission)

    rows: list[Row] = []
    counters = Counters(roster=len(roster))
    roster_numbers = {r.student_number for r in roster}
    for number, attempts in by_student.items():
        latest = max(attempts, key=lambda s: s.attempt)
        status = RowStatus(latest.status)
        row = Row(
            student_number=number,
            first_name=latest.first_name,
            infix=latest.infix,
            last_name=latest.last_name,
            class_name=None,
            status=status,
            on_roster=number in roster_numbers,
            submission=latest,
            attempts=len(attempts),
            extension_until=grants.get(number),
        )
        row.signals = submission_signals(exam, latest, owners, has_roster=has_roster)
        if status is RowStatus.ACTIVE:
            row.filled_slots = {f.expected_file_id for f in latest.files if f.expected_file_id}
        if number in grants:
            row.signals.append(Signal.EXTENSION)
        rows.append(row)
        counters.cancelled += sum(1 for s in attempts if s.status == "cancelled")
        counters.invalid += sum(1 for s in attempts if s.status == "invalid")
        if status is RowStatus.ACTIVE:
            counters.active += 1
            if has_roster and number not in roster_numbers:
                counters.not_on_list += 1

    entries = {r.student_number: r for r in roster}
    for row in rows:
        entry = entries.get(row.student_number)
        if entry is not None:
            row.class_name = entry.class_name
    active_numbers = {r.student_number for r in rows if r.status is RowStatus.ACTIVE}
    if has_roster:
        counters.missing = 0
        submitted = set(by_student)
        for entry in roster:
            if entry.student_number in active_numbers:
                continue
            counters.missing += 1
            if entry.student_number in submitted:
                continue  # shown with their cancelled/invalid status
            rows.append(
                Row(
                    student_number=entry.student_number,
                    first_name=entry.first_name,
                    infix=entry.infix,
                    last_name=entry.last_name,
                    class_name=entry.class_name,
                    status=RowStatus.MISSING,
                    on_roster=True,
                    extension_until=grants.get(entry.student_number),
                    signals=[Signal.EXTENSION] if entry.student_number in grants else [],
                )
            )
    rows.sort(key=lambda r: r.sort_name)
    return Overview(
        rows=rows,
        counters=counters,
        expected_files=len(exam.expected_files),
        has_roster=has_roster,
        work_processes=[
            WorkProcessColumn(slot_id=s.id, code=s.work_process_code, label=s.label)
            for s in exam.expected_files
            if s.work_process_code
        ],
    )


SORT_KEYS = {
    "name": lambda r: r.sort_name,
    "number": lambda r: r.student_number,
    "status": lambda r: (r.status.value, r.sort_name),
    "time": lambda r: (
        r.submitted_at is None,
        r.submitted_at or datetime.min.replace(tzinfo=UTC),
        r.sort_name,
    ),
    "size": lambda r: (r.total_size, r.sort_name),
}


def filter_and_sort(
    rows: list[Row], status: str | None, sort: str | None, descending: bool
) -> list[Row]:
    selected = rows
    if status == "not_on_list":
        selected = [r for r in rows if Signal.NOT_ON_LIST in r.signals]
    elif status in {s.value for s in RowStatus}:
        selected = [r for r in rows if r.status.value == status]
    key = SORT_KEYS.get(sort or "name", SORT_KEYS["name"])
    return sorted(selected, key=key, reverse=descending)
