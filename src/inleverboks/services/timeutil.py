"""Conversion between local wall-clock input (a date field plus a 24-hour time) and UTC."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from inleverboks.services.errors import DomainError

LOCAL_INPUT_FORMAT = "%Y-%m-%dT%H:%M"


def parse_local(value: str, tz: ZoneInfo) -> datetime:
    """Parse ``YYYY-MM-DDTHH:MM`` in ``tz`` to aware UTC.

    Times that do not exist (the hour skipped when summer time starts) are rejected.
    Ambiguous times (the repeated hour in autumn) take the first occurrence (summer time).
    """
    try:
        naive = datetime.strptime(value.strip(), LOCAL_INPUT_FORMAT)  # noqa: DTZ007
    except ValueError as exc:
        raise DomainError("datetime_invalid") from exc
    local = naive.replace(tzinfo=tz, fold=0)
    as_utc = local.astimezone(UTC)
    if as_utc.astimezone(tz).replace(tzinfo=None) != naive:
        raise DomainError("datetime_nonexistent")
    return as_utc


def format_local(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime(LOCAL_INPUT_FORMAT)


_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]?([0-5]\d)$")


def normalize_time(value: str) -> str | None:
    """24-hour time as typed (``9:00``, ``09.00``, ``0900``) → ``HH:MM``; None if invalid."""
    match = _TIME_RE.fullmatch(value.strip())
    if match is None:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def combine_local(date: str, time: str) -> str:
    """Date (``YYYY-MM-DD``) and 24-hour time fields → ``YYYY-MM-DDTHH:MM`` for parse_local.

    An invalid time yields a string that parse_local rejects with ``datetime_invalid``.
    """
    return f"{date.strip()}T{normalize_time(time) or time.strip() or '?'}"


def split_local(value: str) -> tuple[str, str]:
    """``YYYY-MM-DDTHH:MM`` → (date, time); anything else → (value, "")."""
    date, sep, time = value.partition("T")
    return (date, time) if sep else (value, "")


def round_to_quarter(value: datetime) -> datetime:
    """Round to the nearest quarter of an hour."""
    base = value.replace(second=0, microsecond=0)
    minutes = base.minute % 15
    if minutes >= 8:
        return base + timedelta(minutes=15 - minutes)
    return base - timedelta(minutes=minutes)


def default_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Start now (rounded to the quarter, in local time) and end two hours later."""
    start = round_to_quarter(now.astimezone(tz)).astimezone(UTC)
    return start, start + timedelta(hours=2)
