"""Shared fixtures.

Every test gets a freshly migrated database: SQLite tests copy a template file that is
migrated once per session; with ``TEST_DATABASE_URL`` (PostgreSQL) the schema is migrated
once and all tables are truncated before each test.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.config import Settings
from inleverboks.db import migrate
from inleverboks.db.models import User, WhitelistEntry
from inleverboks.db.session import build_engine
from inleverboks.main import create_app
from inleverboks.services.container import Services
from inleverboks.services.passwords import hash_password
from inleverboks.web.csrf import COOKIE_NAME, sign

TEST_SECRET = "test-secret-key-0123456789abcdef0123456789"
PG_URL = os.environ.get("TEST_DATABASE_URL") or None
START = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
PASSWORD = "Correct-Horse-Battery-42"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_base_url": "http://testserver",
        "secret_key": TEST_SECRET,
        "database_url": PG_URL or f"sqlite:///{tmp_path / 'test.db'}",
        "storage_path": str(tmp_path / "files"),
        "session_cookie_secure": False,
        "log_format": "console",
        "log_level": "warning",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# Database ------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> str:
    if PG_URL:
        engine = build_engine(PG_URL)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
        migrate.upgrade(PG_URL)
        return PG_URL
    path = tmp_path_factory.mktemp("template") / "template.db"
    migrate.upgrade(f"sqlite:///{path}")
    return str(path)


@pytest.fixture
def database_url(_migrated_template: str, tmp_path: Path) -> str:
    if PG_URL:
        engine = build_engine(PG_URL)
        tables = [t for t in inspect(engine).get_table_names() if t != "alembic_version"]
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(f'"{t}"' for t in tables)} CASCADE"))
        engine.dispose()
        return PG_URL
    target = tmp_path / "test.db"
    shutil.copyfile(_migrated_template, target)
    return f"sqlite:///{target}"


# App -----------------------------------------------------------------------------


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    return make_settings(tmp_path, database_url=database_url)


@pytest.fixture
def app(settings: Settings, clock: FrozenClock) -> FastAPI:
    application = create_app(settings, clock=clock)
    application.state.services.sleep = lambda _seconds: None
    return application


@pytest.fixture
def services(app: FastAPI) -> Services:
    result: Services = app.state.services
    return result


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(app: FastAPI) -> Iterator[Session]:
    session: Session = app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


# Helpers -------------------------------------------------------------------------


def csrf_token(client: TestClient) -> str:
    if COOKIE_NAME not in client.cookies:
        client.get("/healthz")
    return sign(TEST_SECRET, client.cookies[COOKIE_NAME])


def post(client: TestClient, url: str, data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    payload = {"csrf_token": csrf_token(client), **(data or {})}
    return client.post(url, data=payload, follow_redirects=False, **kwargs)


def add_whitelist(db: Session, value: str, clock: FrozenClock) -> None:
    kind = "email" if "@" in value else "domain"
    db.add(WhitelistEntry(kind=kind, value=value, created_at=clock.now()))
    db.commit()


def create_user(
    db: Session,
    clock: FrozenClock,
    email: str = "docent@school.nl",
    *,
    name: str = "Docent",
    admin: bool = False,
    local_admin: bool = False,
    password: str = PASSWORD,
    whitelist: bool = True,
    **fields: Any,
) -> User:
    if whitelist and "@" in email:
        add_whitelist(db, email, clock)
    user = User(
        email=email,
        name=name,
        password_hash=hash_password(password),
        is_admin=admin or local_admin,
        is_local_admin=local_admin,
        created_at=clock.now(),
        **fields,
    )
    db.add(user)
    db.commit()
    return user


def enable_totp(db: Session, services: Services, user: User) -> str:
    secret = pyotp.random_base32()
    user.totp_secret_enc = services.totp.encrypt(secret)
    user.totp_enabled = True
    db.commit()
    return secret


def totp_code(secret: str, clock: FrozenClock) -> str:
    return pyotp.TOTP(secret).at(clock.now())


def login(
    client: TestClient,
    email: str = "docent@school.nl",
    password: str = PASSWORD,
    *,
    secret: str | None = None,
    clock: FrozenClock | None = None,
) -> Any:
    response = post(client, "/login", {"email": email, "password": password})
    if secret and clock and response.headers.get("location") == "/login/code":
        response = post(client, "/login/code", {"code": totp_code(secret, clock)})
    return response


def add_first_route(app: FastAPI, path: str, endpoint: Any, methods: list[str]) -> None:
    """Register a test-only route before the catch-all ``/{slug}`` student route."""
    app.add_api_route(path, endpoint, methods=methods)
    app.router.routes.insert(0, app.router.routes.pop())


def extract(pattern: str, html: str) -> str:
    match = re.search(pattern, html)
    assert match, f"pattern {pattern!r} not found"
    return match.group(1)
