"""Teacher pages for exams: dashboard tabs, create/edit, detail, actions, QR, presentation."""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

import segno
from anyio import from_thread
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from inleverboks.db.models import Exam, IpPreset, User
from inleverboks.i18n import gettext as _
from inleverboks.policy import ExamAction, ExamRef, SystemAction, can, can_exam
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.services.exams import ExamInput, ExamStatus, ExamValidationError
from inleverboks.services.template_files import store_template, unreferenced
from inleverboks.services.timeutil import default_window, parse_local
from inleverboks.web.deps import Db, Svc, Teacher, client_ip
from inleverboks.web.downloads import bytes_download, stream_download
from inleverboks.web.exam_form import field_messages, form_values, parse_exam_form
from inleverboks.web.messages import describe
from inleverboks.web.templating import render

router = APIRouter(prefix="/docent")

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
        **extra,
    )


@router.get("")
def dashboard(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    rows = _rows(db, services, services.exams.owned(db, ctx.user))
    return _dashboard(request, "mine", rows)


@router.get("/gedeeld")
def shared(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    return _dashboard(
        request, "shared", _rows(db, services, services.exams.shared_with(db, ctx.user))
    )


@router.get("/afgerond")
def finalized(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    return _dashboard(
        request, "finalized", _rows(db, services, services.exams.finalized(db, ctx.user))
    )


@router.get("/alle")
def all_exams(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    if not ctx.user.is_admin:
        raise PermissionDeniedError()
    return _dashboard(request, "all", _rows(db, services, services.exams.all_exams(db)))


@router.get("/sjablonen")
def exam_templates(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    return render(
        request, "teacher/templates.html.j2", tab="templates",
        templates=services.exams.templates(db, ctx.user),
    )  # fmt: skip


@router.post("/sjablonen/{template_id}/verwijderen")
def delete_exam_template(
    request: Request, template_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    keys = services.exams.delete_template(db, ctx.user, template_id, ip=client_ip(request))
    _delete_unreferenced(db, services, list(keys))
    return _redirect("/docent/sjablonen")


# Create / edit -------------------------------------------------------------------


def _form_context(db: Session, services: Services, user: User) -> dict[str, Any]:
    teachers = [u for u in services.users.teachers(db) if u.id != user.id]
    presets = list(db.query(IpPreset).order_by(IpPreset.name))
    return {
        "teachers": teachers,
        "presets": presets,
        "class_lists": services.classlists.library(db, user),
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
) -> Response:
    tz = services.env.timezone
    locked = exam is not None and services.exams.has_submissions(db, exam)
    return render(
        request,
        "teacher/exam_form.html.j2",
        status_code=status,
        exam=exam,
        values=form_values(data, tz, raw),
        problems=problems or {},
        locked=locked,
        base_url=services.env.app_base_url,
        **_form_context(db, services, user),
    )


@router.get("/examens/nieuw")
def new_exam(
    request: Request, ctx: Teacher, db: Db, services: Svc, sjabloon: uuid.UUID | None = None
) -> Response:
    start, end = default_window(services.clock.now(), services.env.timezone)
    data = services.exams.defaults(db, start, end)
    if sjabloon is not None:
        template = services.exams.get_template(db, ctx.user, sjabloon)
        data = services.exams.input_from_template(db, template, data)
    return _render_form(request, db, services, ctx.user, data, exam=None)


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
    raw = {"starts_at": str(form.get("starts_at") or ""), "ends_at": str(form.get("ends_at") or "")}
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
        return _redirect(f"/docent/examens/{created.id}")
    services.exams.update(db, user, exam, parsed.data, ip=client_ip(request))
    return _redirect(f"/docent/examens/{exam.id}?ok=saved")


@router.post("/examens/nieuw")
def create_exam(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    if not can(ctx.user, SystemAction.CREATE_EXAM):
        raise PermissionDeniedError()
    return _handle_form(request, db, services, ctx.user, None)


@router.get("/examens/{exam_id}/bewerken")
def edit_exam(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.EDIT_SETTINGS)
    data = services.exams.input_from_exam(db, exam)
    return _render_form(request, db, services, ctx.user, data, exam=exam)


@router.post("/examens/{exam_id}/bewerken")
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
        buffer, kind="svg", scale=scale, border=2, xmldecl=False, omitsize=omit_size
    )
    return buffer.getvalue().decode()


@router.get("/examens/{exam_id}")
def exam_detail(
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
    return render(
        request,
        "teacher/exam_detail.html.j2",
        exam=exam,
        owner=owner,
        reviewer=reviewer,
        status=services.exams.status(db, exam),
        status_labels=status_labels(),
        effective_end=services.exams.effective_end(db, exam),
        url=url,
        qr_svg=_qr_svg(url, scale=3),
        perms=_perms(ctx.user, exam),
        locked=services.exams.has_submissions(db, exam),
        roster=services.exams.roster(db, exam),
        class_lists=services.classlists.library(db, ctx.user),
        extensions=services.exams.extensions(db, exam),
        teachers=services.users.teachers(db) if ctx.user.is_admin else [],
        submissions=services.exams.submission_count(db, exam),
        ok=ok,
        errors=[describe(DomainError(error))[0]] if error else [],
    )


def _action(
    request: Request,
    exam_id: uuid.UUID,
    db: Session,
    services: Services,
    user: User,
    action: ExamAction,
    fn: Any,
    ok: str,
) -> Response:
    exam = _load(db, services, user, exam_id, action)
    try:
        fn(exam)
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/docent/examens/{exam.id}?error={exc.code}")
    return _redirect(f"/docent/examens/{exam.id}?ok={ok}")


@router.post("/examens/{exam_id}/publiceren")
def publish(request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc) -> Response:
    return _action(
        request, exam_id, db, services, ctx.user, ExamAction.EDIT_SETTINGS,
        lambda e: services.exams.publish(db, ctx.user, e, ip=client_ip(request)), "published",
    )  # fmt: skip


@router.post("/examens/{exam_id}/concept")
def unpublish(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    return _action(
        request, exam_id, db, services, ctx.user, ExamAction.EDIT_SETTINGS,
        lambda e: services.exams.unpublish(db, ctx.user, e, ip=client_ip(request)), "unpublished",
    )  # fmt: skip


@router.post("/examens/{exam_id}/code")
def regenerate_code(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    return _action(
        request, exam_id, db, services, ctx.user, ExamAction.EDIT_SETTINGS,
        lambda e: services.exams.regenerate_code(db, ctx.user, e, ip=client_ip(request)), "code",
    )  # fmt: skip


@router.post("/examens/{exam_id}/dupliceren")
def duplicate(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DUPLICATE)
    copy = services.exams.duplicate(db, ctx.user, exam, ip=client_ip(request))
    return _redirect(f"/docent/examens/{copy.id}/bewerken")


@router.post("/examens/{exam_id}/sjabloon-opslaan")
def save_template(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    name: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DUPLICATE)
    services.exams.save_as_template(db, ctx.user, exam, name, ip=client_ip(request))
    return _redirect(f"/docent/examens/{exam.id}?ok=template_saved")


@router.post("/examens/{exam_id}/verwijderen")
def delete_exam(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.DELETE_CONCEPT)
    try:
        keys = services.exams.delete_concept(db, ctx.user, exam, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/docent/examens/{exam.id}?error={exc.code}")
    _delete_unreferenced(db, services, list(keys))
    return _redirect("/docent")


@router.post("/examens/{exam_id}/klassenlijst")
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
        return _redirect(f"/docent/examens/{exam.id}?error={exc.code}")
    return _redirect(f"/docent/examens/{exam.id}?ok=roster")


@router.post("/examens/{exam_id}/verlengingen")
def grant_extension(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    student_number: Annotated[str, Form(max_length=32)] = "",
    ends_at: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.MANAGE_SUBMISSIONS)
    try:
        when = parse_local(ends_at, services.env.timezone)
        services.exams.grant_extension(
            db, ctx.user, exam, student_number, when, ip=client_ip(request)
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/docent/examens/{exam.id}?error={exc.code}#verlengingen")
    return _redirect(f"/docent/examens/{exam.id}?ok=extension#verlengingen")


@router.post("/examens/{exam_id}/verlengingen/{grant_id}/verwijderen")
def revoke_extension(
    request: Request, exam_id: uuid.UUID, grant_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.MANAGE_SUBMISSIONS)
    services.exams.revoke_extension(db, ctx.user, exam, grant_id, ip=client_ip(request))
    return _redirect(f"/docent/examens/{exam.id}?ok=extension_removed#verlengingen")


@router.post("/examens/{exam_id}/eigenaar")
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
        return _redirect(f"/docent/examens/{exam.id}?error={code}")
    return _redirect(f"/docent/examens/{exam.id}?ok=owner")


# Template files ------------------------------------------------------------------


@router.post("/examens/{exam_id}/vakjes/{slot_id}/sjabloon")
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
        return _redirect(f"/docent/examens/{exam.id}?error=requirements_locked")
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
        return _redirect(f"/docent/examens/{exam.id}?error={exc.code}")
    _delete_unreferenced(db, services, [replaced])
    return _redirect(f"/docent/examens/{exam.id}?ok=template_uploaded")


@router.post("/examens/{exam_id}/vakjes/{slot_id}/sjabloon/verwijderen")
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
        return _redirect(f"/docent/examens/{exam.id}?error={exc.code}")
    _delete_unreferenced(db, services, [key])
    return _redirect(f"/docent/examens/{exam.id}?ok=template_removed")


@router.get("/examens/{exam_id}/vakjes/{slot_id}/sjabloon")
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


@router.get("/examens/{exam_id}/qr.svg")
def qr_svg(exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    svg = _qr_svg(public_url(services, exam), scale=10, omit_size=False)
    return bytes_download(svg.encode(), f"{exam.slug}-qr.svg", "image/svg+xml")


@router.get("/examens/{exam_id}/qr.png")
def qr_png(exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    buffer = io.BytesIO()
    segno.make(public_url(services, exam), error="m").save(buffer, kind="png", scale=16, border=2)
    return bytes_download(buffer.getvalue(), f"{exam.slug}-qr.png", "image/png")


def _counter(db: Session, services: Services, exam: Exam) -> dict[str, Any]:
    return {
        "submitted": services.exams.submission_count(db, exam),
        "expected": services.exams.roster_count(db, exam),
        "status": services.exams.status(db, exam),
    }


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


@router.get("/examens/{exam_id}/presentatie")
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


@router.get("/examens/{exam_id}/presentatie/teller")
def presentation_counter(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = _load(db, services, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    return render(
        request, "teacher/_counter.html.j2", exam=exam, status_labels=status_labels(),
        **_counter(db, services, exam),
    )  # fmt: skip
