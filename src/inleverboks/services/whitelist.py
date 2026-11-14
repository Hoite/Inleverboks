"""Whitelist of domains and addresses that may be invited and may log in."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import WhitelistEntry

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
DOMAIN_RE = re.compile(r"^([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class WhitelistError(ValueError):
    pass


def normalize_email(value: str) -> str:
    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.fullmatch(normalize_email(value)))


def parse_entry(raw: str) -> tuple[str, str]:
    """``@school.nl`` or ``school.nl`` → domain; ``a@school.nl`` → email."""
    value = raw.strip().lower()
    if value.startswith("@"):
        value = value[1:]
    if "@" in value:
        if not EMAIL_RE.fullmatch(value):
            raise WhitelistError("invalid_email")
        return "email", value
    if not DOMAIN_RE.fullmatch(value):
        raise WhitelistError("invalid_domain")
    return "domain", value


class WhitelistService:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def entries(self, db: Session) -> list[WhitelistEntry]:
        return list(
            db.scalars(select(WhitelistEntry).order_by(WhitelistEntry.kind, WhitelistEntry.value))
        )

    def is_allowed(self, db: Session, email: str) -> bool:
        """Exact address match, or the address's domain is listed (no subdomains)."""
        email = normalize_email(email)
        domain = email.rsplit("@", 1)[-1]
        found = db.scalar(
            select(WhitelistEntry.id).where(
                ((WhitelistEntry.kind == "email") & (WhitelistEntry.value == email))
                | ((WhitelistEntry.kind == "domain") & (WhitelistEntry.value == domain))
            )
        )
        return found is not None

    def add(self, db: Session, raw: str, *, by: uuid.UUID) -> WhitelistEntry:
        kind, value = parse_entry(raw)
        existing = db.scalar(
            select(WhitelistEntry).where(WhitelistEntry.kind == kind, WhitelistEntry.value == value)
        )
        if existing:
            raise WhitelistError("duplicate")
        entry = WhitelistEntry(
            kind=kind, value=value, created_at=self.clock.now(), created_by_id=by
        )
        db.add(entry)
        db.flush()
        return entry

    def remove(self, db: Session, entry_id: uuid.UUID) -> WhitelistEntry | None:
        entry = db.get(WhitelistEntry, entry_id)
        if entry is not None:
            db.delete(entry)
            db.flush()
        return entry
