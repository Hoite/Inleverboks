"""Admin screens: users and invitations, whitelist, settings and the system audit log."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, update

from inleverboks.db.models import AuditEvent, Exam, Invite, IpPreset, User
from inleverboks.db.models.audit import SYSTEM_CHAIN
from inleverboks.i18n import gettext as _
from inleverboks.services.audit import Actor
from inleverboks.services.errors import DomainError
from inleverboks.services.ipranges import parse_cidrs
from inleverboks.services.system_settings import SettingsValidationError, parse_extensions
from inleverboks.services.whitelist import WhitelistError
from inleverboks.web.deps import Admin, Db, Svc, client_ip
from inleverboks.web.messages import describe
from inleverboks.web.routes.review import audit_export
from inleverboks.web.templating import render

router = APIRouter(prefix="/admin")

AUDIT_PAGE_SIZE = 100


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# Users ---------------------------------------------------------------------------


def _users_page(
    request: Request, db: Db, services: Svc, *, errors: list[str] | None = None,
    ok: str | None = None, status: int = 200, invite_email: str = "",
) -> Response:  # fmt: skip
    now = services.clock.now()
    open_invites = list(
        db.scalars(
            select(Invite)
            .where(Invite.used_at.is_(None), Invite.revoked_at.is_(None), Invite.expires_at > now)
            .order_by(Invite.created_at.desc())
        )
    )
    return render(
        request,
        "admin/users.html.j2",
        status_code=status,
        users=services.users.list_users(db),
        open_invites=open_invites,
        errors=errors or [],
        ok=ok,
        invite_email=invite_email,
    )


@router.get("")
def admin_home() -> Response:
    return _redirect("/admin/gebruikers")


@router.get("/gebruikers")
def users(request: Request, ctx: Admin, db: Db, services: Svc, ok: str | None = None) -> Response:
    return _users_page(request, db, services, ok=ok)


@router.post("/uitnodigingen")
def create_invite(
    request: Request,
    ctx: Admin,
    db: Db,
    services: Svc,
    email: Annotated[str, Form(max_length=320)] = "",
) -> Response:
    try:
        link = services.users.create_invite(db, ctx.user, email, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _users_page(
            request, db, services, errors=describe(exc), status=400, invite_email=email
        )
    return render(
        request,
        "admin/link_created.html.j2",
        title=_("Invitation link created"),
        url=f"{services.env.app_base_url}/uitnodiging/{link.token}",
        email=email.strip().lower(),
        valid_text=_("Valid for 7 days, can be used once."),
    )


@router.post("/uitnodigingen/{invite_id}/intrekken")
def revoke_invite(
    request: Request, invite_id: uuid.UUID, ctx: Admin, db: Db, services: Svc
) -> Response:
    invite = db.get(Invite, invite_id)
    if invite is not None and invite.used_at is None and invite.revoked_at is None:
        invite.revoked_at = services.clock.now()
        services.audit.append(
            db, "invite.revoked", actor=Actor.for_user(ctx.user), ip=client_ip(request),
            details={"invite_id": str(invite.id), "email": invite.email},
        )  # fmt: skip
        db.commit()
    return _redirect("/admin/gebruikers?ok=invite_revoked")


@router.post("/gebruikers/{user_id}/reset")
def create_reset(
    request: Request,
    user_id: uuid.UUID,
    ctx: Admin,
    db: Db,
    services: Svc,
    reset_totp: Annotated[bool, Form()] = False,
) -> Response:
    try:
        link = services.users.create_reset(
            db, ctx.user, user_id, reset_totp=reset_totp, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _users_page(request, db, services, errors=describe(exc), status=400)
    target = db.get(User, user_id)
    return render(
        request,
        "admin/link_created.html.j2",
        title=_("Password reset link created"),
        url=f"{services.env.app_base_url}/reset/{link.token}",
        email=target.email if target else "",
        valid_text=_("Valid for 24 hours, can be used once."),
    )


@router.post("/gebruikers/{user_id}/actief")
def set_active(
    request: Request,
    user_id: uuid.UUID,
    ctx: Admin,
    db: Db,
    services: Svc,
    active: Annotated[bool, Form()] = False,
) -> Response:
    try:
        services.users.set_active(db, ctx.user, user_id, active, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _users_page(request, db, services, errors=describe(exc), status=400)
    return _redirect("/admin/gebruikers?ok=saved")


@router.post("/gebruikers/{user_id}/admin")
def set_admin(
    request: Request,
    user_id: uuid.UUID,
    ctx: Admin,
    db: Db,
    services: Svc,
    admin: Annotated[bool, Form()] = False,
) -> Response:
    try:
        services.users.set_admin(db, ctx.user, user_id, admin, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _users_page(request, db, services, errors=describe(exc), status=400)
    return _redirect("/admin/gebruikers?ok=saved")


# Whitelist -----------------------------------------------------------------------


def _whitelist_page(
    request: Request, db: Db, services: Svc, *, errors: list[str] | None = None,
    status: int = 200, value: str = "", ok: str | None = None,
) -> Response:  # fmt: skip
    return render(
        request,
        "admin/whitelist.html.j2",
        status_code=status,
        entries=services.whitelist.entries(db),
        errors=errors or [],
        value=value,
        ok=ok,
    )


@router.get("/whitelist")
def whitelist(
    request: Request, ctx: Admin, db: Db, services: Svc, ok: str | None = None
) -> Response:
    return _whitelist_page(request, db, services, ok=ok)


@router.post("/whitelist")
def whitelist_add(
    request: Request,
    ctx: Admin,
    db: Db,
    services: Svc,
    value: Annotated[str, Form(max_length=320)] = "",
) -> Response:
    try:
        entry = services.whitelist.add(db, value, by=ctx.user.id)
    except WhitelistError as exc:
        db.rollback()
        return _whitelist_page(
            request, db, services, errors=describe(DomainError(str(exc))), status=400, value=value
        )
    services.audit.append(
        db, "whitelist.added", actor=Actor.for_user(ctx.user), ip=client_ip(request),
        details={"kind": entry.kind, "value": entry.value},
    )  # fmt: skip
    db.commit()
    return _redirect("/admin/whitelist?ok=added")


@router.post("/whitelist/{entry_id}/verwijderen")
def whitelist_remove(
    request: Request, entry_id: uuid.UUID, ctx: Admin, db: Db, services: Svc
) -> Response:
    entry = services.whitelist.remove(db, entry_id)
    if entry is not None:
        services.audit.append(
            db, "whitelist.removed", actor=Actor.for_user(ctx.user), ip=client_ip(request),
            details={"kind": entry.kind, "value": entry.value},
        )  # fmt: skip
        db.commit()
    return _redirect("/admin/whitelist?ok=removed")


# Settings ------------------------------------------------------------------------


def _settings_page(
    request: Request, db: Db, services: Svc, *, values: object = None,
    errors: dict[str, str] | None = None, status: int = 200, ok: str | None = None,
) -> Response:  # fmt: skip
    return render(
        request,
        "admin/settings.html.j2",
        status_code=status,
        values=values or services.settings.load(db),
        errors=errors or {},
        env=services.env,
        ok=ok,
    )


@router.get("/instellingen")
def settings_form(
    request: Request, ctx: Admin, db: Db, services: Svc, ok: str | None = None
) -> Response:
    return _settings_page(request, db, services, ok=ok)


def _settings_errors(problems: dict[str, str]) -> dict[str, str]:
    messages = {
        "range": _("Value out of range."),
        "invalid_regex": _("This is not a valid regular expression."),
        "exceeds_submission": _("May not exceed the maximum per submission."),
        "invalid": _("Enter extensions such as docx, pdf, py."),
    }
    return {key: messages.get(code, code) for key, code in problems.items()}


def _int(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return -1


@router.post("/instellingen")
def settings_save(
    request: Request,
    ctx: Admin,
    db: Db,
    services: Svc,
    retention_days: Annotated[str, Form()] = "",
    student_number_regex: Annotated[str, Form(max_length=200)] = "",
    default_max_file_mb: Annotated[str, Form()] = "",
    default_max_submission_mb: Annotated[str, Form()] = "",
    max_files_per_submission: Annotated[str, Form()] = "",
    default_extensions: Annotated[str, Form(max_length=2000)] = "",
    require_totp_all: Annotated[bool, Form()] = False,
) -> Response:
    current = services.settings.load(db)
    new = replace(
        current,
        retention_days=_int(retention_days),
        student_number_regex=student_number_regex.strip(),
        default_max_file_mb=_int(default_max_file_mb),
        default_max_submission_mb=_int(default_max_submission_mb),
        max_files_per_submission=_int(max_files_per_submission),
        default_extensions=parse_extensions(default_extensions),
        require_totp_all=require_totp_all,
    )
    try:
        changes = services.settings.save(db, new, by=ctx.user.id)
    except SettingsValidationError as exc:
        db.rollback()
        return _settings_page(
            request, db, services, values=new, errors=_settings_errors(exc.problems), status=400
        )
    if changes:
        services.audit.append(
            db, "settings.changed", actor=Actor.for_user(ctx.user), ip=client_ip(request),
            details={"changes": changes},
        )  # fmt: skip
    db.commit()
    return _redirect("/admin/instellingen?ok=saved")


# System audit log ----------------------------------------------------------------


@router.get("/auditlog")
def system_audit(
    request: Request,
    ctx: Admin,
    db: Db,
    services: Svc,
    event: str | None = None,
    format: str | None = None,
) -> Response:
    if format in {"csv", "pdf"}:
        services.audit.append(
            db, "export.audit", actor=Actor.for_user(ctx.user), ip=client_ip(request),
            details={"format": format, "chain": SYSTEM_CHAIN},
        )  # fmt: skip
        db.commit()
        query = select(AuditEvent).where(AuditEvent.chain == SYSTEM_CHAIN)
        if event:
            query = query.where(AuditEvent.event.startswith(event))
        events = list(db.scalars(query.order_by(AuditEvent.seq)))
        return audit_export(
            request, db, services, SYSTEM_CHAIN, _("System log"), "systeemlog", format, events
        )
    query = select(AuditEvent).where(AuditEvent.chain == SYSTEM_CHAIN)
    if event:
        query = query.where(AuditEvent.event.startswith(event))
    rows = list(db.scalars(query.order_by(AuditEvent.seq.desc()).limit(AUDIT_PAGE_SIZE)))
    return render(request, "admin/audit.html.j2", events=rows, event_filter=event or "")


# IP presets ----------------------------------------------------------------------


def _presets_page(
    request: Request, db: Db, *, errors: list[str] | None = None, status: int = 200,
    name: str = "", cidrs: str = "", ok: str | None = None,
) -> Response:  # fmt: skip
    presets = list(db.scalars(select(IpPreset).order_by(IpPreset.name)))
    return render(
        request, "admin/ip_presets.html.j2", status_code=status, presets=presets,
        errors=errors or [], name=name, cidrs=cidrs, ok=ok,
    )  # fmt: skip


@router.get("/ip-presets")
def ip_presets(request: Request, ctx: Admin, db: Db, ok: str | None = None) -> Response:
    return _presets_page(request, db, ok=ok)


@router.post("/ip-presets")
def ip_preset_save(
    request: Request,
    ctx: Admin,
    db: Db,
    services: Svc,
    name: Annotated[str, Form(max_length=100)] = "",
    cidrs: Annotated[str, Form(max_length=4000)] = "",
    preset_id: Annotated[str, Form()] = "",
) -> Response:
    errors: list[str] = []
    parsed: list[str] = []
    name = name.strip()
    if not name:
        errors.append(_("Enter a name."))
    try:
        parsed = parse_cidrs(cidrs)
        if not parsed:
            errors.append(_("Enter at least one IP range."))
    except DomainError:
        errors.append(_("Enter IP addresses or ranges such as 10.0.0.0/8."))
    existing = db.scalar(select(IpPreset).where(IpPreset.name == name))
    target = db.get(IpPreset, uuid.UUID(preset_id)) if preset_id else None
    if existing is not None and existing is not target:
        errors.append(_("A preset with this name already exists."))
    if errors:
        return _presets_page(request, db, errors=errors, status=400, name=name, cidrs=cidrs)
    if target is None:
        target = IpPreset(name=name, cidrs=parsed, created_at=services.clock.now())
        db.add(target)
        event = "ip_preset.created"
        old = None
    else:
        old = {"name": target.name, "cidrs": list(target.cidrs)}
        target.name, target.cidrs = name, parsed
        event = "ip_preset.updated"
    db.flush()
    services.audit.append(
        db, event, actor=Actor.for_user(ctx.user), ip=client_ip(request),
        details={"preset_id": str(target.id), "name": name, "cidrs": parsed, "old": old},
    )  # fmt: skip
    db.commit()
    return _redirect("/admin/ip-presets?ok=saved")


@router.post("/ip-presets/{preset_id}/verwijderen")
def ip_preset_delete(
    request: Request, preset_id: uuid.UUID, ctx: Admin, db: Db, services: Svc
) -> Response:
    preset = db.get(IpPreset, preset_id)
    if preset is not None:
        db.execute(update(Exam).where(Exam.ip_preset_id == preset.id).values(ip_preset_id=None))
        services.audit.append(
            db, "ip_preset.deleted", actor=Actor.for_user(ctx.user), ip=client_ip(request),
            details={"preset_id": str(preset.id), "name": preset.name, "cidrs": list(preset.cidrs)},
        )  # fmt: skip
        db.delete(preset)
        db.commit()
    return _redirect("/admin/ip-presets?ok=removed")
