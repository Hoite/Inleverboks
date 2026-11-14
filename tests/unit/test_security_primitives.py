from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from inleverboks.adapters.clock import FrozenClock
from inleverboks.adapters.ratelimit import MemoryRateLimiter, RateLimitRule
from inleverboks.services.passwords import (
    PasswordProblem,
    check_password_policy,
    hash_password,
    verify_password,
)
from inleverboks.services.security import HUMAN_ALPHABET, SecretBox, human_code
from inleverboks.services.totp import TotpService
from tests.conftest import TEST_SECRET

# Passwords -----------------------------------------------------------------------


def test_argon2id_hash_and_verify() -> None:
    hashed = hash_password("Een-lang-wachtwoord-42")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "Een-lang-wachtwoord-42")
    assert not verify_password(hashed, "Een-lang-wachtwoord-43")
    assert not verify_password(None, "anything")
    assert not verify_password("not-a-hash", "anything")


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short-pw-1", PasswordProblem.TOO_SHORT),
        ("x" * 300, PasswordProblem.TOO_LONG),
        ("123456789012", PasswordProblem.TOO_COMMON),
        ("qwertyuiopasdfghjkl", PasswordProblem.TOO_COMMON),
        ("Wachtwoord123", PasswordProblem.TOO_COMMON),
        ("jan.jansen-is-cool", PasswordProblem.CONTAINS_EMAIL),
    ],
)
def test_password_policy_rejects(password: str, problem: PasswordProblem) -> None:
    assert problem in check_password_policy(password, "jan.jansen@school.nl")


def test_password_policy_accepts_strong() -> None:
    assert check_password_policy("Paarse-olifant-fietst-7", "jan@school.nl") == []


# Secrets and codes ---------------------------------------------------------------


def test_secret_box_roundtrip_and_purpose_separation() -> None:
    box = SecretBox(TEST_SECRET, "totp-secret")
    token = box.encrypt("JBSWY3DPEHPK3PXP")
    assert token != "JBSWY3DPEHPK3PXP"
    assert box.decrypt(token) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(Exception):  # noqa: B017 - any crypto failure
        SecretBox(TEST_SECRET, "other-purpose").decrypt(token)
    with pytest.raises(Exception):  # noqa: B017
        SecretBox("another-secret-key-0123456789abcdef", "totp-secret").decrypt(token)


def test_human_code_has_no_confusing_characters() -> None:
    code = human_code(2000)
    assert set(code) <= set(HUMAN_ALPHABET)
    assert not set(code) & set("0O1Il")


# TOTP ----------------------------------------------------------------------------


def test_totp_accepts_adjacent_steps_and_rejects_reuse() -> None:
    service = TotpService(TEST_SECRET)
    secret = service.new_secret()
    now = int(datetime(2026, 11, 13, 9, 0, tzinfo=UTC).timestamp())
    totp = pyotp.TOTP(secret)
    current = totp.at(now)
    result = service.verify(secret, current, now=now)
    assert result.ok
    assert result.step is not None
    assert service.verify(secret, totp.at(now - 30), now=now).ok
    assert service.verify(secret, totp.at(now + 30), now=now).ok
    assert not service.verify(secret, totp.at(now - 90), now=now).ok
    # The same step may not be used twice.
    assert not service.verify(secret, current, now=now, last_step=result.step).ok
    assert not service.verify(secret, "12345", now=now).ok


def test_recovery_codes_are_hashed_and_normalized() -> None:
    service = TotpService(TEST_SECRET)
    codes = service.new_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    code = codes[0]
    assert service.hash_recovery_code(code) == service.hash_recovery_code(code.lower())
    assert service.hash_recovery_code(code) == service.hash_recovery_code(code.replace("-", ""))
    assert code not in service.hash_recovery_code(code)


def test_qr_svg_contains_no_script() -> None:
    service = TotpService(TEST_SECRET)
    svg = service.qr_svg(service.provisioning_uri(service.new_secret(), "a@school.nl"))
    assert svg.startswith("<svg")
    assert "<script" not in svg


# Rate limiting -------------------------------------------------------------------

RULE = RateLimitRule(
    free=2, block_after=5, window=timedelta(minutes=10), block_for=timedelta(minutes=15)
)


def test_rate_limit_delay_grows_then_blocks() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    limiter = MemoryRateLimiter(clock, {"x": RULE})
    delays = [limiter.fail("x", "k").delay for _ in range(4)]
    assert delays == [0.0, 0.0, 1.0, 2.0]
    status = limiter.fail("x", "k")
    assert status.blocked
    assert status.retry_after == pytest.approx(15 * 60)
    clock.advance(minutes=14)
    assert limiter.status("x", "k").blocked
    clock.advance(minutes=2)
    assert not limiter.status("x", "k").blocked
    assert limiter.status("x", "k").delay == 0.0


def test_rate_limit_window_expires_and_reset() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    limiter = MemoryRateLimiter(clock, {"x": RULE})
    for _ in range(4):
        limiter.fail("x", "k")
    clock.advance(minutes=11)
    assert limiter.status("x", "k").delay == 0.0
    for _ in range(4):
        limiter.fail("x", "k")
    limiter.reset("x", "k")
    assert limiter.status("x", "k") == limiter.status("x", "other")


def test_rate_limit_keys_are_independent() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    limiter = MemoryRateLimiter(clock, {"x": RULE})
    for _ in range(5):
        limiter.fail("x", "a")
    assert limiter.status("x", "a").blocked
    assert not limiter.status("x", "b").blocked


def test_max_delay_is_capped() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    rule = RateLimitRule(
        free=0, block_after=100, window=timedelta(hours=1), block_for=timedelta(minutes=1)
    )
    limiter = MemoryRateLimiter(clock, {"x": rule})
    delays = [limiter.fail("x", "k").delay for _ in range(10)]
    assert max(delays) == rule.max_delay
