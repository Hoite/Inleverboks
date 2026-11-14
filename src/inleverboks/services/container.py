"""Wires adapters and services together. One instance lives on ``app.state.services``."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from inleverboks.adapters.auth import LocalAuthProvider
from inleverboks.adapters.clock import Clock, SystemClock
from inleverboks.adapters.mail import Mailer, NullMailer
from inleverboks.adapters.ratelimit import MemoryRateLimiter, RateLimiter
from inleverboks.adapters.scanner import FileScanner, NoopScanner
from inleverboks.adapters.storage import StorageBackend
from inleverboks.config import Settings
from inleverboks.services.audit import AuditService
from inleverboks.services.auth import AuthService
from inleverboks.services.classlists import ClassListService
from inleverboks.services.exams import ExamService
from inleverboks.services.pv import ProcesVerbaalService
from inleverboks.services.review import ReviewService
from inleverboks.services.student_flow import StudentFlowService
from inleverboks.services.submissions import SubmissionService
from inleverboks.services.system_settings import SettingsService
from inleverboks.services.totp import TotpService
from inleverboks.services.users import UserService
from inleverboks.services.whitelist import WhitelistService


@dataclass
class Services:
    env: Settings
    clock: Clock
    storage: StorageBackend
    limiter: RateLimiter
    audit: AuditService
    settings: SettingsService
    whitelist: WhitelistService
    totp: TotpService
    auth: AuthService
    users: UserService
    exams: ExamService
    classlists: ClassListService
    flow: StudentFlowService
    submissions: SubmissionService
    review: ReviewService
    pv: ProcesVerbaalService
    mailer: Mailer = field(default_factory=NullMailer)
    scanner: FileScanner = field(default_factory=NoopScanner)
    # Rate-limit delays; tests replace this with a no-op.
    sleep: Callable[[float], None] = time.sleep


def build_services(
    env: Settings,
    storage: StorageBackend,
    *,
    clock: Clock | None = None,
    limiter: RateLimiter | None = None,
) -> Services:
    clock = clock or SystemClock()
    limiter = limiter or MemoryRateLimiter(clock)
    audit = AuditService(clock)
    settings = SettingsService(clock, env)
    whitelist = WhitelistService(clock)
    totp = TotpService(env.secret_key.get_secret_value())
    auth = AuthService(
        clock=clock,
        audit=audit,
        provider=LocalAuthProvider(),
        limiter=limiter,
        whitelist=whitelist,
        settings=settings,
        totp=totp,
    )
    users = UserService(clock=clock, audit=audit, auth=auth, whitelist=whitelist, totp=totp)
    exams = ExamService(clock=clock, audit=audit, settings=settings, env=env)
    flow = StudentFlowService(clock=clock, audit=audit, exams=exams, settings=settings)
    return Services(
        env=env,
        clock=clock,
        storage=storage,
        limiter=limiter,
        audit=audit,
        settings=settings,
        whitelist=whitelist,
        totp=totp,
        auth=auth,
        users=users,
        exams=exams,
        classlists=ClassListService(clock, audit),
        flow=flow,
        submissions=SubmissionService(
            clock=clock,
            audit=audit,
            settings=settings,
            flow=flow,
            secret_key=env.secret_key.get_secret_value(),
        ),
        review=ReviewService(clock=clock, audit=audit),
        pv=ProcesVerbaalService(clock=clock, audit=audit, exams=exams),
    )
