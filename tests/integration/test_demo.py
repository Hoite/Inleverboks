from __future__ import annotations

from collections.abc import Iterator

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from inleverboks.adapters.clock import FrozenClock
from inleverboks.config import Settings, get_settings
from inleverboks.db.models import Exam, Submission, SubmissionFile
from inleverboks.services.container import Services
from inleverboks.services.demo import DEMO_TEACHER, seed
from tests.conftest import TEST_SECRET, login


def test_seed_creates_realistic_data(
    app: FastAPI, client: TestClient, db: Session, services: Services, settings: Settings,
    clock: FrozenClock,
) -> None:  # fmt: skip
    result = seed(settings, services.storage, app.state.sessionmaker, clock.now())
    assert result.created
    assert result.password
    db.expire_all()
    exams = {e.slug: e for e in db.scalars(select(Exam))}
    status = {slug: services.exams.status(db, e) for slug, e in exams.items()}
    assert status == {
        "demo-programmeren-p1": "closed",
        "demo-netwerken-p2": "open",
        "demo-databases-p3": "scheduled",
        "demo-concept": "concept",
    }
    submissions = list(db.scalars(select(Submission)))
    assert sorted(s.status for s in submissions).count("cancelled") == 1
    assert len([s for s in submissions if s.status == "active"]) == 6
    assert max(s.attempt for s in submissions) == 2
    for file in db.scalars(select(SubmissionFile)):
        assert file.storage_key is not None
        assert anyio.run(services.storage.exists, file.storage_key)
    # Signals show up in the overview, like real hand-ins.
    closed = exams["demo-programmeren-p1"]
    _checked, issues = services.audit.verify(db)
    assert issues == []
    # The demo teacher can log in with the printed password and sees the exams.
    teacher = TestClient(client.app)
    login(teacher, DEMO_TEACHER, password=result.password)
    page = teacher.get("/examens").text
    assert "Demo: Netwerken P2" in page
    overview = teacher.get(f"/examens/{closed.id}").text
    assert "Identiek aan" in overview
    # Running it again changes nothing.
    assert not seed(settings, services.storage, app.state.sessionmaker, clock.now()).created


@pytest.fixture
def cli_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("STORAGE_PATH", settings.storage_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_requires_demo_mode(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from inleverboks import cli  # noqa: PLC0415

    runner = CliRunner()
    monkeypatch.setenv("DEMO_MODE", "false")
    refused = runner.invoke(cli.app, ["demo-seed"])
    assert refused.exit_code == 2
    assert "DEMO_MODE=true" in refused.output
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    created = runner.invoke(cli.app, ["demo-seed"])
    assert created.exit_code == 0, created.output
    assert "Password (shown once):" in created.stdout
    assert "http://testserver/demo-netwerken-p2" in created.stdout
    again = runner.invoke(cli.app, ["demo-seed"])
    assert "already present" in again.stdout
