"""Team templates (PLAN B66): team contacts make them from the team's crebos or copy their
own templates into the team; members use and copy them; other teachers do not see them."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent, ExamTemplate, Qualification, Team, User
from inleverboks.services.audit import Actor
from inleverboks.services.container import Services
from tests.conftest import create_user, enable_totp, login, post
from tests.integration.test_qualifications import record


@pytest.fixture
def admin(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "admin@school.nl", name="Admin", admin=True)


@pytest.fixture
def qualification(db: Session, services: Services) -> Qualification:
    services.qualifications.import_records(
        db, [record()], actor=Actor.system(), ip=None, source="t"
    )
    found = db.scalar(select(Qualification).where(Qualification.crebo == "27016"))
    assert found is not None
    return found


@pytest.fixture
def other_qualification(db: Session, clock: FrozenClock) -> Qualification:
    row = Qualification(crebo="25604", name="Software developer", level=4,
                        dossier_version="2024", valid_from=date(2024, 8, 1),
                        imported_at=clock.now())  # fmt: skip
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def team(db: Session, services: Services, admin: User, qualification: Qualification) -> Team:
    team = services.teams.create(db, admin, "ICT", "", ip=None)
    services.teams.add_crebo(db, admin, team, "27016", ip=None)
    return team


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
def team_template(
    db: Session,
    services: Services,
    people: dict[str, User],
    team: Team,
    qualification: Qualification,
) -> ExamTemplate:
    return services.exams.templates_from_qualification(
        db, people["tcp"], qualification, ["B1-K1"], team_id=team.id, ip=None
    )[0]


def test_team_contact_makes_team_templates_from_the_team_crebos(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team: Team, qualification: Qualification,
    other_qualification: Qualification,
) -> None:  # fmt: skip
    tcp = _client(client, db, services, clock, people["tcp"])
    listing = tcp.get("/examens/sjablonen").text
    assert "Teamsjablonen: ICT" in listing
    assert f'href="/examens/sjablonen/crebo?team={team.id}"' in listing

    search = tcp.get(f"/examens/sjablonen/crebo?team={team.id}").text  # no query needed
    assert "Teamsjabloon voor ICT" in search
    assert f"/examens/sjablonen/crebo/{qualification.id}?team={team.id}" in search
    assert str(other_qualification.id) not in search  # not a crebo of the team

    detail = tcp.get(f"/examens/sjablonen/crebo/{qualification.id}?team={team.id}").text
    assert f'name="team_id" value="{team.id}" checked' in detail
    response = post(tcp, f"/examens/sjablonen/crebo/{qualification.id}",
                    {"core_task": ["B1-K1"], "team_id": str(team.id)})  # fmt: skip
    assert response.status_code == 303
    template = db.scalars(select(ExamTemplate)).one()
    assert template.team_id == team.id
    event = db.scalars(select(AuditEvent).where(
        AuditEvent.event == "exam_template.created_from_qualification")).one()  # fmt: skip
    assert event.details["team_id"] == str(team.id)

    # A crebo that is not linked to the team gives no team template.
    refused = post(tcp, f"/examens/sjablonen/crebo/{other_qualification.id}",
                   {"core_task": [], "team_id": str(team.id)})  # fmt: skip
    assert refused.headers["location"].endswith("error=crebo_not_in_team")
    assert (
        'name="team_id"' not in tcp.get(f"/examens/sjablonen/crebo/{other_qualification.id}").text
    )


def test_successor_crebo_counts_for_the_team(
    db: Session, clock: FrozenClock, services: Services, team: Team, people: dict[str, User]
) -> None:
    successor = Qualification(crebo="29999", name="ICT System Engineer (nieuw)", level=4,
                              dossier_version="2027", previous_crebo="27016",
                              imported_at=clock.now())  # fmt: skip
    db.add(successor)
    db.commit()
    assert {q.crebo for q in services.teams.qualifications(db, team.id)} == {"27016", "29999"}
    assert services.exams.qualification_teams(db, people["tcp"], successor) == [team]


def test_member_uses_and_copies_but_does_not_manage(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team_template: ExamTemplate,
) -> None:  # fmt: skip
    member = _client(client, db, services, clock, people["member"])
    base = f"/examens/sjablonen/{team_template.id}"
    assert team_template.name in member.get("/examens/sjablonen").text
    detail = member.get(base).text
    assert "Teamsjabloon" in detail
    assert f"{base}/bewerken" not in detail and "Kopiëren naar een team" not in detail
    assert member.get(f"{base}/bewerken").status_code == 403
    assert post(member, f"{base}/verwijderen").status_code == 403
    assert member.get(f"/examens/nieuw?sjabloon={team_template.id}").status_code == 200
    copy = post(member, f"{base}/kopie")
    assert copy.headers["location"].endswith("?ok=copied")
    team_copy = post(member, f"{base}/kopie", {"team_id": str(team_template.team_id)})
    assert team_copy.status_code == 403


def test_outsider_does_not_see_team_templates(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team_template: ExamTemplate,
) -> None:  # fmt: skip
    outsider = _client(client, db, services, clock, people["outsider"])
    assert team_template.name not in outsider.get("/examens/sjablonen").text
    assert outsider.get(f"/examens/sjablonen/{team_template.id}").status_code == 404
    assert team_template.name not in outsider.get("/examens/nieuw").text


def test_team_contact_edits_deletes_and_copies_into_the_team(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    people: dict[str, User], team: Team, team_template: ExamTemplate,
) -> None:  # fmt: skip
    tcp = _client(client, db, services, clock, people["tcp"])
    template_id = team_template.id
    base = f"/examens/sjablonen/{template_id}"
    assert 'class="button button-tcp"' in tcp.get(base).text
    assert tcp.get(f"{base}/bewerken").status_code == 200

    own = services.exams.copy_template(db, people["tcp"], team_template, ip=None)
    page = tcp.get(f"/examens/sjablonen/{own.id}").text
    assert "Kopiëren naar ICT" in page
    response = post(tcp, f"/examens/sjablonen/{own.id}/kopie", {"team_id": str(team.id)})
    assert response.headers["location"].endswith("?ok=copied_team")
    assert (
        post(tcp, f"/examens/sjablonen/{own.id}/kopie", {"team_id": str(uuid.uuid4())}).status_code
        == 403
    )

    assert post(tcp, f"{base}/verwijderen").status_code == 303
    db.expire_all()
    assert db.get(ExamTemplate, template_id) is None


def test_admin_sees_the_templates_of_every_team(
    client: TestClient, db: Session, clock: FrozenClock, services: Services, admin: User,
    team_template: ExamTemplate,
) -> None:  # fmt: skip
    ac = _client(client, db, services, clock, admin)
    listing = ac.get("/examens/sjablonen").text
    assert "Teamsjablonen: ICT" in listing and team_template.name in listing
    assert ac.get(f"/examens/sjablonen/{team_template.id}/bewerken").status_code == 200
