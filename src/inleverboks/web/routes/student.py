"""Student pages at ``/{slug}``: code, student number, files, upload and receipt.

There is deliberately no route that returns a handed-in file to a student.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Callable
from html import escape
from pathlib import Path
from typing import Annotated, Any, TypeVar

import structlog
from anyio import to_thread
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from inleverboks.db.models import Exam
from inleverboks.i18n import gettext as _
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, NotFoundError
from inleverboks.services.slugs import SLUG_RE
from inleverboks.services.student_flow import Entry, Stage, StudentState, abbreviate
from inleverboks.services.submissions import Plan, Receipt
from inleverboks.web.deps import Db, Svc, client_ip
from inleverboks.web.downloads import bytes_download
from inleverboks.web.templating import render
from inleverboks.web.upload import ParsedUpload, UploadError, parse_upload

router = APIRouter()
log = structlog.get_logger(__name__)

COOKIE = "ib_student"
INACTIVITY_SECONDS = 15 * 60
RECEIPT_RESET_SECONDS = 5 * 60
T = TypeVar("T")


def messages() -> dict[str, str]:
    return {
        "code_invalid": _("The access code is incorrect."),
        "too_many": _("Too many attempts. Wait a moment and try again."),
        "number_invalid": _("This student number is not valid."),
        "already_submitted": _("You have already handed in. Report to the teacher."),
        "not_on_list": _("Check your student number or report to the teacher."),
        "names_required": _("Enter your first name and last name."),
        "closed": _("Closed"),
        "hash_mismatch": _("Upload failed, try again."),
        "bad_request": _("Upload failed, try again."),
        "storage": _("Upload failed, try again."),
        "session": _("Your session has expired. Start again."),
        "declaration_required": _("Confirm the declaration first."),
        "warnings_not_confirmed": _("Confirm the warnings first."),
        "no_files": _("Choose at least one file."),
        "too_many_files": _("Too many files."),
        "file_too_large": _("A file is too large."),
        "submission_too_large": _("The files are too large together."),
        "extension_not_allowed": _("A file type is not allowed."),
        "extra_not_allowed": _("Extra files are not allowed."),
        "slot_taken": _("Each expected file can hold one file."),
        "unknown_slot": _("Upload failed, try again."),
    }


def warning_labels() -> dict[str, str]:
    return {
        "empty_template": _("Identical to the empty template"),
        "temp_file": _("Temporary or lock file of Office"),
        "empty_file": _("The file is empty (0 bytes)"),
        "duplicate_file": _("The same file is chosen twice"),
        "name_mismatch": _("The file name differs from the expected name"),
        "count_mismatch": _("Number of files differs from the expected number"),
        "empty_slot": _("No file for expected file"),
    }


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _exam(db: Session, services: Services, slug: str) -> Exam:
    if not SLUG_RE.fullmatch(slug):
        raise NotFoundError()
    exam = services.exams.by_slug(db, slug)
    if exam is None:
        raise NotFoundError()
    return exam


def _state(request: Request, services: Services, exam: Exam) -> StudentState:
    codec = request.app.state.student_codec
    return codec.loads(request.cookies.get(COOKIE), exam) or services.flow.new_state(exam)


def _with_state(
    request: Request, response: Response, exam: Exam, state: StudentState | None
) -> Response:
    path = f"/{exam.slug}"
    if state is None:
        response.delete_cookie(COOKIE, path=path)
        return response
    response.set_cookie(
        COOKIE,
        request.app.state.student_codec.dumps(state),
        path=path,
        httponly=True,
        samesite="strict",
        secure=request.app.state.settings.session_cookie_secure,
    )
    return response


def _page(
    request: Request,
    template: str,
    exam: Exam,
    services: Services,
    *,
    status: int = 200,
    **ctx: Any,
) -> Response:
    now = services.clock.now()
    response = render(
        request,
        template,
        status_code=status,
        exam=exam,
        now=now,
        inactivity_seconds=INACTIVITY_SECONDS,
        **ctx,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _status_page(
    request: Request, db: Session, services: Services, exam: Exam, entry: Entry
) -> Response:
    response = _page(request, "student/status.html.j2", exam, services, entry=entry.value)
    return _with_state(request, response, exam, None)


def _step(
    request: Request,
    db: Session,
    services: Services,
    exam: Exam,
    state: StudentState,
    *,
    error: str | None = None,
    status: int = 200,
) -> Response:
    templates = {
        Stage.CODE: "student/code.html.j2",
        Stage.NUMBER: "student/number.html.j2",
        Stage.CONFIRM: "student/confirm.html.j2",
        Stage.NAMES: "student/names.html.j2",
    }
    stage = Stage(state.stage)
    if stage is Stage.READY:
        return _upload_page(request, db, services, exam, state)
    end = services.exams.effective_end(db, exam)
    response = _page(
        request,
        templates[stage],
        exam,
        services,
        status=status,
        state=state,
        error=messages().get(error, error) if error else None,
        short_name=abbreviate(state.first_name, state.infix, state.last_name),
        remaining=max(0, int((exam.ends_at - services.clock.now()).total_seconds())),
        end=end,
    )
    return _with_state(request, response, exam, state)


def _client_config(
    db: Session, services: Services, exam: Exam, state: StudentState
) -> dict[str, Any]:
    rules = services.submissions.rules_for(db, exam)
    end = services.exams.end_for_student(db, exam, state.student_number)
    return {
        "slug": exam.slug,
        "uploadUrl": f"/{exam.slug}/inleveren",
        "resetUrl": f"/{exam.slug}/opnieuw",
        "slots": [
            {"id": str(s.id), "label": s.label, "pattern": s.pattern, "extensions": s.extensions}
            for s in rules.slots
        ],
        "extensions": rules.extensions,
        "allowExtra": rules.allow_extra_file,
        "maxFileBytes": rules.max_file_bytes,
        "maxSubmissionBytes": rules.max_submission_bytes,
        "maxFiles": rules.max_files,
        "templateHashes": sorted(rules.template_hashes),
        "serverNow": int(services.clock.now().timestamp() * 1000),
        "end": int(end.timestamp() * 1000),
        "inactivitySeconds": INACTIVITY_SECONDS,
        "receiptResetSeconds": RECEIPT_RESET_SECONDS,
        "previewFrame": "/static/preview/frame.html",
        "text": client_texts(),
    }


def client_texts() -> dict[str, str]:
    """All strings the student JavaScript shows, translated on the server."""
    return {
        "slotLabel": _("Expected file"),
        "extra": _("Extra file"),
        "expectedName": _("Expected name"),
        "choose": _("Choose file"),
        "chooseMany": _("Choose files"),
        "dropHere": _("or drag files here"),
        "remove": _("Remove"),
        "preview": _("Preview"),
        "copy": _("Copy"),
        "copied": _("Copied"),
        "size": _("Size"),
        "slot": _("Expected file"),
        "hashing": _("Calculating SHA-256…"),
        "previewNote": _("Preview may differ from the layout in Office."),
        "noPreview": _("No preview available for this file type."),
        "zipNote": _("The content of a zip file is not checked."),
        "close": _("Close"),
        "submit": _("Hand in"),
        "uploading": _("Uploading…"),
        "uploadFailed": _("Upload failed, try again."),
        "confirmTitle": _("Are you sure?"),
        "confirmIntro": _("Your hand-in differs from what is expected:"),
        "confirmYes": _("Yes, hand in anyway"),
        "confirmNo": _("No, go back"),
        "declarationRequired": _("Confirm the declaration first."),
        "noFiles": _("Choose at least one file."),
        "inactiveTitle": _("Are you still there?"),
        "inactiveBody": _("The page is reset in %s seconds."),
        "inactiveStay": _("Continue"),
        "timeLeft": _("Time remaining"),
        "closed": _("Closed"),
        "w_empty_template": _("Identical to the empty template"),
        "w_temp_file": _("Temporary or lock file of Office"),
        "w_empty_file": _("The file is empty (0 bytes)"),
        "w_duplicate_file": _("The same file is chosen twice"),
        "w_name_mismatch": _("The file name differs from the expected name"),
        "w_count_mismatch": _("You hand in %s files; %s are expected"),
        "w_empty_slot": _("No file chosen for: %s"),
        "e_extension_not_allowed": _("This file type is not allowed"),
        "e_file_too_large": _("The file is too large"),
        "e_submission_too_large": _("The files are too large together"),
        "e_too_many_files": _("Too many files"),
        "e_slot_taken": _("This expected file already has a file"),
        "e_extra_not_allowed": _("Extra files are not allowed"),
    }


def _upload_page(
    request: Request, db: Session, services: Services, exam: Exam, state: StudentState
) -> Response:
    response = _page(
        request,
        "student/upload.html.j2",
        exam,
        services,
        state=state,
        config_json=json.dumps(_client_config(db, services, exam, state)),
    )
    return _with_state(request, response, exam, state)


def _guard(
    request: Request, db: Session, services: Services, slug: str
) -> tuple[Exam, StudentState, Response | None]:
    exam = _exam(db, services, slug)
    entry = services.flow.entry(db, exam, client_ip(request))
    state = _state(request, services, exam)
    if entry is Entry.IP_DENIED:
        services.flow.record_denied(db, exam, client_ip(request), "ip")
    if entry is not Entry.OPEN:
        if entry is Entry.CLOSED:
            services.flow.record_denied(db, exam, client_ip(request), "closed")
        return exam, state, _status_page(request, db, services, exam, entry)
    return exam, state, None


# Pages -------------------------------------------------------------------------------


@router.get("/{slug}", include_in_schema=False)
def exam_page(request: Request, slug: str, db: Db, services: Svc) -> Response:
    exam = _exam(db, services, slug)
    entry = services.flow.entry(db, exam, client_ip(request))
    if entry is Entry.IP_DENIED:
        services.flow.record_denied(db, exam, client_ip(request), "ip")
    if entry is not Entry.OPEN:
        return _status_page(request, db, services, exam, entry)
    state = _state(request, services, exam)
    if state.stage == Stage.READY and services.flow.blocked_by_previous(
        db, exam, state.student_number
    ):
        state = services.flow.new_state(exam)
    return _step(request, db, services, exam, state)


@router.get("/{slug}/opnieuw", include_in_schema=False)
def reset(request: Request, slug: str, db: Db, services: Svc) -> Response:
    exam = _exam(db, services, slug)
    return _with_state(request, _redirect(f"/{exam.slug}"), exam, None)


@router.post("/{slug}/code", include_in_schema=False)
def submit_code(
    request: Request,
    slug: str,
    db: Db,
    services: Svc,
    code: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    exam, state, closed = _guard(request, db, services, slug)
    if closed:
        return closed
    if state.stage != Stage.CODE:
        return _redirect(f"/{exam.slug}")
    key = f"{exam.id}:{client_ip(request)}"
    if services.limiter.status("access_code", key).blocked:
        return _step(request, db, services, exam, state, error="too_many", status=429)
    if not services.flow.check_code(exam, code):
        services.limiter.fail("access_code", key)
        services.flow.record_denied(db, exam, client_ip(request), "code")
        return _step(request, db, services, exam, state, error="code_invalid", status=400)
    state.stage = Stage.NUMBER.value
    return _with_state(request, _redirect(f"/{exam.slug}"), exam, state)


@router.post("/{slug}/student", include_in_schema=False)
def submit_number(  # noqa: PLR0911 - one early return per refusal reason
    request: Request,
    slug: str,
    db: Db,
    services: Svc,
    student_number: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    exam, state, closed = _guard(request, db, services, slug)
    if closed:
        return closed
    if state.stage != Stage.NUMBER:
        return _redirect(f"/{exam.slug}")
    key = f"{exam.id}:{client_ip(request)}"
    if services.limiter.status("student_lookup", key).blocked:
        return _step(request, db, services, exam, state, error="too_many", status=429)
    number = student_number.strip()
    if not services.flow.number_valid(db, number):
        services.limiter.fail("student_lookup", key)
        return _step(request, db, services, exam, state, error="number_invalid", status=400)
    if services.flow.blocked_by_previous(db, exam, number):
        return _step(request, db, services, exam, state, error="already_submitted", status=409)
    if not services.flow.is_open_for(db, exam, number, services.clock.now()):
        return _status_page(request, db, services, exam, Entry.CLOSED)
    entry = services.flow.roster_entry(db, exam, number)
    state.student_number = number
    if entry is not None:
        state.first_name, state.infix, state.last_name = (
            entry.first_name,
            entry.infix or "",
            entry.last_name,
        )
        state.stage = Stage.CONFIRM.value
    elif services.flow.has_roster(db, exam) and not exam.allow_unlisted:
        services.limiter.fail("student_lookup", key)
        state.student_number = ""
        return _step(request, db, services, exam, state, error="not_on_list", status=400)
    else:
        state.first_name = state.infix = state.last_name = ""
        state.stage = Stage.NAMES.value
    return _with_state(request, _redirect(f"/{exam.slug}"), exam, state)


@router.post("/{slug}/bevestigen", include_in_schema=False)
def confirm_identity(
    request: Request, slug: str, db: Db, services: Svc, answer: Annotated[str, Form()] = ""
) -> Response:
    exam, state, closed = _guard(request, db, services, slug)
    if closed:
        return closed
    if state.stage != Stage.CONFIRM:
        return _redirect(f"/{exam.slug}")
    if answer != "yes":
        state = StudentState(exam_id=str(exam.id), stage=Stage.NUMBER.value)
        return _step(request, db, services, exam, state, error="not_on_list")
    state.on_roster = True
    state.stage = Stage.READY.value
    return _with_state(request, _redirect(f"/{exam.slug}"), exam, state)


@router.post("/{slug}/naam", include_in_schema=False)
def submit_names(
    request: Request,
    slug: str,
    db: Db,
    services: Svc,
    first_name: Annotated[str, Form(max_length=100)] = "",
    infix: Annotated[str, Form(max_length=50)] = "",
    last_name: Annotated[str, Form(max_length=150)] = "",
) -> Response:
    exam, state, closed = _guard(request, db, services, slug)
    if closed:
        return closed
    if state.stage != Stage.NAMES:
        return _redirect(f"/{exam.slug}")
    first, last = " ".join(first_name.split()), " ".join(last_name.split())
    if not first or not last:
        return _step(request, db, services, exam, state, error="names_required", status=400)
    state.first_name, state.infix, state.last_name = first, " ".join(infix.split()), last
    state.on_roster = False
    state.stage = Stage.READY.value
    return _with_state(request, _redirect(f"/{exam.slug}"), exam, state)


# Upload ------------------------------------------------------------------------------


def _error(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": code, "message": messages().get(code, messages()["bad_request"])},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def _file_chunks(path: Path, size: int = 256 * 1024) -> AsyncIterator[bytes]:
    with path.open("rb") as fh:
        while chunk := await to_thread.run_sync(fh.read, size):
            yield chunk


class _UploadContext:
    """Per-request helper: runs database work in a worker thread with its own session."""

    def __init__(self, request: Request, slug: str) -> None:
        self.request = request
        self.slug = slug
        self.services: Services = request.app.state.services
        self.started_at = self.services.clock.now()
        self.ip = client_ip(request)
        self.exam: Exam | None = None
        self.state: StudentState | None = None

    async def db(self, fn: Callable[[Session], T]) -> T:
        def run() -> T:
            with self.request.app.state.sessionmaker() as session:
                return fn(session)

        return await to_thread.run_sync(run)

    def exam_in(self, db: Session) -> Exam:
        assert self.exam is not None  # noqa: S101 - set before any call
        return db.merge(self.exam)

    async def failed(self, code: str) -> None:
        if self.exam is not None:
            await self.db(
                lambda db: self.services.submissions.record_failed(
                    db, self.exam_in(db), self.state, code, ip=self.ip
                )
            )


async def _load(ctx: _UploadContext) -> tuple[Entry | None, int]:
    services = ctx.services

    def load(db: Session) -> tuple[Exam | None, Entry | None, StudentState | None, int]:
        exam = services.exams.by_slug(db, ctx.slug) if SLUG_RE.fullmatch(ctx.slug) else None
        if exam is None:
            return None, None, None, 0
        db.expunge(exam)
        state = ctx.request.app.state.student_codec.loads(ctx.request.cookies.get(COOKIE), exam)
        max_files = services.settings.load(db).max_files_per_submission
        return exam, services.flow.entry(db, exam, ctx.ip), state, max_files

    ctx.exam, entry, ctx.state, max_files = await ctx.db(load)
    return entry, max_files


async def _store(ctx: _UploadContext, plan: Plan, stored: list[str]) -> None:
    """Temporary key first; promote to the final key once every file is written."""
    storage = ctx.services.storage
    for file in plan.files:
        await storage.put(file.temp_key, _file_chunks(file.path))
        stored.append(file.temp_key)
    for file in plan.files:
        await storage.move(file.temp_key, file.storage_key)
        stored.remove(file.temp_key)
        stored.append(file.storage_key)


async def _process(ctx: _UploadContext, parsed: ParsedUpload, limiter_key: str) -> Response:
    services, exam, state = ctx.services, ctx.exam, ctx.state
    assert exam is not None and state is not None  # noqa: S101 - checked by caller
    fields = parsed.fields

    def prepare(db: Session) -> Plan:
        return services.submissions.prepare(
            db,
            ctx.exam_in(db),
            state,
            manifest_json=fields.get("manifest", ""),
            received=parsed.files,
            idempotency_key=fields.get("idempotency_key", ""),
            declaration=fields.get("declaration") == "accepted",
            warnings_confirmed=fields.get("warnings_confirmed") == "true",
            started_at=ctx.started_at,
            ip=ctx.ip,
        )

    try:
        plan = await ctx.db(prepare)
    except DomainError as exc:
        code = exc.code
        services.limiter.fail("upload", limiter_key)
        await ctx.failed(code)
        return _error(code, 409 if code in {"already_submitted", "closed"} else 400)

    stored: list[str] = []
    try:
        try:
            await _store(ctx, plan, stored)
        except Exception:
            await ctx.failed("storage")
            return _error("storage", 500)
        try:
            context = await ctx.db(
                lambda db: _receipt_context(services.submissions.commit(db, ctx.exam_in(db), plan))
            )
        except DomainError as exc:
            code = exc.code
            await ctx.failed(code)
            return _error(code, 409)
        stored.clear()  # committed: the files stay
    finally:
        for key in stored:
            await services.storage.delete(key)

    services.limiter.reset("upload", limiter_key)
    try:
        html = ctx.request.app.state.templates.get_template("student/_receipt.html.j2").render(
            {
                **context,
                "csrf_token": ctx.request.state.csrf_token,
                "receipt_reset_seconds": RECEIPT_RESET_SECONDS,
            }
        )
    except Exception:  # the hand-in is committed: never report it as failed
        log.exception("receipt.render_failed", exam_id=str(exam.id))
        html = (
            '<section class="receipt" id="receipt" tabindex="-1"><p><strong>'
            + escape(_("Handed in."))
            + "</strong> "
            + escape(_("Hand-in code"))
            + ': <span class="mono">'
            + escape(str(context["receipt_code"]))
            + "</span></p></section>"
        )
    response = JSONResponse({"ok": True, "html": html}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE, path=f"/{exam.slug}")
    return response


@router.post("/{slug}/inleveren", include_in_schema=False)
async def upload(request: Request, slug: str) -> Response:
    ctx = _UploadContext(request, slug)
    entry, max_files = await _load(ctx)
    exam, state = ctx.exam, ctx.state
    if exam is None:
        return _error("bad_request", 404)
    if state is None or state.stage != Stage.READY:
        return _error("session", 409)
    if entry is not Entry.OPEN:
        return _error("closed", 409)
    limiter_key = f"{exam.id}:{ctx.ip}"
    if ctx.services.limiter.status("upload", limiter_key).blocked:
        return _error("too_many", 429)
    await ctx.db(
        lambda db: ctx.services.submissions.record_started(
            db, ctx.exam_in(db), state, ip=ctx.ip, started_at=ctx.started_at
        )
    )
    try:
        parsed = await parse_upload(
            request,
            max_file=exam.max_file_bytes,
            max_total=exam.max_submission_bytes,
            max_files=max_files,
        )
    except UploadError as exc:
        code = exc.code
        ctx.services.limiter.fail("upload", limiter_key)
        await ctx.failed(code)
        return _error(code)
    try:
        return await _process(ctx, parsed, limiter_key)
    finally:
        parsed.cleanup()


def _receipt_context(receipt: Receipt) -> dict[str, Any]:
    submission = receipt.submission
    code = receipt.verification_code
    return {
        "exam_name": receipt.exam.name,
        "exam_slug": receipt.exam.slug,
        "receipt_code": submission.receipt_code,
        "verification_code": f"{code[:4]}-{code[4:]}" if code else None,
        "student_number": submission.student_number,
        "student_name": " ".join(
            p for p in (submission.first_name, submission.infix, submission.last_name) if p
        ),
        "started_at": submission.started_at,
        "completed_at": submission.completed_at,
        "files": receipt.files,
        "warnings": list(submission.confirmed_warnings or []),
        "warning_labels": warning_labels(),
        "token": receipt.token,
    }


# Receipt PDF -------------------------------------------------------------------------


@router.post("/ontvangstbewijs", include_in_schema=False)
def receipt_pdf(
    request: Request,
    db: Db,
    services: Svc,
    token: Annotated[str, Form(max_length=128)] = "",
    verification_code: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    ip_key = client_ip(request) or "unknown"
    if services.limiter.status("token", ip_key).blocked:
        raise NotFoundError()
    try:
        submission = services.submissions.use_receipt_token(db, token)
    except NotFoundError:
        services.limiter.fail("token", ip_key)
        raise
    exam = db.get(Exam, submission.exam_id)
    if exam is None:  # pragma: no cover - submissions always belong to an exam
        raise NotFoundError()
    code = (
        verification_code
        if services.submissions.verification_matches(submission, verification_code)
        else None
    )
    context = {
        "exam_name": exam.name,
        "receipt_code": submission.receipt_code,
        "verification_code": code.strip().upper() if code else None,
        "student_number": submission.student_number,
        "student_name": " ".join(
            p for p in (submission.first_name, submission.infix, submission.last_name) if p
        ),
        "started_at": submission.started_at,
        "completed_at": submission.completed_at,
        "files": services.submissions.receipt_files(exam, submission),
        "warnings": list(submission.confirmed_warnings or []),
        "warning_labels": warning_labels(),
    }
    pdf = request.app.state.pdf.render("pdf/receipt.html.j2", context)
    filename = f"ontvangstbewijs-{submission.receipt_code}.pdf"
    return bytes_download(pdf, filename, "application/pdf")


def new_idempotency_key() -> str:
    return secrets.token_urlsafe(24)
