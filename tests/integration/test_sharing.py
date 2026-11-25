"""Shared class lists (PLAN B60) and the presentation counter that keeps counting
invalidated work (PLAN B62)."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import (
    AuditEvent,
    ClassList,
    Exam,
    ExamRosterEntry,
    Submission,
    User,
)
from inleverboks.services.classlists import ImportResult, ParsedEntry
from inleverboks.services.container import Services
from tests.conftest import START, create_user, csrf_token, enable_totp, extract, login, post
from tests.student_helpers import make_exam, ready, slot_ids, upload


@pytest.fixture
def teacher(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "docent@school.nl", name="Anna Eigenaar")


@pytest.fixture
def colleague(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "ander@school.nl", name="Bert Collega")


@pytest.fixture
def admin(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "admin@school.nl", name="Carla Admin", admin=True)


def _client(client: TestClient, email: str, *, secret: str | None = None,
            clock: FrozenClock | None = None) -> TestClient:  # fmt: skip
    other = TestClient(client.app)
    assert login(other, email, secret=secret, clock=clock).status_code == 303
    return other


@pytest.fixture
def clients(
    client: TestClient, db: Session, services: Services, clock: FrozenClock,
    teacher: User, colleague: User, admin: User,
) -> dict[str, TestClient]:  # fmt: skip
    secret = enable_totp(db, services, admin)
    return {
        "owner": _client(client, "docent@school.nl"),
        "other": _client(client, "ander@school.nl"),
        "admin": _client(client, "admin@school.nl", secret=secret, clock=clock),
    }


def events(db: Session, name: str) -> list[AuditEvent]:
    db.expire_all()
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.event == name).order_by(AuditEvent.seq))
    )


def _result(*numbers: str) -> ImportResult:
    return ImportResult(
        entries=[
            ParsedEntry(
                row_number=i,
                student_number=n,
                first_name="Jan",
                infix=None,
                last_name=f"Student{n}",
                class_name="ICT-2A",
            )
            for i, n in enumerate(numbers, start=2)
        ]
    )


@pytest.fixture
def class_list(db: Session, services: Services, teacher: User) -> ClassList:
    return services.classlists.save(db, teacher, "ICT-2A", _result("123456", "123457"), ip=None)


# Class lists: everyone uses them, the owner manages them ---------------------------


def test_library_filter_and_owner_column(
    clients: dict[str, TestClient], class_list: ClassList
) -> None:
    mine = clients["other"].get("/examens/klassenlijsten").text
    assert "ICT-2A" not in mine
    assert 'href="/examens/klassenlijsten" aria-current="page"' in mine
    everyone = clients["other"].get("/examens/klassenlijsten?toon=alle").text
    assert f'href="/examens/klassenlijsten/{class_list.id}"' in everyone
    assert '<th scope="col">Eigenaar</th>' in everyone
    assert "Anna Eigenaar" in everyone
    assert 'href="/examens/klassenlijsten?toon=alle" aria-current="page"' in everyone
    # The owner sees "Jij" in the owner column of their own list.
    assert re.search(
        r"ICT-2A</a></td><td>Jij</td>", clients["owner"].get("/examens/klassenlijsten").text
    )


@pytest.mark.parametrize("role", ["owner", "other", "admin"])
def test_detail_visible_to_everyone_buttons_by_role(
    role: str, clients: dict[str, TestClient], class_list: ClassList
) -> None:
    page = clients[role].get(f"/examens/klassenlijsten/{class_list.id}")
    assert page.status_code == 200
    assert "Student123456" in page.text
    assert "Kopiëren naar mijn lijsten" in page.text
    manages = role != "other"
    assert ("Lijst vervangen" in page.text) is manages
    assert (f"/examens/klassenlijsten/{class_list.id}/verwijderen" in page.text) is manages
    assert (f"/examens/klassenlijsten/{class_list.id}/naam" in page.text) is manages
    assert ("Eigenaar: Anna Eigenaar" in page.text) is (role != "owner")


def _replace(client: TestClient, class_list_id: uuid.UUID, csv: bytes) -> Any:
    page = client.post(
        "/examens/klassenlijsten/nieuw",
        data={"csrf_token": csrf_token(client), "name": "ICT-2A nieuw",
              "replace_id": str(class_list_id)},
        files={"file": ("lijst.csv", csv, "text/csv")},
        follow_redirects=False,
    )  # fmt: skip
    if page.status_code != 200:
        return page
    return post(client, "/examens/klassenlijsten/importeren", {
        "csv_b64": extract(r'name="csv_b64" value="([^"]+)"', page.text),
        "delimiter": extract(r'name="delimiter" value="([^"]+)"', page.text),
        "replace_id": extract(r'name="replace_id" value="([^"]+)"', page.text),
        "name": "ICT-2A nieuw", "has_header": "true",
        "map_student_number": "0", "map_first_name": "1", "map_infix": "",
        "map_last_name": "2", "map_class_name": "", "action": "save",
    })  # fmt: skip


CSV = b"Studentnummer;Voornaam;Achternaam\n223456;Piet;Nieuw\n"


def test_other_teacher_cannot_change_or_delete(
    db: Session, clients: dict[str, TestClient], class_list: ClassList
) -> None:
    other = clients["other"]
    base = f"/examens/klassenlijsten/{class_list.id}"
    assert post(other, f"{base}/naam", {"name": "Gekaapt"}).status_code == 403
    assert post(other, f"{base}/verwijderen").status_code == 403
    assert other.get(f"{base}/vervangen").status_code == 403
    assert _replace(other, class_list.id, CSV).status_code == 403
    # A forged replace_id on the import step is checked again.
    forged = post(other, "/examens/klassenlijsten/importeren", {
        "csv_b64": "", "replace_id": str(class_list.id), "action": "save",
    })  # fmt: skip
    assert forged.status_code == 403
    db.expire_all()
    fresh = db.get(ClassList, class_list.id)
    assert fresh is not None and fresh.name == "ICT-2A" and len(fresh.entries) == 2
    assert not events(db, "class_list.renamed") and not events(db, "class_list.deleted")
    # Unknown lists stay 404.
    assert other.get(f"/examens/klassenlijsten/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_rename_replace_delete(
    role: str, db: Session, clients: dict[str, TestClient], class_list: ClassList,
    teacher: User, admin: User,
) -> None:  # fmt: skip
    actor = teacher if role == "owner" else admin
    c = clients[role]
    list_id = class_list.id
    base = f"/examens/klassenlijsten/{list_id}"
    assert (
        post(c, f"{base}/naam", {"name": " "})
        .headers["location"]
        .endswith("error=class_list_name_invalid")
    )
    assert post(c, f"{base}/naam", {"name": "ICT-2B"}).headers["location"].endswith("?ok=renamed")
    renamed = events(db, "class_list.renamed")[0]
    assert renamed.actor_id == str(actor.id)
    assert renamed.details == {
        "class_list_id": str(class_list.id), "owner_id": str(teacher.id),
        "name": "ICT-2A", "new_name": "ICT-2B",
    }  # fmt: skip

    assert c.get(f"{base}/vervangen").status_code == 200
    response = _replace(c, class_list.id, CSV)
    assert response.headers["location"] == f"{base}?ok=replaced"
    db.expire_all()
    fresh = db.get(ClassList, class_list.id)
    assert fresh is not None and fresh.owner_id == teacher.id  # stays with its owner
    assert fresh.name == "ICT-2A nieuw"
    assert [e.student_number for e in fresh.entries] == ["223456"]
    replaced = events(db, "class_list.replaced")[0]
    assert replaced.details["class_list_id"] == str(class_list.id)
    assert (replaced.details["old_rows"], replaced.details["rows"]) == (2, 1)

    assert post(c, f"{base}/verwijderen").status_code == 303
    db.expire_all()
    assert db.get(ClassList, list_id) is None
    deleted = events(db, "class_list.deleted")[0]
    assert deleted.actor_id == str(actor.id)
    assert deleted.details["class_list_id"] == str(list_id)
    assert deleted.details["owner_id"] == str(teacher.id)


def test_copy_to_my_lists(
    db: Session, clients: dict[str, TestClient], class_list: ClassList, colleague: User
) -> None:
    other = clients["other"]
    response = post(other, f"/examens/klassenlijsten/{class_list.id}/kopie")
    copy_id = uuid.UUID(response.headers["location"].split("/")[3].split("?")[0])
    copy = db.get(ClassList, copy_id)
    assert copy is not None and copy.owner_id == colleague.id
    assert copy.name == "ICT-2A (kopie)"
    assert sorted(e.student_number for e in copy.entries) == ["123456", "123457"]
    event = events(db, "class_list.copied")[0]
    assert event.details["class_list_id"] == str(copy_id)
    assert event.details["source_class_list_id"] == str(class_list.id)
    # The copy is the colleague's own: they may change it, the original stays.
    assert (
        post(other, f"/examens/klassenlijsten/{copy_id}/naam", {"name": "Mijn ICT"}).status_code
        == 303
    )
    assert "ICT-2A (kopie)" not in other.get("/examens/klassenlijsten").text
    assert "Mijn ICT" in other.get("/examens/klassenlijsten").text


def test_other_teacher_uses_list_in_exam_and_snapshot_stays(
    db: Session, services: Services, clients: dict[str, TestClient], class_list: ClassList,
    colleague: User, teacher: User,
) -> None:  # fmt: skip
    other = clients["other"]
    new_form = other.get("/examens/nieuw").text
    assert f'<option value="{class_list.id}">ICT-2A (2) · Anna Eigenaar</option>' in new_form
    exam = make_exam(db, services, colleague, class_list_id=class_list.id)
    roster = db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id))
    assert {r.student_number for r in roster} == {"123456", "123457"}
    # Also on an existing exam via the settings page.
    second = make_exam(db, services, colleague, slug="linux-p4")
    settings_page = other.get(f"/examens/{second.id}/instellingen").text
    assert "ICT-2A (2) · Anna Eigenaar" in settings_page
    response = post(
        other, f"/examens/{second.id}/klassenlijst", {"class_list_id": str(class_list.id)}
    )
    assert response.headers["location"].endswith("?ok=roster")
    # The owner replaces the list: the exams keep their snapshot.
    services.classlists.replace(db, teacher, class_list.id, "ICT-2A", _result("999999"), ip=None)
    db.expire_all()
    for e in (exam, second):
        numbers = set(
            db.scalars(
                select(ExamRosterEntry.student_number).where(ExamRosterEntry.exam_id == e.id)
            )
        )
        assert numbers == {"123456", "123457"}


# Presentation counter -------------------------------------------------------------


def _hand_in(client: TestClient, exam: Exam, number: str) -> None:
    student = TestClient(client.app)
    ready(student, exam, number, first="Piet", infix="", last=f"Jansen{number}")
    files = [
        (f"Examen_Linux_P3_{number}.docx", b"PK verslag " + number.encode(), slot_ids(exam)[0])
    ]
    assert upload(student, exam, files, confirmed=True).status_code == 200


def _submission(db: Session, exam: Exam, number: str) -> Submission:
    db.expire_all()
    found = db.scalar(
        select(Submission).where(
            Submission.exam_id == exam.id, Submission.student_number == number,
            Submission.status == "active",
        )
    )  # fmt: skip
    assert found is not None
    return found


def test_counter_keeps_invalidated_work_once_per_student(
    client: TestClient, db: Session, services: Services, clock: FrozenClock,
    clients: dict[str, TestClient], teacher: User,
) -> None:  # fmt: skip
    exam = make_exam(db, services, teacher)
    clock.set(START + timedelta(minutes=5))
    for number in ("111111", "222222", "333333"):
        _hand_in(client, exam, number)

    def count() -> int:
        db.expire_all()
        page = clients["owner"].get(f"/examens/{exam.id}/presentatie/teller").text
        shown = int(extract(r'class="present-count">(\d+) ingeleverd', page))
        assert shown == services.exams.handed_in_count(db, exam)
        return shown

    assert count() == 3
    # Invalidating work does not lower the count.
    services.review.invalidate(
        db, teacher, exam, _submission(db, exam, "111111").id, "Fraude", may_resubmit=True, ip=None
    )
    assert count() == 3
    # Invalid + a new active hand-in counts once.
    _hand_in(client, exam, "111111")
    assert count() == 3
    # A cancelled submission does not count: that student hands in again.
    services.review.cancel(
        db, teacher, exam, _submission(db, exam, "222222").id, "Vervangen", ip=None
    )
    assert count() == 2
    _hand_in(client, exam, "222222")
    assert count() == 3
