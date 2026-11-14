"""Domain errors carry a stable code; the web layer maps codes to translated messages."""

from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


class NotFoundError(DomainError):
    def __init__(self, code: str = "not_found") -> None:
        super().__init__(code)


class PermissionDeniedError(DomainError):
    def __init__(self, code: str = "forbidden") -> None:
        super().__init__(code)
