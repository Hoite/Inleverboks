"""Slugs for hand-in links: ``{APP_BASE_URL}/{slug}`` (PLAN B2)."""

from __future__ import annotations

import re
import secrets
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.db.models import Exam, RetiredSlug
from inleverboks.services.errors import DomainError

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,78}[a-z0-9])$")
MAX_LENGTH = 80
# Unambiguous lowercase alphabet for generated slugs (no 0/o, 1/i/l).
GENERATED_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# Every top-level path of the application, plus names that could become paths later.
RESERVED = frozenset(
    {
        "account", "admin", "api", "assets", "docent", "docenten", "examen", "examens",
        "favicon-ico", "health", "healthz", "help", "i", "inleveren", "inloggen", "login",
        "logout", "metrics", "ontvangstbewijs", "over", "presentatie", "privacy", "qr",
        "readyz", "reset", "robots-txt", "s", "static", "status", "student", "uitloggen",
        "uitnodiging", "vendor", "well-known", "www",
    }
)  # fmt: skip


def slugify(text: str) -> str:
    """Lowercase ASCII, dashes between words: 'Examen P2 K1!' → 'examen-p2-k1'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:MAX_LENGTH].strip("-")


def generate_random() -> str:
    """``xxx-xxx-xxx`` from an unambiguous alphabet."""
    return "-".join("".join(secrets.choice(GENERATED_ALPHABET) for _ in range(3)) for _ in range(3))


def validate_format(slug: str) -> None:
    if not SLUG_RE.fullmatch(slug) or "--" in slug:
        raise DomainError("slug_invalid")
    if slug in RESERVED:
        raise DomainError("slug_reserved")


def is_taken(db: Session, slug: str, *, exclude_exam_id: object = None) -> bool:
    query = select(Exam.id).where(Exam.slug == slug)
    if exclude_exam_id is not None:
        query = query.where(Exam.id != exclude_exam_id)
    if db.scalar(query) is not None:
        return True
    return db.get(RetiredSlug, slug) is not None


def check_available(db: Session, slug: str, *, exclude_exam_id: object = None) -> None:
    validate_format(slug)
    if is_taken(db, slug, exclude_exam_id=exclude_exam_id):
        raise DomainError("slug_taken")


def suggest(db: Session, name: str) -> str:
    """A free slug derived from the exam name, with a numeric suffix when needed."""
    base = slugify(name)
    if len(base) < 3 or base in RESERVED:
        base = f"examen-{base}".strip("-") if base else "examen"
    base = base[: MAX_LENGTH - 4].strip("-")
    candidate = base
    counter = 2
    while is_taken(db, candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def unique_random(db: Session) -> str:
    while True:
        candidate = generate_random()
        if not is_taken(db, candidate):
            return candidate
