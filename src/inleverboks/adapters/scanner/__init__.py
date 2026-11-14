"""File scanning interface. v1 uses ``NoopScanner``; ClamAV can be added later."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    detail: str | None = None


class FileScanner(Protocol):
    async def scan(self, storage_key: str) -> ScanResult: ...


class NoopScanner:
    async def scan(self, storage_key: str) -> ScanResult:
        return ScanResult(clean=True)
