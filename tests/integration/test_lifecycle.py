from __future__ import annotations

import os
import tarfile
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from inleverboks.adapters.clock import FrozenClock
from inleverboks.adapters.storage import LocalStorage
from inleverboks.config import Settings, get_settings
from inleverboks.db.models import (
    AuditEvent,
    Exam,
    ExamRosterEntry,
    ExtensionGrant,
    ProcesVerbaal,
    ReceiptToken,
    RetiredSlug,
    Submission,
    SubmissionFile,
    User,
)
from inleverboks.services.backup import create_backup, prune_backups, restore_backup
from inleverboks.services.container import Services
from inleverboks.services.scheduler import AppScheduler, JobLocks, Jobs
from tests.conftest import START, TEST_SECRET, create_user, enable_totp, login
from tests.student_helpers import add_roster, form, good_files, ready, upload

AMS_MIDNIGHT_13_JAN = datetime(2027, 1, 12, 23, 0, tzinfo=UTC)  # 13 Jan 2027 00:00 Amsterdam


def events(db: Session, name: str, chain: str | None = None) -> list[AuditEvent]:
    db.expire_all()
    query = select(AuditEvent).where(AuditEvent.event == name)
    if chain is not None:
        query = query.where(AuditEvent.chain == chain)
    return list(db.scalars(query.order_by(AuditEvent.seq)))


def fake_render(context: dict[str, Any]) -> bytes:
    return b"%PDF-1.7 " + str(context["document_id"]).encode()


def stored(services: Services, prefix: str = "") -> list[str]:
    async def collect() -> list[str]:
        return [k async for k in services.storage.list_keys(prefix)]

    return anyio.run(collect)


@pytest.fixture
def handed_in(client: TestClient, db: Session, exam: Exam, owner: User) -> Exam:
    """Two submissions: one active, one cancelled (its files must also be deleted)."""
    add_roster(db, exam)
    for number in ("123456", "999999"):
        student = TestClient(client.app)
        ready(student, exam, number)
        assert upload(student, exam, good_files(exam)).status_code == 200
    cancelled = db.scalar(select(Submission).where(Submission.student_number == "999999"))
    assert cancelled is not None
    cancelled.status = "cancelled"
    db.commit()
    return exam


# Retention -----------------------------------------------------------------------


def test_retention_dates_and_due(
    db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    info = services.lifecycle.retention(db, handed_in)
    assert info.due == AMS_MIDNIGHT_13_JAN
    assert info.warn_from == AMS_MIDNIGHT_13_JAN - timedelta(days=7)
    assert not info.warning(datetime(2027, 1, 5, 22, 59, tzinfo=UTC))
    assert info.warning(datetime(2027, 1, 5, 23, 0, tzinfo=UTC))  # 6 Jan 00:00 Amsterdam
    assert services.lifecycle.due_exams(db, AMS_MIDNIGHT_13_JAN - timedelta(seconds=1)) == []
    assert [e.id for e in services.lifecycle.due_exams(db, AMS_MIDNIGHT_13_JAN)] == [handed_in.id]


def test_extension_moves_retention(
    db: Session, services: Services, handed_in: Exam, owner: User
) -> None:
    db.add(
        ExtensionGrant(
            exam_id=handed_in.id,
            student_number="123457",
            ends_at=datetime(2026, 11, 14, 8, 0, tzinfo=UTC),  # 14 Nov
            created_by_id=owner.id,
            created_at=START,
        )
    )
    db.commit()
    assert services.lifecycle.retention(db, handed_in).due == AMS_MIDNIGHT_13_JAN + timedelta(
        days=1
    )


def test_finalize_deletes_all_files_after_final_pv(
    client: TestClient, db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    keys = list(db.scalars(select(SubmissionFile.storage_key)))
    assert len(keys) == 4
    clock.set(AMS_MIDNIGHT_13_JAN)
    result = services.lifecycle.run(db, fake_render)
    assert result.finalized == [handed_in.id]
    db.expire_all()
    exam = db.get(Exam, handed_in.id)
    assert exam is not None
    assert exam.finalized_at == AMS_MIDNIGHT_13_JAN
    assert services.exams.status(db, exam) == "finalized"
    # All files are gone (cancelled attempt included); metadata stays.
    assert all(f.storage_key is None and f.deleted_at for f in db.scalars(select(SubmissionFile)))
    assert stored(services, str(exam.id)) == []
    assert not db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id)).all()
    assert len(db.scalars(select(Submission)).all()) == 2
    # The final PV was made first, with an integrity check, and it is kept.
    pv = db.scalar(select(ProcesVerbaal).where(ProcesVerbaal.is_final.is_(True)))
    assert pv is not None
    assert anyio.run(services.storage.exists, pv.storage_key)
    order = [
        e.event
        for e in db.scalars(
            select(AuditEvent).where(AuditEvent.chain == str(exam.id)).order_by(AuditEvent.seq)
        )
    ]
    assert (
        order.index("export.pv") < order.index("retention.deleted") < order.index("exam.finalized")
    )
    assert events(db, "export.pv")[0].details["checked"] == 4
    assert events(db, "retention.deleted")[0].details["files"] == 4
    # A second run does nothing.
    assert services.lifecycle.run(db, fake_render).finalized == []


