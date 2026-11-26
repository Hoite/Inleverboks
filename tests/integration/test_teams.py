"""Education teams (PLAN B63, B64, B69): admin management, the team page of members and
team contacts, two-step verification for team contacts and the users overview."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import (
    AuditEvent,
    ClassList,
    Qualification,
    Team,
    TeamCrebo,
    TeamMember,
    User,
    UserSession,
)
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, PermissionDeniedError
from tests.conftest import create_user, enable_totp, login, post


@pytest.fixture
def admin(db: Session, clock: FrozenClock, services: Services) -> tuple[User, str]:
    user = create_user(db, clock, "admin@school.nl", name="Admin", admin=True)
    return user, enable_totp(db, services, user)


@pytest.fixture
def admin_client(client: TestClient, admin: tuple[User, str], clock: FrozenClock) -> TestClient:
    other = TestClient(client.app)
    login(other, "admin@school.nl", secret=admin[1], clock=clock)
    return other


@pytest.fixture
def team(db: Session, services: Services, admin: tuple[User, str]) -> Team:
    return services.teams.create(db, admin[0], "ICT", "Applicatieontwikkeling", ip=None)


def _qualification(db: Session, clock: FrozenClock, crebo: str = "25604") -> None:
    db.add(Qualification(
        crebo=crebo, name="Software developer", level=4, dossier_version="2024",
        valid_from=date(2024, 8, 1), imported_at=clock.now(),
    ))  # fmt: skip
    db.commit()


def _events(db: Session, name: str) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.event == name)))


# Admin ---------------------------------------------------------------------------


def test_admin_creates_and_renames_a_team(admin_client: TestClient, db: Session) -> None:
    response = post(
        admin_client,
        "/admin/onderwijsteams",
        {"name": "  Zorg  en Welzijn ", "description": "Niveau 3 en 4"},
    )
    assert response.status_code == 303
    team = db.scalars(select(Team)).one()
    assert team.name == "Zorg en Welzijn"
    assert response.headers["location"] == f"/admin/onderwijsteams/{team.id}?ok=created"

    duplicate = post(admin_client, "/admin/onderwijsteams", {"name": "zorg en welzijn"})
    assert duplicate.status_code == 400
    assert "Er bestaat al een onderwijsteam met deze naam." in duplicate.text

    response = post(
        admin_client,
        f"/admin/onderwijsteams/{team.id}/bewerken",
        {"name": "Zorg", "description": ""},
    )
    assert response.status_code == 303
    db.refresh(team)
    assert (team.name, team.description) == ("Zorg", None)
    assert [e.details["team_id"] for e in _events(db, "team.updated")] == [str(team.id)]

    page = admin_client.get("/admin/onderwijsteams").text
    assert "Zorg" in page
    assert "Geen teamcontactpersoon" in page  # signal: a team without TCP (B69)


def test_team_name_is_required(admin_client: TestClient, db: Session) -> None:
    response = post(admin_client, "/admin/onderwijsteams", {"name": "   "})
    assert response.status_code == 400
    assert db.scalars(select(Team)).all() == []


def test_teachers_cannot_manage_teams(
    client: TestClient, db: Session, clock: FrozenClock, team: Team
) -> None:
    create_user(db, clock)
    login(client)
    assert client.get("/admin/onderwijsteams").status_code == 403
    assert post(client, "/admin/onderwijsteams", {"name": "X"}).status_code == 403
    # not a member: the team page does not exist for him
    assert client.get(f"/examens/teams/{team.id}").status_code == 404


def test_admin_adds_members_and_a_team_contact(
    admin_client: TestClient, db: Session, clock: FrozenClock, services: Services, team: Team
) -> None:
    docent = create_user(db, clock, name="Docent")
    tcp = create_user(db, clock, "tcp@school.nl", name="Tessa")
    assert not services.auth.totp_required(db, tcp)

    post(admin_client, f"/admin/onderwijsteams/{team.id}/leden", {"user_id": str(docent.id)})
    response = post(
        admin_client,
        f"/admin/onderwijsteams/{team.id}/leden",
        {"user_id": str(tcp.id), "contact": "true"},
    )
    assert response.status_code == 303

    rows = {m.user_id: m.is_contact for m in db.scalars(select(TeamMember))}
    assert rows == {docent.id: False, tcp.id: True}
    assert services.auth.totp_required(db, tcp)  # B64
    assert [e.details["user_id"] for e in _events(db, "team.contact_set")] == [str(tcp.id)]
    page = admin_client.get(f"/admin/onderwijsteams/{team.id}").text
    assert "Tessa" in page and "Teamcontactpersoon maken" in page

    again = post(
        admin_client, f"/admin/onderwijsteams/{team.id}/leden", {"user_id": str(docent.id)}
    )
    assert again.status_code == 400
    assert "Dit account is al lid van het team." in again.text

    post(admin_client, f"/admin/onderwijsteams/{team.id}/leden/{tcp.id}/tcp", {})
    assert db.get(TeamMember, (team.id, tcp.id)).is_contact is False  # type: ignore[union-attr]
    assert not services.auth.totp_required(db, tcp)


def test_new_team_contact_must_set_up_two_step(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    admin: tuple[User, str], team: Team,
) -> None:  # fmt: skip
    tcp = create_user(db, clock)
    services.teams.add_member(db, admin[0], team, tcp.id, ip=None)
    login(client)
    assert client.get("/examens").status_code == 200

    services.teams.set_contact(db, admin[0], team, tcp.id, True, ip=None)

    # B64: his sessions ended; the next login asks for two-step verification first
    assert db.scalars(select(UserSession).where(UserSession.revoked_at.is_(None))).all() == []
    response = client.get("/examens", follow_redirects=False)
    assert response.status_code == 303
    response = login(client)
    assert response.headers["location"] == "/account/tweestaps"
    disabled = post(client, "/account/tweestaps/uit", {"password": "x"})
    assert disabled.status_code in {303, 400}  # cannot turn it off later either


def test_contact_with_two_step_keeps_his_session(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    admin: tuple[User, str], team: Team,
) -> None:  # fmt: skip
    tcp = create_user(db, clock)
    secret = enable_totp(db, services, tcp)
    services.teams.add_member(db, admin[0], team, tcp.id, ip=None)
    login(client, secret=secret, clock=clock)
    services.teams.set_contact(db, admin[0], team, tcp.id, True, ip=None)
    assert client.get("/examens").status_code == 200


def test_crebos_must_be_imported(
    admin_client: TestClient, db: Session, clock: FrozenClock, team: Team
) -> None:
    unknown = post(admin_client, f"/admin/onderwijsteams/{team.id}/crebos", {"crebo": "25604"})
    assert unknown.status_code == 400
    assert "Kies een crebo uit de geïmporteerde kwalificaties." in unknown.text

    _qualification(db, clock)
    assert (
        post(
            admin_client, f"/admin/onderwijsteams/{team.id}/crebos", {"crebo": "25604"}
        ).status_code
        == 303
    )
    assert [r.crebo for r in db.scalars(select(TeamCrebo))] == ["25604"]
    assert (
        post(
            admin_client, f"/admin/onderwijsteams/{team.id}/crebos", {"crebo": "25604"}
        ).status_code
        == 400
    )

    # a crebo may belong to several teams
    other = post(admin_client, "/admin/onderwijsteams", {"name": "Techniek"})
    other_id = other.headers["location"].split("/")[3].split("?")[0]
    assert (
        post(
            admin_client, f"/admin/onderwijsteams/{other_id}/crebos", {"crebo": "25604"}
        ).status_code
        == 303
    )
    assert "25604 Software developer" in admin_client.get(f"/admin/onderwijsteams/{team.id}").text

    post(admin_client, f"/admin/onderwijsteams/{team.id}/crebos/25604/verwijderen", {})
    assert [r.team_id for r in db.scalars(select(TeamCrebo))] != [team.id]
    assert len(_events(db, "team.crebo_removed")) == 1


def test_team_is_only_deleted_when_empty(
    admin_client: TestClient, db: Session, clock: FrozenClock, admin: tuple[User, str], team: Team
) -> None:
    db.add(ClassList(owner_id=admin[0].id, name="ICT-1A", created_at=clock.now(), team_id=team.id))
    db.commit()
    refused = post(admin_client, f"/admin/onderwijsteams/{team.id}/verwijderen", {})
    assert refused.status_code == 400
    assert "Dit onderwijsteam heeft nog klassenlijsten" in refused.text

    db.query(ClassList).delete()
    db.commit()
    response = post(admin_client, f"/admin/onderwijsteams/{team.id}/verwijderen", {})
    assert response.headers["location"] == "/admin/onderwijsteams?ok=deleted"
    db.expire_all()
    assert db.scalars(select(Team)).all() == []
    assert [e.details["name"] for e in _events(db, "team.deleted")] == ["ICT"]


# Team page -----------------------------------------------------------------------


def test_team_contact_adds_and_removes_members(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    admin: tuple[User, str], team: Team,
) -> None:  # fmt: skip
    tcp = create_user(db, clock, name="Tessa")
    secret = enable_totp(db, services, tcp)
    other_tcp = create_user(db, clock, "tcp2@school.nl", name="Theo")
    colleague = create_user(db, clock, "collega@school.nl", name="Collega")
    services.teams.add_member(db, admin[0], team, tcp.id, contact=True, ip=None)
    services.teams.add_member(db, admin[0], team, other_tcp.id, contact=True, ip=None)
    login(client, secret=secret, clock=clock)

    page = client.get(f"/examens/teams/{team.id}").text
    assert "Collega (collega@school.nl)" in page  # candidate

    response = post(
        client, f"/examens/teams/{team.id}/leden", {"user_id": str(colleague.id), "contact": "true"}
    )
    assert response.status_code == 303
    assert db.get(TeamMember, (team.id, colleague.id)).is_contact is False  # type: ignore[union-attr]

    refused = post(client, f"/examens/teams/{team.id}/leden/{other_tcp.id}/verwijderen", {})
    assert refused.status_code == 400
    assert "Alleen een admin kan een teamcontactpersoon uit het team halen." in refused.text
    assert (
        post(client, f"/examens/teams/{team.id}/leden/{colleague.id}/verwijderen", {}).status_code
        == 303
    )
    db.expire_all()
    assert db.get(TeamMember, (team.id, colleague.id)) is None
    # a TCP cannot appoint team contacts
    assert (
        post(
            client, f"/admin/onderwijsteams/{team.id}/leden/{colleague.id}/tcp", {"contact": "true"}
        ).status_code
        == 403
    )


def test_member_sees_the_team_but_cannot_manage_it(
    client: TestClient, db: Session, clock: FrozenClock, services: Services,
    admin: tuple[User, str], team: Team,
) -> None:  # fmt: skip
    member = create_user(db, clock)
    stranger = create_user(db, clock, "ander@school.nl", name="Ander")
    services.teams.add_member(db, admin[0], team, member.id, ip=None)
    login(client)

    page = client.get(f"/examens/teams/{team.id}").text
    assert "Applicatieontwikkeling" in page
    assert "Account toevoegen" not in page and "Uit team halen" not in page
    assert (
        post(client, f"/examens/teams/{team.id}/leden", {"user_id": str(stranger.id)}).status_code
        == 404
    )
    with pytest.raises(PermissionDeniedError):
        services.teams.add_member(db, member, team, stranger.id, ip=None)

    account = client.get("/account").text
    assert f'href="/examens/teams/{team.id}">ICT<' in account


def test_inactive_accounts_cannot_be_added(
    db: Session, clock: FrozenClock, services: Services, admin: tuple[User, str], team: Team
) -> None:
    user = create_user(db, clock)
    user.is_active = False
    db.commit()
    with pytest.raises(DomainError, match="user_inactive"):
        services.teams.add_member(db, admin[0], team, user.id, ip=None)


# Users overview (B69) ------------------------------------------------------------


def test_users_page_shows_teams_and_accounts_without_team(
    admin_client: TestClient, db: Session, clock: FrozenClock, services: Services,
    admin: tuple[User, str], team: Team,
) -> None:  # fmt: skip
    member = create_user(db, clock, name="Lid van ICT")
    create_user(db, clock, "los@school.nl", name="Zonder Team")
    services.teams.add_member(db, admin[0], team, member.id, contact=True, ip=None)

    page = admin_client.get("/admin/gebruikers").text
    assert f'href="/admin/onderwijsteams/{team.id}">ICT</a> (teamcontactpersoon)' in page

    filtered = admin_client.get("/admin/gebruikers?toon=zonder-team").text
    assert "Zonder Team" in filtered
    assert "Lid van ICT" not in filtered
