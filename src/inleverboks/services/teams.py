"""Education teams (PLAN B63, B64): teams, members, team contacts (TCP) and crebos.

Every change goes through ``policy.can_team``; the service also enforces the rules the
matrix cannot express (a TCP never adds a contact or removes one, a team is only deleted
when empty).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import (
    ClassList,
    Exam,
    ExamTemplate,
    Qualification,
    Team,
    TeamCrebo,
    TeamMember,
    User,
)
from inleverboks.policy import SystemAction, TeamAction, TeamContext, can, can_team
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.auth import AuthService
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError

MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500
_CREBO = re.compile(r"^\d{3,10}$")


@dataclass(frozen=True)
class TeamContents:
    class_lists: int
    templates: int
    exams: int

    @property
    def empty(self) -> bool:
        return not (self.class_lists or self.templates or self.exams)


@dataclass(frozen=True)
class Membership:
    """One team of a user, for the users page and the account page."""

    team_id: uuid.UUID
    name: str
    is_contact: bool


def team_context(db: Session, user_id: uuid.UUID) -> TeamContext:
    """The teams of a user and those where he is team contact, for ``policy``."""
    rows = db.execute(
        select(TeamMember.team_id, TeamMember.is_contact).where(TeamMember.user_id == user_id)
    ).all()
    return TeamContext(
        member_of=frozenset(team_id for team_id, _ in rows),
        contact_of=frozenset(team_id for team_id, contact in rows if contact),
    )


def _clean(value: str) -> str:
    return " ".join(value.split())


class TeamService:
    def __init__(self, clock: Clock, audit: AuditService, auth: AuthService) -> None:
        self.clock = clock
        self.audit = audit
        self.auth = auth

    # Lookups -------------------------------------------------------------------------

    def context(self, db: Session, user: User) -> TeamContext:
        return team_context(db, user.id)

    def is_contact_anywhere(self, db: Session, user_id: uuid.UUID) -> bool:
        query = select(TeamMember.team_id).where(
            TeamMember.user_id == user_id, TeamMember.is_contact.is_(True)
        )
        return db.scalar(query.limit(1)) is not None

    def all(self, db: Session) -> list[Team]:
        query = select(Team).options(
            selectinload(Team.members).selectinload(TeamMember.user), selectinload(Team.crebos)
        )
        return sorted(db.scalars(query), key=lambda t: t.name.lower())

    def names(self, db: Session) -> dict[uuid.UUID, str]:
        return {row.id: row.name for row in db.execute(select(Team.id, Team.name))}

    def get(self, db: Session, user: User, team_id: uuid.UUID, action: TeamAction) -> Team:
        """A team the user may act on. Unknown and forbidden look the same (404)."""
        team = db.get(Team, team_id)
        if team is None or not can_team(user, self.context(db, user), action, team.id):
            raise NotFoundError("team_not_found")
        return team

    def memberships(
        self, db: Session, user_id: uuid.UUID | None = None
    ) -> dict[uuid.UUID, list[Membership]]:
        """The teams per user (of everyone, or of one user), sorted by team name."""
        query = (
            select(TeamMember.user_id, Team.id, Team.name, TeamMember.is_contact)
            .join(Team, Team.id == TeamMember.team_id)
            .order_by(func.lower(Team.name))
        )
        if user_id is not None:
            query = query.where(TeamMember.user_id == user_id)
        rows = db.execute(query).all()
        result: dict[uuid.UUID, list[Membership]] = {}
        for member_id, team_id, name, contact in rows:
            result.setdefault(member_id, []).append(Membership(team_id, name, contact))
        return result

    def contents(self, db: Session, team_id: uuid.UUID) -> TeamContents:
        def count(model: type[ClassList] | type[ExamTemplate] | type[Exam]) -> int:
            query = select(func.count()).select_from(model).where(model.team_id == team_id)
            return db.scalar(query) or 0

        return TeamContents(count(ClassList), count(ExamTemplate), count(Exam))

    def qualifications(
        self, db: Session, team_id: uuid.UUID, query: str = ""
    ) -> list[Qualification]:
        """The newest version of each qualification of the team's crebos, including the
        crebos that replaced them (``previous_crebo``), filtered on crebo or name."""
        linked = select(TeamCrebo.crebo).where(TeamCrebo.team_id == team_id)
        rows = db.scalars(
            select(Qualification).where(
                or_(Qualification.crebo.in_(linked), Qualification.previous_crebo.in_(linked))
            )
        )
        newest: dict[str, Qualification] = {}
        for row in rows:
            if row.crebo not in newest or row.revision > newest[row.crebo].revision:
                newest[row.crebo] = row
        query = query.strip().lower()
        found = [
            q for q in newest.values()
            if not query or q.crebo.startswith(query) or query in q.name.lower()
        ]  # fmt: skip
        return sorted(found, key=lambda q: (q.name.lower(), q.crebo))

    def crebo_names(self, db: Session) -> dict[str, str]:
        """The name of the newest imported version of every crebo."""
        rows = db.execute(
            select(Qualification.crebo, Qualification.name, Qualification.revision).order_by(
                Qualification.crebo, Qualification.revision
            )
        ).all()
        return {crebo: name for crebo, name, _ in rows}  # the last (newest) wins

    # Teams (admin) -------------------------------------------------------------------

    def create(
        self, db: Session, admin: User, name: str, description: str, *, ip: str | None
    ) -> Team:
        if not can(admin, SystemAction.MANAGE_TEAMS):
            raise PermissionDeniedError()
        team = Team(created_at=self.clock.now())
        self._set_fields(team, name, description)
        self._flush_unique(db, team)
        db.add(team)
        db.flush()
        self._log(db, admin, "team.created", team, ip=ip, name=team.name)
        db.commit()
        return team

    def update(
        self, db: Session, admin: User, team: Team, name: str, description: str, *,
        ip: str | None,
    ) -> None:  # fmt: skip
        self._require(db, admin, TeamAction.EDIT, team)
        self._set_fields(team, name, description)
        self._flush_unique(db, team)
        self._log(db, admin, "team.updated", team, ip=ip, name=team.name)
        db.commit()

    def delete(self, db: Session, admin: User, team: Team, *, ip: str | None) -> None:
        self._require(db, admin, TeamAction.DELETE, team)
        if not self.contents(db, team.id).empty:
            raise DomainError("team_not_empty")
        self._log(db, admin, "team.deleted", team, ip=ip, name=team.name)
        db.delete(team)
        db.commit()

    # Members -------------------------------------------------------------------------

    def candidates(self, db: Session, team: Team) -> list[User]:
        """Active accounts that are not yet in the team."""
        members = select(TeamMember.user_id).where(TeamMember.team_id == team.id)
        query = select(User).where(User.is_active.is_(True), User.id.not_in(members))
        return list(db.scalars(query.order_by(func.lower(User.name), User.email)))

    def add_member(
        self, db: Session, actor: User, team: Team, user_id: uuid.UUID, *,
        contact: bool = False, ip: str | None,
    ) -> None:  # fmt: skip
        self._require(db, actor, TeamAction.ADD_MEMBER, team)
        if contact:
            self._require(db, actor, TeamAction.SET_CONTACT, team)
        user = db.get(User, user_id)
        if user is None:
            raise NotFoundError("user_not_found")
        if not user.is_active:
            raise DomainError("user_inactive")
        if db.get(TeamMember, (team.id, user.id)) is not None:
            raise DomainError("already_member")
        db.add(
            TeamMember(
                team_id=team.id, user_id=user.id, is_contact=False, added_by_id=actor.id,
                added_at=self.clock.now(),
            )
        )  # fmt: skip
        db.flush()
        self._log(db, actor, "team.member_added", team, ip=ip, user_id=str(user.id))
        if contact:
            self._set_contact(db, actor, team, user, True, ip=ip)
        db.commit()

    def remove_member(
        self, db: Session, actor: User, team: Team, user_id: uuid.UUID, *, ip: str | None
    ) -> None:
        self._require(db, actor, TeamAction.REMOVE_MEMBER, team)
        member = db.get(TeamMember, (team.id, user_id))
        if member is None:
            raise NotFoundError("user_not_found")
        if member.is_contact and not can_team(
            actor, self.context(db, actor), TeamAction.SET_CONTACT, team.id
        ):
            raise PermissionDeniedError("contact_protected")
        was_contact = member.is_contact
        db.delete(member)
        db.flush()
        self._log(db, actor, "team.member_removed", team, ip=ip, user_id=str(user_id),
                  was_contact=was_contact)  # fmt: skip
        db.commit()

    def set_contact(
        self, db: Session, admin: User, team: Team, user_id: uuid.UUID, value: bool, *,
        ip: str | None,
    ) -> None:  # fmt: skip
        self._require(db, admin, TeamAction.SET_CONTACT, team)
        user = db.get(User, user_id)
        if user is None or db.get(TeamMember, (team.id, user_id)) is None:
            raise NotFoundError("user_not_found")
        if value and not user.is_active:
            raise DomainError("user_inactive")
        self._set_contact(db, admin, team, user, value, ip=ip)
        db.commit()

    def _set_contact(
        self, db: Session, admin: User, team: Team, user: User, value: bool, *, ip: str | None
    ) -> None:
        member = db.get(TeamMember, (team.id, user.id))
        if member is None:
            raise NotFoundError("user_not_found")
        if member.is_contact == value:
            return
        needed_totp = self.auth.totp_required(db, user)
        member.is_contact = value
        db.flush()
        event = "team.contact_set" if value else "team.contact_unset"
        self._log(db, admin, event, team, ip=ip, user_id=str(user.id))
        # PLAN B64: a TCP needs two-step verification. Without it, end his sessions so the
        # next login sets it up.
        if value and not needed_totp and not user.totp_enabled:
            self.auth.revoke_all(db, user.id)

    # Crebos (admin) ------------------------------------------------------------------

    def add_crebo(
        self, db: Session, admin: User, team: Team, crebo: str, *, ip: str | None
    ) -> None:
        self._require(db, admin, TeamAction.MANAGE_CREBOS, team)
        crebo = crebo.strip()
        if not _CREBO.match(crebo) or crebo not in self.crebo_names(db):
            raise DomainError("unknown_crebo")
        if db.get(TeamCrebo, (team.id, crebo)) is not None:
            raise DomainError("duplicate")
        db.add(TeamCrebo(team_id=team.id, crebo=crebo))
        db.flush()
        self._log(db, admin, "team.crebo_added", team, ip=ip, crebo=crebo)
        db.commit()

    def remove_crebo(
        self, db: Session, admin: User, team: Team, crebo: str, *, ip: str | None
    ) -> None:
        self._require(db, admin, TeamAction.MANAGE_CREBOS, team)
        row = db.get(TeamCrebo, (team.id, crebo))
        if row is None:
            raise NotFoundError()
        db.delete(row)
        db.flush()
        self._log(db, admin, "team.crebo_removed", team, ip=ip, crebo=crebo)
        db.commit()

    # Helpers -------------------------------------------------------------------------

    def _require(self, db: Session, user: User, action: TeamAction, team: Team) -> None:
        if not can_team(user, self.context(db, user), action, team.id):
            raise PermissionDeniedError()

    def _set_fields(self, team: Team, name: str, description: str) -> None:
        name = _clean(name)
        description = description.strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise DomainError("invalid_team_name")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise DomainError("description_too_long")
        team.name = name
        team.description = description or None

    def _flush_unique(self, db: Session, team: Team) -> None:
        query = select(Team.id).where(func.lower(Team.name) == team.name.lower())
        if team.id is not None:
            query = query.where(Team.id != team.id)
        if db.scalar(query.limit(1)) is not None:
            raise DomainError("team_name_taken")
        db.flush()

    def _log(
        self, db: Session, actor: User, event: str, team: Team, *, ip: str | None,
        **details: object,
    ) -> None:  # fmt: skip
        self.audit.append(
            db, event, actor=Actor.for_user(actor), ip=ip,
            details={"team_id": str(team.id), **details},
        )  # fmt: skip
