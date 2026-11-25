"""The account page: changing your own display name, and the admin marking in the menu."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent, User
from inleverboks.services import branding
from inleverboks.services.container import Services
from tests.conftest import create_user, enable_totp, login, post

CSS = Path(__file__).parents[2] / "src/inleverboks/web/static/css/app.css"


def test_user_changes_own_name(client: TestClient, db: Session, clock: FrozenClock) -> None:
    user = create_user(db, clock, name="Docnet")
    login(client)

    response = post(client, "/account/naam", {"name": "  Hoite   de Docent "})

    assert response.status_code == 303
    assert response.headers["location"] == "/account?ok=name"
    db.refresh(user)
    assert user.name == "Hoite de Docent"
    assert user.email == "docent@school.nl"  # the login does not change
    event = db.scalars(select(AuditEvent).where(AuditEvent.event == "user.renamed")).one()
    assert "Hoite" not in str(event.details)  # ids only in the audit log
    page = client.get("/account?ok=name").text
    assert "Je naam is gewijzigd." in page
    assert 'class="account-name">Hoite de Docent<' in page


def test_new_name_is_offered_as_second_reviewer(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    admin = create_user(db, clock, email="admin@school.nl", name="Admin", admin=True)
    secret = enable_totp(db, services, admin)
    create_user(db, clock)
    login(client, "admin@school.nl", secret=secret, clock=clock)
    post(client, "/account/naam", {"name": "Hoite Prins"})
    client.cookies.clear()
    login(client)

    form = client.get("/examens/nieuw").text
    assert "Hoite Prins (admin@school.nl)" in form
    assert ">Admin (admin@school.nl)<" not in form


def test_unchanged_name_is_not_logged(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    login(client)

    response = post(client, "/account/naam", {"name": "Docent"})

    assert response.headers["location"] == "/account"
    assert db.scalars(select(AuditEvent).where(AuditEvent.event == "user.renamed")).all() == []


def test_empty_or_too_long_name_is_refused(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    user = create_user(db, clock)
    login(client)

    for name in ("   ", "x" * 201):
        response = post(client, "/account/naam", {"name": name})
        assert response.status_code == 400
        assert "Vul je naam in." in response.text

    db.refresh(user)
    assert user.name == "Docent"


def test_rename_needs_csrf_and_login(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock)
    assert client.post("/account/naam", data={"name": "X"}, follow_redirects=False).status_code in {
        303,
        403,
    }
    login(client)
    assert client.post("/account/naam", data={"name": "X"}).status_code == 403
    assert db.scalars(select(User.name)).one() == "Docent"


def test_admin_link_has_a_crown_for_admins_only(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    admin = create_user(db, clock, email="admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    create_user(db, clock)
    login(client, "admin@school.nl", secret=secret, clock=clock)
    page = client.get("/examens").text
    assert re.search(
        r'class="admin-link" href="/admin/gebruikers"[^>]*><svg class="admin-crown"', page
    )
    assert re.search(r'class="admin-tab" href="/examens/alle"[^>]*><svg class="admin-crown"', page)

    client.cookies.clear()
    login(client)
    assert "admin-crown" not in client.get("/examens").text


def test_admin_colours_match_the_contrast_check() -> None:
    css = CSS.read_text()
    assert f"--color-admin: {branding.ADMIN};" in css
    assert f"--color-admin-bg: {branding.ADMIN_BG};" in css
    assert f"--color-admin-text: {branding.ADMIN_TEXT};" in css
    keys = {pair.key for pair in branding.CONTRAST_PAIRS}
    assert {"admin_link", "admin_tabs", "admin_tab_current"} <= keys
