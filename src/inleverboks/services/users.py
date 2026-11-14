"""Teacher accounts: bootstrap, invitations, resets, roles, passwords and TOTP setup."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.config import Settings
from inleverboks.db.models import Invite, PasswordReset, RecoveryCode, User
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.auth import AuthService
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.services.passwords import (
    check_password_policy,
    hash_password,
    verify_password,
)
from inleverboks.services.security import hash_token, new_token
from inleverboks.services.totp import TotpService
from inleverboks.services.whitelist import WhitelistService, is_valid_email, normalize_email

INVITE_LIFETIME = timedelta(days=7)
RESET_LIFETIME = timedelta(hours=24)
MAX_NAME_LENGTH = 200

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IssuedLink:
    token: str
    expires_at_iso: str


class UserService:
    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditService,
        auth: AuthService,
        whitelist: WhitelistService,
        totp: TotpService,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.auth = auth
        self.whitelist = whitelist
        self.totp = totp

    # Lookups -----------------------------------------------------------------------

    def by_email(self, db: Session, email: str) -> User | None:
        return db.scalar(select(User).where(func.lower(User.email) == normalize_email(email)))

    def list_users(self, db: Session) -> list[User]:
        return list(db.scalars(select(User).order_by(User.name, User.email)))

    def teachers(self, db: Session) -> list[User]:
        return list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.name)))

    def admin_count(self, db: Session) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.is_admin.is_(True), User.is_active.is_(True))
            )
            or 0
        )

    # Bootstrap ---------------------------------------------------------------------

    def bootstrap_admin(self, db: Session, env: Settings) -> bool:
        """Create the local admin on first start. Returns True when one was created."""
        if db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)) is not None:
            return False
        if not env.admin_email:
            log.warning("bootstrap.no_admin_email")
            return False
        email = normalize_email(env.admin_email)
        if not is_valid_email(email):
            log.error("bootstrap.invalid_admin_email")
            return False
        generated = env.admin_password is None or not env.admin_password.get_secret_value()
        password = (
            secrets.token_urlsafe(18)
            if generated or env.admin_password is None
            else env.admin_password.get_secret_value()
        )
        user = self.by_email(db, email)
        if user is None:
            user = User(email=email, name="Admin", created_at=self.clock.now())
            db.add(user)
        user.password_hash = hash_password(password)
        user.is_admin = True
        user.is_local_admin = True
        user.is_active = True
        user.must_change_password = True
        try:
            db.flush()
            self.audit.append(
                db,
                "user.bootstrap_admin",
                actor=Actor.system(),
                details={"user_id": str(user.id), "email": email, "generated_password": generated},
            )
            db.commit()
        except IntegrityError:  # another replica won the race
            db.rollback()
            return False
        if generated:
            # Shown exactly once, by design (spec §3.1). Never logged again.
            log.warning("bootstrap.admin_created", email=email, password=password)
        else:
            log.info("bootstrap.admin_created", email=email)
        return True

    # Invitations -------------------------------------------------------------------

    def create_invite(self, db: Session, admin: User, email: str, *, ip: str | None) -> IssuedLink:
        email = normalize_email(email)
        if not is_valid_email(email):
            raise DomainError("invalid_email")
        if not self.whitelist.is_allowed(db, email):
            raise DomainError("not_whitelisted")
        if self.by_email(db, email) is not None:
            raise DomainError("account_exists")
        now = self.clock.now()
        db.execute(
            update(Invite)
            .where(Invite.email == email, Invite.used_at.is_(None), Invite.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        token = new_token()
        invite = Invite(
            email=email,
            token_hash=hash_token(token),
            created_by_id=admin.id,
            created_at=now,
            expires_at=now + INVITE_LIFETIME,
        )
        db.add(invite)
        db.flush()
        self.audit.append(
            db,
            "invite.created",
            actor=Actor.for_user(admin),
            ip=ip,
            details={"invite_id": str(invite.id), "email": email},
        )
        db.commit()
        return IssuedLink(token=token, expires_at_iso=invite.expires_at.isoformat())

    def open_invite(self, db: Session, token: str) -> Invite:
        invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
        now = self.clock.now()
        if (
            invite is None
            or invite.used_at is not None
            or invite.revoked_at is not None
            or invite.expires_at <= now
        ):
            raise NotFoundError("invite_invalid")
        return invite

    def accept_invite(
        self, db: Session, token: str, *, name: str, password: str, ip: str | None
    ) -> User:
        invite = self.open_invite(db, token)
        if not self.whitelist.is_allowed(db, invite.email):
            raise DomainError("not_whitelisted")
        if self.by_email(db, invite.email) is not None:
            raise DomainError("account_exists")
        name = name.strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise DomainError("invalid_name")
        problems = check_password_policy(password, invite.email)
        if problems:
            raise DomainError("weak_password", problems=problems)
        now = self.clock.now()
        user = User(
            email=invite.email,
            name=name,
            password_hash=hash_password(password),
            created_at=now,
        )
        db.add(user)
        invite.used_at = now
        db.flush()
        self.audit.append(
            db,
            "invite.used",
            actor=Actor.for_user(user),
            ip=ip,
            details={"invite_id": str(invite.id), "user_id": str(user.id)},
        )
        db.commit()
        return user

    # Password resets ---------------------------------------------------------------

    def create_reset(
        self, db: Session, admin: User, user_id: uuid.UUID, *, reset_totp: bool, ip: str | None
    ) -> IssuedLink:
        user = self._get(db, user_id)
        now = self.clock.now()
        db.execute(
            update(PasswordReset)
            .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
            .values(used_at=now)
        )
        token = new_token()
        reset = PasswordReset(
            user_id=user.id,
            token_hash=hash_token(token),
            created_by_id=admin.id,
            created_at=now,
            expires_at=now + RESET_LIFETIME,
        )
        db.add(reset)
        if reset_totp:
            self._clear_totp(db, user)
        db.flush()
        self.audit.append(
            db,
            "password_reset.created",
            actor=Actor.for_user(admin),
            ip=ip,
            details={"user_id": str(user.id), "email": user.email, "totp_reset": reset_totp},
        )
        db.commit()
        return IssuedLink(token=token, expires_at_iso=reset.expires_at.isoformat())

    def open_reset(self, db: Session, token: str) -> PasswordReset:
        reset = db.scalar(
            select(PasswordReset).where(PasswordReset.token_hash == hash_token(token))
        )
        if reset is None or reset.used_at is not None or reset.expires_at <= self.clock.now():
            raise NotFoundError("reset_invalid")
        return reset

    def use_reset(self, db: Session, token: str, *, password: str, ip: str | None) -> User:
        reset = self.open_reset(db, token)
        user = self._get(db, reset.user_id)
        problems = check_password_policy(password, user.email)
        if problems:
            raise DomainError("weak_password", problems=problems)
        user.password_hash = hash_password(password)
        user.must_change_password = False
        reset.used_at = self.clock.now()
        self.auth.revoke_all(db, user.id)
        self.audit.append(
            db, "password_reset.used", actor=Actor.for_user(user), ip=ip,
            details={"user_id": str(user.id)},
        )  # fmt: skip
        db.commit()
        return user

    # Own account -------------------------------------------------------------------

    def change_password(
        self,
        db: Session,
        user: User,
        *,
        current: str,
        new: str,
        keep_session_id: uuid.UUID,
        ip: str | None,
    ) -> None:
        if not verify_password(user.password_hash, current):
            raise DomainError("wrong_password")
        if current == new:
            raise DomainError("same_password")
        problems = check_password_policy(new, user.email)
        if problems:
            raise DomainError("weak_password", problems=problems)
        user.password_hash = hash_password(new)
        user.must_change_password = False
        self.auth.revoke_all(db, user.id, except_id=keep_session_id)
        self.audit.append(db, "password.changed", actor=Actor.for_user(user), ip=ip)

    def start_totp_setup(self, db: Session, user: User) -> str:
        """Store a fresh (not yet enabled) secret and return it for the QR code."""
        if user.totp_enabled:
            raise DomainError("totp_already_enabled")
        secret = self.totp.new_secret()
        user.totp_secret_enc = self.totp.encrypt(secret)
        user.totp_last_step = None
        db.commit()
        return secret

    def pending_totp_secret(self, user: User) -> str | None:
        if user.totp_enabled or not user.totp_secret_enc:
            return None
        return self.totp.decrypt(user.totp_secret_enc)

    def confirm_totp(self, db: Session, user: User, code: str, *, ip: str | None) -> list[str]:
        secret = self.pending_totp_secret(user)
        if secret is None:
            raise DomainError("totp_not_started")
        check = self.totp.verify(secret, code, now=self.clock.now().timestamp())
        if not check.ok:
            raise DomainError("wrong_code")
        user.totp_enabled = True
        user.totp_last_step = check.step
        codes = self._replace_recovery_codes(db, user)
        self.audit.append(db, "totp.enabled", actor=Actor.for_user(user), ip=ip)
        return codes

    def regenerate_recovery_codes(self, db: Session, user: User, *, ip: str | None) -> list[str]:
        if not user.totp_enabled:
            raise DomainError("totp_not_enabled")
        codes = self._replace_recovery_codes(db, user)
        self.audit.append(db, "totp.recovery_codes_regenerated", actor=Actor.for_user(user), ip=ip)
        db.commit()
        return codes

    def disable_totp(self, db: Session, user: User, *, password: str, ip: str | None) -> None:
        if self.auth.totp_required(db, user):
            raise DomainError("totp_required")
        if not verify_password(user.password_hash, password):
            raise DomainError("wrong_password")
        self._clear_totp(db, user)
        self.audit.append(db, "totp.disabled", actor=Actor.for_user(user), ip=ip)
        db.commit()

    def _replace_recovery_codes(self, db: Session, user: User) -> list[str]:
        for old in list(user.recovery_codes):
            db.delete(old)
        codes = self.totp.new_recovery_codes()
        for code in codes:
            db.add(RecoveryCode(user_id=user.id, code_hash=self.totp.hash_recovery_code(code)))
        return codes

    def _clear_totp(self, db: Session, user: User) -> None:
        user.totp_enabled = False
        user.totp_secret_enc = None
        user.totp_last_step = None
        for old in list(user.recovery_codes):
            db.delete(old)

    # Admin actions -----------------------------------------------------------------

    def _get(self, db: Session, user_id: uuid.UUID) -> User:
        user = db.get(User, user_id)
        if user is None:
            raise NotFoundError("user_not_found")
        return user

    def set_active(
        self, db: Session, admin: User, user_id: uuid.UUID, active: bool, *, ip: str | None
    ) -> User:
        user = self._get(db, user_id)
        if not active and (user.is_local_admin or user.id == admin.id):
            raise PermissionDeniedError("cannot_deactivate")
        if user.is_active == active:
            return user
        user.is_active = active
        if not active:
            self.auth.revoke_all(db, user.id)
        self.audit.append(
            db,
            "user.activated" if active else "user.deactivated",
            actor=Actor.for_user(admin),
            ip=ip,
            details={"user_id": str(user.id), "email": user.email},
        )
        db.commit()
        return user

    def set_admin(
        self, db: Session, admin: User, user_id: uuid.UUID, value: bool, *, ip: str | None
    ) -> User:
        user = self._get(db, user_id)
        if user.is_admin == value:
            return user
        if not value:
            if user.is_local_admin:
                raise PermissionDeniedError("local_admin_protected")
            if self.admin_count(db) <= 1:
                raise PermissionDeniedError("last_admin")
        if value and not user.is_active:
            raise DomainError("user_inactive")
        user.is_admin = value
        self.audit.append(
            db,
            "user.role_changed",
            actor=Actor.for_user(admin),
            ip=ip,
            details={
                "user_id": str(user.id),
                "email": user.email,
                "role": "admin",
                "old": not value,
                "new": value,
            },
        )
        db.commit()
        return user
