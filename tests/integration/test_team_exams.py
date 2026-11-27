"""Exams of an education team (PLAN B67, B68): the team of a new exam, copies and resits,
class lists of the same team, what team contacts may do, and the admin moving an exam."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent, ClassList, Exam, Team, User
from inleverboks.services.classlists import ImportResult, ParsedEntry
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError
from inleverboks.services.exams import ExamValidationError
from tests.conftest import create_user, enable_totp, login, post
from tests.integration.test_exams import form
from tests.student_helpers import make_exam


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
    colleague = create_user(db, clock, "collega@school.nl", name="Cor Collega")
    outsider = create_user(db, clock, "ander@school.nl", name="Otto Ander")
    services.teams.add_member(db, admin, team, tcp.id, contact=True, ip=None)
    services.teams.add_member(db, admin, team, member.id, ip=None)
    services.teams.add_member(db, admin, team, colleague.id, ip=None)
    return {"tcp": tcp, "member": member, "colleague": colleague, "outsider": outsider}


def _client(client: TestClient, db: Session, services: Services, clock: FrozenClock,
            user: User) -> TestClient:  # fmt: skip
    other = TestClient(client.app)
    secret = enable_totp(db, services, user) if services.auth.totp_required(db, user) else None
    login(other, user.email, secret=secret, clock=clock)
    return other


def _new_exam(client: TestClient, db: Session, **fields: str) -> tuple[int, Exam | None, str]:
    response = post(client, "/examens/nieuw", form(**fields))
    if response.status_code != 303:
        return response.status_code, None, response.text
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    return 303, exam, ""


def _team_list(db: Session, services: Services, actor: User, team: Team) -> ClassList:
    result = ImportResult(entries=[ParsedEntry(row_number=2, student_number="123456",
                                               first_name="Jan", infix=None, last_name="Vries",
                                               class_name=None)])  # fmt: skip
    return services.classlists.save(db, actor, team.id, f"Lijst {team.name}", result, ip=None)


def test_one_team_is_chosen_for_the_teacher(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team: Team,
) -> None:  # fmt: skip
    member = _client(client, db, services, clock, people["member"])
    assert "Onderwijsteam: <strong>ICT</strong>" in member.get("/examens/nieuw").text
    status, exam, _ = _new_exam(member, db)
    assert status == 303 and exam is not None and exam.team_id == team.id
    created = db.scalars(select(AuditEvent).where(AuditEvent.event == "exam.created")).one()
    assert created.details["exam"]["team_id"] == str(team.id)


def test_teacher_in_two_teams_chooses(
    client: TestClient, db: Session, clock: FrozenClock, services: Services, admin: User,
    people: dict[str, User], team: Team, other_team: Team,
) -> None:  # fmt: skip
    services.teams.add_member(db, admin, other_team, people["member"].id, ip=None)
    member = _client(client, db, services, clock, people["member"])
    assert 'name="team_id"' in member.get("/examens/nieuw").text
    status, _, page = _new_exam(member, db)
    assert status == 400 and "Dit veld is verplicht." in page
    forged = _new_exam(member, db, team_id=str(uuid.uuid4()))
    assert forged[0] == 400
    status, exam, _ = _new_exam(member, db, team_id=str(other_team.id))
    assert exam is not None and exam.team_id == other_team.id


def test_teacher_without_team_makes_an_exam_without_team(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team: Team,
) -> None:  # fmt: skip
    outsider = _client(client, db, services, clock, people["outsider"])
    # a team id of a team he is not in is refused
    assert _new_exam(outsider, db, team_id=str(team.id))[0] == 400
    _, exam, _ = _new_exam(outsider, db)
    assert exam is not None and exam.team_id is None


def test_team_template_and_copy_fix_the_team(
    db: Session, services: Services, admin: User, people: dict[str, User], team: Team,
    other_team: Team,
) -> None:  # fmt: skip
    services.teams.add_member(db, admin, other_team, people["tcp"].id, ip=None)
    # in two teams now: a new exam without a team choice is refused
    with pytest.raises(ExamValidationError) as caught:
        make_exam(db, services, people["tcp"], slug="bron")
    assert caught.value.problems == {"team_id": "required"}
    source = make_exam(db, services, people["tcp"], slug="bron", team_id=team.id)
    template = services.exams.save_as_template(db, people["tcp"], source, "Bron", ip=None)
    team_template = services.exams.copy_template(db, admin, template, ip=None,
                                                 team_id=other_team.id)  # fmt: skip
    data = services.exams.input_from_exam(db, source)
    data.slug, data.source_template_id, data.team_id = "uit-sjabloon", team_template.id, team.id
    exam = services.exams.create(db, people["tcp"], data, ip=None)
    assert exam.team_id == other_team.id  # the template decides, not the form

    copy = services.exams.duplicate(db, people["tcp"], exam, ip=None)
    assert copy.team_id == other_team.id


def test_resit_keeps_the_team(
    db: Session, services: Services, people: dict[str, User], team: Team
) -> None:
    source = make_exam(db, services, people["member"], slug="bron")
    assert source.team_id == team.id
    data = services.exams.input_from_exam(db, source)
    data.slug, data.source_exam_id, data.team_id = "herkansing", source.id, None
    resit = services.exams.create(db, people["member"], data, ip=None)
    assert resit.team_id == team.id


def test_class_list_must_belong_to_the_exam_team(
    db: Session, services: Services, admin: User, people: dict[str, User], team: Team,
    other_team: Team,
) -> None:  # fmt: skip
    services.teams.add_member(db, admin, other_team, people["member"].id, ip=None)
    other_list = _team_list(db, services, admin, other_team)
    with pytest.raises(ExamValidationError) as caught:
        make_exam(db, services, people["member"], team_id=team.id, class_list_id=other_list.id)
    assert caught.value.problems == {"class_list_id": "other_team"}
    own_list = _team_list(db, services, admin, team)
    exam = make_exam(db, services, people["member"], team_id=team.id, class_list_id=own_list.id)
    assert exam.team_id == team.id


def test_team_contact_sees_and_follows_the_team_exams(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team: Team,
) -> None:  # fmt: skip
    exam = make_exam(db, services, people["member"], slug="team-examen")
    own = make_exam(db, services, people["outsider"], slug="los-examen")
    tcp = _client(client, db, services, clock, people["tcp"])
    tabs = tcp.get("/examens").text
    assert 'class="tcp-tab" href="/examens/team"' in tabs
    listing = tcp.get("/examens/team").text
    assert "/team-examen" in listing and "/los-examen" not in listing
    assert "Lars Lid" in listing  # owner column

    assert tcp.get(f"/examens/{exam.id}").status_code == 200
    assert tcp.get(f"/examens/{exam.id}/auditlog").status_code == 200
    assert tcp.get(f"/examens/{exam.id}/bewerken").status_code == 403
    assert tcp.get(f"/examens/{own.id}").status_code == 404
    services.lifecycle.set_hold(db, people["tcp"], exam, "bezwaar", ip=None)

    advanced = tcp.get(f"/examens/{exam.id}/geavanceerd").text
    assert "Cor Collega (collega@school.nl)" in advanced
    assert "Otto Ander" not in advanced  # not in the team
    refused = post(tcp, f"/examens/{exam.id}/eigenaar", {"owner_id": str(people["outsider"].id)})
    assert refused.headers["location"].endswith("error=owner_not_in_team")
    moved = post(tcp, f"/examens/{exam.id}/eigenaar", {"owner_id": str(people["colleague"].id)})
    assert moved.headers["location"].endswith("?ok=owner")
    db.refresh(exam)
    assert exam.owner_id == people["colleague"].id


def test_members_get_no_team_rights(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User],
) -> None:  # fmt: skip
    exam = make_exam(db, services, people["colleague"], slug="van-collega")
    member = _client(client, db, services, clock, people["member"])
    assert "/examens/team" not in member.get("/examens").text
    assert member.get("/examens/team").status_code == 403
    assert member.get(f"/examens/{exam.id}").status_code == 404


def test_only_an_admin_moves_an_exam_to_another_team(
    client: TestClient, db: Session, clock: FrozenClock, services: Services, admin: User,
    people: dict[str, User], team: Team, other_team: Team,
) -> None:  # fmt: skip
    exam = make_exam(db, services, people["member"], slug="verhuizen")
    tcp = _client(client, db, services, clock, people["tcp"])
    assert post(tcp, f"/examens/{exam.id}/team", {"team_id": str(other_team.id)}).status_code == 403
    ac = _client(client, db, services, clock, admin)
    response = post(ac, f"/examens/{exam.id}/team", {"team_id": str(other_team.id)})
    assert response.headers["location"].endswith("?ok=team_changed")
    db.refresh(exam)
    assert exam.team_id == other_team.id
    event = db.scalars(select(AuditEvent).where(AuditEvent.event == "exam.team_changed")).one()
    assert event.chain == str(exam.id)
    assert event.details == {"old_team_id": str(team.id), "new_team_id": str(other_team.id)}
    # The team contact of the old team no longer sees it.
    assert tcp.get(f"/examens/{exam.id}").status_code == 404
    with pytest.raises(DomainError):
        services.exams.set_team(db, admin, exam, uuid.uuid4(), ip=None)
