"""The retention example from spec §10, plus edge cases around midnight and DST."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from inleverboks.services.lifecycle import WARNING_PERIOD, deletion_due

AMS = ZoneInfo("Europe/Amsterdam")


def local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=AMS)


def test_spec_example() -> None:
    # Exam ends Friday 13 November 2026 → day 1 = 14 Nov, day 60 = 12 Jan 2027.
    due = deletion_due(local(2026, 11, 13, 12, 0), 60, AMS)
    assert due == local(2027, 1, 13, 0, 0).astimezone(UTC)
    assert due.astimezone(AMS) == local(2027, 1, 13, 0, 0)
    assert (due - WARNING_PERIOD).astimezone(AMS) == local(2027, 1, 6, 0, 0)


@pytest.mark.parametrize(
    ("end", "due"),
    [
        # Late in the evening, local time.
        (local(2026, 11, 13, 23, 30), local(2027, 1, 13, 0, 0)),
        # Just after midnight local: the end date is the 14th.
        (local(2026, 11, 14, 0, 30), local(2027, 1, 14, 0, 0)),
        # Summer time end, deletion in winter time.
        (local(2026, 9, 1, 15, 0), local(2026, 11, 1, 0, 0)),
        # Winter time end, deletion in summer time (clock moves on 28 Mar 2027).
        (local(2027, 2, 1, 10, 0), local(2027, 4, 3, 0, 0)),
    ],
)
def test_deletion_due_uses_local_dates(end: datetime, due: datetime) -> None:
    assert deletion_due(end, 60, AMS).astimezone(AMS) == due


def test_end_given_in_utc_counts_local_date() -> None:
    # 23:30 UTC on 13 Nov is 00:30 on 14 Nov in Amsterdam.
    assert deletion_due(datetime(2026, 11, 13, 23, 30, tzinfo=UTC), 60, AMS).astimezone(
        AMS
    ) == local(2027, 1, 14, 0, 0)


def test_short_retention() -> None:
    assert deletion_due(local(2026, 11, 13, 12, 0), 1, AMS).astimezone(AMS) == local(
        2026, 11, 15, 0, 0
    )
