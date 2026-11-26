"""Class list import (CSV) and the class lists of the education teams (PLAN B65)."""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import ClassList, ClassListEntry, Team, User
from inleverboks.policy import (
    ClassListAction,
    ClassListRef,
    TeamAction,
    TeamContext,
    can_class_list,
    can_team,
)
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.services.teams import team_context

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
FIELDS = ("student_number", "first_name", "infix", "last_name", "class_name")
REQUIRED_FIELDS = ("student_number", "first_name", "last_name")

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "student_number": (
        "studentnummer",
        "student nummer",
        "studentnr",
        "stud nr",
        "stud.nr",
        "leerlingnummer",
        "leerlingnr",
        "nummer",
        "student number",
        "student id",
        "id",
    ),
    "first_name": ("voornaam", "roepnaam", "first name", "firstname", "voornamen"),
    "infix": ("tussenvoegsel", "tussenvoegsels", "voorvoegsel", "tussenv", "prefix", "infix"),
    "last_name": ("achternaam", "familienaam", "last name", "lastname", "naam", "surname"),
    "class_name": ("klas", "groep", "class", "klascode", "lesgroep"),
}


def decode_csv(data: bytes) -> str:
    """UTF-8 (with or without BOM), otherwise Windows-1252."""
    if len(data) > MAX_CSV_BYTES:
        raise DomainError("csv_too_large")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="strict")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def detect_delimiter(text: str) -> str:
    first = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: first.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=lambda d: (counts[d], d == ";"))
    return best if counts[best] else ";"


def read_rows(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader]
    rows = [row for row in rows if any(row)]
    if len(rows) > MAX_ROWS + 1:
        raise DomainError("csv_too_many_rows")
    return rows


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_.\-]+", " ", value.strip().lower()).strip()


def guess_mapping(header: list[str]) -> dict[str, int | None]:
    normalized = [_normalize_header(h) for h in header]
    mapping: dict[str, int | None] = dict.fromkeys(FIELDS)
    used: set[int] = set()
    for field_name in FIELDS:
        for synonym in _SYNONYMS[field_name]:
            target = _normalize_header(synonym)
            for index, value in enumerate(normalized):
                if index not in used and value == target:
                    mapping[field_name] = index
                    used.add(index)
                    break
            if mapping[field_name] is not None:
                break
    return mapping


@dataclass
class ParsedEntry:
    row_number: int
    student_number: str
    first_name: str
    infix: str | None
    last_name: str
    class_name: str | None


@dataclass
class ImportResult:
    entries: list[ParsedEntry] = field(default_factory=list)
    errors: list[tuple[int, str, str]] = field(default_factory=list)  # (row, code, value)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.entries)


def apply_mapping(
    rows: list[list[str]],
    mapping: dict[str, int | None],
    *,
    has_header: bool,
    student_number_regex: str,
) -> ImportResult:
    for required in REQUIRED_FIELDS:
        if mapping.get(required) is None:
            raise DomainError("mapping_incomplete")
    pattern = re.compile(student_number_regex)
    result = ImportResult()
    seen: dict[str, int] = {}
    start = 1 if has_header else 0
    for offset, row in enumerate(rows[start:]):
        row_number = offset + start + 1

        def cell(name: str, row: list[str] = row) -> str:
            index = mapping.get(name)
            return row[index].strip() if index is not None and index < len(row) else ""

        number = cell("student_number")
        first, last = cell("first_name"), cell("last_name")
        if not number:
            result.errors.append((row_number, "missing_number", ""))
            continue
        if not pattern.fullmatch(number):
            result.errors.append((row_number, "invalid_number", number))
            continue
        if not first or not last:
            result.errors.append((row_number, "missing_name", number))
            continue
        if number in seen:
            result.errors.append((row_number, "duplicate_number", number))
            continue
        seen[number] = row_number
        result.entries.append(
            ParsedEntry(
                row_number=row_number,
                student_number=number,
                first_name=first[:100],
                infix=cell("infix")[:50] or None,
                last_name=last[:150],
                class_name=cell("class_name")[:50] or None,
            )
        )
    return result


def _entries(result: ImportResult) -> list[ClassListEntry]:
    return [
        ClassListEntry(
            student_number=e.student_number,
            first_name=e.first_name,
            infix=e.infix,
            last_name=e.last_name,
            class_name=e.class_name,
        )
        for e in result.entries
    ]


def _valid_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 200:
        raise DomainError("class_list_name_invalid")
    return name


def ref(class_list: ClassList) -> ClassListRef:
    return ClassListRef(owner_id=class_list.owner_id, team_id=class_list.team_id)


@dataclass(frozen=True)
class LibrarySection:
    """The lists of one team, or (``team`` None) the lists without a team (PLAN B69)."""

    team: Team | None
    class_lists: list[ClassList]
    may_manage: bool  # create lists for this team


