"""Clock abstraction so time-dependent logic (windows, DST, retention) is testable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as an aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock: fixed time that can be moved forward."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            msg = "FrozenClock needs an aware datetime"
            raise ValueError(msg)
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(UTC)

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> None:
        self._now += delta if delta is not None else timedelta(**kwargs)
