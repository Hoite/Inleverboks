"""Rate limiting with growing delays and temporary blocks.

The default implementation keeps state in memory, so with several replicas the limits
apply per pod (documented in the README). A Redis implementation can replace it later
behind the same interface.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from inleverboks.adapters.clock import Clock


@dataclass(frozen=True)
class RateLimitRule:
    """``free`` failures are allowed without delay; each further failure doubles the
    delay (from ``base_delay`` up to ``max_delay``). After ``block_after`` failures within
    ``window`` the key is blocked for ``block_for``."""

    free: int
    block_after: int
    window: timedelta
    block_for: timedelta
    base_delay: float = 1.0
    max_delay: float = 8.0


@dataclass(frozen=True)
class RateLimitStatus:
    blocked: bool
    retry_after: float = 0.0
    delay: float = 0.0


RULES: dict[str, RateLimitRule] = {
    "login": RateLimitRule(
        free=3, block_after=10, window=timedelta(minutes=15), block_for=timedelta(minutes=15)
    ),
    "login_ip": RateLimitRule(
        free=10, block_after=50, window=timedelta(minutes=15), block_for=timedelta(minutes=15)
    ),
    "totp": RateLimitRule(
        free=3, block_after=8, window=timedelta(minutes=15), block_for=timedelta(minutes=15)
    ),
    "token": RateLimitRule(
        free=5, block_after=20, window=timedelta(minutes=15), block_for=timedelta(minutes=30)
    ),
    "access_code": RateLimitRule(
        free=5, block_after=15, window=timedelta(minutes=10), block_for=timedelta(minutes=10)
    ),
    "student_lookup": RateLimitRule(
        free=10, block_after=40, window=timedelta(minutes=10), block_for=timedelta(minutes=10)
    ),
    "upload": RateLimitRule(
        free=5, block_after=20, window=timedelta(minutes=10), block_for=timedelta(minutes=10)
    ),
}


class RateLimiter(Protocol):
    def status(self, rule: str, key: str) -> RateLimitStatus: ...

    def fail(self, rule: str, key: str) -> RateLimitStatus: ...

    def reset(self, rule: str, key: str) -> None: ...


@dataclass
class _Entry:
    failures: list[datetime] = field(default_factory=list)
    blocked_until: datetime | None = None


class MemoryRateLimiter:
    def __init__(self, clock: Clock, rules: dict[str, RateLimitRule] | None = None) -> None:
        self.clock = clock
        self.rules = rules or RULES
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()

    def _prune(self, rule: RateLimitRule, entry: _Entry, now: datetime) -> None:
        cutoff = now - rule.window
        entry.failures = [ts for ts in entry.failures if ts > cutoff]
        if entry.blocked_until and entry.blocked_until <= now:
            entry.blocked_until = None
            entry.failures = []

    def _status(self, rule: RateLimitRule, entry: _Entry, now: datetime) -> RateLimitStatus:
        if entry.blocked_until:
            return RateLimitStatus(
                blocked=True, retry_after=(entry.blocked_until - now).total_seconds()
            )
        over = len(entry.failures) - rule.free
        delay = 0.0 if over <= 0 else min(rule.base_delay * 2 ** (over - 1), rule.max_delay)
        return RateLimitStatus(blocked=False, delay=delay)

    def status(self, rule: str, key: str) -> RateLimitStatus:
        cfg = self.rules[rule]
        now = self.clock.now()
        with self._lock:
            entry = self._entries.get((rule, key))
            if entry is None:
                return RateLimitStatus(blocked=False)
            self._prune(cfg, entry, now)
            return self._status(cfg, entry, now)

    def fail(self, rule: str, key: str) -> RateLimitStatus:
        cfg = self.rules[rule]
        now = self.clock.now()
        with self._lock:
            entry = self._entries.setdefault((rule, key), _Entry())
            self._prune(cfg, entry, now)
            entry.failures.append(now)
            if len(entry.failures) >= cfg.block_after:
                entry.blocked_until = now + cfg.block_for
            return self._status(cfg, entry, now)

    def reset(self, rule: str, key: str) -> None:
        with self._lock:
            self._entries.pop((rule, key), None)
