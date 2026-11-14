"""Background jobs in the app (SCHEDULER_ENABLED) with a database lease lock, so each job
runs once even with several replicas: retention, cleanup and the scheduled backup."""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import JobLock
from inleverboks.services.backup import create_backup, prune_backups
from inleverboks.services.container import Services
from inleverboks.services.pv import RenderPv

LEASE = timedelta(minutes=30)

log = structlog.get_logger(__name__)


def holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class JobLocks:
    def __init__(self, clock: Clock, holder: str) -> None:
        self.clock = clock
        self.holder = holder

    def acquire(self, db: Session, name: str, lease: timedelta = LEASE) -> bool:
        now = self.clock.now()
        if db.scalar(select(JobLock.name).where(JobLock.name == name)) is None:
            try:
                db.add(JobLock(name=name, holder=self.holder, expires_at=now + lease))
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
        result = db.execute(
            update(JobLock)
            .where(
                JobLock.name == name, or_(JobLock.expires_at <= now, JobLock.holder == self.holder)
            )
            .values(holder=self.holder, expires_at=now + lease)
        )
        db.commit()
        return result.rowcount == 1  # type: ignore[attr-defined, no-any-return]

    def release(self, db: Session, name: str) -> None:
        db.execute(
            update(JobLock)
            .where(JobLock.name == name, JobLock.holder == self.holder)
            .values(expires_at=self.clock.now())
        )
        db.commit()


class Jobs:
    """The job bodies; also used by the CLI and the tests."""

    def __init__(
        self, services: Services, make_session: sessionmaker[Session], render_pv: RenderPv
    ) -> None:
        self.services = services
        self.make_session = make_session
        self.render_pv = render_pv
        self.locks = JobLocks(services.clock, holder_id())

    def run_locked(self, name: str, fn: Callable[[Session], object]) -> bool:
        with self.make_session() as db:
            if not self.locks.acquire(db, name):
                log.info("job.skipped_locked", job=name)
                return False
            try:
                fn(db)
            except Exception:
                db.rollback()
                log.exception("job.failed", job=name)
            finally:
                self.locks.release(db, name)
        return True

    def retention(self, db: Session) -> None:
        result = self.services.lifecycle.run(db, self.render_pv)
        if result.finalized or result.failed:
            log.info("job.retention", finalized=len(result.finalized), failed=len(result.failed))

    def cleanup(self, db: Session) -> None:
        result = self.services.maintenance.cleanup(db)
        log.info("job.cleanup", objects=len(result.deleted_objects), tokens=result.deleted_tokens)

    def backup(self, _db: Session) -> None:
        env = self.services.env
        if not env.backup_path:
            return
        target = Path(env.backup_path)
        now = self.services.clock.now()
        result = create_backup(env.database_url, self.services.storage, target, now)
        removed = prune_backups(target, now, env.backup_keep_days)
        log.info("job.backup", path=str(result.path), objects=result.objects, pruned=len(removed))


class AppScheduler:
    def __init__(self, jobs: Jobs) -> None:
        self.jobs = jobs
        self.scheduler = BackgroundScheduler(
            timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1}
        )

    def start(self) -> None:
        env = self.jobs.services.env
        soon = self.jobs.services.clock.now() + timedelta(minutes=1)
        self.scheduler.add_job(
            lambda: self.jobs.run_locked("retention", self.jobs.retention),
            IntervalTrigger(minutes=15),
            id="retention",
            next_run_time=soon,
        )
        self.scheduler.add_job(
            lambda: self.jobs.run_locked("cleanup", self.jobs.cleanup),
            IntervalTrigger(minutes=60),
            id="cleanup",
            next_run_time=soon + timedelta(minutes=1),
        )
        if env.backup_path:
            self.scheduler.add_job(
                lambda: self.jobs.run_locked("backup", self.jobs.backup),
                CronTrigger.from_crontab(env.backup_schedule, timezone=env.app_timezone),
                id="backup",
            )
        self.scheduler.start()
        log.info("scheduler.started", backup=bool(env.backup_path))

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
