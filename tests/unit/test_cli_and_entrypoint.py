from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from typer.testing import CliRunner

from inleverboks import __version__
from inleverboks.cli import app
from inleverboks.config import get_settings
from inleverboks.entrypoint import uvicorn_argv
from tests.conftest import TEST_SECRET

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "cli.db"
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "files"))
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_migrate_creates_database_in_wal_mode(env: Path) -> None:
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert env.exists()
    engine = create_engine(f"sqlite:///{env}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    assert "alembic_version" in inspect(engine).get_table_names()


def test_uvicorn_argv_without_trusted_proxies() -> None:
    argv = uvicorn_argv("")
    assert "--no-proxy-headers" in argv
    assert "--forwarded-allow-ips" not in argv
    assert argv[argv.index("--port") + 1] == "8080"


def test_uvicorn_argv_with_trusted_proxies() -> None:
    argv = uvicorn_argv("10.42.0.0/16")
    assert "--proxy-headers" in argv
    assert argv[argv.index("--forwarded-allow-ips") + 1] == "10.42.0.0/16"


def _password_from(output: str) -> str:
    return output.strip().splitlines()[-1].rsplit(": ", 1)[1]


def test_create_admin_and_reset(env: Path) -> None:
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    result = runner.invoke(app, ["create-admin", "--email", "Beheer@School.nl"])
    assert result.exit_code == 0, result.output
    password = _password_from(result.stdout)
    assert len(password) >= 20

    engine = create_engine(f"sqlite:///{env}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT email, is_admin, is_local_admin, must_change_password FROM app_user")
        ).one()
    assert tuple(row) == ("beheer@school.nl", 1, 1, 1)

    # Existing account without --reset is refused.
    assert runner.invoke(app, ["create-admin", "--email", "beheer@school.nl"]).exit_code == 1
    result = runner.invoke(app, ["create-admin", "--email", "beheer@school.nl", "--reset"])
    assert result.exit_code == 0
    assert _password_from(result.stdout) != password

    # A second admin is not a local admin.
    result = runner.invoke(app, ["create-admin", "--email", "tweede@school.nl"])
    with engine.connect() as conn:
        local = conn.execute(
            text("SELECT is_local_admin FROM app_user WHERE email = 'tweede@school.nl'")
        ).scalar()
    assert local == 0
    assert runner.invoke(app, ["create-admin", "--email", "geen-adres"]).exit_code == 2


def test_audit_verify_cli(env: Path) -> None:
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    runner.invoke(app, ["create-admin", "--email", "a@school.nl"])
    runner.invoke(app, ["create-admin", "--email", "b@school.nl"])
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 0, result.output
    assert "2 events checked" in result.stdout

    engine = create_engine(f"sqlite:///{env}")
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_event_no_update"))
        conn.execute(text("UPDATE audit_event SET actor_label = 'iemand' WHERE seq = 1"))
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 1
    assert "BROKEN chain=system seq=1 problem=hash_mismatch" in result.stdout
