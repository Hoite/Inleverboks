from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent, Invite, User, UserSession, WhitelistEntry
from inleverboks.main import create_app
from inleverboks.services.container import Services
from inleverboks.services.passwords import verify_password
from tests.conftest import (
    PASSWORD,
    add_whitelist,
    create_user,
    enable_totp,
    extract,
    login,
    make_settings,
    post,
    totp_code,
)

NEW_PASSWORD = "Paarse-Olifant-Fietst-2026"


def events(db: Session, name: str) -> list[AuditEvent]:
    db.expire_all()
    return list(db.scalars(select(AuditEvent).where(AuditEvent.event == name)))


# Bootstrap -----------------------------------------------------------------------


def test_bootstrap_admin_with_generated_password(
    tmp_path: Path,
    database_url: str,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(
        tmp_path, database_url=database_url, admin_email="Beheer@School.nl", log_format="json"
    )
    app = create_app(settings, clock=clock)
    app.state.services.sleep = lambda _s: None
    with TestClient(app) as client:
        output = capsys.readouterr().out
        assert "bootstrap.admin_created" in output
        password = extract(r'"password": "([^"]+)"', output)
        with app.state.sessionmaker() as db:
            admin = db.scalar(select(User))
            assert admin is not None
            assert admin.email == "beheer@school.nl"
            assert admin.is_admin and admin.is_local_admin and admin.must_change_password
            assert verify_password(admin.password_hash, password)

        # Forced: change password, then set up TOTP, then full access.
        response = login(client, "beheer@school.nl", password)
        assert response.headers["location"] == "/account/wachtwoord"
        assert client.get("/examens", follow_redirects=False).headers["location"] == (
            "/account/wachtwoord"
        )
        response = post(
            client,
            "/account/wachtwoord",
            {"current": password, "new": NEW_PASSWORD, "confirm": NEW_PASSWORD},
        )
        assert response.headers["location"] == "/account/tweestaps"
        page = client.get("/account/tweestaps")
        secret = extract(r'<code class="mono secret" data-secret="([A-Z2-7]+)">', page.text)
        secret = secret.replace(" ", "")
        response = post(client, "/account/tweestaps", {"code": "000000"})
        assert response.status_code == 400
        response = post(client, "/account/tweestaps", {"code": pyotp.TOTP(secret).at(clock.now())})
        assert response.status_code == 200
        assert "Herstelcodes" in response.text
        assert client.get("/examens").status_code == 200

    # A second start does not create another admin and logs no password.
    capsys.readouterr()
    with TestClient(create_app(settings, clock=clock)):
        assert "bootstrap.admin_created" not in capsys.readouterr().out


def test_bootstrap_with_given_password(
    tmp_path: Path, database_url: str, clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(
        tmp_path, database_url=database_url, admin_email="a@school.nl", admin_password=PASSWORD
    )
    with TestClient(create_app(settings, clock=clock)) as client:
        assert PASSWORD not in capsys.readouterr().out
        assert login(client, "a@school.nl").headers["location"] == "/account/wachtwoord"


# Login ---------------------------------------------------------------------------


def test_login_and_logout(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/examens"
    cookie = response.headers["set-cookie"]
    assert "ib_session=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert client.get("/examens").status_code == 200
    assert len(events(db, "login.succeeded")) == 1

    post(client, "/logout")
    assert client.get("/examens", follow_redirects=False).status_code == 303
    assert len(events(db, "logout")) == 1


def test_session_cookie_is_secure_by_default(
    tmp_path: Path, database_url: str, clock: FrozenClock
) -> None:
    settings = make_settings(tmp_path, database_url=database_url, session_cookie_secure=True)
    app = create_app(settings, clock=clock)
    with TestClient(app) as client, app.state.sessionmaker() as db:
        create_user(db, clock)
        response = login(client)
        assert "secure" in response.headers["set-cookie"].lower()


def test_wrong_password(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    response = login(client, password="Wrong-Password-123")
    assert response.status_code == 400
    assert "E-mailadres of wachtwoord klopt niet" in response.text
    failed = events(db, "login.failed")
    assert failed[0].details["reason"] == "bad_credentials"
    assert "Wrong-Password" not in str(failed[0].details)


def test_unknown_user_same_message(client: TestClient) -> None:
    response = login(client, "nobody@school.nl")
    assert response.status_code == 400
    assert "E-mailadres of wachtwoord klopt niet" in response.text


def test_login_rate_limit_blocks(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    create_user(db, clock)
    statuses = [login(client, password="Wrong-Password-123").status_code for _ in range(10)]
    assert statuses[:9] == [400] * 9
    assert statuses[9] == 429
    # Even the right password is refused while blocked.
    assert login(client).status_code == 429
    assert any(e.details["reason"] == "rate_limited" for e in events(db, "login.failed"))
    clock.advance(minutes=16)
    assert login(client).status_code == 303


def test_progressive_delay_is_applied(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    create_user(db, clock)
    slept: list[float] = []
    services.sleep = slept.append
    for _ in range(6):
        login(client, password="Wrong-Password-123")
    assert slept == [1.0, 2.0, 4.0]


def test_open_redirect_is_prevented(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    for target in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
        response = post(
            client, "/login", {"email": "docent@school.nl", "password": PASSWORD, "next": target}
        )
        assert response.headers["location"] == "/examens"
        post(client, "/logout")
    response = post(
        client, "/login", {"email": "docent@school.nl", "password": PASSWORD, "next": "/account"}
    )
    assert response.headers["location"] == "/account"


def test_anonymous_is_redirected_to_login(client: TestClient) -> None:
    response = client.get("/admin/gebruikers?x=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fadmin%2Fgebruikers%3Fx%3D1"


# Whitelist -----------------------------------------------------------------------


def test_login_requires_whitelist_every_time(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    user = create_user(db, clock)
    login(client)
    assert client.get("/examens").status_code == 200
    db.query(WhitelistEntry).delete()
    db.commit()
    # Existing session stops working, and a new login is refused.
    assert client.get("/examens", follow_redirects=False).status_code == 303
    response = login(client)
    assert response.status_code == 400
    assert events(db, "login.failed")[-1].details["reason"] == "not_whitelisted"
    # A domain entry also allows the user.
    add_whitelist(db, "school.nl", clock)
    assert login(client).status_code == 303
    assert user.email.endswith("@school.nl")


def test_local_admin_is_exempt_from_whitelist(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    admin = create_user(db, clock, "admin@elders.nl", local_admin=True, whitelist=False)
    secret = enable_totp(db, services, admin)
    response = login(client, "admin@elders.nl", secret=secret, clock=clock)
    assert response.headers["location"] == "/examens"


def test_deactivated_user_cannot_log_in(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock, is_active=False)
    assert login(client).status_code == 400
    assert events(db, "login.failed")[-1].details["reason"] == "inactive"


# Sessions ------------------------------------------------------------------------


def test_idle_timeout(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    login(client)
    clock.advance(hours=1, minutes=59)
    assert client.get("/examens").status_code == 200
    clock.advance(hours=1, minutes=59)
    assert client.get("/examens").status_code == 200
    clock.advance(hours=2)
    assert client.get("/examens", follow_redirects=False).status_code == 303


def test_absolute_timeout(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    login(client)
    for _ in range(12):
        clock.advance(hours=1)
        response = client.get("/examens", follow_redirects=False)
    assert response.status_code == 303


def test_polling_does_not_extend_session(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    login(client)
    for _ in range(12):
        clock.advance(minutes=10)
        assert client.get("/examens", headers={"HX-Trigger": "poll-overview"}).status_code == 200
    clock.advance(minutes=1)
    assert client.get("/examens", follow_redirects=False).status_code == 303


def test_session_token_is_stored_hashed(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    login(client)
    token = client.cookies["ib_session"]
    stored = db.scalar(select(UserSession.token_hash))
    assert stored is not None
    assert token not in stored
    assert len(stored) == 64


# TOTP ----------------------------------------------------------------------------


def test_totp_login(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    user = create_user(db, clock)
    secret = enable_totp(db, services, user)
    response = login(client)
    assert response.headers["location"] == "/login/code"
    # Not yet authenticated.
    assert client.get("/examens", follow_redirects=False).headers["location"] == "/login/code"
    assert post(client, "/login/code", {"code": "123456"}).status_code == 400
    response = post(client, "/login/code", {"code": totp_code(secret, clock)})
    assert response.headers["location"] == "/examens"
    assert client.get("/examens").status_code == 200

    # The same code cannot be used again for a new login.
    post(client, "/logout")
    login(client)
    assert post(client, "/login/code", {"code": totp_code(secret, clock)}).status_code == 400


def test_totp_rate_limit_ends_pending_session(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    user = create_user(db, clock)
    enable_totp(db, services, user)
    login(client)
    for _ in range(7):
        assert post(client, "/login/code", {"code": "000000"}).status_code == 400
    response = post(client, "/login/code", {"code": "000000"})
    assert response.headers["location"] == "/login"
    assert (
        client.get("/login/code", follow_redirects=False).headers["location"].startswith("/login")
    )


def test_recovery_code_works_once(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    create_user(db, clock)
    login(client)
    page = client.get("/account/tweestaps")
    secret = extract(r'<code class="mono secret" data-secret="([A-Z2-7]+)">', page.text).replace(
        " ", ""
    )
    response = post(client, "/account/tweestaps", {"code": totp_code(secret, clock)})
    codes = [c.strip() for c in response.text.split('<ul class="code-list mono">')[1]
             .split("</ul>")[0].replace("<li>", "").split("</li>") if c.strip()]  # fmt: skip
    assert len(codes) == 10
    post(client, "/logout")
    clock.advance(minutes=1)

    login(client)
    assert post(client, "/login/code", {"code": codes[0]}).headers["location"] == "/examens"
    assert events(db, "totp.recovery_code_used")
    post(client, "/logout")
    login(client)
    assert post(client, "/login/code", {"code": codes[0]}).status_code == 400


def test_admin_must_set_up_totp(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock, admin=True)
    assert login(client).headers["location"] == "/account/tweestaps"
    assert client.get("/admin/gebruikers", follow_redirects=False).headers["location"] == (
        "/account/tweestaps"
    )


def test_require_totp_for_everyone(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    create_user(db, clock)
    login(client)
    assert client.get("/examens").status_code == 200
    settings = services.settings.load(db)
    settings.require_totp_all = True
    services.settings.save(db, settings, by=None)
    db.commit()
    # Enforced on the running session as well.
    assert client.get("/examens", follow_redirects=False).headers["location"] == (
        "/account/tweestaps"
    )


def test_disable_totp_only_when_optional(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    user = create_user(db, clock)
    secret = enable_totp(db, services, user)
    login(client, secret=secret, clock=clock)
    response = post(client, "/account/tweestaps/uit", {"password": "wrong"})
    assert response.status_code == 400
    response = post(client, "/account/tweestaps/uit", {"password": PASSWORD})
    assert response.headers["location"] == "/account?ok=totp_disabled"
    db.expire_all()
    assert not db.get(User, user.id).totp_enabled  # type: ignore[union-attr]


# Admin: invitations, resets, roles -----------------------------------------------


@pytest.fixture
def admin_client(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> TestClient:
    admin = create_user(db, clock, "admin@school.nl", name="Beheer", local_admin=True)
    secret = enable_totp(db, services, admin)
    assert login(client, "admin@school.nl", secret=secret, clock=clock).headers["location"] == (
        "/examens"
    )
    return client


def _invite(admin_client: TestClient, email: str) -> str:
    response = post(admin_client, "/admin/uitnodigingen", {"email": email})
    assert response.status_code == 200, response.text
    return extract(r'value="http://testserver(/uitnodiging/[^"]+)"', response.text)


def test_invite_flow(
    admin_client: TestClient, app: object, db: Session, clock: FrozenClock
) -> None:
    add_whitelist(db, "school.nl", clock)
    link = _invite(admin_client, "Nieuw@School.nl")
    invite = db.scalar(select(Invite))
    assert invite is not None
    assert invite.email == "nieuw@school.nl"
    assert link.split("/")[-1] not in invite.token_hash
    assert events(db, "invite.created")

    teacher = TestClient(admin_client.app)
    page = teacher.get(link)
    assert page.status_code == 200
    assert "nieuw@school.nl" in page.text
    response = post(teacher, link, {"name": "Nieuwe Docent", "password": "kort", "confirm": "kort"})
    assert response.status_code == 400
    response = post(
        teacher, link, {"name": "Nieuwe Docent", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert "Je account is aangemaakt" in response.text
    assert events(db, "invite.used")
    # The link works only once.
    assert teacher.get(link).status_code == 404
    assert login(teacher, "nieuw@school.nl", NEW_PASSWORD).headers["location"] == "/examens"


def test_invite_requires_whitelist(admin_client: TestClient, db: Session) -> None:
    response = post(admin_client, "/admin/uitnodigingen", {"email": "iemand@elders.nl"})
    assert response.status_code == 400
    assert "niet op de whitelist" in response.text
    assert db.scalar(select(Invite)) is None


def test_invite_expires_after_seven_days(
    admin_client: TestClient, db: Session, clock: FrozenClock
) -> None:
    add_whitelist(db, "school.nl", clock)
    link = _invite(admin_client, "nieuw@school.nl")
    clock.advance(days=6, hours=23)
    assert admin_client.get(link).status_code == 200
    clock.advance(hours=1)
    assert TestClient(admin_client.app).get(link).status_code == 404


def test_invite_rechecks_whitelist_on_accept(
    admin_client: TestClient, db: Session, clock: FrozenClock
) -> None:
    add_whitelist(db, "school.nl", clock)
    link = _invite(admin_client, "nieuw@school.nl")
    db.query(WhitelistEntry).filter_by(value="school.nl").delete()
    db.commit()
    teacher = TestClient(admin_client.app)
    response = post(teacher, link, {"name": "X", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert response.status_code == 400
    assert db.scalar(select(User).where(User.email == "nieuw@school.nl")) is None


def test_new_invite_revokes_previous(
    admin_client: TestClient, db: Session, clock: FrozenClock
) -> None:
    add_whitelist(db, "school.nl", clock)
    first = _invite(admin_client, "nieuw@school.nl")
    _invite(admin_client, "nieuw@school.nl")
    assert TestClient(admin_client.app).get(first).status_code == 404


def test_password_reset_flow(
    admin_client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    user = create_user(db, clock)
    teacher = TestClient(admin_client.app)
    login(teacher)
    assert teacher.get("/examens").status_code == 200

    response = post(admin_client, f"/admin/gebruikers/{user.id}/reset")
    assert response.status_code == 200
    link = extract(r'value="http://testserver(/reset/[^"]+)"', response.text)
    assert events(db, "password_reset.created")

    anonymous = TestClient(admin_client.app)
    response = post(anonymous, link, {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert response.status_code == 200
    # Old sessions are revoked, the old password no longer works, the link is single-use.
    assert teacher.get("/examens", follow_redirects=False).status_code == 303
    assert login(anonymous).status_code == 400
    assert login(anonymous, password=NEW_PASSWORD).status_code == 303
    assert TestClient(admin_client.app).get(link).status_code == 404


def test_reset_link_expires_after_24_hours(
    admin_client: TestClient, db: Session, clock: FrozenClock
) -> None:
    user = create_user(db, clock)
    response = post(admin_client, f"/admin/gebruikers/{user.id}/reset")
    link = extract(r'value="http://testserver(/reset/[^"]+)"', response.text)
    clock.advance(hours=24)
    assert TestClient(admin_client.app).get(link).status_code == 404


def test_reset_can_clear_totp(
    admin_client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    user = create_user(db, clock)
    enable_totp(db, services, user)
    post(admin_client, f"/admin/gebruikers/{user.id}/reset", {"reset_totp": "true"})
    db.expire_all()
    assert not db.get(User, user.id).totp_enabled  # type: ignore[union-attr]


def test_deactivate_and_activate(admin_client: TestClient, db: Session, clock: FrozenClock) -> None:
    user = create_user(db, clock)
    teacher = TestClient(admin_client.app)
    login(teacher)
    response = post(admin_client, f"/admin/gebruikers/{user.id}/actief")
    assert response.headers["location"] == "/admin/gebruikers?ok=saved"
    assert teacher.get("/examens", follow_redirects=False).status_code == 303
    assert login(teacher).status_code == 400
    post(admin_client, f"/admin/gebruikers/{user.id}/actief", {"active": "true"})
    assert login(teacher).status_code == 303
    assert events(db, "user.deactivated")
    assert events(db, "user.activated")


def test_local_admin_cannot_be_deactivated_or_demoted(
    admin_client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    local_admin = db.scalar(select(User).where(User.is_local_admin.is_(True)))
    assert local_admin is not None
    # The local admin cannot deactivate or demote itself ...
    for url in (
        f"/admin/gebruikers/{local_admin.id}/actief",
        f"/admin/gebruikers/{local_admin.id}/admin",
    ):
        assert post(admin_client, url).status_code == 400
    # ... and neither can another admin.
    other = create_user(db, clock, "admin2@school.nl", admin=True)
    secret = enable_totp(db, services, other)
    other_client = TestClient(admin_client.app)
    login(other_client, "admin2@school.nl", secret=secret, clock=clock)
    response = post(other_client, f"/admin/gebruikers/{local_admin.id}/admin")
    assert response.status_code == 400
    assert "local admin" in response.text
    assert post(other_client, f"/admin/gebruikers/{local_admin.id}/actief").status_code == 400
    db.expire_all()
    db.refresh(local_admin)
    assert local_admin.is_admin
    assert local_admin.is_active
    # The other admin can be demoted (there is still an admin left).
    assert post(admin_client, f"/admin/gebruikers/{other.id}/admin").status_code == 303


def test_last_admin_rule(db: Session, clock: FrozenClock, services: Services) -> None:
    """Without a local admin, the last remaining admin cannot drop its own role."""
    admin = create_user(db, clock, "enige@school.nl", admin=True)
    with pytest.raises(Exception, match="last_admin"):
        services.users.set_admin(db, admin, admin.id, False, ip=None)


def test_admin_can_grant_and_revoke_admin(
    admin_client: TestClient, db: Session, clock: FrozenClock
) -> None:
    user = create_user(db, clock)
    post(admin_client, f"/admin/gebruikers/{user.id}/admin", {"admin": "true"})
    db.expire_all()
    assert db.get(User, user.id).is_admin  # type: ignore[union-attr]
    change = events(db, "user.role_changed")[-1]
    assert change.details == {
        "user_id": str(user.id), "email": user.email, "role": "admin", "old": False, "new": True,
    }  # fmt: skip
    post(admin_client, f"/admin/gebruikers/{user.id}/admin")
    db.expire_all()
    assert not db.get(User, user.id).is_admin  # type: ignore[union-attr]


def test_teacher_cannot_open_admin_pages(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    login(client)
    for url in ("/admin/gebruikers", "/admin/whitelist", "/admin/instellingen", "/admin/auditlog"):
        assert client.get(url).status_code == 403
    user = db.scalar(select(User))
    assert user is not None
    assert post(client, f"/admin/gebruikers/{user.id}/admin", {"admin": "true"}).status_code == 403
    db.expire_all()
    assert not db.get(User, user.id).is_admin  # type: ignore[union-attr]


# Whitelist and settings screens --------------------------------------------------


def test_whitelist_screen(admin_client: TestClient, db: Session) -> None:
    assert post(admin_client, "/admin/whitelist", {"value": "@Alfa-College.nl"}).status_code == 303
    assert post(admin_client, "/admin/whitelist", {"value": "jan@elders.nl"}).status_code == 303
    assert post(admin_client, "/admin/whitelist", {"value": "alfa-college.nl"}).status_code == 400
    assert post(admin_client, "/admin/whitelist", {"value": "geen domein"}).status_code == 400
    values = {(e.kind, e.value) for e in db.scalars(select(WhitelistEntry))}
    assert ("domain", "alfa-college.nl") in values
    assert ("email", "jan@elders.nl") in values
    entry = db.scalar(select(WhitelistEntry).where(WhitelistEntry.value == "jan@elders.nl"))
    assert entry is not None
    post(admin_client, f"/admin/whitelist/{entry.id}/verwijderen")
    assert len(events(db, "whitelist.added")) == 2
    assert len(events(db, "whitelist.removed")) == 1


def test_settings_screen(admin_client: TestClient, db: Session, services: Services) -> None:
    page = admin_client.get("/admin/instellingen")
    assert page.status_code == 200
    data = {
        "retention_days": "90",
        "student_number_regex": r"^\d{6}$",
        "default_max_file_mb": "10",
        "default_max_submission_mb": "50",
        "max_files_per_submission": "10",
        "default_extensions": ".DOCX, pdf py",
    }
    assert post(admin_client, "/admin/instellingen", data).status_code == 303
    loaded = services.settings.load(db)
    assert loaded.retention_days == 90
    assert loaded.default_extensions == ["docx", "pdf", "py"]
    change = events(db, "settings.changed")[-1]
    assert change.details["changes"]["retention_days"] == {"old": 60, "new": 90}

    bad = {**data, "student_number_regex": "([", "default_max_file_mb": "500"}
    response = post(admin_client, "/admin/instellingen", bad)
    assert response.status_code == 400
    assert "geen geldige reguliere expressie" in response.text
    assert services.settings.load(db).student_number_regex == r"^\d{6}$"


def test_system_audit_log_screen(admin_client: TestClient) -> None:
    page = admin_client.get("/admin/auditlog?event=login")
    assert page.status_code == 200
    assert "login.succeeded" in page.text


# CSRF ----------------------------------------------------------------------------


def test_post_without_csrf_token_is_refused(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    response = client.post(
        "/login", data={"email": "docent@school.nl", "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 403
    response = client.post(
        "/login",
        data={"email": "docent@school.nl", "password": PASSWORD, "csrf_token": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_post_from_other_origin_is_refused(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    response = post(
        client,
        "/login",
        {"email": "docent@school.nl", "password": PASSWORD},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_teacher_pages_are_not_cached(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    login(client)
    assert client.get("/examens").headers["cache-control"] == "no-store"


def test_audit_chain_stays_valid_after_flow(
    admin_client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    add_whitelist(db, "school.nl", clock)
    _invite(admin_client, "nieuw@school.nl")
    post(admin_client, "/admin/whitelist", {"value": "elders.nl"})
    checked, issues = services.audit.verify(db)
    assert checked >= 3
    assert issues == []
