"""System-wide configuration and infrastructure tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from inleverboks.db.base import Base
from inleverboks.db.types import UTCDateTime, new_id


class WhitelistEntry(Base):
    """Who may be invited and log in: a whole domain or a single address."""

    __tablename__ = "whitelist_entry"
    __table_args__ = (
        UniqueConstraint("kind", "value"),
        CheckConstraint("kind IN ('domain', 'email')", name="kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))


class IpPreset(Base):
    """Named list of CIDR ranges that a teacher can pick for an exam."""

    __tablename__ = "ip_preset"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    cidrs: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Setting(Base):
    """Admin-managed settings as JSON values (retention, regex, limits, branding, ...)."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))


class BrandingAsset(Base):
    __tablename__ = "branding_asset"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16), unique=True)  # logo | favicon
    storage_key: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobLock(Base):
    """Lease-based lock so scheduled jobs run once across replicas."""

    __tablename__ = "job_lock"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
