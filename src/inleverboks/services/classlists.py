"""Class list import (CSV) and the teacher's personal library of class lists."""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import ClassList, ClassListEntry, User
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.errors import DomainError, NotFoundError

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


class ClassListService:
    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self.clock = clock
        self.audit = audit

    def library(self, db: Session, owner: User) -> list[ClassList]:
        return list(
            db.scalars(
                select(ClassList).where(ClassList.owner_id == owner.id).order_by(ClassList.name)
            )
        )

    def get_owned(self, db: Session, owner: User, class_list_id: uuid.UUID) -> ClassList:
        class_list = db.get(ClassList, class_list_id)
        if class_list is None or class_list.owner_id != owner.id:
            raise NotFoundError("class_list_not_found")
        return class_list

    def save(
        self, db: Session, owner: User, name: str, result: ImportResult, *, ip: str | None
    ) -> ClassList:
        name = name.strip()
        if not name or len(name) > 200:
            raise DomainError("class_list_name_invalid")
        if not result.ok:
            raise DomainError("class_list_has_errors")
        class_list = ClassList(owner_id=owner.id, name=name, created_at=self.clock.now())
        class_list.entries = [
            ClassListEntry(
                student_number=e.student_number,
                first_name=e.first_name,
                infix=e.infix,
                last_name=e.last_name,
                class_name=e.class_name,
            )
            for e in result.entries
        ]
        db.add(class_list)
        db.flush()
        self.audit.append(
            db,
            "class_list.imported",
            actor=Actor.for_user(owner),
            ip=ip,
            details={
                "class_list_id": str(class_list.id),
                "name": name,
                "rows": len(result.entries),
            },
        )
        db.commit()
        return class_list

    def delete(self, db: Session, owner: User, class_list_id: uuid.UUID, *, ip: str | None) -> None:
        class_list = self.get_owned(db, owner, class_list_id)
        self.audit.append(
            db,
            "class_list.deleted",
            actor=Actor.for_user(owner),
            ip=ip,
            details={"class_list_id": str(class_list.id), "name": class_list.name},
        )
        db.delete(class_list)
        db.commit()