def test_failed_pv_deletes_nothing(
    db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    def broken(context: dict[str, Any]) -> bytes:
        raise RuntimeError("renderer down")

    clock.set(AMS_MIDNIGHT_13_JAN)
    result = services.lifecycle.run(db, broken)
    assert result.failed == [handed_in.id]
    db.expire_all()
    assert all(f.storage_key for f in db.scalars(select(SubmissionFile)))
    assert db.get(Exam, handed_in.id).finalized_at is None  # type: ignore[union-attr]
    assert services.lifecycle.run(db, fake_render).finalized == [handed_in.id]


def test_finalized_exam_pages(
    client: TestClient, db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    clock.set(AMS_MIDNIGHT_13_JAN)
    services.lifecycle.run(db, fake_render)
    teacher = TestClient(client.app)
    login(teacher)
    assert handed_in.name in teacher.get("/examens/afgerond").text
    assert handed_in.name not in teacher.get("/examens").text
    page = teacher.get(f"/examens/{handed_in.id}").text
    assert "Dit examen is afgerond" in page
    assert "Alles downloaden" not in page
    pv = db.scalar(select(ProcesVerbaal))
    assert pv is not None
    assert pv.document_id in page
    download = teacher.get(f"/examens/{handed_in.id}/proces-verbalen/{pv.id}")
    assert download.content.startswith(b"%PDF")
    assert download.headers["content-disposition"].startswith("attachment;")


def test_dashboard_warns_seven_days_before(
    client: TestClient, db: Session, handed_in: Exam, clock: FrozenClock
) -> None:
    teacher = TestClient(client.app)
    clock.set(datetime(2027, 1, 5, 22, 0, tzinfo=UTC))
    login(teacher)
    assert "Bestanden worden verwijderd op" not in teacher.get("/examens").text
    clock.set(datetime(2027, 1, 6, 9, 0, tzinfo=UTC))
    login(teacher)
    page = teacher.get("/examens").text
    assert "Bestanden worden verwijderd op 13-01-2027" in page


# Hold flag -----------------------------------------------------------------------


def test_hold_blocks_deletion_and_grace_after_removal(
    client: TestClient, db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    teacher = TestClient(client.app)
    login(teacher)
    url = f"/examens/{handed_in.id}/bewaren"
    assert "error=reason_required" in form(teacher, url, {"reason": ""}).headers["location"]
    location = form(teacher, url, {"reason": "Bezwaar ingediend"}).headers["location"]
    assert location == f"/examens/{handed_in.id}/geavanceerd?ok=hold_set#bewaartermijn"
    assert "De bewaren-vlag staat aan" in teacher.get(location).text
    assert events(db, "exam.hold_set")[0].details["reason"] == "Bezwaar ingediend"

    clock.set(AMS_MIDNIGHT_13_JAN + timedelta(days=30))
    assert services.lifecycle.run(db, fake_render).finalized == []
    login(teacher)
    assert (
        "ok=hold_removed"
        in form(teacher, url, {"reason": "Bezwaar afgehandeld", "remove": "true"}).headers[
            "location"
        ]
    )
    removed = events(db, "exam.hold_removed")[0]
    assert removed.details["hold_reason"] == "Bezwaar ingediend"
    # At the earliest 7 days later.
    clock.advance(days=6, hours=23)
    assert services.lifecycle.run(db, fake_render).finalized == []
    clock.advance(hours=1)
    assert services.lifecycle.run(db, fake_render).finalized == [handed_in.id]


def test_hold_permissions(
    client: TestClient, db: Session, clock: FrozenClock, services: Services, handed_in: Exam
) -> None:
    reviewer = create_user(db, clock, "tweede@school.nl")
    handed_in.second_reviewer_id = reviewer.id
    db.commit()
    rc = TestClient(client.app)
    login(rc, "tweede@school.nl")
    assert form(rc, f"/examens/{handed_in.id}/bewaren", {"reason": "x"}).status_code == 403
    admin = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    ac = TestClient(client.app)
    login(ac, "admin@school.nl", secret=secret, clock=clock)
    assert (
        "ok=hold_set"
        in form(ac, f"/examens/{handed_in.id}/bewaren", {"reason": "Beroep"}).headers["location"]
    )


# Permanent deletion --------------------------------------------------------------


def test_permanent_deletion_by_admin(
    client: TestClient, db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    admin = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    ac = TestClient(client.app)
    login(ac, "admin@school.nl", secret=secret, clock=clock)
    exam_id, slug = handed_in.id, handed_in.slug
    url = f"/examens/{exam_id}/definitief-verwijderen"
    # Not before it is finalized.
    assert "error=not_finalized" in form(ac, url, {"reason": "x"}).headers["location"]
    clock.set(AMS_MIDNIGHT_13_JAN)
    services.lifecycle.run(db, fake_render)
    login(ac, "admin@school.nl", secret=secret, clock=clock)
    teacher = TestClient(client.app)
    login(teacher)
    assert form(teacher, url, {"reason": "x"}).status_code == 403
    assert "error=reason_required" in form(ac, url, {"reason": ""}).headers["location"]
    response = form(ac, url, {"reason": "Termijn voor bezwaar verstreken"})
    assert response.headers["location"] == "/examens/afgerond?ok=deleted"
    db.expire_all()
    assert db.get(Exam, exam_id) is None
    assert not db.scalars(select(Submission)).all()
    assert not db.scalars(select(ProcesVerbaal)).all()
    assert stored(services, "pv") == []
    assert db.get(RetiredSlug, slug) is not None
    system = events(db, "exam.deleted_permanently", "system")
    assert system[0].details["reason"] == "Termijn voor bezwaar verstreken"
    assert events(db, "exam.created", str(exam_id))  # the exam's own log remains
    _checked, issues = services.audit.verify(db)
    assert issues == []


# Cleanup -------------------------------------------------------------------------


def _age(settings: Settings, key: str, hours: float) -> None:
    path = Path(settings.storage_path) / key
    stamp = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def _put(services: Services, key: str, data: bytes = b"x") -> None:
    async def run() -> None:
        async def chunks() -> AsyncIterator[bytes]:
            yield data

        await services.storage.put(key, chunks())

    anyio.run(run)


def test_cleanup_removes_only_old_orphans(
    db: Session, services: Services, settings: Settings, handed_in: Exam, clock: FrozenClock
) -> None:
    clock.set(datetime.now(UTC))  # the cleanup compares with real modification times
    exam_id = handed_in.id
    _put(services, "tmp/upload-old")
    _put(services, "tmp/upload-new")
    orphan = f"{exam_id}/{uuid.uuid4()}/{uuid.uuid4().hex}"
    _put(services, orphan)
    _put(services, "templates/" + "f" * 64)
    _put(services, "pv/somewhere/doc.pdf")
    for key in ("tmp/upload-old", orphan, "templates/" + "f" * 64, "pv/somewhere/doc.pdf"):
        _age(settings, key, 2)
    referenced = list(db.scalars(select(SubmissionFile.storage_key)))
    for ref in referenced:
        _age(settings, ref or "", 2)
    db.add(
        ReceiptToken(
            submission_id=db.scalar(select(Submission.id)),
            token_hash="z" * 64,
            expires_at=clock.now() - timedelta(days=2),
        )
    )
    db.commit()
    result = services.maintenance.cleanup(db)
    assert sorted(result.deleted_objects) == sorted(
        ["tmp/upload-old", orphan, "templates/" + "f" * 64]
    )
    remaining = stored(services)
    assert "tmp/upload-new" in remaining
    assert "pv/somewhere/doc.pdf" in remaining
    assert all(key in remaining for key in referenced)
    assert result.deleted_tokens >= 1


# Integrity -----------------------------------------------------------------------


def test_integrity_check(
    db: Session, services: Services, settings: Settings, handed_in: Exam
) -> None:
    report = services.maintenance.integrity_check(db)
    assert report.checked == 4
    assert not report.mismatches
    key = db.scalar(select(SubmissionFile.storage_key).limit(1))
    assert key is not None
    (Path(settings.storage_path) / key).write_bytes(b"veranderd")
    report = services.maintenance.integrity_check(db, handed_in.id)
    assert len(report.mismatches) == 1
    mismatch = events(db, "integrity.mismatch", str(handed_in.id))
    assert mismatch[0].details["during"] == "integrity_check"


# Backup and restore --------------------------------------------------------------


def test_backup_prune_and_restore(
    db: Session,
    services: Services,
    settings: Settings,
    handed_in: Exam,
    tmp_path: Path,
    clock: FrozenClock,
) -> None:
    target = tmp_path / "backups"
    _put(services, "tmp/upload-skip")
    result = create_backup(settings.database_url, services.storage, target, clock.now())
    assert result.path.name == "inleverboks-backup-20261113-090000.tar.gz"
    with tarfile.open(result.path) as tar:
        names = tar.getnames()
    assert "backup.json" in names
    assert not any(n.startswith("files/tmp/") for n in names)
    keys = [k for k in stored(services) if not k.startswith("tmp/")]
    assert sorted(n.removeprefix("files/") for n in names if n.startswith("files/")) == sorted(keys)
    if settings.is_sqlite:
        assert "database/inleverboks.db" in names
    else:
        assert "database/README.txt" in names

    # Keep at most BACKUP_KEEP_DAYS.
    old = target / "inleverboks-backup-20261101-020000.tar.gz"
    old.write_bytes(b"old")
    assert prune_backups(target, clock.now(), 7) == [old]
    assert result.path.exists()

    # Restore into an empty storage (and SQLite file).
    fresh_storage = LocalStorage(tmp_path / "restored-files")
    fresh_db = f"sqlite:///{tmp_path / 'restored.db'}"
    restored = restore_backup(result.path, fresh_db, fresh_storage)
    assert restored.objects == len(keys)
    assert restored.database_restored is settings.is_sqlite
    one = keys[0]
    assert anyio.run(fresh_storage.exists, one)


def test_restore_ignores_unsafe_paths(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("files/../../escape")
        info.size = len(data)
        import io  # noqa: PLC0415

        tar.addfile(info, io.BytesIO(data))
    storage = LocalStorage(tmp_path / "files")
    result = restore_backup(archive, f"sqlite:///{tmp_path / 'x.db'}", storage)
    assert result.objects == 0
    assert not (tmp_path / "escape").exists()


# Scheduler -----------------------------------------------------------------------


def test_job_lock_lease(db: Session, clock: FrozenClock) -> None:
    first, second = JobLocks(clock, "pod-a"), JobLocks(clock, "pod-b")
    assert first.acquire(db, "retention")
    assert not second.acquire(db, "retention")
    clock.advance(minutes=31)  # lease expired (crashed pod)
    assert second.acquire(db, "retention")
    second.release(db, "retention")
    assert first.acquire(db, "retention")


def test_jobs_run_locked(
    app: FastAPI, db: Session, services: Services, handed_in: Exam, clock: FrozenClock
) -> None:
    jobs = Jobs(services, app.state.sessionmaker, fake_render)
    clock.set(AMS_MIDNIGHT_13_JAN)
    other = JobLocks(clock, "another-pod")
    assert other.acquire(db, "retention")
    assert jobs.run_locked("retention", jobs.retention) is False  # someone else holds the lock
    other.release(db, "retention")
    assert jobs.run_locked("retention", jobs.retention) is True
    db.expire_all()
    assert db.get(Exam, handed_in.id).finalized_at is not None  # type: ignore[union-attr]


def test_scheduler_starts_and_stops(app: FastAPI, services: Services, tmp_path: Path) -> None:
    services.env.backup_path = str(tmp_path / "b")
    scheduler = AppScheduler(Jobs(services, app.state.sessionmaker, fake_render))
    scheduler.start()
    try:
        assert {job.id for job in scheduler.scheduler.get_jobs()} == {
            "retention",
            "cleanup",
            "backup",
        }
    finally:
        scheduler.shutdown()
        services.env.backup_path = None


# CLI -----------------------------------------------------------------------------


@pytest.fixture
def cli_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("STORAGE_PATH", settings.storage_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_retention_integrity_backup(cli_env: None, handed_in: Exam, tmp_path: Path) -> None:
    from inleverboks import cli  # noqa: PLC0415

    runner = CliRunner()
    result = runner.invoke(cli.app, ["retention", "run", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "0 exam(s) due." in result.stdout  # the CLI uses the real clock (2026), not yet due
    result = runner.invoke(cli.app, ["integrity", "check"])
    assert result.exit_code == 0, result.output
    assert "4 files checked, 0 mismatch(es)." in result.stdout
    result = runner.invoke(cli.app, ["backup", "--to", str(tmp_path / "bk")])
    assert result.exit_code == 0, result.output
    assert "Backup written" in result.stdout
    archive = next((tmp_path / "bk").glob("*.tar.gz"))
    assert runner.invoke(cli.app, ["restore", str(archive)]).exit_code == 2  # needs --yes
    result = runner.invoke(cli.app, ["cleanup", "run"])
    assert result.exit_code == 0, result.output