class ClassListService:
    """Class lists belong to an education team: its members see and use them, its team
    contacts and admins change them (PLAN B65, ``policy.CLASS_LIST_MATRIX``). Lists without
    a team keep the shared rules of B60 until an admin assigns them (B69)."""

    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self.clock = clock
        self.audit = audit

    def library(self, db: Session, user: User) -> list[LibrarySection]:
        """Per team the user is in (an admin: every team), then the unassigned lists."""
        teams = team_context(db, user.id)
        team_query = select(Team)
        if not user.is_admin:
            team_query = team_query.where(Team.id.in_(teams.member_of))
        team_rows = sorted(db.scalars(team_query), key=lambda t: t.name.lower())
        lists = self._visible(db, user, teams)
        sections = [
            LibrarySection(
                team=team,
                class_lists=[cl for cl in lists if cl.team_id == team.id],
                may_manage=can_team(user, teams, TeamAction.MANAGE_CLASS_LISTS, team.id),
            )
            for team in team_rows
        ]
        unassigned = [cl for cl in lists if cl.team_id is None]
        if unassigned:
            sections.append(LibrarySection(None, unassigned, False))
        return sections

    def usable(self, db: Session, user: User) -> list[ClassList]:
        """The lists the user may put into an exam."""
        teams = team_context(db, user.id)
        return [
            cl
            for cl in self._visible(db, user, teams)
            if can_class_list(user, teams, ClassListAction.USE, ref(cl))
        ]

    def creatable_teams(self, db: Session, user: User) -> list[Team]:
        """The teams the user may create class lists for (team contact or admin)."""
        teams = team_context(db, user.id)
        return [
            team
            for team in sorted(db.scalars(select(Team)), key=lambda t: t.name.lower())
            if can_team(user, teams, TeamAction.MANAGE_CLASS_LISTS, team.id)
        ]

    def _visible(self, db: Session, user: User, teams: TeamContext) -> list[ClassList]:
        query = select(ClassList).order_by(ClassList.name, ClassList.created_at)
        return [
            cl
            for cl in db.scalars(query)
            if can_class_list(user, teams, ClassListAction.VIEW, ref(cl))
        ]

    def permissions(self, db: Session, user: User, class_list: ClassList) -> dict[str, bool]:
        teams = team_context(db, user.id)
        return {a.value: can_class_list(user, teams, a, ref(class_list)) for a in ClassListAction}

    def get(
        self, db: Session, user: User, class_list_id: uuid.UUID, action: ClassListAction
    ) -> ClassList:
        class_list = db.get(ClassList, class_list_id)
        teams = team_context(db, user.id)
        if class_list is None or not can_class_list(
            user, teams, ClassListAction.VIEW, ref(class_list)
        ):
            raise NotFoundError("class_list_not_found")
        if not can_class_list(user, teams, action, ref(class_list)):
            raise PermissionDeniedError()
        return class_list

    def _details(self, class_list: ClassList, **extra: object) -> dict[str, object]:
        return {
            "class_list_id": str(class_list.id),
            "owner_id": str(class_list.owner_id),
            "team_id": str(class_list.team_id) if class_list.team_id else None,
            "name": class_list.name,
            **extra,
        }

    def save(
        self, db: Session, owner: User, team_id: uuid.UUID | None, name: str,
        result: ImportResult, *, ip: str | None,
    ) -> ClassList:  # fmt: skip
        """A new list for a team; only its team contacts and admins create one (B65)."""
        if team_id is None or db.get(Team, team_id) is None:
            raise DomainError("class_list_team_required")
        if not can_team(owner, team_context(db, owner.id), TeamAction.MANAGE_CLASS_LISTS, team_id):
            raise PermissionDeniedError()
        name = _valid_name(name)
        if not result.ok:
            raise DomainError("class_list_has_errors")
        class_list = ClassList(
            owner_id=owner.id, team_id=team_id, name=name, created_at=self.clock.now()
        )
        class_list.entries = _entries(result)
        db.add(class_list)
        db.flush()
        self.audit.append(
            db,
            "class_list.imported",
            actor=Actor.for_user(owner),
            ip=ip,
            details={
                "class_list_id": str(class_list.id),
                "team_id": str(team_id),
                "name": name,
                "rows": len(result.entries),
            },
        )
        db.commit()
        return class_list

    def rename(
        self, db: Session, user: User, class_list_id: uuid.UUID, name: str, *, ip: str | None
    ) -> ClassList:
        class_list = self.get(db, user, class_list_id, ClassListAction.RENAME)
        name = _valid_name(name)
        if name == class_list.name:
            return class_list
        self.audit.append(
            db,
            "class_list.renamed",
            actor=Actor.for_user(user),
            ip=ip,
            details=self._details(class_list, new_name=name),
        )
        class_list.name = name
        db.commit()
        return class_list

    def replace(
        self,
        db: Session,
        user: User,
        class_list_id: uuid.UUID,
        name: str,
        result: ImportResult,
        *,
        ip: str | None,
    ) -> ClassList:
        """New entries from a fresh import. Exams keep their own snapshot."""
        class_list = self.get(db, user, class_list_id, ClassListAction.REPLACE)
        name = _valid_name(name)
        if not result.ok:
            raise DomainError("class_list_has_errors")
        extra: dict[str, object] = {
            "old_rows": len(class_list.entries),
            "rows": len(result.entries),
        }
        if name != class_list.name:
            extra["new_name"] = name
        self.audit.append(
            db,
            "class_list.replaced",
            actor=Actor.for_user(user),
            ip=ip,
            details=self._details(class_list, **extra),
        )
        class_list.name = name
        class_list.entries.clear()
        db.flush()  # the unique (list, student number) needs the old rows gone first
        class_list.entries.extend(_entries(result))
        db.commit()
        return class_list

    def delete(self, db: Session, user: User, class_list_id: uuid.UUID, *, ip: str | None) -> None:
        class_list = self.get(db, user, class_list_id, ClassListAction.DELETE)
        self.audit.append(
            db,
            "class_list.deleted",
            actor=Actor.for_user(user),
            ip=ip,
            details=self._details(class_list),
        )
        db.delete(class_list)
        db.commit()
