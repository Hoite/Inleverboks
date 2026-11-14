"""Small cryptographic helpers: random tokens, token hashing and secret encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Unambiguous alphabet: no 0/O, 1/I/l. Used for human-readable codes.
HUMAN_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_token(nbytes: int = 32) -> str:
    """URL-safe random token for links and cookies."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 of a high-entropy token. Only this hash is stored in the database."""
    return hashlib.sha256(token.encode()).hexdigest()


def human_code(length: int, alphabet: str = HUMAN_ALPHABET) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class SecretBox:
    """AES-256-GCM encryption with a key derived from SECRET_KEY via HKDF.

    Used for values that must be recoverable (TOTP secrets). Rotating SECRET_KEY makes
    existing ciphertexts unreadable; users then have to set up TOTP again.
    """

    def __init__(self, secret_key: str, purpose: str) -> None:
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"inleverboks",
            info=purpose.encode(),
        ).derive(secret_key.encode())
        self._aead = AESGCM(key)
        self._aad = purpose.encode()

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode(), self._aad)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, token: str) -> str:
        raw = base64.urlsafe_b64decode(token.encode())
        return self._aead.decrypt(raw[:12], raw[12:], self._aad).decode()


def keyed_hash(secret_key: str, purpose: str, value: str) -> str:
    """HMAC-SHA256 for short secrets (recovery and verification codes)."""
    key = hashlib.sha256(f"{purpose}:{secret_key}".encode()).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
