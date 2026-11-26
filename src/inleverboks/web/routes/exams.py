"""Teacher pages for exams: dashboard tabs, create/edit, detail, actions, QR, presentation."""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

import segno
from anyio import from_thread
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from inleverboks.db.models import Exam, ExamTemplate, IpPreset, Qualification, User
from inleverboks.i18n import gettext as _
from inleverboks.policy import ExamAction, ExamRef, SystemAction, can, can_exam
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.services.exams import (
    MB,
    ExamInput,
    ExamStatus,
    ExamValidationError,
    can_manage_template,
    can_use_template,
)
from inleverboks.services.overview import build_overview
from inleverboks.services.template_files import store_template, unreferenced
from inleverboks.services.timeutil import default_window, format_local, parse_local, split_local
from inleverboks.services.totp import QUIET_ZONE
from inleverboks.web.deps import Db, Svc, Teacher, client_ip
from inleverboks.web.downloads import bytes_download, stream_download
from inleverboks.web.exam_form import (
    field_messages,
    form_values,
    parse_exam_form,
    raw_datetimes,
    read_datetime,
)
from inleverboks.web.messages import describe
from inleverboks.web.templating import render

router = APIRouter(prefix="/examens")

UPLOAD_CHUNK = 64 * 1024


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def public_url(services: Services, exam: Exam) -> str:
    return f"{services.env.app_base_url}/{exam.slug}"


def status_labels() -> dict[str, str]:
    return {
        ExamStatus.CONCEPT: _("Concept"),
        ExamStatus.SCHEDULED: _("Scheduled"),
        ExamStatus.OPEN: _("Open"),
        ExamStatus.CLOSED: _("Closed"),
        ExamStatus.FINALIZED: _("Finalized"),
    }


def _rows(db: Session, services: Services, exams: list[Exam]) -> list[dict[str, Any]]:
    return [
        {
            "exam": exam,
            "status": services.exams.status(db, exam),
            "submissions": services.exams.submission_count(db, exam),
            "retention": services.lifecycle.retention(db, exam),
        }
        for exam in exams
    ]


def _load(
    db: Session, services: Services, user: User, exam_id: uuid.UUID, action: ExamAction
) -> Exam:
    return services.exams.get_for(db, user, exam_id, action)


def _delete_unreferenced(db: Session, services: Services, keys: list[str | None]) -> None:
    for key in keys:
        if key and unreferenced(db, key):
            from_thread.run(services.storage.delete, key)


# Dashboard tabs ------------------------------------------------------------------


def _dashboard(request: Request, tab: str, rows: list[dict[str, Any]], **extra: Any) -> Response:
    return render(
        request,
        "teacher/dashboard.html.j2",
        tab=tab,
        rows=rows,
        status_labels=status_labels(),
        now=request.app.state.services.clock.now(),
        **extra,
    )


