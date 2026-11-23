"""Parsing and rendering helpers for the one-screen exam form (no JavaScript needed).

Slots are added and removed with submit buttons (``action=add_slot`` /
``action=remove_slot:N``) that re-render the form without saving.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from starlette.datastructures import FormData

from inleverboks.i18n import gettext as _
from inleverboks.services.errors import DomainError
from inleverboks.services.exams import ExamInput, SlotInput
from inleverboks.services.timeutil import combine_local, format_local, parse_local, split_local


def field_messages() -> dict[str, str]:
    return {
        "required": _("This field is required."),
        "too_long": _("This text is too long."),
        "end_before_start": _("The end time must be after the start time."),
        "slug_invalid": _("Use 3 to 80 characters: a-z, 0-9 and single dashes."),
        "slug_reserved": _("This link name is reserved. Choose another one."),
        "slug_taken": _("This link name is already in use."),
        "invalid": _("This value is not valid."),
        "ip_invalid": _("Enter IP addresses or ranges such as 10.0.0.0/8."),
        "ip_too_many": _("Too many IP ranges."),
        "ip_required": _("Enter at least one IP range or choose a preset."),
        "range": _("Value out of range."),
        "exceeds_submission": _("May not exceed the maximum per submission."),
        "too_many": _("Too many expected files."),
        "no_files_possible": _("Add an expected file or allow an extra file."),
        "datetime_invalid": _("Enter a valid date and time."),
        "datetime_nonexistent": _(
            "This time does not exist (the clock moves forward for summer time)."
        ),
    }


@dataclass
class ParsedForm:
    data: ExamInput
    problems: dict[str, str] = field(default_factory=dict)
    action: str = "save"


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _bool(form: FormData, name: str) -> bool:
    return _text(form, name) in {"1", "true", "on"}


def _int(form: FormData, name: str) -> int:
    try:
        return int(_text(form, name).strip())
    except ValueError:
        return -1


def _uuid(form: FormData, name: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(_text(form, name))
    except ValueError:
        return None


def read_datetime(form: FormData | dict[str, str], name: str) -> str:
    """The local date-time of a field as ``YYYY-MM-DDTHH:MM``.

    The form has a date field (``{name}_date``) and a 24-hour time field (``{name}_time``),
    because ``datetime-local`` shows AM/PM in an English browser. A single ``{name}`` field
    (the old form) is still accepted.
    """
    get = form.get
    date = get(f"{name}_date")
    if isinstance(date, str):
        time = get(f"{name}_time")
        return combine_local(date, time if isinstance(time, str) else "")
    value = get(name)
    return value if isinstance(value, str) else ""


def raw_datetimes(form: FormData, *names: str) -> dict[str, str]:
    """The date and time fields exactly as typed, to show them again after an error."""
    raw: dict[str, str] = {}
    for name in names:
        date, time = form.get(f"{name}_date"), form.get(f"{name}_time")
        if isinstance(date, str):
            raw[f"{name}_date"] = date
            raw[f"{name}_time"] = time if isinstance(time, str) else ""
        else:
            raw[f"{name}_date"], raw[f"{name}_time"] = split_local(read_datetime(form, name))
    return raw


def parse_exam_form(
    form: FormData, tz: ZoneInfo, fallback: ExamInput, *, with_times: bool = True
) -> ParsedForm:
    problems: dict[str, str] = {}
    starts_at, ends_at = fallback.starts_at, fallback.ends_at
    if with_times:
        try:
            starts_at = parse_local(read_datetime(form, "starts_at"), tz)
        except DomainError as exc:
            problems["starts_at"] = exc.code
        try:
            ends_at = parse_local(read_datetime(form, "ends_at"), tz)
        except DomainError as exc:
            problems["ends_at"] = exc.code

    count = max(0, min(_int(form, "slot_count"), 100))
    slots: list[SlotInput] = []
    for index in range(count):
        extensions = _text(form, f"slot_extensions_{index}").strip()
        slots.append(
            SlotInput(
                label=_text(form, f"slot_label_{index}"),
                pattern=_text(form, f"slot_pattern_{index}") or None,
                extensions=[extensions] if extensions else None,
                id=_uuid(form, f"slot_id_{index}"),
            )
        )

    action = _text(form, "action") or "save"
    if action == "add_slot":
        slots.append(SlotInput(label=""))
    elif action.startswith("remove_slot:"):
        with contextlib.suppress(ValueError, IndexError):
            del slots[int(action.split(":", 1)[1])]

    data = ExamInput(
        name=_text(form, "name"),
        description=_text(form, "description") or None,
        starts_at=starts_at,
        ends_at=ends_at,
        second_reviewer_id=_uuid(form, "second_reviewer_id"),
        slug=_text(form, "slug"),
        access_code_enabled=_bool(form, "access_code_enabled"),
        ip_restriction_enabled=_bool(form, "ip_restriction_enabled"),
        ip_cidrs=[_text(form, "ip_cidrs")],
        ip_preset_id=_uuid(form, "ip_preset_id"),
        allow_unlisted=_bool(form, "allow_unlisted"),
        allow_extra_file=_bool(form, "allow_extra_file"),
        extensions=[_text(form, "extensions")],
        max_file_mb=_int(form, "max_file_mb"),
        max_submission_mb=_int(form, "max_submission_mb"),
        slots=slots,
        class_list_id=_uuid(form, "class_list_id"),
        source_template_id=_uuid(form, "source_template_id"),
    )
    return ParsedForm(data=data, problems=problems, action=action)


def form_values(data: ExamInput, tz: ZoneInfo, raw: dict[str, str] | None = None) -> dict[str, Any]:
    """Values for re-rendering the form; ``raw`` keeps invalid date input as typed."""
    raw = raw or {}

    def joined(value: list[str] | None) -> str:
        return ", ".join(v for v in (value or []) if v)

    def typed(name: str, value: datetime) -> tuple[str, str]:
        if f"{name}_date" in raw:
            return raw[f"{name}_date"], raw.get(f"{name}_time", "")
        return split_local(format_local(value, tz))

    starts = typed("starts_at", data.starts_at)
    ends = typed("ends_at", data.ends_at)
    return {
        "name": data.name,
        "description": data.description or "",
        "starts_at_date": starts[0],
        "starts_at_time": starts[1],
        "ends_at_date": ends[0],
        "ends_at_time": ends[1],
        "second_reviewer_id": str(data.second_reviewer_id or ""),
        "slug": data.slug,
        "access_code_enabled": data.access_code_enabled,
        "ip_restriction_enabled": data.ip_restriction_enabled,
        "ip_cidrs": joined(data.ip_cidrs),
        "ip_preset_id": str(data.ip_preset_id or ""),
        "allow_unlisted": data.allow_unlisted,
        "allow_extra_file": data.allow_extra_file,
        "extensions": joined(data.extensions),
        "max_file_mb": data.max_file_mb,
        "max_submission_mb": data.max_submission_mb,
        "class_list_id": str(data.class_list_id or ""),
        "source_template_id": str(data.source_template_id or ""),
        "slots": [
            {
                "id": str(s.id or ""),
                "label": s.label,
                "pattern": s.pattern or "",
                "extensions": joined(s.extensions),
            }
            for s in data.slots
        ],
    }
