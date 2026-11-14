"""Class list library: CSV import with column mapping and preview."""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Annotated, Any

from anyio import from_thread
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from inleverboks.i18n import gettext as _
from inleverboks.services.classlists import (
    FIELDS,
    MAX_CSV_BYTES,
    ImportResult,
    apply_mapping,
    decode_csv,
    detect_delimiter,
    guess_mapping,
    read_rows,
)
from inleverboks.services.errors import DomainError
from inleverboks.web.deps import Db, Svc, Teacher, client_ip
from inleverboks.web.templating import render

router = APIRouter(prefix="/docent/klassenlijsten")

PREVIEW_ROWS = 20
DELIMITERS = {";": ";", ",": ",", "tab": "\t"}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def import_messages() -> dict[str, str]:
    return {
        "csv_too_large": _("The file is too large (maximum 2 MB)."),
        "csv_too_many_rows": _("The file has too many rows."),
        "csv_empty": _("The file contains no rows."),
        "mapping_incomplete": _("Choose a column for student number, first name and last name."),
        "class_list_name_invalid": _("Enter a name for the class list."),
        "class_list_has_errors": _("Fix the errors in the file first."),
        "missing_number": _("Student number missing"),
        "invalid_number": _("Invalid student number"),
        "missing_name": _("First or last name missing"),
        "duplicate_number": _("Duplicate student number"),
    }


def field_labels() -> dict[str, str]:
    return {
        "student_number": _("Student number"),
        "first_name": _("First name"),
        "infix": _("Infix"),
        "last_name": _("Last name"),
        "class_name": _("Class (optional)"),
    }


@router.get("")
def library(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    return render(
        request, "teacher/classlists.html.j2", tab="classlists",
        class_lists=services.classlists.library(db, ctx.user),
    )  # fmt: skip


@router.get("/nieuw")
def upload_form(request: Request, ctx: Teacher) -> Response:
    return render(request, "teacher/classlist_upload.html.j2", errors=[], name="")


def _mapping_page(
    request: Request,
    *,
    name: str,
    text: str,
    delimiter: str,
    has_header: bool,
    mapping: dict[str, int | None],
    result: ImportResult | None,
    errors: list[str],
    status: int = 200,
) -> Response:
    rows = read_rows(text, delimiter)
    header = rows[0] if rows else []
    width = max((len(r) for r in rows), default=0)
    columns = [
        (
            index,
            header[index]
            if has_header and index < len(header)
            else _("Column %(n)s") % {"n": index + 1},
        )
        for index in range(width)
    ]
    return render(
        request,
        "teacher/classlist_mapping.html.j2",
        status_code=status,
        name=name,
        csv_b64=base64.b64encode(text.encode()).decode(),
        delimiter=next(k for k, v in DELIMITERS.items() if v == delimiter),
        has_header=has_header,
        mapping=mapping,
        columns=columns,
        fields=FIELDS,
        field_labels=field_labels(),
        result=result,
        preview=(result.entries[:PREVIEW_ROWS] if result else []),
        messages=import_messages(),
        errors=errors,
    )


@router.post("/nieuw")
def upload(
    request: Request,
    ctx: Teacher,
    name: Annotated[str, Form(max_length=200)] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    if file is None:
        return render(
            request,
            "teacher/classlist_upload.html.j2",
            status_code=400,
            errors=[import_messages()["csv_empty"]],
            name=name,
        )
    data = from_thread.run(file.read, MAX_CSV_BYTES + 1)
    try:
        text = decode_csv(data)
        delimiter = detect_delimiter(text)
        rows = read_rows(text, delimiter)
        if not rows:
            raise DomainError("csv_empty")
    except DomainError as exc:
        return render(
            request, "teacher/classlist_upload.html.j2", status_code=400,
            errors=[import_messages().get(exc.code, exc.code)], name=name,
        )  # fmt: skip
    mapping = guess_mapping(rows[0])
    has_header = any(v is not None for v in mapping.values())
    return _mapping_page(
        request, name=name or (file.filename or "").rsplit(".", 1)[0], text=text,
        delimiter=delimiter, has_header=has_header, mapping=mapping, result=None, errors=[],
    )  # fmt: skip


async def _form(request: Request) -> Any:
    return await request.form()


@router.post("/importeren")
def import_rows(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    form = from_thread.run(_form, request)

    def text_field(key: str) -> str:
        value = form.get(key)
        return value if isinstance(value, str) else ""

    try:
        text = base64.b64decode(text_field("csv_b64"), validate=True).decode()
    except binascii.Error, UnicodeDecodeError:
        return _redirect("/docent/klassenlijsten/nieuw")
    if len(text.encode()) > MAX_CSV_BYTES:
        return _redirect("/docent/klassenlijsten/nieuw")
    delimiter = DELIMITERS.get(text_field("delimiter"), ";")
    has_header = text_field("has_header") == "true"
    name = text_field("name").strip()
    mapping: dict[str, int | None] = {}
    for field_name in FIELDS:
        raw = text_field(f"map_{field_name}")
        mapping[field_name] = int(raw) if raw.isdigit() else None
    errors: list[str] = []
    result: ImportResult | None = None
    try:
        rows = read_rows(text, delimiter)
        result = apply_mapping(
            rows, mapping, has_header=has_header,
            student_number_regex=services.settings.load(db).student_number_regex,
        )  # fmt: skip
        if text_field("action") == "save":
            class_list = services.classlists.save(db, ctx.user, name, result, ip=client_ip(request))
            return _redirect(f"/docent/klassenlijsten/{class_list.id}")
    except DomainError as exc:
        db.rollback()
        errors.append(import_messages().get(exc.code, exc.code))
    return _mapping_page(
        request, name=name, text=text, delimiter=delimiter, has_header=has_header,
        mapping=mapping, result=result, errors=errors, status=400 if errors else 200,
    )  # fmt: skip


@router.get("/{class_list_id}")
def detail(
    request: Request, class_list_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    class_list = services.classlists.get_owned(db, ctx.user, class_list_id)
    entries = sorted(class_list.entries, key=lambda e: (e.last_name.lower(), e.first_name.lower()))
    return render(
        request, "teacher/classlist_detail.html.j2", class_list=class_list, entries=entries
    )


@router.post("/{class_list_id}/verwijderen")
def delete(
    request: Request, class_list_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    services.classlists.delete(db, ctx.user, class_list_id, ip=client_ip(request))
    return _redirect("/docent/klassenlijsten")