@router.get("")
def dashboard(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    rows = _rows(db, services, services.exams.owned(db, ctx.user))
    return _dashboard(request, "mine", rows)


@router.get("/gedeeld")
def shared(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    return _dashboard(
        request, "shared", _rows(db, services, services.exams.shared_with(db, ctx.user)),
        owner_names=services.users.names(db),
    )  # fmt: skip


@router.get("/afgerond")
def finalized(
    request: Request, ctx: Teacher, db: Db, services: Svc, ok: str | None = None
) -> Response:
    return _dashboard(
        request, "finalized", _rows(db, services, services.exams.finalized(db, ctx.user)), ok=ok,
        # Admins see every finalized exam, so they get the owner column too.
        owner_names=services.users.names(db) if ctx.user.is_admin else None,
    )  # fmt: skip


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@router.get("/alle")
def all_exams(
    request: Request, ctx: Teacher, db: Db, services: Svc, eigenaar: str = ""
) -> Response:
    if not ctx.user.is_admin:
        raise PermissionDeniedError()
    names = services.users.names(db)
    owner_id = _uuid_or_none(eigenaar)
    owners = sorted(
        ((uid, names.get(uid, "")) for uid in services.exams.exam_owner_ids(db)),
        key=lambda item: item[1].lower(),
    )
    return _dashboard(
        request, "all", _rows(db, services, services.exams.all_exams(db, owner_id)),
        owner_names=names, owners=owners, owner_filter=owner_id,
    )  # fmt: skip


@router.get("/sjablonen")
def exam_templates(
    request: Request, ctx: Teacher, db: Db, services: Svc, ok: str | None = None, n: int = 0,
    toon: str = "",
) -> Response:  # fmt: skip
    everyone = toon == "alle" and ctx.user.is_admin  # PLAN B61: admin view of all templates
    templates = (
        services.exams.all_templates(db) if everyone else services.exams.templates(db, ctx.user)
    )
    outdated = {
        t.id for t in templates
        if t.qualification is not None
        and services.qualifications.status(db, t.qualification).outdated
    }  # fmt: skip
    return render(
        request, "teacher/templates.html.j2", tab="templates", ok=ok, created=n,
        shared_templates=[t for t in templates if t.shared],
        own_templates=[t for t in templates if not t.shared],
        all_templates=templates if everyone else [], everyone=everyone,
        owner_names=services.users.names(db) if everyone else {},
        outdated=outdated,
    )  # fmt: skip


@router.post("/sjablonen/{template_id}/verwijderen")
def delete_exam_template(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    keys = services.exams.delete_template(db, ctx.user, template_id, ip=client_ip(request))
    _delete_unreferenced(db, services, list(keys))
    return _redirect("/examens/sjablonen?ok=deleted")


@router.get("/sjablonen/nieuw")
def new_exam_template(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    start, end = default_window(services.clock.now(), services.env.timezone)
    data = services.exams.defaults(db, start, end)
    data.name = ""
    return _render_form(request, db, services, ctx.user, data, exam=None, mode="template")


@router.post("/sjablonen/nieuw")
def create_exam_template(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    return _handle_template_form(request, db, services, ctx.user, None)


# Templates from the SBB qualification structure ------------------------------------
# These routes come before /sjablonen/{template_id}, which would take "crebo" as an id.


@router.get("/sjablonen/crebo")
def qualification_search(
    request: Request, ctx: Teacher, db: Db, services: Svc, q: str = ""
) -> Response:
    found = services.qualifications.search(db, q[:100])
    statuses = {row.id: services.qualifications.status(db, row) for row in found}
    # Current qualifications first; replaced or expired ones stay findable for old cohorts.
    results = sorted(found, key=lambda row: statuses[row.id].outdated)
    template = (
        "teacher/_qualification_results.html.j2"
        if request.headers.get("hx-request")
        else "teacher/qualification_search.html.j2"
    )
    return render(
        request, template, tab="templates", query=q, results=results, statuses=statuses,
        imported=services.qualifications.count(db),
    )  # fmt: skip


def _qualification(db: Session, services: Services, qualification_id: uuid.UUID) -> Qualification:
    qualification = services.qualifications.get(db, qualification_id)
    if qualification is None:
        raise NotFoundError()
    return qualification


@router.get("/sjablonen/crebo/{qualification_id}")
def qualification_detail(
    request: Request, qualification_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    error: str | None = None,
) -> Response:  # fmt: skip
    qualification = _qualification(db, services, qualification_id)
    return render(
        request, "teacher/qualification_detail.html.j2", tab="templates",
        qualification=qualification, status=services.qualifications.status(db, qualification),
        errors=[describe(DomainError(error))[0]] if error else [],
    )  # fmt: skip


@router.post("/sjablonen/crebo/{qualification_id}")
def templates_from_qualification(
    request: Request, qualification_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    core_task: Annotated[list[str] | None, Form()] = None,
    shared: Annotated[bool, Form()] = False,
) -> Response:  # fmt: skip
    qualification = _qualification(db, services, qualification_id)
    if shared and not ctx.user.is_admin:
        raise PermissionDeniedError()  # shared templates are managed by admins (PLAN B53)
    try:
        created = services.exams.templates_from_qualification(
            db, ctx.user, qualification, core_task or [], shared=shared, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/sjablonen/crebo/{qualification.id}?error={exc.code}")
    return _redirect(f"/examens/sjablonen?ok=created_from_crebo&n={len(created)}")


@router.get("/sjablonen/{template_id}")
def exam_template_detail(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    ok: str | None = None, error: str | None = None,
) -> Response:  # fmt: skip
    template = services.exams.get_template(db, ctx.user, template_id)
    preset_id = template.config.get("ip_preset_id")
    preset = db.get(IpPreset, uuid.UUID(preset_id)) if preset_id else None
    return render(
        request, "teacher/template_detail.html.j2", exam_template=template, config=template.config,
        preset=preset, ok=ok, errors=[describe(DomainError(error))[0]] if error else [],
        can_manage=can_manage_template(ctx.user, template),
        dossier=_dossier_info(db, services, template),
        owner_name=services.users.names(db).get(template.owner_id, ""),
    )  # fmt: skip


def _dossier_info(db: Session, services: Services, template: ExamTemplate) -> dict[str, Any] | None:
    """Status of the SBB dossier behind a template, for the "replaced" warning."""
    qualification = template.qualification
    if qualification is None:
        return None
    status = services.qualifications.status(db, qualification)
    changes = (
        services.qualifications.changes(qualification, status.replaced_by, template.core_task_code)
        if status.replaced_by is not None and template.core_task_code
        else []
    )
    return {"qualification": qualification, "status": status, "changes": changes}


@router.post("/sjablonen/{template_id}/delen")
def share_exam_template(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    shared: Annotated[bool, Form()] = False,
) -> Response:  # fmt: skip
    template = services.exams.get_template(db, ctx.user, template_id)
    services.exams.set_template_shared(db, ctx.user, template, shared, ip=client_ip(request))
    return _redirect(f"/examens/sjablonen/{template.id}?ok={'shared' if shared else 'unshared'}")


@router.post("/sjablonen/{template_id}/kopie")
def copy_exam_template(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    shared: Annotated[bool, Form()] = False,
) -> Response:  # fmt: skip
    template = services.exams.get_template(db, ctx.user, template_id)
    copy = services.exams.copy_template(
        db, ctx.user, template, ip=client_ip(request), shared=shared
    )
    return _redirect(f"/examens/sjablonen/{copy.id}?ok={'copied_shared' if shared else 'copied'}")


@router.get("/sjablonen/{template_id}/bewerken")
def edit_exam_template(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    template = services.exams.get_managed_template(db, ctx.user, template_id)
    data = services.exams.template_input(template)
    return _render_form(
        request, db, services, ctx.user, data, exam=None, mode="template", template=template
    )


@router.post("/sjablonen/{template_id}/bewerken")
def update_exam_template(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    template = services.exams.get_managed_template(db, ctx.user, template_id)
    return _handle_template_form(request, db, services, ctx.user, template)


def _handle_template_form(
    request: Request, db: Db, services: Svc, user: User, template: ExamTemplate | None
) -> Response:
    form = from_thread.run(_form, request)
    tz = services.env.timezone
    fallback = (
        services.exams.template_input(template)
        if template
        else services.exams.defaults(db, *default_window(services.clock.now(), tz))
    )
    parsed = parse_exam_form(form, tz, fallback, with_times=False)
    if parsed.action != "save":
        return _render_form(
            request, db, services, user, parsed.data, exam=None, mode="template", template=template
        )
    try:
        if template is None:
            template = services.exams.create_template(db, user, parsed.data, ip=client_ip(request))
            return _redirect(f"/examens/sjablonen/{template.id}?ok=created")
        removed = services.exams.update_template(
            db, user, template, parsed.data, ip=client_ip(request)
        )
    except ExamValidationError as exc:
        db.rollback()
        return _render_form(
            request, db, services, user, parsed.data, exam=None, mode="template",
            template=template, problems=exc.problems, status=400,
        )  # fmt: skip
    _delete_unreferenced(db, services, list(removed))
    return _redirect(f"/examens/sjablonen/{template.id}?ok=saved")


@router.post("/sjablonen/{template_id}/vakjes/{slot_id}/sjabloon")
def upload_template_slot_file(
    request: Request, template_id: uuid.UUID, slot_id: uuid.UUID, ctx: Teacher, db: Db,
    services: Svc, file: Annotated[UploadFile, File()],
) -> Response:  # fmt: skip
    template = services.exams.get_managed_template(db, ctx.user, template_id)
    back = f"/examens/sjablonen/{template.id}"
    filename = (file.filename or "sjabloon").replace("\\", "/").rsplit("/", 1)[-1]
    max_bytes = int(template.config.get("max_file_mb", services.env.max_file_mb)) * MB

    async def chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(UPLOAD_CHUNK):
            yield chunk

    try:
        stored = from_thread.run(
            lambda: store_template(services.storage, chunks(), max_bytes=max_bytes)
        )
        replaced = services.exams.attach_template_slot_file(
            db, ctx.user, template, slot_id, stored, filename, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"{back}?error={exc.code}")
    _delete_unreferenced(db, services, [replaced])
    return _redirect(f"{back}?ok=template_uploaded")


@router.post("/sjablonen/{template_id}/vakjes/{slot_id}/sjabloon/verwijderen")
def remove_template_slot_file(
    request: Request, template_id: uuid.UUID, slot_id: uuid.UUID, ctx: Teacher, db: Db,
    services: Svc,
) -> Response:  # fmt: skip
    template = services.exams.get_managed_template(db, ctx.user, template_id)
    key = services.exams.remove_template_slot_file(
        db, ctx.user, template, slot_id, ip=client_ip(request)
    )
    _delete_unreferenced(db, services, [key])
    return _redirect(f"/examens/sjablonen/{template.id}?ok=template_removed")


@router.get("/sjablonen/{template_id}/vakjes/{slot_id}/sjabloon")
def download_template_slot_file(
    template_id: uuid.UUID, slot_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    template = services.exams.get_template(db, ctx.user, template_id)
    slot = next((s for s in template.slots if s.id == slot_id), None)
    if slot is None or slot.template_file is None:
        raise NotFoundError()
    file = slot.template_file
    return stream_download(
        services.storage.open(file.storage_key), file.original_name, size=file.size
    )


# Create / edit -------------------------------------------------------------------


def _form_context(db: Session, services: Services, user: User) -> dict[str, Any]:
    teachers = [u for u in services.users.teachers(db) if u.id != user.id]
    presets = list(db.query(IpPreset).order_by(IpPreset.name))
    return {
        "teachers": teachers,
        "presets": presets,
        "class_lists": services.classlists.usable(db, user),
        "team_names": services.teams.names(db),
        "owner_names": services.users.names(db),
        "field_messages": field_messages(),
        "env": services.env,
    }


def _render_form(
    request: Request,
    db: Session,
    services: Services,
    user: User,
    data: ExamInput,
    *,
    exam: Exam | None,
    problems: dict[str, str] | None = None,
    raw: dict[str, str] | None = None,
    status: int = 200,
    mode: str = "exam",
    template: ExamTemplate | None = None,
    resit_from: Exam | None = None,
) -> Response:
    tz = services.env.timezone
    locked = exam is not None and services.exams.has_submissions(db, exam)
    templates = services.exams.templates(db, user)
    source = next((t for t in templates if t.id == data.source_template_id), None)
    if resit_from is None and data.source_exam_id is not None:
        candidate = db.get(Exam, data.source_exam_id)
        resit_from = candidate if candidate is not None and candidate.owner_id == user.id else None
    slot_template = None
    if mode == "exam" and resit_from is None:
        owner_id = exam.owner_id if exam is not None else user.id
        slot_template = services.exams.slots_template(db, owner_id, data, exam)
    chosen = {s.work_process_code for s in data.slots if s.work_process_code}
    picker_processes = [
        {"code": s.work_process_code, "label": s.label, "checked": s.work_process_code in chosen}
        for s in (source.slots if source else [])
        if s.work_process_code
    ]
    return render(
        request,
        "teacher/exam_form.html.j2",
        status_code=status,
        exam=exam,
        mode=mode,
        exam_template=template,
        templates=templates,
        source_template=source,
        picker_processes=picker_processes,
        slot_template=slot_template,
        slot_template_link=slot_template is not None and can_use_template(user, slot_template),
        resit_from=resit_from,
        values=form_values(data, tz, raw),
        problems=problems or {},
        locked=locked,
        has_roster=exam is not None and services.exams.roster_count(db, exam) > 0,
        base_url=services.env.app_base_url,
        **_form_context(db, services, user),
    )


@router.get("/nieuw")
def new_exam(
    request: Request, ctx: Teacher, db: Db, services: Svc, sjabloon: uuid.UUID | None = None,
    wp: Annotated[list[str] | None, Query()] = None,
) -> Response:  # fmt: skip
    start, end = default_window(services.clock.now(), services.env.timezone)
    data = services.exams.defaults(db, start, end)
    if sjabloon is not None:
        template = services.exams.get_template(db, ctx.user, sjabloon)
        codes = {s.work_process_code for s in template.slots if s.work_process_code}
        selected = [c for c in (wp or []) if c in codes]
        data = services.exams.input_from_template(db, template, data, selected)
    return _render_form(request, db, services, ctx.user, data, exam=None)


@router.get("/{exam_id:uuid}/herkansing")
def resit_selection(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    """Choose the students for one resit. Students with an open expected file (or no
    active hand-in) are ticked already."""
    from inleverboks.web.routes.review import row_status_labels  # noqa: PLC0415

    exam = _load(db, services, ctx.user, exam_id, ExamAction.DUPLICATE)
    data = build_overview(db, exam)
    slot_ids = {s.id for s in exam.expected_files}
    rows = [
        {"row": row, "open": row.status.value != "active" or not slot_ids <= row.filled_slots}
        for row in data.rows
    ]
    return render(
        request, "teacher/resit_select.html.j2", exam=exam, rows=rows,
        row_status_labels=row_status_labels(),
    )  # fmt: skip


@router.post("/{exam_id:uuid}/herkansing")
def new_resit(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    student_number: Annotated[list[str] | None, Form()] = None,
) -> Response:  # fmt: skip
    """One click from the overview: a new exam form, filled in for the selected students
    with only the work processes that are still open for them."""
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DUPLICATE)
    try:
        data = services.exams.resit_input(
            db, ctx.user, exam, (student_number or [])[:200], name_suffix=_("resit")
        )
    except DomainError as exc:
        return _redirect(f"/examens/{exam.id}?error={exc.code}")
    return _render_form(request, db, services, ctx.user, data, exam=None, resit_from=exam)


async def _form(request: Request) -> Any:
    return await request.form()


def _handle_form(
    request: Request, db: Db, services: Svc, user: User, exam: Exam | None
) -> Response:
    form = from_thread.run(_form, request)
    tz = services.env.timezone
    fallback = (
        services.exams.input_from_exam(db, exam)
        if exam
        else services.exams.defaults(db, *default_window(services.clock.now(), tz))
    )
    parsed = parse_exam_form(form, tz, fallback)
    raw = raw_datetimes(form, "starts_at", "ends_at")
    if parsed.action != "save":
        return _render_form(request, db, services, user, parsed.data, exam=exam, raw=raw)
    problems = dict(parsed.problems)
    owner_id = exam.owner_id if exam else user.id
    try:
        services.exams.validate(db, owner_id, parsed.data, exam=exam)
    except ExamValidationError as exc:
        problems = {**exc.problems, **problems}
    if problems:
        db.rollback()
        return _render_form(
            request, db, services, user, parsed.data, exam=exam, problems=problems, raw=raw,
            status=400,
        )  # fmt: skip
    if exam is None:
        created = services.exams.create(db, user, parsed.data, ip=client_ip(request))
        return _redirect(f"/examens/{created.id}/instellingen")
    services.exams.update(db, user, exam, parsed.data, ip=client_ip(request))
    return _redirect(f"/examens/{exam.id}/instellingen?ok=saved")


@router.post("/nieuw")
def create_exam(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    if not can(ctx.user, SystemAction.CREATE_EXAM):
        raise PermissionDeniedError()
    return _handle_form(request, db, services, ctx.user, None)


@router.get("/{exam_id:uuid}/bewerken")
def edit_exam(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.EDIT_SETTINGS)
    data = services.exams.input_from_exam(db, exam)
    return _render_form(request, db, services, ctx.user, data, exam=exam)


@router.post("/{exam_id:uuid}/bewerken")
def update_exam(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.EDIT_SETTINGS)
    return _handle_form(request, db, services, ctx.user, exam)


# Detail --------------------------------------------------------------------------


def _perms(user: User, exam: Exam) -> dict[str, bool]:
    ref = ExamRef(owner_id=exam.owner_id, second_reviewer_id=exam.second_reviewer_id)
    return {action.value: can_exam(user, action, ref) for action in ExamAction}


def _qr_svg(url: str, *, scale: int = 4, omit_size: bool = True) -> str:
    buffer = io.BytesIO()
    segno.make(url, error="m").save(
        buffer, kind="svg", scale=scale, border=QUIET_ZONE, xmldecl=False, omitsize=omit_size
    )
    return buffer.getvalue().decode()


@router.get("/{exam_id:uuid}/instellingen")
def exam_settings(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    ok: str | None = None,
    error: str | None = None,
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    url = public_url(services, exam)
    owner = db.get(User, exam.owner_id)
    reviewer = db.get(User, exam.second_reviewer_id) if exam.second_reviewer_id else None
    effective_end = services.exams.effective_end(db, exam)
    default_date, default_time = split_local(format_local(effective_end, services.env.timezone))
    return render(
        request,
        "teacher/exam_settings.html.j2",
        extension_default_date=default_date,
        extension_default_time=default_time,
        exam=exam,
        owner=owner,
        reviewer=reviewer,
        status=services.exams.status(db, exam),
        status_labels=status_labels(),
        effective_end=effective_end,
        url=url,
        qr_svg=_qr_svg(url, scale=3),
        perms=_perms(ctx.user, exam),
        locked=services.exams.has_submissions(db, exam),
        roster=services.exams.roster(db, exam),
        class_lists=services.classlists.usable(db, ctx.user),
        team_names=services.teams.names(db),
        owner_names=services.users.names(db),
        extensions=services.exams.extensions(db, exam),
        submissions=services.exams.submission_count(db, exam),
        ok=ok,
        errors=[describe(DomainError(error))[0]] if error else [],
    )


@router.get("/{exam_id:uuid}/geavanceerd")
def exam_advanced(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    ok: str | None = None, error: str | None = None,
) -> Response:  # fmt: skip
    """Rarely used and far-reaching actions: duplicate, back to concept, delete, save as
    template, transfer ownership, and the retention period with the retention hold."""
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    return render(
        request, "teacher/exam_advanced.html.j2", exam=exam,
        status=services.exams.status(db, exam), status_labels=status_labels(),
        perms=_perms(ctx.user, exam), submissions=services.exams.submission_count(db, exam),
        teachers=services.users.teachers(db) if ctx.user.is_admin else [],
        retention=services.lifecycle.retention(db, exam), now=services.clock.now(),
        ok=ok, errors=[describe(DomainError(error))[0]] if error else [],
    )  # fmt: skip


def _action(
    request: Request,
    exam_id: uuid.UUID,
    db: Session,
    services: Services,
    user: User,
    action: ExamAction,
    fn: Any,
    ok: str,
    tab: str = "instellingen",
) -> Response:
    exam = _load(db, services, user, exam_id, action)
    try:
        fn(exam)
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/{tab}?error={exc.code}")
    return _redirect(f"/examens/{exam.id}/{tab}?ok={ok}")


@router.post("/{exam_id:uuid}/publiceren")
def publish(request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc) -> Response:
    return _action(
        request, exam_id, db, services, ctx.user, ExamAction.EDIT_SETTINGS,
        lambda e: services.exams.publish(db, ctx.user, e, ip=client_ip(request)), "published",
    )  # fmt: skip


@router.post("/{exam_id:uuid}/concept")
def unpublish(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    return _action(
        request, exam_id, db, services, ctx.user, ExamAction.EDIT_SETTINGS,
        lambda e: services.exams.unpublish(db, ctx.user, e, ip=client_ip(request)), "unpublished",
        tab="geavanceerd",
    )  # fmt: skip


@router.post("/{exam_id:uuid}/code")
def regenerate_code(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    return _action(
        request, exam_id, db, services, ctx.user, ExamAction.EDIT_SETTINGS,
        lambda e: services.exams.regenerate_code(db, ctx.user, e, ip=client_ip(request)), "code",
    )  # fmt: skip


@router.post("/{exam_id:uuid}/dupliceren")
def duplicate(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DUPLICATE)
    copy = services.exams.duplicate(db, ctx.user, exam, ip=client_ip(request))
    return _redirect(f"/examens/{copy.id}/bewerken")


@router.post("/{exam_id:uuid}/sjabloon-opslaan")
def save_template(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    name: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DUPLICATE)
    template = services.exams.save_as_template(db, ctx.user, exam, name, ip=client_ip(request))
    return _redirect(f"/examens/sjablonen/{template.id}?ok=saved_from_exam")


@router.post("/{exam_id:uuid}/verwijderen")
def delete_exam(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DELETE_CONCEPT)
    try:
        keys = services.exams.delete_concept(db, ctx.user, exam, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/geavanceerd?error={exc.code}")
    _delete_unreferenced(db, services, list(keys))
    return _redirect("/examens")


@router.post("/{exam_id:uuid}/klassenlijst")
def set_roster(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    class_list_id: Annotated[str, Form()] = "",
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.EDIT_SETTINGS)
    target = uuid.UUID(class_list_id) if class_list_id else None
    try:
        services.exams.set_roster(db, ctx.user, exam, target, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/instellingen?error={exc.code}")
    return _redirect(f"/examens/{exam.id}/instellingen?ok=roster")


@router.post("/{exam_id:uuid}/verlengingen")
def grant_extension(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    student_number: Annotated[str, Form(max_length=32)] = "",
    ends_at: Annotated[str, Form(max_length=32)] = "",
    ends_at_date: Annotated[str | None, Form(max_length=16)] = None,
    ends_at_time: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.MANAGE_SUBMISSIONS)
    fields = {"ends_at": ends_at, "ends_at_time": ends_at_time}
    if ends_at_date is not None:
        fields["ends_at_date"] = ends_at_date
    try:
        when = parse_local(read_datetime(fields, "ends_at"), services.env.timezone)
        services.exams.grant_extension(
            db, ctx.user, exam, student_number, when, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/instellingen?error={exc.code}#verlengingen")
    return _redirect(f"/examens/{exam.id}/instellingen?ok=extension#verlengingen")


@router.post("/{exam_id:uuid}/verlengingen/{grant_id}/verwijderen")
def revoke_extension(
    request: Request, exam_id: uuid.UUID, grant_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.MANAGE_SUBMISSIONS)
    services.exams.revoke_extension(db, ctx.user, exam, grant_id, ip=client_ip(request))
    return _redirect(f"/examens/{exam.id}/instellingen?ok=extension_removed#verlengingen")


@router.post("/{exam_id:uuid}/eigenaar")
def transfer_ownership(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    owner_id: Annotated[str, Form()] = "",
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.TRANSFER_OWNERSHIP)
    try:
        services.exams.transfer_ownership(
            db, ctx.user, exam, uuid.UUID(owner_id), ip=client_ip(request)
        )
    except (DomainError, ValueError) as exc:
        db.rollback()
        code = exc.code if isinstance(exc, DomainError) else "owner_invalid"
        return _redirect(f"/examens/{exam.id}/geavanceerd?error={code}")
    return _redirect(f"/examens/{exam.id}/geavanceerd?ok=owner")


# Template files ------------------------------------------------------------------


@router.post("/{exam_id:uuid}/vakjes/{slot_id}/sjabloon")
def upload_template_file(
    request: Request,
    exam_id: uuid.UUID,
    slot_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    file: Annotated[UploadFile, File()],
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.EDIT_SETTINGS)
    if services.exams.has_submissions(db, exam):
        return _redirect(f"/examens/{exam.id}/instellingen?error=requirements_locked")
    filename = (file.filename or "sjabloon").replace("\\", "/").rsplit("/", 1)[-1]

    async def chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(UPLOAD_CHUNK):
            yield chunk

    try:
        stored = from_thread.run(
            lambda: store_template(services.storage, chunks(), max_bytes=exam.max_file_bytes)
        )
        replaced = services.exams.attach_template_file(
            db, ctx.user, exam, slot_id, stored, filename, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/instellingen?error={exc.code}")
    _delete_unreferenced(db, services, [replaced])
    return _redirect(f"/examens/{exam.id}/instellingen?ok=template_uploaded")


@router.post("/{exam_id:uuid}/vakjes/{slot_id}/sjabloon/verwijderen")
def remove_template_file(
    request: Request, exam_id: uuid.UUID, slot_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.EDIT_SETTINGS)
    try:
        key = services.exams.remove_template_file(
            db, ctx.user, exam, slot_id, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/instellingen?error={exc.code}")
    _delete_unreferenced(db, services, [key])
    return _redirect(f"/examens/{exam.id}/instellingen?ok=template_removed")


@router.get("/{exam_id:uuid}/vakjes/{slot_id}/sjabloon")
def download_template_file(
    exam_id: uuid.UUID, slot_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    slot = next((s for s in exam.expected_files if s.id == slot_id), None)
    if slot is None or slot.template_file is None:
        raise NotFoundError()
    template = slot.template_file
    return stream_download(
        services.storage.open(template.storage_key), template.original_name, size=template.size
    )


# QR and presentation -------------------------------------------------------------


@router.get("/{exam_id:uuid}/qr.svg")
def qr_svg(exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    svg = _qr_svg(public_url(services, exam), scale=10, omit_size=False)
    return bytes_download(svg.encode(), f"{exam.slug}-qr.svg", "image/svg+xml")


@router.get("/{exam_id:uuid}/qr.png")
def qr_png(exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    buffer = io.BytesIO()
    segno.make(public_url(services, exam), error="m").save(
        buffer, kind="png", scale=16, border=QUIET_ZONE
    )
    return bytes_download(buffer.getvalue(), f"{exam.slug}-qr.png", "image/png")


def _counter(db: Session, services: Services, exam: Exam) -> dict[str, Any]:
    return {
        "submitted": services.exams.handed_in_count(db, exam),
        "expected": services.exams.roster_count(db, exam),
        "status": services.exams.status(db, exam),
    }


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


@router.get("/{exam_id:uuid}/presentatie")
def presentation(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    url = public_url(services, exam)
    return render(
        request,
        "teacher/presentation.html.j2",
        exam=exam,
        url=url,
        short_url=url.split("://", 1)[-1],
        qr_svg=_qr_svg(url, scale=8),
        server_now_ms=_ms(services.clock.now()),
        start_ms=_ms(exam.starts_at),
        end_ms=_ms(exam.ends_at),
        status_labels=status_labels(),
        **_counter(db, services, exam),
    )


@router.get("/{exam_id:uuid}/presentatie/teller")
def presentation_counter(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    return render(
        request, "teacher/_counter.html.j2", exam=exam, status_labels=status_labels(),
        **_counter(db, services, exam),
    )  # fmt: skip
