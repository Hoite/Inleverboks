"""Education teams (PLAN B63): admin management under /admin/onderwijsteams and the team
page for members and team contacts under /examens/teams."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from inleverboks.db.models import Exam, Team, TeamMember
from inleverboks.policy import TeamAction, can_team
from inleverboks.services.errors import DomainError, NotFoundError
from inleverboks.web.deps import Admin, Db, Svc, Teacher, client_ip
from inleverboks.web.messages import describe
from inleverboks.web.templating import render

router = APIRouter()

ADMIN_BASE = "/admin/onderwijsteams"
TEAM_BASE = "/examens/teams"

Name = Annotated[str, Form(max_length=1024)]
Description = Annotated[str, Form(max_length=4096)]


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# Admin: list and create ----------------------------------------------------------


def _list_page(
    request: Request, db: Db, services: Svc, *, errors: list[str] | None = None,
    ok: str | None = None, status: int = 200, name: str = "", description: str = "",
) -> Response:  # fmt: skip
    teams = services.teams.all(db)
    return render(
        request,
        "admin/teams.html.j2",
        status_code=status,
        teams=teams,
        todo=services.teams.unassigned(db),
        crebo_names=services.teams.crebo_names(db),
        errors=errors or [],
        ok=ok,
        name_value=name,
        description_value=description,
    )


@router.get(ADMIN_BASE)
def teams(request: Request, ctx: Admin, db: Db, services: Svc, ok: str | None = None) -> Response:
    return _list_page(request, db, services, ok=ok)


@router.post(ADMIN_BASE)
def create_team(
    request: Request, ctx: Admin, db: Db, services: Svc, name: Name = "",
    description: Description = "",
) -> Response:  # fmt: skip
    try:
        team = services.teams.create(db, ctx.user, name, description, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _list_page(
            request, db, services, errors=describe(exc), status=400, name=name,
            description=description,
        )  # fmt: skip
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=created")


# Admin: what is still to assign (PLAN B69) --------------------------------------

ASSIGN = ADMIN_BASE + "/toewijzen"


@router.get(ASSIGN)
def unassigned(
    request: Request, ctx: Admin, db: Db, services: Svc, ok: str | None = None,
    error: str | None = None,
) -> Response:  # fmt: skip
    todo = services.teams.unassigned(db)
    memberships = services.teams.memberships(db)
    by_owner: dict[uuid.UUID, list[Exam]] = {}
    for exam in todo.exams:
        by_owner.setdefault(exam.owner_id, []).append(exam)
    return render(
        request, "admin/team_assign.html.j2", todo=todo, teams=services.teams.all(db),
        memberships=memberships, owner_names=services.users.names(db), by_owner=by_owner,
        ok=ok, errors=describe(DomainError(error)) if error else [],
    )  # fmt: skip


def _assigned(action: Callable[[], object], db: Db) -> RedirectResponse:
    try:
        action()
    except DomainError as exc:
        db.rollback()
        return _redirect(f"{ASSIGN}?error={exc.code}")
    return _redirect(f"{ASSIGN}?ok=assigned")


@router.post(ASSIGN + "/klassenlijst/{class_list_id:uuid}")
def assign_class_list(
    request: Request, class_list_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    team_id: Annotated[uuid.UUID, Form()],
) -> Response:  # fmt: skip
    return _assigned(lambda: services.teams.assign_class_list(
        db, ctx.user, class_list_id, team_id, ip=client_ip(request)), db)  # fmt: skip


@router.post(ASSIGN + "/sjabloon/{template_id:uuid}")
def assign_template(
    request: Request, template_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    team_id: Annotated[uuid.UUID, Form()],
) -> Response:  # fmt: skip
    return _assigned(lambda: services.teams.assign_template(
        db, ctx.user, template_id, team_id, ip=client_ip(request)), db)  # fmt: skip


@router.post(ASSIGN + "/examen/{exam_id:uuid}")
def assign_exam(
    request: Request, exam_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    team_id: Annotated[uuid.UUID, Form()],
) -> Response:  # fmt: skip
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise NotFoundError()
    return _assigned(lambda: services.exams.set_team(
        db, ctx.user, exam, team_id, ip=client_ip(request)), db)  # fmt: skip


@router.post(ASSIGN + "/eigenaar/{owner_id:uuid}")
def assign_owner_exams(
    request: Request, owner_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    team_id: Annotated[uuid.UUID, Form()],
) -> Response:  # fmt: skip
    """All exams without a team of one owner at once, each with its own audit entry."""
    exams = [e for e in services.teams.unassigned(db).exams if e.owner_id == owner_id]

    def assign_all() -> None:
        for exam in exams:
            services.exams.set_team(db, ctx.user, exam, team_id, ip=client_ip(request))

    return _assigned(assign_all, db)


# Admin: one team -----------------------------------------------------------------


def _admin_page(
    request: Request, ctx: Admin, db: Db, services: Svc, team: Team, *,
    errors: list[str] | None = None, ok: str | None = None, status: int = 200,
    name: str | None = None, description: str | None = None,
) -> Response:  # fmt: skip
    return render(
        request,
        "admin/team.html.j2",
        status_code=status,
        team=team,
        members=_sorted_members(team),
        candidates=services.teams.candidates(db, team),
        crebo_names=services.teams.crebo_names(db),
        contents=services.teams.contents(db, team.id),
        errors=errors or [],
        ok=ok,
        name_value=team.name if name is None else name,
        description_value=(team.description or "") if description is None else description,
    )


def _sorted_members(team: Team) -> list[TeamMember]:
    return sorted(team.members, key=lambda m: (not m.is_contact, m.user.name.lower(), m.user.email))


@router.get(ADMIN_BASE + "/{team_id:uuid}")
def admin_team(
    request: Request, team_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    ok: str | None = None,
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.EDIT)
    return _admin_page(request, ctx, db, services, team, ok=ok)


@router.post(ADMIN_BASE + "/{team_id:uuid}/bewerken")
def update_team(
    request: Request, team_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    name: Name = "", description: Description = "",
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.EDIT)
    try:
        services.teams.update(db, ctx.user, team, name, description, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(
            request, ctx, db, services, team, errors=describe(exc), status=400, name=name,
            description=description,
        )  # fmt: skip
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=saved")


@router.post(ADMIN_BASE + "/{team_id:uuid}/verwijderen")
def delete_team(
    request: Request, team_id: uuid.UUID, ctx: Admin, db: Db, services: Svc
) -> Response:
    team = services.teams.get(db, ctx.user, team_id, TeamAction.DELETE)
    try:
        services.teams.delete(db, ctx.user, team, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{ADMIN_BASE}?ok=deleted")


@router.post(ADMIN_BASE + "/{team_id:uuid}/leden")
def admin_add_member(
    request: Request, team_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    user_id: Annotated[uuid.UUID, Form()], contact: Annotated[bool, Form()] = False,
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.EDIT)
    try:
        services.teams.add_member(
            db, ctx.user, team, user_id, contact=contact, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=member_added#leden")


@router.post(ADMIN_BASE + "/{team_id:uuid}/leden/{user_id:uuid}/verwijderen")
def admin_remove_member(
    request: Request, team_id: uuid.UUID, user_id: uuid.UUID, ctx: Admin, db: Db, services: Svc
) -> Response:
    team = services.teams.get(db, ctx.user, team_id, TeamAction.EDIT)
    try:
        services.teams.remove_member(db, ctx.user, team, user_id, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=member_removed#leden")


@router.post(ADMIN_BASE + "/{team_id:uuid}/leden/{user_id:uuid}/tcp")
def admin_set_contact(
    request: Request, team_id: uuid.UUID, user_id: uuid.UUID, ctx: Admin, db: Db,
    services: Svc, contact: Annotated[bool, Form()] = False,
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.SET_CONTACT)
    try:
        services.teams.set_contact(db, ctx.user, team, user_id, contact, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=saved#leden")


@router.post(ADMIN_BASE + "/{team_id:uuid}/crebos")
def add_crebo(
    request: Request, team_id: uuid.UUID, ctx: Admin, db: Db, services: Svc,
    crebo: Annotated[str, Form(max_length=20)] = "",
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.MANAGE_CREBOS)
    try:
        services.teams.add_crebo(db, ctx.user, team, crebo, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=crebo_added#crebos")


@router.post(ADMIN_BASE + "/{team_id:uuid}/crebos/{crebo}/verwijderen")
def remove_crebo(
    request: Request, team_id: uuid.UUID, crebo: str, ctx: Admin, db: Db, services: Svc
) -> Response:
    team = services.teams.get(db, ctx.user, team_id, TeamAction.MANAGE_CREBOS)
    try:
        services.teams.remove_crebo(db, ctx.user, team, crebo, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _admin_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{ADMIN_BASE}/{team.id}?ok=crebo_removed#crebos")


# Team page (members and team contacts) -------------------------------------------


def _team_page(
    request: Request, ctx: Teacher, db: Db, services: Svc, team: Team, *,
    errors: list[str] | None = None, ok: str | None = None, status: int = 200,
) -> Response:  # fmt: skip
    teams_ctx = services.teams.context(db, ctx.user)
    may_manage = can_team(ctx.user, teams_ctx, TeamAction.ADD_MEMBER, team.id)
    return render(
        request,
        "teacher/team.html.j2",
        status_code=status,
        team=team,
        members=_sorted_members(team),
        candidates=services.teams.candidates(db, team) if may_manage else [],
        crebo_names=services.teams.crebo_names(db),
        may_manage=may_manage,
        errors=errors or [],
        ok=ok,
    )


@router.get(TEAM_BASE + "/{team_id:uuid}")
def team_page(
    request: Request, team_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    ok: str | None = None,
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.VIEW)
    return _team_page(request, ctx, db, services, team, ok=ok)


@router.post(TEAM_BASE + "/{team_id:uuid}/leden")
def team_add_member(
    request: Request, team_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    user_id: Annotated[uuid.UUID, Form()],
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.ADD_MEMBER)
    try:
        services.teams.add_member(db, ctx.user, team, user_id, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _team_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{TEAM_BASE}/{team.id}?ok=member_added#leden")


@router.post(TEAM_BASE + "/{team_id:uuid}/leden/{user_id:uuid}/verwijderen")
def team_remove_member(
    request: Request, team_id: uuid.UUID, user_id: uuid.UUID, ctx: Teacher, db: Db,
    services: Svc,
) -> Response:  # fmt: skip
    team = services.teams.get(db, ctx.user, team_id, TeamAction.REMOVE_MEMBER)
    try:
        services.teams.remove_member(db, ctx.user, team, user_id, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        db.refresh(team)
        return _team_page(request, ctx, db, services, team, errors=describe(exc), status=400)
    return _redirect(f"{TEAM_BASE}/{team.id}?ok=member_removed#leden")
