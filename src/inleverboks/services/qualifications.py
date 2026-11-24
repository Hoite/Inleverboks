"""The SBB qualification structure: import, search and dossier-version checks.

The import takes plain records (``QualificationRecord``), so the SBB XML parser only has
to produce these. Search is by the crebo or name of the qualification ("uitstroom"),
not the dossier: teams work per outflow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import CoreTask, Qualification, WorkProcess
from inleverboks.services.audit import Actor, AuditService

CODE_RE = re.compile(r"^[A-Z]\d{1,2}(-[A-Z]\d{1,2}){0,2}$")  # B1, B1-K1, B1-K1-W1
SEARCH_LIMIT = 25


@dataclass(frozen=True)
class WorkProcessRecord:
    code: str
    title: str


@dataclass(frozen=True)
class CoreTaskRecord:
    code: str
    title: str
    work_processes: list[WorkProcessRecord] = field(default_factory=list)


@dataclass(frozen=True)
class QualificationRecord:
    crebo: str
    name: str
    dossier_version: str
    level: int | None = None
    dossier_crebo: str | None = None
    dossier_name: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    revision: int = 0
    previous_crebo: str | None = None
    core_tasks: list[CoreTaskRecord] = field(default_factory=list)


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0


@dataclass(frozen=True)
class TitleChange:
    code: str
    old: str | None  # None: new work process
    new: str | None  # None: removed work process


@dataclass(frozen=True)
class DossierStatus:
    """How a qualification version compares to what SBB currently has."""

    replaced_by: Qualification | None
    expired: bool

    @property
    def outdated(self) -> bool:
        return self.replaced_by is not None or self.expired


def label(code: str, title: str) -> str:
    """``B1-K1-W1 Title``: the format of slot labels and the selection lists."""
    return f"{code} {title}".strip()


class QualificationService:
    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self.clock = clock
        self.audit = audit

    # Import --------------------------------------------------------------------------

    def import_records(
        self, db: Session, records: list[QualificationRecord], *, actor: Actor, ip: str | None,
        source: str,
    ) -> ImportResult:  # fmt: skip
        """Add or refresh qualification versions. A version is (crebo, dossier version)."""
        result = ImportResult()
        now = self.clock.now()
        for record in records:
            row = db.scalar(
                select(Qualification).where(
                    Qualification.crebo == record.crebo,
                    Qualification.dossier_version == record.dossier_version,
                )
            )
            if row is None:
                row = Qualification(crebo=record.crebo, dossier_version=record.dossier_version)
                db.add(row)
                result.created += 1
            else:
                result.updated += 1
            row.name = record.name
            row.level = record.level
            row.dossier_crebo = record.dossier_crebo
            row.dossier_name = record.dossier_name
            row.valid_from = record.valid_from
            row.valid_until = record.valid_until
            row.revision = record.revision
            row.previous_crebo = record.previous_crebo
            row.imported_at = now
            # Remove the old tasks first: the new rows reuse their (qualification, code).
            row.core_tasks.clear()
            db.flush()
            row.core_tasks = [
                CoreTask(
                    code=task.code, title=task.title, position=index,
                    work_processes=[
                        WorkProcess(code=wp.code, title=wp.title, position=wp_index)
                        for wp_index, wp in enumerate(task.work_processes, start=1)
                    ],
                )
                for index, task in enumerate(record.core_tasks, start=1)
            ]  # fmt: skip
            db.flush()
        self.audit.append(
            db, "qualifications.imported", actor=actor, ip=ip,
            details={"source": source, "created": result.created, "updated": result.updated},
        )  # fmt: skip
        db.commit()
        return result

    # Reading -------------------------------------------------------------------------

    def count(self, db: Session) -> int:
        return db.scalar(select(func.count(func.distinct(Qualification.crebo)))) or 0

    def last_imported_at(self, db: Session) -> datetime | None:
        return db.scalar(select(func.max(Qualification.imported_at)))

    def get(self, db: Session, qualification_id: object) -> Qualification | None:
        return db.get(Qualification, qualification_id)

    def versions(self, db: Session, crebo: str) -> list[Qualification]:
        """All versions of one crebo, newest first."""
        rows = db.scalars(select(Qualification).where(Qualification.crebo == crebo))
        return sorted(rows, key=_version_key, reverse=True)

    def current(self, db: Session, crebo: str) -> Qualification | None:
        versions = self.versions(db, crebo)
        return versions[0] if versions else None

    def search(self, db: Session, query: str) -> list[Qualification]:
        """The current version of each qualification whose crebo or name matches."""
        query = query.strip()
        if len(query) < 2:
            return []
        pattern = f"%{query.lower()}%"
        rows = db.scalars(
            select(Qualification).where(
                or_(
                    Qualification.crebo.startswith(query),
                    func.lower(Qualification.name).like(pattern),
                )
            )
        )
        newest: dict[str, Qualification] = {}
        for row in rows:
            if row.crebo not in newest or _version_key(row) > _version_key(newest[row.crebo]):
                newest[row.crebo] = row
        ordered = sorted(newest.values(), key=lambda q: (q.crebo != query, q.name.lower()))
        return ordered[:SEARCH_LIMIT]

    # Dossier versions ------------------------------------------------------------------

    def status(self, db: Session, qualification: Qualification) -> DossierStatus:
        today = self.clock.now().date()
        current = self.current(db, qualification.crebo)
        newest = self._successor(db, current or qualification)
        replaced_by = newest if newest.id != qualification.id else None
        expired = qualification.valid_until is not None and qualification.valid_until < today
        return DossierStatus(replaced_by=replaced_by, expired=expired)

    def _successor(self, db: Session, qualification: Qualification) -> Qualification:
        """Follow revisions that gave the outflow a new crebo, to the newest version."""
        seen = {qualification.crebo}
        while True:
            crebos = db.scalars(
                select(Qualification.crebo)
                .where(Qualification.previous_crebo == qualification.crebo)
                .distinct()
            ).all()
            candidates = (self.current(db, crebo) for crebo in crebos if crebo not in seen)
            nexts = [c for c in candidates if c is not None]
            if not nexts:
                return qualification
            qualification = max(nexts, key=lambda q: (q.valid_from or date.min, q.crebo))
            seen.add(qualification.crebo)

    def changes(
        self, old: Qualification, new: Qualification, core_task_code: str
    ) -> list[TitleChange]:
        """Differences in the work processes of one core task between two versions."""

        def processes(q: Qualification) -> dict[str, str]:
            task = next((t for t in q.core_tasks if t.code == core_task_code), None)
            return {wp.code: wp.title for wp in task.work_processes} if task else {}

        before, after = processes(old), processes(new)
        changes = [
            TitleChange(code, before.get(code), after.get(code))
            for code in sorted(set(before) | set(after))
            if before.get(code) != after.get(code)
        ]
        task_before = next((t.title for t in old.core_tasks if t.code == core_task_code), None)
        task_after = next((t.title for t in new.core_tasks if t.code == core_task_code), None)
        if task_before != task_after:
            changes.insert(0, TitleChange(core_task_code, task_before, task_after))
        return changes


def _version_key(q: Qualification) -> tuple[int, date, str]:
    return (q.revision, q.valid_from or date.min, q.dossier_version)
