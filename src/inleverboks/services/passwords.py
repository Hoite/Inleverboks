"""Password hashing (argon2id) and password policy."""

from __future__ import annotations

import gzip
from enum import StrEnum
from functools import cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 256
COMMON_PASSWORDS_FILE = Path(__file__).resolve().parent.parent / "data" / "common-passwords.txt.gz"

_hasher = PasswordHasher()  # argon2id, RFC 9106 defaults

# Pre-computed hash used when no user exists, so login timing does not reveal accounts.
_DUMMY_HASH = _hasher.hash("inleverboks-dummy-password-for-timing")


class PasswordProblem(StrEnum):
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    TOO_COMMON = "too_common"
    CONTAINS_EMAIL = "contains_email"


@cache
def _common_passwords() -> frozenset[str]:
    data = gzip.decompress(COMMON_PASSWORDS_FILE.read_bytes()).decode()
    return frozenset(data.splitlines())


def check_password_policy(password: str, email: str | None = None) -> list[PasswordProblem]:
    problems: list[PasswordProblem] = []
    if len(password) < MIN_LENGTH:
        problems.append(PasswordProblem.TOO_SHORT)
    if len(password) > MAX_LENGTH:
        problems.append(PasswordProblem.TOO_LONG)
    lowered = password.strip().lower()
    if lowered in _common_passwords():
        problems.append(PasswordProblem.TOO_COMMON)
    if email:
        local_part = email.split("@", 1)[0].lower()
        if lowered == email.lower() or (len(local_part) >= 4 and local_part in lowered):
            problems.append(PasswordProblem.CONTAINS_EMAIL)
    return problems


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time verification; also burns time when there is no hash."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
