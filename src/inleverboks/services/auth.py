"""Login, server-side sessions and second-factor checks for teachers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from inleverboks.adapters.auth import AuthProvider
from inleverboks.adapters.clock import Clock
from inleverboks.adapters.ratelimit import RateLimiter
from inleverboks.db.models import RecoveryCode, User, UserSession
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.passwords import hash_password, needs_rehash
from inleverboks.services.security import hash_token, new_token
from inleverboks.services.system_settings import SettingsService
from inleverboks.services.totp import TotpService
from inleverboks.services.whitelist import WhitelistService, normalize_email

IDLE_TIMEOUT = timedelta(hours=2)
ABSOLUTE_TIMEOUT = timedelta(hours=12)
PENDING_TIMEOUT = timedelta(minutes=10)  # sessions still waiting for a TOTP code


class Stage(StrEnum):
    MFA = "mfa"
    CHANGE_PASSWORD = "change_password"  # noqa: S105 - a stage name, not a password
    SETUP_TOTP = "setup_totp"
    FULL = "full"


class LoginOutcome(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class LoginResult:
    outcome: LoginOutcome
    token: str | None = None
    stage: Stage | None = None
    delay: float = 0.0
    retry_after: float = 0.0


@dataclass(frozen=True)
class AuthContext:
    session: UserSession
    user: User

    @property
    def stage(self) -> Stage:
        return Stage(self.session.stage)


class AuthService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditService,
        provider: AuthProvider,
        limiter: RateLimiter,
        whitelist: WhitelistService,
        settings: SettingsService,
        totp: TotpService,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.provider = provider
        self.limiter = limiter
        self.whitelist = whitelist
        self.settings = settings
        self.totp = totp

    # Stages ------------------------------------------------------------------------

    def totp_required(self, db: Session, user: User) -> bool:
        return user.is_admin or self.settings.load(db).require_totp_all

    def next_stage(self, db: Session, user: User) -> Stage:
        if user.must_change_password:
            return Stage.CHANGE_PASSWORD
        if self.totp_required(db, user) and not user.totp_enabled:
            return Stage.SETUP_TOTP
        return Stage.FULL

    def may_log_in(self, db: Session, user: User) -> bool:
        """Active, and on the whitelist. The local admin is exempt from the whitelist so
        the installation can never lock itself out."""
        if not user.is_active:
            return False
        return user.is_local_admin or self.whitelist.is_allowed(db, user.email)

    # Login -------------------------------------------------------------------------

    def login(
        self, db: Session, email: str, password: str, *, ip: str | None, user_agent: str | None
    ) -> LoginResult:
        email = normalize_email(email)
        ip_key = ip or "unknown"
        for rule, key in (("login", email), ("login_ip", ip_key)):
            status = self.limiter.status(rule, key)
            if status.blocked:
                self.audit.append(
                    db,
                    "login.failed",
                    actor=Actor.anonymous(),
                    ip=ip,
                    details={"email": email, "reason": "rate_limited"},
                )
                db.commit()
                return LoginResult(LoginOutcome.BLOCKED, retry_after=status.retry_after)

        user = self.provider.authenticate(db, email, password)
        reason = None
        if user is None:
            reason = "bad_credentials"
        elif not user.is_active:
            reason = "inactive"
        elif not self.may_log_in(db, user):
            reason = "not_whitelisted"
        if reason is not None or user is None:
            self.limiter.fail("login_ip", ip_key)
            status = self.limiter.fail("login", email)
            self.audit.append(
                db,
                "login.failed",
                actor=Actor.for_user(user) if user else Actor.anonymous(),
                ip=ip,
                details={"email": email, "reason": reason},
            )
            db.commit()
            if status.blocked:
                return LoginResult(LoginOutcome.BLOCKED, retry_after=status.retry_after)
            return LoginResult(LoginOutcome.INVALID, delay=status.delay)

        self.limiter.reset("login", email)
        if user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        stage = Stage.MFA if user.totp_enabled else self.next_stage(db, user)
        token = self._create_session(db, user, stage, ip=ip, user_agent=user_agent)
        if stage is not Stage.MFA:
            self._complete_login(db, user, ip)
        db.commit()
        return LoginResult(LoginOutcome.OK, token=token, stage=stage)

    def _complete_login(self, db: Session, user: User, ip: str | None) -> None:
        user.last_login_at = self.clock.now()
        self.audit.append(db, "login.succeeded", actor=Actor.for_user(user), ip=ip)

    def verify_second_factor(
        self, db: Session, ctx: AuthContext, code: str, *, ip: str | None
    ) -> LoginResult:
        """Check a TOTP code or a recovery code for a session in the MFA stage."""
        user = ctx.user
        key = str(user.id)
        status = self.limiter.status("totp", key)
        if status.blocked:
            return LoginResult(LoginOutcome.BLOCKED, retry_after=status.retry_after)

        ok = False
        method = "totp"
        if user.totp_secret_enc:
            check = self.totp.verify(
                self.totp.decrypt(user.totp_secret_enc),
                code,
                now=self.clock.now().timestamp(),
                last_step=user.totp_last_step,
            )
            if check.ok:
                ok = True
                user.totp_last_step = check.step
        compact = code.strip().replace("-", "").replace(" ", "")
        if not ok and len(compact) == 8:
            method = "recovery_code"
            ok = self._use_recovery_code(db, user, code)

        if not ok:
            status = self.limiter.fail("totp", key)
            self.audit.append(
                db,
                "login.failed",
                actor=Actor.for_user(user),
                ip=ip,
                details={"email": user.email, "reason": "bad_second_factor"},
            )
            db.commit()
            if status.blocked:
                self.revoke(db, ctx.session)
                db.commit()
                return LoginResult(LoginOutcome.BLOCKED, retry_after=status.retry_after)
            return LoginResult(LoginOutcome.INVALID, delay=status.delay)

        self.limiter.reset("totp", key)
        if not self.may_log_in(db, user):
            self.revoke(db, ctx.session)
            db.commit()
            return LoginResult(LoginOutcome.INVALID)
        # Rotate the session token once fully authenticated.
        self.revoke(db, ctx.session)
        stage = self.next_stage(db, user)
        token = self._create_session(db, user, stage, ip=ip, user_agent=ctx.session.user_agent)
        if method == "recovery_code":
            self.audit.append(db, "totp.recovery_code_used", actor=Actor.for_user(user), ip=ip)
        self._complete_login(db, user, ip)
        db.commit()
        return LoginResult(LoginOutcome.OK, token=token, stage=stage)

    def _use_recovery_code(self, db: Session, user: User, code: str) -> bool:
        code_hash = self.totp.hash_recovery_code(code)
        row = db.scalar(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id,
                RecoveryCode.code_hash == code_hash,
                RecoveryCode.used_at.is_(None),
            )
        )
        if row is None:
            return False
        row.used_at = self.clock.now()
        return True

    # Sessions ----------------------------------------------------------------------

    def _create_session(
        self,
        db: Session,
        user: User,
        stage: Stage,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> str:
        token = new_token()
        now = self.clock.now()
        lifetime = PENDING_TIMEOUT if stage is Stage.MFA else ABSOLUTE_TIMEOUT
        db.add(
            UserSession(
                token_hash=hash_token(token),
                user_id=user.id,
                stage=stage.value,
                csrf_token=new_token(16),
                created_at=now,
                last_seen_at=now,
                expires_at=now + lifetime,
                ip=ip,
                user_agent=(user_agent or "")[:300] or None,
            )
        )
        return token

    def load(self, db: Session, token: str | None, *, touch: bool = True) -> AuthContext | None:
        """Resolve a session cookie. Expired, revoked or no-longer-allowed sessions are
        rejected (and revoked). ``touch=False`` for background polling, so polling never
        keeps an idle session alive."""
        if not token:
            return None
        session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        if session is None or session.revoked_at is not None:
            return None
        now = self.clock.now()
        user = session.user
        if (
            now >= session.expires_at
            or now - session.last_seen_at >= IDLE_TIMEOUT
            or not self.may_log_in(db, user)
        ):
            session.revoked_at = now
            db.commit()
            return None
        if session.stage == Stage.FULL:
            # Requirements can change mid-session (admin role granted, TOTP made
            # mandatory for everyone): enforce them on the next request.
            required = self.next_stage(db, user)
            if required is not Stage.FULL:
                session.stage = required.value
                touch = True
        if touch:
            session.last_seen_at = now
            db.commit()
        return AuthContext(session=session, user=user)

    def advance(self, db: Session, ctx: AuthContext) -> Stage:
        """Move a session to the next stage after a forced step was completed."""
        stage = self.next_stage(db, ctx.user)
        ctx.session.stage = stage.value
        return stage

    def revoke(self, db: Session, session: UserSession) -> None:
        if session.revoked_at is None:
            session.revoked_at = self.clock.now()

    def revoke_all(
        self, db: Session, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None
    ) -> None:
        query = (
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=self.clock.now())
        )
        if except_id is not None:
            query = query.where(UserSession.id != except_id)
        db.execute(query)

    def logout(self, db: Session, ctx: AuthContext, *, ip: str | None) -> None:
        self.revoke(db, ctx.session)
        self.audit.append(db, "logout", actor=Actor.for_user(ctx.user), ip=ip)
        db.commit()
