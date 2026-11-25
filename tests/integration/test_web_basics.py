from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.main import create_app
from tests.conftest import add_first_route, create_user, csrf_token, login, make_settings


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_readyz_ok(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "storage": "ok"}


def test_readyz_fails_when_storage_not_writable(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is expected")
    settings = make_settings(tmp_path, storage_path=str(blocker / "files"))
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == "error"


def test_readyz_fails_when_database_unreachable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database_url="postgresql://nobody:nothing@127.0.0.1:1/none")
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]
    assert "strict-transport-security" not in response.headers


def test_hsts_when_enabled(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, hsts_enabled=True))) as client:
        response = client.get("/healthz")
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_index_is_dutch(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert '<html lang="nl">' in response.text
    assert "Examenwerk inleveren" in response.text
    assert "Inloggen docent" in response.text


def test_home_page_knows_the_session(client: TestClient, db: Session, clock: FrozenClock) -> None:
    create_user(db, clock, name="Hoite de Docent")
    login(client)
    page = client.get("/").text
    # Signed in: the normal header with the account menu, no login link.
    assert "Inloggen docent" not in page
    assert "Examenwerk inleveren" in page  # the student explanation stays
    assert '<a class="button" href="/examens">Naar mijn examens</a>' in page
    assert '<a href="/examens">Examens</a>' in page
    assert '<details class="account-menu" data-menu>' in page
    assert "Hoite de Docent" in page
    assert "Accountmenu" in page
    assert 'action="/logout"' in page
    assert page.count('class="brand" href="/"') == 1  # the logo always goes home


def test_account_menu_marks_the_account_page(
    client: TestClient, db: Session, clock: FrozenClock
) -> None:
    create_user(db, clock)
    login(client)
    exams = client.get("/examens").text
    assert '<a class="brand" href="/">' in exams
    assert '<a href="/examens" aria-current="page">Examens</a>' in exams
    assert '<a href="/account">Account</a>' in exams
    account = client.get("/account").text
    assert '<a href="/account" aria-current="page">Account</a>' in account
    assert '<summary class="is-current">' in account
    assert "js/menu.js" in account


def test_demo_banner(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, demo_mode=True))) as client:
        assert "Demo-omgeving" in client.get("/").text


def test_404_html_page(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "Pagina niet gevonden" in response.text


def test_static_files_served(client: TestClient) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert "--color-primary: #005381" in response.text


def _echo_app(tmp_path: Path) -> tuple[TestClient, int]:
    settings = make_settings(tmp_path, max_file_mb=1, max_submission_mb=1)
    app = create_app(settings)

    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    add_first_route(app, "/_echo", echo, ["POST"])

    return TestClient(app), settings.max_request_bytes


def test_body_within_limit_accepted(tmp_path: Path) -> None:
    client, limit = _echo_app(tmp_path)
    with client:
        headers = {"x-csrf-token": csrf_token(client)}
        response = client.post("/_echo", content=b"x" * limit, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"size": limit}


def test_body_limit_by_content_length(tmp_path: Path) -> None:
    client, limit = _echo_app(tmp_path)
    with client:
        response = client.post("/_echo", content=b"x" * (limit + 1))
    assert response.status_code == 413


def test_body_limit_when_streamed_without_length(tmp_path: Path) -> None:
    client, _limit = _echo_app(tmp_path)

    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b"x" * (1024 * 1024)

    with client:
        headers = {"x-csrf-token": csrf_token(client)}
        response = client.post("/_echo", content=chunks(), headers=headers)
    assert response.status_code == 413


def _client_ip_app(tmp_path: Path, trusted: str) -> TestClient:
    app = create_app(make_settings(tmp_path, trusted_proxies=trusted))

    async def ip(request: Request) -> dict[str, str | None]:
        return {"ip": request.client.host if request.client else None}

    add_first_route(app, "/_ip", ip, ["GET"])

    return TestClient(app, client=("10.42.0.7", 50000))


def test_forwarded_for_ignored_without_trusted_proxies(tmp_path: Path) -> None:
    with _client_ip_app(tmp_path, "") as client:
        response = client.get("/_ip", headers={"x-forwarded-for": "203.0.113.9"})
    assert response.json()["ip"] == "10.42.0.7"


def test_forwarded_for_used_from_trusted_proxy(tmp_path: Path) -> None:
    with _client_ip_app(tmp_path, "10.42.0.0/16") as client:
        response = client.get("/_ip", headers={"x-forwarded-for": "203.0.113.9"})
    assert response.json()["ip"] == "203.0.113.9"


def test_forwarded_for_spoofed_hop_ignored(tmp_path: Path) -> None:
    # The client prepends a fake address; only the hop added by the trusted proxy counts.
    with _client_ip_app(tmp_path, "10.42.0.0/16") as client:
        response = client.get("/_ip", headers={"x-forwarded-for": "1.2.3.4, 203.0.113.9"})
    assert response.json()["ip"] == "203.0.113.9"
