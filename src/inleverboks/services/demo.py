"""Demo data (``inleverboks demo-seed``, only with DEMO_MODE=true).

Everything goes through the normal services, so the demo looks exactly like real use:
exams are created and published by the demo teacher, and the hand-ins are prepared,
stored and committed like an upload from the browser. A frozen clock puts the closed
exam in the past. All names and student numbers are fictitious.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.adapters.storage import StorageBackend
from inleverboks.config import Settings
from inleverboks.db.models import (
    ClassList,
    ClassListEntry,
    Exam,
    ExamRosterEntry,
    Team,
    TeamCrebo,
    TeamMember,
    User,
)
from inleverboks.services.asyncutil import call_async
from inleverboks.services.audit import Actor
from inleverboks.services.container import Services, build_services
from inleverboks.services.exams import ExamInput, SlotInput
from inleverboks.services.passwords import hash_password
from inleverboks.services.qualifications import (
    CoreTaskRecord,
    QualificationRecord,
    WorkProcessRecord,
)
from inleverboks.services.student_flow import StudentState
from inleverboks.services.submissions import ReceivedFile
from inleverboks.services.whitelist import WhitelistError

DEMO_DOMAIN = "demo.test"
DEMO_TEACHER = f"docent@{DEMO_DOMAIN}"

# (student number, first name, infix, last name) - fictitious.
STUDENTS = [
    ("200101", "Sanne", "", "Bakker"),
    ("200102", "Daan", "van", "Dijk"),
    ("200103", "Fatima", "", "El Amrani"),
    ("200104", "Lars", "de", "Jong"),
    ("200105", "Mila", "", "Visser"),
    ("200106", "Noah", "", "Smit"),
    ("200107", "Zoë", "van der", "Berg"),
]


DEMO_QUALIFICATION = QualificationRecord(
    crebo="99999", name="Demo-opleiding ICT (fictief)", level=4, dossier_crebo="99998",
    dossier_name="Demodossier ICT (fictief)", dossier_version="demo", valid_from=date(2025, 8, 1),
    core_tasks=[
        CoreTaskRecord("B1-K1", "Richt een werkplek in", [
            WorkProcessRecord("B1-K1-W1", "Inventariseert de wensen"),
            WorkProcessRecord("B1-K1-W2", "Installeert de werkplek"),
            WorkProcessRecord("B1-K1-W3", "Test en levert op"),
        ]),
        CoreTaskRecord("P1-K1", "Beheert een netwerk", [
            WorkProcessRecord("P1-K1-W1", "Monitort het netwerk"),
            WorkProcessRecord("P1-K1-W2", "Lost storingen op"),
        ]),
    ],
)  # fmt: skip


@dataclass
class DemoResult:
    created: bool
    email: str = DEMO_TEACHER
    password: str | None = None
    exams: list[tuple[str, str, str | None]] = field(default_factory=list)  # name, slug, code


def _round_quarter(value: datetime) -> datetime:
    value = value.replace(second=0, microsecond=0)
    return value - timedelta(minutes=value.minute % 15)


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


class _Seeder:
    def __init__(self, env: Settings, storage: StorageBackend, db: Session, now: datetime) -> None:
        self.env = env
        self.storage = storage
        self.db = db
        self.clock = FrozenClock(now)
        self.services: Services = build_services(env, storage, clock=self.clock)
        self.tmp = Path(tempfile.mkdtemp(prefix="inleverboks-demo-"))

    def teacher(self) -> tuple[User, str]:
        password = secrets.token_urlsafe(12)
        user = User(
            email=DEMO_TEACHER, name="Demo Docent", created_at=self.clock.now(),
            password_hash=hash_password(password), is_active=True,
        )  # fmt: skip
        self.db.add(user)
        self.db.flush()
        with contextlib.suppress(WhitelistError):  # already on the list
            self.services.whitelist.add(self.db, DEMO_DOMAIN, by=user.id)
        self.db.commit()
        return user, password

    def team(self, member: User) -> Team:
        """An education team (PLAN B63) with the demo teacher as member (not as team
        contact: that needs two-step verification), the demo crebo and a class list. The
        exams below get the team automatically."""
        now = self.clock.now()
        team = Team(name="Demo-team ICT", description="Voorbeeldteam", created_at=now)
        team.crebos = [TeamCrebo(crebo=DEMO_QUALIFICATION.crebo)]
        team.members = [TeamMember(user_id=member.id, added_at=now)]
        class_list = ClassList(owner_id=member.id, name="Demo ICT-1A", created_at=now)
        class_list.entries = [
            ClassListEntry(student_number=number, first_name=first, infix=infix or None,
                           last_name=last, class_name="ICT-1A")
            for number, first, infix, last in STUDENTS
        ]  # fmt: skip
        self.db.add(team)
        self.db.flush()
        class_list.team_id = team.id
        self.db.add(class_list)
        self.db.commit()
        return team

    def exam(
        self, owner: User, data: ExamInput, *, publish: bool = True, roster: bool = True
    ) -> Exam:
        exam = self.services.exams.create(self.db, owner, data, ip=None)
        if roster:
            self.db.add_all(
                ExamRosterEntry(
                    exam_id=exam.id, student_number=number, first_name=first, infix=infix or None,
                    last_name=last,
                )
                for number, first, infix, last in STUDENTS
            )  # fmt: skip
            self.db.commit()
        if publish:
            self.services.exams.publish(self.db, owner, exam, ip=None)
        return exam

    def hand_in(
        self, exam: Exam, student: tuple[str, str, str, str],
        files: list[tuple[str, bytes, int | None]], *, at: datetime,
    ) -> uuid.UUID:  # fmt: skip
        """``files``: (name, content, slot position or None for an extra file)."""
        self.clock.set(at)
        number, first, infix, last = student
        state = StudentState(
            exam_id=str(exam.id), stage="upload", student_number=number, first_name=first,
            infix=infix, last_name=last, on_roster=True,
        )  # fmt: skip
        slots = sorted(exam.expected_files, key=lambda s: s.position)
        received, manifest = [], []
        for name, content, slot in files:
            path = self.tmp / uuid.uuid4().hex
            path.write_bytes(content)
            sha = hashlib.sha256(content).hexdigest()
            received.append(ReceivedFile(filename=name, size=len(content), sha256=sha, path=path))
            slot_id = str(slots[slot].id) if slot is not None else None
            manifest.append({"slot_id": slot_id, "size": len(content), "sha256": sha})
        submissions = self.services.submissions
        plan = submissions.prepare(
            self.db, exam, state, manifest_json=json.dumps(manifest), received=received,
            idempotency_key=uuid.uuid4().hex, declaration=True, warnings_confirmed=True,
            started_at=at, ip="192.0.2.10",
        )  # fmt: skip
        for planned in plan.files:
            call_async(self.storage.put, planned.storage_key, _chunks(planned.path.read_bytes()))
        receipt = submissions.commit(self.db, exam, plan)
        return receipt.submission.id


def _script(name: str, extra: str = "") -> bytes:
    return f"#!/bin/sh\n# {name}\nset -eu\nip -br addr\n{extra}".encode()


def _report(student: str, body: str) -> bytes:
    return f"# Verslag van {student}\n\n{body}\n".encode()


def seed(
    env: Settings, storage: StorageBackend, make_session: Callable[[], Session], now: datetime
) -> DemoResult:
    tz = env.timezone
    with make_session() as db:
        if db.query(User).filter(User.email == DEMO_TEACHER).first() is not None:
            return DemoResult(created=False)
        local_now = now.astimezone(tz)
        today = local_now.date()
        seeder = _Seeder(env, storage, db, now - timedelta(days=1, hours=2))
        owner, password = seeder.teacher()
        seeder.team(owner)
        result = DemoResult(created=True, password=password)
        s = STUDENTS

        # A closed exam from yesterday, with a cancelled and a repeated attempt.
        yesterday = datetime.combine(today - timedelta(days=1), time(9, 0), tzinfo=tz)
        seeder.clock.set(yesterday - timedelta(hours=1))
        closed = seeder.exam(owner, ExamInput(
            name="Demo: Programmeren P1", description=None, starts_at=yesterday,
            ends_at=yesterday + timedelta(hours=2), slug="demo-programmeren-p1",
            extensions=["py", "md", "txt"], max_file_mb=5, max_submission_mb=20,
            slots=[SlotInput(label="Programma", pattern="opdracht_*.py", extensions=["py"]),
                   SlotInput(label="Uitleg", pattern="README.md", extensions=["md"])],
        ))  # fmt: skip
        program = b"def main() -> None:\n    print('Hallo, wereld')\n\n\nmain()\n"
        start = yesterday + timedelta(minutes=70)
        seeder.hand_in(
            closed,
            s[0],
            [
                ("opdracht_1.py", program + b"# Sanne\n", 0),
                ("README.md", _report("Sanne", "Uitleg."), 1),
            ],
            at=start,
        )
        seeder.hand_in(
            closed,
            s[1],
            [("opdracht_1.py", program, 0), ("README.md", _report("Daan", "Uitleg."), 1)],
            at=start + timedelta(minutes=4),
        )
        # Identical to Daan's file: shows the "identical to another student" signal.
        seeder.hand_in(
            closed,
            s[2],
            [("opdracht_1.py", program, 0), ("README.md", _report("Fatima", "Uitleg."), 1)],
            at=start + timedelta(minutes=9),
        )
        first = seeder.hand_in(
            closed,
            s[3],
            [("opdracht1.py", b"print('versie 1')\n", 0), ("README.md", b"", 1)],
            at=start + timedelta(minutes=12),
        )
        seeder.clock.set(start + timedelta(minutes=15))
        seeder.services.review.cancel(
            db,
            owner,
            closed,
            first,
            "Verkeerde versie ingeleverd, op verzoek van de student.",
            ip=None,
        )
        seeder.hand_in(
            closed,
            s[3],
            [
                ("opdracht_1.py", program + b"# Lars\n", 0),
                ("README.md", _report("Lars", "Uitleg."), 1),
            ],
            at=start + timedelta(minutes=20),
        )
        result.exams.append((closed.name, closed.slug, closed.access_code))

        # An exam that is open right now.
        seeder.clock.set(now)
        starts = _round_quarter(now - timedelta(minutes=30))
        active = seeder.exam(owner, ExamInput(
            name="Demo: Netwerken P2", description=None, starts_at=starts,
            ends_at=starts + timedelta(hours=2), slug="demo-netwerken-p2",
            extensions=["sh", "md", "txt", "pdf"], max_file_mb=5, max_submission_mb=20,
            slots=[SlotInput(label="Script", pattern="Netwerken_P2_*.sh", extensions=["sh"]),
                   SlotInput(label="Verslag", pattern="*.md", extensions=["md"])],
        ))  # fmt: skip
        seeder.hand_in(
            active,
            s[0],
            [
                ("Netwerken_P2_Sanne.sh", _script("Sanne"), 0),
                ("verslag.md", _report("Sanne", "Netwerkconfiguratie."), 1),
            ],
            at=starts + timedelta(minutes=20),
        )
        # A name that does not match the expected pattern, plus an extra file.
        seeder.hand_in(
            active,
            s[4],
            [
                ("netwerk.sh", _script("Mila"), 0),
                ("verslag.md", _report("Mila", "Zie bijlage."), 1),
                ("notities.txt", b"extra aantekeningen\n", None),
            ],
            at=starts + timedelta(minutes=25),
        )
        result.exams.append((active.name, active.slug, active.access_code))

        # A planned exam tomorrow and a concept.
        tomorrow = datetime.combine(today + timedelta(days=1), time(9, 0), tzinfo=tz)
        planned = seeder.exam(owner, ExamInput(
            name="Demo: Databases P3", description=None, starts_at=tomorrow,
            ends_at=tomorrow + timedelta(hours=2), slug="demo-databases-p3",
            extensions=["sql", "md"], max_file_mb=5, max_submission_mb=20,
            slots=[SlotInput(label="Queries", pattern="*.sql", extensions=["sql"])],
        ))  # fmt: skip
        result.exams.append((planned.name, planned.slug, planned.access_code))
        concept = seeder.exam(owner, ExamInput(
            name="Demo: Concept", description=None, starts_at=tomorrow + timedelta(days=7),
            ends_at=tomorrow + timedelta(days=7, hours=2), slug="demo-concept",
            extensions=["pdf"], max_file_mb=5, max_submission_mb=20,
        ), publish=False, roster=False)  # fmt: skip
        result.exams.append((concept.name, concept.slug, concept.access_code))

        # A fictitious qualification, to try templates from a crebo number before the
        # SBB qualification structure is imported. Crebo 99999 does not exist.
        seeder.services.qualifications.import_records(
            db, [DEMO_QUALIFICATION], actor=Actor.system(), ip=None, source="demo-seed"
        )
        return result
