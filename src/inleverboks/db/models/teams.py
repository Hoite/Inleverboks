"""Education teams ("onderwijsteams", PLAN B63): members, team contacts and crebos."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false

from inleverboks.db.base import Base
from inleverboks.db.models.users import User
from inleverboks.db.types import UTCDateTime, new_id


class Team(Base):
    __tablename__ = "team"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    members: Mapped[list[TeamMember]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    crebos: Mapped[list[TeamCrebo]] = relationship(
        order_by="TeamCrebo.crebo", cascade="all, delete-orphan"
    )


class TeamMember(Base):
    """Membership of one user in one team. ``is_contact`` is the TCP role (PLAN B63)."""

    __tablename__ = "team_member"

    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("team.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    is_contact: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime] = mapped_column(UTCDateTime)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class TeamCrebo(Base):
    """A crebo linked to a team. Linked by crebo, not by qualification row, so a new
    dossier version of the same crebo stays linked."""

    __tablename__ = "team_crebo"

    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("team.id", ondelete="CASCADE"), primary_key=True
    )
    crebo: Mapped[str] = mapped_column(String(10), primary_key=True, index=True)
