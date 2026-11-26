"""Class list library: CSV import with column mapping and preview.

Lists belong to an education team: its members see and use them, its team contacts and
admins import, rename, replace and delete them (PLAN B65, ``policy.CLASS_LIST_MATRIX``).
Lists without a team follow B60 until an admin assigns them (B69)."""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Annotated, Any

from anyio import from_thread
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from inleverboks.db.models import ClassList, User
from inleverboks.i18n import gettext as _
from inleverboks.policy import ClassListAction
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
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError, NotFoundError, PermissionDeniedError
from inleverboks.web.deps import Db, Svc, Teacher, client_ip
from inleverboks.web.templating import render

router = APIRouter(prefix="/examens/klassenlijsten")

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
        "class_list_team_required": _("Choose the education team of the class list."),
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
        sections=services.classlists.library(db, ctx.user),
        creatable=bool(services.classlists.creatable_teams(db, ctx.user)),
        owner_names=services.users.names(db),
    )  # fmt: skip


def _upload_page(
    request: Request, db: Db, services: Services, user: User, *, errors: list[str],
    name: str, target: ClassList | None, team_id: str = "", status: int = 200,
) -> Response:  # fmt: skip
    teams = [] if target else services.classlists.creatable_teams(db, user)
    if target is None and not teams:
        raise PermissionDeniedError()  # only team contacts and admins import (B65)
    return render(
        request, "teacher/classlist_upload.html.j2", status_code=status, errors=errors,
        name=name, target=target, teams=teams, team_id=team_id,
    )  # fmt: skip


def _replace_target(db: Db, services: Services, user: User, replace_id: str) -> ClassList | None:
    """The list to replace when the import started from "Replace list"; checked again on
    every step, so a hidden field never grants anything."""
    if not replace_id:
        return None
    try:
        class_list_id = uuid.UUID(replace_id)
    except ValueError:
        raise NotFoundError("class_list_not_found") from None
    return services.classlists.get(db, user, class_list_id, ClassListAction.REPLACE)


@router.get("/nieuw")
def upload_form(request: Request, ctx: Teacher, db: Db, services: Svc, team: str = "") -> Response:
    return _upload_page(request, db, services, ctx.user, errors=[], name="", target=None,
                        team_id=team)  # fmt: skip


@router.get("/{class_list_id}/vervangen")
def replace_form(
    request: Request, class_list_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    target = services.classlists.get(db, ctx.user, class_list_id, ClassListAction.REPLACE)
    return _upload_page(request, db, services, ctx.user, errors=[], name=target.name,
                        target=target)  # fmt: skip


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
    target: ClassList | None,
    team_id: str,
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
        target=target,
        team_id=team_id,
    )


@router.post("/nieuw")
def upload(
    request: Request,
    ctx: Teacher,
    db: Db,
    services: Svc,
    name: Annotated[str, Form(max_length=200)] = "",
    replace_id: Annotated[str, Form(max_length=36)] = "",
    team_id: Annotated[str, Form(max_length=36)] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    target = _replace_target(db, services, ctx.user, replace_id)
    if file is None:
        return _upload_page(
            request, db, services, ctx.user, errors=[import_messages()["csv_empty"]],
            name=name, target=target, team_id=team_id, status=400,
        )  # fmt: skip
    data = from_thread.run(file.read, MAX_CSV_BYTES + 1)
    try:
        text = decode_csv(data)
        delimiter = detect_delimiter(text)
        rows = read_rows(text, delimiter)
        if not rows:
            raise DomainError("csv_empty")
    except DomainError as exc:
        return _upload_page(
            request, db, services, ctx.user, errors=[import_messages().get(exc.code, exc.code)],
            name=name, target=target, team_id=team_id, status=400,
        )  # fmt: skip
    mapping = guess_mapping(rows[0])
    has_header = any(v is not None for v in mapping.values())
    return _mapping_page(
        request, name=name or (file.filename or "").rsplit(".", 1)[0], text=text,
        delimiter=delimiter, has_header=has_header, mapping=mapping, result=None, errors=[],
        target=target, team_id=team_id,
    )  # fmt: skip


def _team_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _form(request: Request) -> Any:
    return await request.form()


@router.post("/importeren")
def import_rows(request: Request, ctx: Teacher, db: Db, services: Svc) -> Response:
    form = from_thread.run(_form, request)

    def text_field(key: str) -> str:
        value = form.get(key)
        return value if isinstance(value, str) else ""

    target = _replace_target(db, services, ctx.user, text_field("replace_id"))
    try:
        text = base64.b64decode(text_field("csv_b64"), validate=True).decode()
    except binascii.Error, UnicodeDecodeError:
        return _redirect("/examens/klassenlijsten/nieuw")
    if len(text.encode()) > MAX_CSV_BYTES:
        return _redirect("/examens/klassenlijsten/nieuw")
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
            if target is not None:
                class_list = services.classlists.replace(
                    db, ctx.user, target.id, name, result, ip=client_ip(request)
                )
                return _redirect(f"/examens/klassenlijsten/{class_list.id}?ok=replaced")
            class_list = services.classlists.save(
                db, ctx.user, _team_id(text_field("team_id")), name, result,
                ip=client_ip(request),
            )  # fmt: skip
            return _redirect(f"/examens/klassenlijsten/{class_list.id}")
    except PermissionDeniedError:
        raise  # 403 from the app's handler
    except DomainError as exc:
        db.rollback()
        errors.append(import_messages().get(exc.code, exc.code))
    return _mapping_page(
        request, name=name, text=text, delimiter=delimiter, has_header=has_header,
        mapping=mapping, result=result, errors=errors, target=target,
        team_id=text_field("team_id"), status=400 if errors else 200,
    )  # fmt: skip


@router.get("/{class_list_id}")
def detail(
    request: Request, class_list_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    ok: str | None = None, error: str | None = None,
) -> Response:  # fmt: skip
    class_list = services.classlists.get(db, ctx.user, class_list_id, ClassListAction.VIEW)
    entries = sorted(class_list.entries, key=lambda e: (e.last_name.lower(), e.first_name.lower()))
    return render(
        request, "teacher/classlist_detail.html.j2", class_list=class_list, entries=entries,
        owner_name=services.users.names(db).get(class_list.owner_id, ""),
        own=class_list.owner_id == ctx.user.id,
        team_name=services.teams.names(db).get(class_list.team_id) if class_list.team_id else None,
        perms=services.classlists.permissions(db, ctx.user, class_list),
        ok=ok, errors=[import_messages().get(error, error)] if error else [],
    )  # fmt: skip


@router.post("/{class_list_id}/naam")
def rename(
    request: Request, class_list_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc,
    name: Annotated[str, Form(max_length=200)] = "",
) -> Response:  # fmt: skip
    try:
        services.classlists.rename(db, ctx.user, class_list_id, name, ip=client_ip(request))
    except DomainError as exc:
        if exc.code != "class_list_name_invalid":
            raise  # not found / not allowed: the app's handlers answer 404 / 403
        db.rollback()
        return _redirect(f"/examens/klassenlijsten/{class_list_id}?error={exc.code}")
    return _redirect(f"/examens/klassenlijsten/{class_list_id}?ok=renamed")


@router.post("/{class_list_id}/verwijderen")
def delete(
    request: Request, class_list_id: uuid.UUID, ctx: Teacher, db: Db, services: Svc
) -> Response:
    services.classlists.delete(db, ctx.user, class_list_id, ip=client_ip(request))
    return _redirect("/examens/klassenlijsten")
