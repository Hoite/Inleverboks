"""Authentication providers. Now: local e-mail + password. Later: OIDC (Entra ID).

Whatever the provider, the whitelist and the account state are checked afterwards by
``AuthService``, so a future OIDC provider cannot bypass them.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inleverboks.db.models import User
from inleverboks.services.passwords import verify_password


class AuthProvider(Protocol):
    name: str

    def authenticate(self, db: Session, email: str, password: str) -> User | None:
        """Return the user when the credentials are valid, else None."""
        ...


class LocalAuthProvider:
    name = "local"

    def authenticate(self, db: Session, email: str, password: str) -> User | None:
        user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
        if not verify_password(user.password_hash if user else None, password):
            return None
        return user
