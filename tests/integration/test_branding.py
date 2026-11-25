from __future__ import annotations

import io
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent
from inleverboks.services.branding import DEFAULT_COLORS, Branding
from inleverboks.services.container import Services
from inleverboks.web.pdf import PdfRenderer
from tests.conftest import create_user, enable_totp, login, post


@pytest.fixture
def admin_client(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> TestClient:
    admin = create_user(db, clock, "admin@school.nl", name="Beheer", local_admin=True)
    secret = enable_totp(db, services, admin)
    login(client, "admin@school.nl", secret=secret, clock=clock)
    return client


def image(fmt: str = "PNG", size: tuple[int, int] = (300, 100)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (0, 83, 129)).save(buffer, format=fmt)
    return buffer.getvalue()


def colors(**overrides: str) -> dict[str, str]:
    return {f"color_{k}": v for k, v in {**DEFAULT_COLORS, **overrides}.items()}


def events(db: Session, name: str) -> list[AuditEvent]:
    db.expire_all()
    return list(db.scalars(select(AuditEvent).where(AuditEvent.event == name)))


def test_defaults_on_every_page(client: TestClient) -> None:
    page = client.get("/login").text
    assert f'href="/huisstijl/stijl.css?v={Branding().version}"' in page
    assert "placeholder-logo.svg" in page
    css = client.get(f"/huisstijl/stijl.css?v={Branding().version}")
    assert css.headers["content-type"].startswith("text/css")
    assert "immutable" in css.headers["cache-control"]
    assert "--color-primary: #005381;" in css.text
    assert '--font-sans: "Poppins"' in css.text
    # An old or missing version is revalidated, never cached for a year.
    assert client.get("/huisstijl/stijl.css?v=old").headers["cache-control"] == "no-cache"
    assert client.get("/huisstijl/logo.png").status_code == 404


def test_teachers_cannot_open_the_screen(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    login(client)
    assert client.get("/admin/huisstijl").status_code == 403
    assert (
        post(
            client, "/admin/huisstijl", {"app_name": "X", "font": "system", **colors()}
        ).status_code
        == 403
    )


def test_save_changes_name_colours_and_font(admin_client: TestClient, db: Session) -> None:
    page = admin_client.get("/admin/huisstijl").text
    assert "Contrastcontrole (WCAG AA)" in page
    assert "Alle combinaties voldoen aan WCAG AA." in page
    response = post(
        admin_client, "/admin/huisstijl",
        {"app_name": "  Inleveren   ROC ", "font": "system", **colors(primary="#123456")},
    )  # fmt: skip
    assert response.headers["location"] == "/admin/huisstijl?ok=saved"
    page = admin_client.get("/login").text
    assert "<span>Inleveren ROC</span>" in page
    css = admin_client.get("/huisstijl/stijl.css").text
    assert "--color-primary: #123456;" in css
    assert "Poppins" not in css
    changed = events(db, "branding.changed")[0]
    assert changed.details["changes"]["primary"] == {"old": "#005381", "new": "#123456"}
    assert changed.details["changes"]["app_name"]["new"] == "Inleveren ROC"


def test_check_shows_contrast_problems_without_saving(
    admin_client: TestClient, db: Session
) -> None:
    response = post(
        admin_client, "/admin/huisstijl",
        {"app_name": "Inleverboks", "font": "poppins", "action": "check", **colors(primary="#9fc5e8")},
    )  # fmt: skip
    assert response.status_code == 200
    assert "Alleen een voorbeeld" in response.text
    assert "Sommige combinaties zijn slecht leesbaar." in response.text
    assert "Te laag" in response.text
    assert not events(db, "branding.changed")
    assert "#005381" in admin_client.get("/huisstijl/stijl.css").text


def test_low_contrast_can_be_saved_with_warning_in_audit(
    admin_client: TestClient, db: Session
) -> None:
    post(
        admin_client,
        "/admin/huisstijl",
        {"app_name": "X", "font": "poppins", **colors(text="#bbbbbb")},
    )
    warnings = events(db, "branding.changed")[0].details["contrast_warnings"]
    assert warnings == ["body_text", "text_on_tint"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("color_bg", "white"), ("color_text", "#12345g"), ("app_name", ""), ("font", "comic")],
)
def test_invalid_values(admin_client: TestClient, db: Session, field: str, value: str) -> None:
    data = {"app_name": "X", "font": "poppins", **colors(), field: value}
    response = post(admin_client, "/admin/huisstijl", data)
    assert response.status_code == 400
    assert 'aria-invalid="true"' in response.text or "Kies een lettertype." in response.text
    assert not events(db, "branding.changed")


def test_short_hex_is_expanded(admin_client: TestClient) -> None:
    post(
        admin_client,
        "/admin/huisstijl",
        {"app_name": "X", "font": "poppins", **colors(accent="#F00")},
    )
    assert "--color-accent: #ff0000;" in admin_client.get("/huisstijl/stijl.css").text


def test_reset_keeps_logo(admin_client: TestClient, db: Session, services: Services) -> None:
    post(admin_client, "/admin/huisstijl/logo", files={"logo": ("logo.png", image(), "image/png")})
    post(
        admin_client,
        "/admin/huisstijl",
        {"app_name": "Anders", "font": "system", **colors(primary="#222222")},
    )
    assert (
        post(admin_client, "/admin/huisstijl/standaard").headers["location"]
        == "/admin/huisstijl?ok=reset"
    )
    current = services.branding.load(db)
    assert current.app_name == "Inleverboks"
    assert current.colors == DEFAULT_COLORS
    assert current.logo is not None
    assert events(db, "branding.reset")


def test_logo_upload_serve_and_remove(
    admin_client: TestClient, db: Session, services: Services
) -> None:
    response = post(
        admin_client,
        "/admin/huisstijl/logo",
        files={"logo": ("logo.webp", image("WEBP"), "image/webp")},
    )
    assert response.headers["location"] == "/admin/huisstijl?ok=logo"
    brand = services.branding.load(db)
    page = admin_client.get("/examens").text
    assert f'src="/huisstijl/logo.png?v={brand.version}"' in page
    assert f'href="/huisstijl/favicon.png?v={brand.version}"' in page
    logo = admin_client.get(f"/huisstijl/logo.png?v={brand.version}")
    assert logo.headers["content-type"] == "image/png"
    assert logo.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in logo.headers["cache-control"]
    with Image.open(io.BytesIO(logo.content)) as stored:
        assert stored.format == "PNG"
        assert stored.size == (300, 100)
    with Image.open(io.BytesIO(admin_client.get("/huisstijl/favicon.png").content)) as icon:
        assert icon.size == (64, 64)
    assert events(db, "branding.logo_changed")[0].details["sha256"] == brand.logo

    assert (
        post(admin_client, "/admin/huisstijl/logo/verwijderen")
        .headers["location"]
        .endswith("logo_removed")
    )
    assert services.branding.load(db).logo is None
    assert brand.logo_key is not None
    assert not anyio.run(services.storage.exists, brand.logo_key)
    assert admin_client.get("/huisstijl/logo.png").status_code == 404


def test_large_logo_is_scaled_down(
    admin_client: TestClient, db: Session, services: Services
) -> None:
    post(
        admin_client,
        "/admin/huisstijl/logo",
        files={"logo": ("logo.png", image(size=(1600, 400)), "image/png")},
    )
    with Image.open(io.BytesIO(admin_client.get("/huisstijl/logo.png").content)) as stored:
        assert stored.size == (512, 128)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "Kies een logobestand."),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "PNG- of WebP-bestand"),
        (b"GIF89a" + b"\0" * 20, "PNG- of WebP-bestand"),
        (b"\x89PNG\r\n\x1a\n" + b"rubbish" * 10, "kan niet worden gelezen"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * (1024 * 1024), "te groot"),
    ],
)
def test_logo_rejected(admin_client: TestClient, db: Session, data: bytes, message: str) -> None:
    response = post(
        admin_client, "/admin/huisstijl/logo", files={"logo": ("logo.png", data, "image/png")}
    )
    assert response.status_code == 400
    assert message in response.text
    assert not events(db, "branding.logo_changed")


def test_too_many_pixels_rejected(admin_client: TestClient) -> None:
    response = post(
        admin_client,
        "/admin/huisstijl/logo",
        files={"logo": ("l.png", image(size=(2500, 2000)), "image/png")},
    )
    assert response.status_code == 400
    assert "te groot" in response.text


def test_pdf_uses_app_name(app: FastAPI, admin_client: TestClient) -> None:
    post(admin_client, "/admin/huisstijl", {"app_name": "Mijn Boks", "font": "poppins", **colors()})
    seen: dict[str, Any] = {}

    class Spy(PdfRenderer):
        def _render(self, template: str, context: dict[str, Any]) -> bytes:
            seen.update(context)
            return b"%PDF"

    Spy(app.state.templates.env, app.state.brand).render("pdf/receipt.html.j2", {"x": 1})
    assert seen["app_name"] == "Mijn Boks"
    assert seen["x"] == 1
