"""Conversion between local wall-clock input (``datetime-local``) and UTC."""

from __future__ import annotations

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
