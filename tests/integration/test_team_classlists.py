"""Class lists per education team (PLAN B65): team contacts import and manage them, the
members of the team see and use them, other teachers do not see them at all."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent, ClassList, Team, User
from inleverboks.services.classlists import ImportResult, ParsedEntry
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, PermissionDeniedError
from inleverboks.services.exams import ExamValidationError
from tests.conftest import create_user, csrf_token, enable_totp, extract, login, post
from tests.student_helpers import make_exam


def _result(*numbers: str) -> ImportResult:
    return ImportResult(entries=[
        ParsedEntry(row_number=i, student_number=n, first_name="Jan", infix=None,
                    last_name=f"Student{n}", class_name="ICT-2A")
        for i, n in enumerate(numbers, start=2)
    ])  # fmt: skip


@pytest.fixture
def admin(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "admin@school.nl", name="Admin", admin=True)


@pytest.fixture
def team(db: Session, services: Services, admin: User) -> Team:
    return services.teams.create(db, admin, "ICT", "", ip=None)


@pytest.fixture
def other_team(db: Session, services: Services, admin: User) -> Team:
    return services.teams.create(db, admin, "Zorg", "", ip=None)


@pytest.fixture
def people(
    db: Session, clock: FrozenClock, services: Services, admin: User, team: Team
) -> dict[str, User]:
    tcp = create_user(db, clock, "tcp@school.nl", name="Tessa TCP")
    member = create_user(db, clock, "lid@school.nl", name="Lars Lid")
    outsider = create_user(db, clock, "ander@school.nl", name="Otto Ander")
    services.teams.add_member(db, admin, team, tcp.id, contact=True, ip=None)
    services.teams.add_member(db, admin, team, member.id, ip=None)
    return {"tcp": tcp, "member": member, "outsider": outsider}


def _client(client: TestClient, db: Session, services: Services, clock: FrozenClock,
            user: User) -> TestClient:  # fmt: skip
    other = TestClient(client.app)
    secret = enable_totp(db, services, user) if services.auth.totp_required(db, user) else None
    login(other, user.email, secret=secret, clock=clock)
    return other


@pytest.fixture
def team_list(db: Session, services: Services, people: dict[str, User], team: Team) -> ClassList:
    return services.classlists.save(
        db, people["tcp"], team.id, "ICT-2A", _result("123456", "123457"), ip=None
    )


def test_team_contact_imports_a_list_for_his_team(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team: Team,
) -> None:  # fmt: skip
    tcp = _client(client, db, services, clock, people["tcp"])
    library = tcp.get("/examens/klassenlijsten").text
    assert f'href="/examens/klassenlijsten/nieuw?team={team.id}"' in library
    assert 'class="tcp-mark"' in library

    form = tcp.get("/examens/klassenlijsten/nieuw").text
    assert f'name="team_id" value="{team.id}"' in form  # one team: chosen for him
    page = tcp.post(
        "/examens/klassenlijsten/nieuw",
        data={"csrf_token": csrf_token(tcp), "name": "ICT-2A", "team_id": str(team.id)},
        files={"file": ("l.csv", b"Studentnummer;Voornaam;Achternaam\n123456;Jan;Vries\n", "text/csv")},
    )  # fmt: skip
    saved = post(tcp, "/examens/klassenlijsten/importeren", {
        "csv_b64": extract(r'name="csv_b64" value="([^"]+)"', page.text),
        "delimiter": ";", "has_header": "true", "name": "ICT-2A",
        "team_id": extract(r'name="team_id" value="([^"]+)"', page.text),
        "map_student_number": "0", "map_first_name": "1", "map_last_name": "2",
        "action": "save",
    })  # fmt: skip
    assert saved.status_code == 303
    class_list = db.scalars(select(ClassList)).one()
    assert (class_list.team_id, class_list.owner_id) == (team.id, people["tcp"].id)
    event = db.scalars(select(AuditEvent).where(AuditEvent.event == "class_list.imported")).one()
    assert event.details["team_id"] == str(team.id)


def test_only_team_contacts_of_that_team_import(
    db: Session, services: Services, people: dict[str, User], admin: User, team: Team,
    other_team: Team,
) -> None:  # fmt: skip
    with pytest.raises(PermissionDeniedError):
        services.classlists.save(db, people["member"], team.id, "X", _result("1"), ip=None)
    with pytest.raises(PermissionDeniedError):  # a TCP of another team
        services.classlists.save(db, people["tcp"], other_team.id, "X", _result("1"), ip=None)
    with pytest.raises(DomainError, match="class_list_team_required"):
        services.classlists.save(db, people["tcp"], None, "X", _result("1"), ip=None)
    # Admins import for any team, and choose it.
    assert services.classlists.save(db, admin, other_team.id, "X", _result("1"), ip=None)
    assert [t.name for t in services.classlists.creatable_teams(db, admin)] == ["ICT", "Zorg"]
    assert services.classlists.creatable_teams(db, people["member"]) == []


def test_member_sees_and_uses_but_does_not_manage(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team_list: ClassList,
) -> None:  # fmt: skip
    member = _client(client, db, services, clock, people["member"])
    library = member.get("/examens/klassenlijsten").text
    assert "<h2>ICT</h2>" in library
    assert "/examens/klassenlijsten/nieuw" not in library
    assert member.get("/examens/klassenlijsten/nieuw").status_code == 403

    base = f"/examens/klassenlijsten/{team_list.id}"
    detail = member.get(base).text
    assert "Student123456" in detail  # he can check the list
    assert "Onderwijsteam: ICT" in detail
    assert "Lijst vervangen" not in detail and f"{base}/verwijderen" not in detail
    assert post(member, f"{base}/naam", {"name": "Gekaapt"}).status_code == 403
    assert post(member, f"{base}/verwijderen").status_code == 403
    assert member.get(f"{base}/vervangen").status_code == 403

    assert "ICT-2A (2) · ICT" in member.get("/examens/nieuw").text
    exam = make_exam(db, services, people["member"], class_list_id=team_list.id)
    assert exam.id is not None


def test_outsider_does_not_see_team_lists(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team_list: ClassList,
) -> None:  # fmt: skip
    outsider = _client(client, db, services, clock, people["outsider"])
    assert "ICT-2A" not in outsider.get("/examens/klassenlijsten").text
    assert "Je zit nog niet in een onderwijsteam" in outsider.get("/examens/klassenlijsten").text
    assert outsider.get(f"/examens/klassenlijsten/{team_list.id}").status_code == 404
    assert "ICT-2A" not in outsider.get("/examens/nieuw").text
    with pytest.raises(ExamValidationError):
        make_exam(db, services, people["outsider"], class_list_id=team_list.id)
    exam = make_exam(db, services, people["outsider"], slug="eigen-examen")
    response = post(
        outsider, f"/examens/{exam.id}/klassenlijst", {"class_list_id": str(team_list.id)}
    )
    assert response.headers["location"].endswith("?error=class_list_not_found")
    assert services.exams.roster(db, exam) == []


def test_team_contact_renames_replaces_and_deletes(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team_list: ClassList,
) -> None:  # fmt: skip
    tcp = _client(client, db, services, clock, people["tcp"])
    list_id = team_list.id
    base = f"/examens/klassenlijsten/{list_id}"
    detail = tcp.get(base).text
    assert 'class="button button-tcp"' in detail
    assert post(tcp, f"{base}/naam", {"name": "ICT-2B"}).headers["location"].endswith("ok=renamed")
    services.classlists.replace(db, people["tcp"], list_id, "ICT-2B", _result("9"), ip=None)
    assert post(tcp, f"{base}/verwijderen").status_code == 303
    db.expire_all()
    assert db.get(ClassList, list_id) is None


def test_admin_sees_every_team(
    client: TestClient, db: Session, clock: FrozenClock, services: Services, admin: User,
    team_list: ClassList, other_team: Team,
) -> None:  # fmt: skip
    ac = _client(client, db, services, clock, admin)
    library = ac.get("/examens/klassenlijsten").text
    assert "<h2>ICT</h2>" in library and "<h2>Zorg</h2>" in library
    assert "Dit team heeft nog geen klassenlijsten." in library
    assert ac.get(f"/examens/klassenlijsten/{team_list.id}").status_code == 200
    assert ac.get(f"/examens/klassenlijsten/{uuid.uuid4()}").status_code == 404
