"""TOTP (RFC 6238) and recovery codes."""

from __future__ import annotations

import io
from dataclasses import dataclass

import pyotp
import segno

from inleverboks.services.security import SecretBox, human_code, keyed_hash

ISSUER = "Inleverboks"
RECOVERY_CODE_COUNT = 10
_RECOVERY_PURPOSE = "recovery-code"


@dataclass(frozen=True)
class TotpCheck:
    ok: bool
    step: int | None = None


QUIET_ZONE = 4  # modules of white around a QR code (ISO/IEC 18004)


class TotpService:
    def __init__(self, secret_key: str, issuer: str = ISSUER) -> None:
        self._box = SecretBox(secret_key, "totp-secret")
        self._secret_key = secret_key
        self.issuer = issuer

    def new_secret(self) -> str:
        return pyotp.random_base32()

    def encrypt(self, secret: str) -> str:
        return self._box.encrypt(secret)

    def decrypt(self, secret_enc: str) -> str:
        return self._box.decrypt(secret_enc)

    def provisioning_uri(self, secret: str, account: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=self.issuer)

    def qr_svg(self, uri: str) -> str:
        buffer = io.BytesIO()
        # A viewBox (omitsize) lets CSS scale the code without cropping it, and the quiet
        # zone of 4 modules is what the QR standard asks for; scanners need it.
        segno.make(uri, error="m").save(
            buffer, kind="svg", scale=5, border=QUIET_ZONE, xmldecl=False, omitsize=True
        )
        return buffer.getvalue().decode()

    def verify(
        self, secret: str, code: str, *, now: float, last_step: int | None = None
    ) -> TotpCheck:
        """Accept the current code and one step either side; reject reuse of a step."""
        code = "".join(ch for ch in code if ch.isdigit())
        if len(code) != 6:
            return TotpCheck(ok=False)
        totp = pyotp.TOTP(secret)
        current = int(now // totp.interval)
        for step in (current - 1, current, current + 1):
            if last_step is not None and step <= last_step:
                continue
            if totp.at(step * totp.interval) == code:
                return TotpCheck(ok=True, step=step)
        return TotpCheck(ok=False)

    # Recovery codes -----------------------------------------------------------------

    def new_recovery_codes(self) -> list[str]:
        return [f"{human_code(4)}-{human_code(4)}" for _ in range(RECOVERY_CODE_COUNT)]

    def hash_recovery_code(self, code: str) -> str:
        normalized = code.strip().upper().replace(" ", "").replace("-", "")
        return keyed_hash(self._secret_key, _RECOVERY_PURPOSE, normalized)
