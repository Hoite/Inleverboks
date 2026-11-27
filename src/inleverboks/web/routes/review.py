"""Teacher side after handing in: overview, student detail, actions, downloads, exports
and the exam audit log (spec §6, §7, §8). Receipt verification is in ``admin.py``."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.db.models import AuditEvent, Exam, ProcesVerbaal, Submission, SubmissionFile, User
from inleverboks.i18n import gettext as _
from inleverboks.policy import ExamAction
from inleverboks.services.audit import Actor
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, NotFoundError
from inleverboks.services.exams import ExamStatus, may
from inleverboks.services.exports import (
    IntegrityReport,
    build_zip,
    local,
    plan_zip,
    write_csv,
)
from inleverboks.services.overview import (
    RowStatus,
    Signal,
    build_overview,
    filter_and_sort,
    load_submissions,
)
from inleverboks.services.pv import RenderPv, generate
from inleverboks.web.deps import Db, Svc, Teacher, client_ip, is_background_poll
from inleverboks.web.downloads import bytes_download, download_headers, stream_download
from inleverboks.web.messages import describe
from inleverboks.web.routes.exams import status_labels
from inleverboks.web.routes.student import warning_labels
from inleverboks.web.templating import render

router = APIRouter(prefix="/examens")

AUDIT_PAGE_SIZE = 200


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def signal_labels() -> dict[str, str]:
    return {
        Signal.EMPTY_TEMPLATE: _("Empty template"),
        Signal.TEMP_FILE: _("Lock file"),
        Signal.EMPTY_FILE: _("Empty file"),
        Signal.NAME_MISMATCH: _("Name differs"),
        Signal.DUPLICATE_FILE: _("Same file twice"),
        Signal.IDENTICAL_TO_OTHER: _("Identical to another student"),
        Signal.NOT_ON_LIST: _("Not on list"),
        Signal.EXTENSION: _("Extension"),
        Signal.ZIP: _("Zip"),
        Signal.COUNT_MISMATCH: _("Number differs"),
    }


def row_status_labels() -> dict[str, str]:
    return {
        RowStatus.ACTIVE: _("Handed in"),
        RowStatus.CANCELLED: _("Cancelled"),
        RowStatus.INVALID: _("Invalid"),
        RowStatus.MISSING: _("Not handed in yet"),
    }


def _audit(
    services: Services,
    db: Session,
    user: User,
    exam: Exam,
    event: str,
    request: Request,
    **details: Any,
) -> None:
    services.audit.append(
        db, event, actor=Actor.for_user(user), chain=exam.id, ip=client_ip(request), details=details
    )
    db.commit()


# Overview ----------------------------------------------------------------------------


@router.get("/{exam_id:uuid}")
def overview(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    status: str | None = None,
    sort: str | None = None,
    desc: bool = False,
    fragment: bool = False,
    ok: str | None = None,
    error: str | None = None,
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    data = build_overview(db, exam)
    exam_status = services.exams.status(db, exam)
    context: dict[str, Any] = {
        "exam": exam,
        "data": data,
        "rows": filter_and_sort(data.rows, status, sort, desc),
        "status_filter": status or "",
        "sort": sort or "name",
        "desc": desc,
        "exam_status": exam_status,
        "status_labels": status_labels(),
        "row_status_labels": row_status_labels(),
        "signal_labels": signal_labels(),
        "polling": exam_status is ExamStatus.OPEN,
        "perms": {a.value: may(db, ctx.user, a, exam) for a in ExamAction},
        "ok": ok,
        "error": [describe(DomainError(error))[0]] if error else [],
        "retention": services.lifecycle.retention(db, exam),
        "now": services.clock.now(),
        "proces_verbalen": list(
            db.scalars(
                select(ProcesVerbaal)
                .where(ProcesVerbaal.exam_id == exam.id)
                .order_by(ProcesVerbaal.generated_at.desc())
            )
        ),
    }
    if fragment or is_background_poll(request):
        return render(request, "teacher/_overview_table.html.j2", **context)
    return render(request, "teacher/exam_overview.html.j2", **context)


# Student detail and actions ----------------------------------------------------------


@router.get("/{exam_id:uuid}/studenten/{student_number}")
def student_detail(
    request: Request,
    exam_id: uuid.UUID,
    student_number: str,
    ctx: Teacher,
    db: Db,
    services: Svc,
    ok: str | None = None,
    error: str | None = None,
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.VIEW_SUBMISSIONS)
    attempts = [s for s in load_submissions(db, exam) if s.student_number == student_number]
    roster = services.flow.roster_entry(db, exam, student_number)
    if not attempts and roster is None:
        raise NotFoundError()
    users = {u.id: u for u in db.scalars(select(User))}
    labels = {s.id: s.label for s in exam.expected_files}
    data = build_overview(db, exam)
    row = next((r for r in data.rows if r.student_number == student_number), None)
    return render(
        request,
        "teacher/student_detail.html.j2",
        exam=exam,
        student_number=student_number,
        row=row,
        attempts=sorted(attempts, key=lambda s: s.attempt, reverse=True),
        roster=roster,
        users=users,
        slot_labels=labels,
        signal_labels=signal_labels(),
        warning_labels=warning_labels(),
        row_status_labels=row_status_labels(),
        perms={a.value: may(db, ctx.user, a, exam) for a in ExamAction},
        extension=services.exams.end_for_student(db, exam, student_number),
        ok=ok,
        errors=[describe(DomainError(error))[0]] if error else [],
    )


def _student_url(exam: Exam, number: str) -> str:
    return f"/examens/{exam.id}/studenten/{number}"


@router.post("/{exam_id:uuid}/inleveringen/{submission_id}/annuleren")
def cancel_submission(
    request: Request,
    exam_id: uuid.UUID,
    submission_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    reason: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.MANAGE_SUBMISSIONS)
    submission = db.get(Submission, submission_id)
    number = submission.student_number if submission and submission.exam_id == exam.id else ""
    try:
        services.review.cancel(db, ctx.user, exam, submission_id, reason, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _redirect(f"{_student_url(exam, number)}?error={exc.code}")
    return _redirect(f"{_student_url(exam, number)}?ok=cancelled")


@router.post("/{exam_id:uuid}/inleveringen/{submission_id}/ongeldig")
def invalidate_submission(
    request: Request,
    exam_id: uuid.UUID,
    submission_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    reason: Annotated[str, Form(max_length=2000)] = "",
    may_resubmit: Annotated[bool, Form()] = False,
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.MANAGE_SUBMISSIONS)
    submission = db.get(Submission, submission_id)
    number = submission.student_number if submission and submission.exam_id == exam.id else ""
    try:
        services.review.invalidate(
            db,
            ctx.user,
            exam,
            submission_id,
            reason,
            may_resubmit=may_resubmit,
            ip=client_ip(request),
        )
    except DomainError as exc:
        db.rollback()
        return _redirect(f"{_student_url(exam, number)}?error={exc.code}")
    return _redirect(f"{_student_url(exam, number)}?ok=invalidated")


# Downloads ---------------------------------------------------------------------------


@router.get("/{exam_id:uuid}/bestanden/{file_id}")
def download_file(
    request: Request, exam_id: uuid.UUID, file_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.DOWNLOAD_FILES)
    file = db.get(SubmissionFile, file_id)
    if file is None or file.storage_key is None:
        raise NotFoundError()
    submission = db.get(Submission, file.submission_id)
    if submission is None or submission.exam_id != exam.id:
        raise NotFoundError()
    _audit(
        services,
        db,
        ctx.user,
        exam,
        "download.file",
        request,
        file_id=str(file.id),
        name=file.original_name,
        student_number=submission.student_number,
    )
    return stream_download(
        services.storage.open(file.storage_key), file.original_name, size=file.size
    )


def _zip_response(
    request: Request,
    db: Session,
    services: Services,
    user: User,
    exam: Exam,
    submissions: list[Submission],
    filename: str,
    *,
    include_cancelled: bool,
    include_invalid: bool,
    event: str,
    details: dict[str, Any],
) -> Response:
    items = plan_zip(
        exam, submissions, include_cancelled=include_cancelled, include_invalid=include_invalid
    )
    _audit(services, db, user, exam, event, request, files=len(items), **details)
    report = IntegrityReport(checked_at=services.clock.now())
    exam_id, user_id, ip = exam.id, user.id, client_ip(request)
    sessionmaker = request.app.state.sessionmaker

    def finished(result: IntegrityReport) -> None:
        with sessionmaker() as session:
            actor_user = session.get(User, user_id)
            actor = Actor.for_user(actor_user) if actor_user else Actor.system()
            for check in result.checks:
                row = session.get(SubmissionFile, check.file.id)
                if row is not None:
                    row.integrity_checked_at = result.checked_at
                    row.integrity_ok = check.ok
            for check in result.mismatches:
                services.audit.append(
                    session,
                    "integrity.mismatch",
                    actor=actor,
                    chain=exam_id,
                    ip=ip,
                    details={
                        "file_id": str(check.file.id),
                        "name": check.file.original_name,
                        "expected": check.file.sha256,
                        "actual": check.actual_sha256,
                        "during": event,
                    },
                )
            session.commit()

    zs = build_zip(services.storage, items, report, services.env.timezone, finished)

    def chunks() -> Iterator[bytes]:
        yield from zs

    headers = download_headers(filename)
    return StreamingResponse(chunks(), media_type="application/zip", headers=headers)


def _zip_date(exam: Exam, services: Services) -> str:
    return exam.starts_at.astimezone(services.env.timezone).strftime("%Y-%m-%d")


@router.get("/{exam_id:uuid}/zip")
def download_zip(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    geannuleerd: bool = False,
    ongeldig: bool = False,
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.DOWNLOAD_FILES)
    return _zip_response(
        request,
        db,
        services,
        ctx.user,
        exam,
        load_submissions(db, exam),
        f"{exam.slug}_{_zip_date(exam, services)}.zip",
        include_cancelled=geannuleerd,
        include_invalid=ongeldig,
        event="download.zip",
        details={"cancelled": geannuleerd, "invalid": ongeldig},
    )


@router.get("/{exam_id:uuid}/studenten/{student_number}/zip")
def download_student_zip(
    request: Request, exam_id: uuid.UUID, student_number: str, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.DOWNLOAD_FILES)
    submissions = [s for s in load_submissions(db, exam) if s.student_number == student_number]
    if not submissions:
        raise NotFoundError()
    return _zip_response(
        request,
        db,
        services,
        ctx.user,
        exam,
        submissions,
        f"{exam.slug}_{student_number}.zip",
        include_cancelled=True,
        include_invalid=True,
        event="download.student",
        details={"student_number": student_number},
    )


# CSV and proces-verbaal --------------------------------------------------------------

OVERVIEW_COLUMNS = [
    "studentnummer",
    "achternaam",
    "tussenvoegsel",
    "voornaam",
    "klas",
    "status",
    "pogingen",
    "inlevertijd_start",
    "inlevertijd_voltooid",
    "aantal_bestanden",
    "verwacht",
    "totale_grootte_bytes",
    "op_lijst",
    "verlenging_tot",
    "signalen",
    "inlevercode",
]
STATUS_CSV = {
    "active": "ingeleverd",
    "cancelled": "geannuleerd",
    "invalid": "ongeldig",
    "missing": "nog niet ingeleverd",
}


@router.get("/{exam_id:uuid}/overzicht.csv")
def overview_csv(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.EXPORT)
    data = build_overview(db, exam)
    tz = services.env.timezone
    rows: list[list[object]] = []
    for row in data.rows:
        sub = row.submission
        rows.append(
            [
                row.student_number,
                row.last_name,
                row.infix or "",
                row.first_name,
                row.class_name or "",
                STATUS_CSV[row.status.value],
                row.attempts,
                local(sub.started_at if sub else None, tz),
                local(sub.completed_at if sub else None, tz),
                row.file_count if sub else "",
                data.expected_files,
                row.total_size if sub else "",
                "ja" if row.on_roster else "nee",
                local(row.extension_until, tz),
                ", ".join(s.value for s in row.signals),
                sub.receipt_code if sub else "",
                # Per work process (SBB code): handed in or not.
                *[
                    ("ja" if wp.slot_id in row.filled_slots else "nee") if sub else ""
                    for wp in data.work_processes
                ],
            ]
        )
    _audit(services, db, ctx.user, exam, "export.csv", request, rows=len(rows))
    return bytes_download(
        write_csv(OVERVIEW_COLUMNS + [wp.code for wp in data.work_processes], rows),
        f"{exam.slug}_overzicht_{_zip_date(exam, services)}.csv",
        "text/csv; charset=utf-8",
    )


def render_pv_factory(pdf_renderer: Any) -> RenderPv:
    """Render a proces-verbaal with the translated labels of the web layer."""

    def render(context: dict[str, Any]) -> bytes:
        context = {
            **context,
            "signal_labels": signal_labels(),
            "row_status_labels": row_status_labels(),
            "warning_labels": warning_labels(),
        }
        pdf: bytes = pdf_renderer.render("pdf/proces_verbaal.html.j2", context)
        return pdf

    return render


def generate_pv(
    request: Request, db: Session, services: Services, exam: Exam, user: User | None, *, final: bool
) -> tuple[bytes, str]:
    pdf, pv = generate(
        db,
        services.pv,
        services.storage,
        render_pv_factory(request.app.state.pdf),
        exam,
        user,
        final=final,
        ip=client_ip(request),
    )
    return pdf, pv.document_id


@router.post("/{exam_id:uuid}/proces-verbaal")
def proces_verbaal(
    request: Request, exam_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.EXPORT)
    pdf, document_id = generate_pv(request, db, services, exam, ctx.user, final=False)
    return bytes_download(pdf, f"{exam.slug}_{document_id}.pdf", "application/pdf")


@router.get("/{exam_id:uuid}/proces-verbalen/{pv_id}")
def download_pv(
    request: Request, exam_id: uuid.UUID, pv_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.EXPORT)
    pv = db.get(ProcesVerbaal, pv_id)
    if pv is None or pv.exam_id != exam.id:
        raise NotFoundError()
    _audit(services, db, ctx.user, exam, "download.pv", request, document_id=pv.document_id)
    return stream_download(
        services.storage.open(pv.storage_key), f"{exam.slug}_{pv.document_id}.pdf"
    )


# Hold flag and permanent deletion ------------------------------------------------------


@router.post("/{exam_id:uuid}/bewaren")
def set_hold(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    reason: Annotated[str, Form(max_length=2000)] = "",
    remove: Annotated[bool, Form()] = False,
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.SET_HOLD)
    try:
        if remove:
            services.lifecycle.remove_hold(db, ctx.user, exam, reason, ip=client_ip(request))
        else:
            services.lifecycle.set_hold(db, ctx.user, exam, reason, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/geavanceerd?error={exc.code}#bewaartermijn")
    return _redirect(
        f"/examens/{exam.id}/geavanceerd?ok={'hold_removed' if remove else 'hold_set'}"
        "#bewaartermijn"
    )


@router.post("/{exam_id:uuid}/definitief-verwijderen")
def delete_permanently(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    reason: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.DELETE_FINALIZED)
    try:
        services.lifecycle.delete_permanently(db, ctx.user, exam, reason, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return _redirect(f"/examens/{exam.id}/geavanceerd?error={exc.code}")
    return _redirect("/examens/afgerond?ok=deleted")


# Audit log ---------------------------------------------------------------------------

AUDIT_COLUMNS = [
    "volgnummer",
    "tijdstip",
    "actor",
    "ip",
    "gebeurtenis",
    "details",
    "prev_hash",
    "hash",
]


def _audit_events(
    db: Session, chain: str, event: str | None, actor: str | None
) -> list[AuditEvent]:
    query = select(AuditEvent).where(AuditEvent.chain == chain)
    if event:
        query = query.where(AuditEvent.event.startswith(event))
    if actor:
        query = query.where(AuditEvent.actor_label.contains(actor))
    return list(db.scalars(query.order_by(AuditEvent.seq)))


def audit_export(
    request: Request,
    db: Session,
    services: Services,
    chain: str,
    title: str,
    filename: str,
    fmt: str,
    events: list[AuditEvent],
) -> Response:
    tz = services.env.timezone
    if fmt == "csv":
        rows: list[list[object]] = [
            [
                e.seq,
                local(e.ts, tz),
                e.actor_label,
                e.ip or "",
                e.event,
                json.dumps(e.details, ensure_ascii=False, sort_keys=True),
                e.prev_hash,
                e.hash,
            ]
            for e in events
        ]
        return bytes_download(
            write_csv(AUDIT_COLUMNS, rows), f"{filename}.csv", "text/csv; charset=utf-8"
        )
    checked, issues = services.audit.verify(db, chain)
    pdf = request.app.state.pdf.render(
        "pdf/audit.html.j2",
        {
            "title": title,
            "events": events,
            "generated_at": services.clock.now(),
            "checked": checked,
            "issues": issues,
            "head": services.audit.head_hash(db, chain),
        },
    )
    return bytes_download(pdf, f"{filename}.pdf", "application/pdf")


@router.get("/{exam_id:uuid}/auditlog")
def exam_audit(
    request: Request,
    exam_id: uuid.UUID,
    ctx: Teacher,
    db: Db,
    services: Svc,
    event: str | None = None,
    actor: str | None = None,
    format: str | None = None,
) -> Response:
    exam = services.exams.get_for(db, ctx.user, exam_id, ExamAction.VIEW_AUDIT)
    events = _audit_events(db, str(exam.id), event, actor)
    if format in {"csv", "pdf"}:
        _audit(services, db, ctx.user, exam, "export.audit", request, format=format)
        events = _audit_events(db, str(exam.id), event, actor)
        return audit_export(
            request,
            db,
            services,
            str(exam.id),
            f"{_('Audit log')}: {exam.name}",
            f"{exam.slug}_auditlog",
            format,
            events,
        )
    _checked, issues = services.audit.verify(db, str(exam.id))
    return render(
        request,
        "teacher/exam_audit.html.j2",
        exam=exam,
        events=list(reversed(events))[:AUDIT_PAGE_SIZE],
        total=len(events),
        event_filter=event or "",
        actor_filter=actor or "",
        chain_ok=not issues,
    )
