"""House style set by the admin: app name, colours, font and logo, with a WCAG contrast check.

The values live in the ``setting`` row ``branding``. Pages load them as a stylesheet
(``/huisstijl/stijl.css?v=<version>``), so the strict CSP needs no inline styles. The logo
is decoded and re-encoded as PNG by Pillow (which drops metadata and anything hidden in
the file) and stored as ``branding/{sha256}.png``, with a derived 64x64 favicon.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.adapters.storage import StorageBackend
from inleverboks.db.models import Setting
from inleverboks.services.asyncutil import call_async
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.errors import DomainError

log = structlog.get_logger(__name__)

SETTING_KEY = "branding"
DEFAULT_APP_NAME = "Inleverboks"
DEFAULT_COLORS: dict[str, str] = {
    "primary": "#005381",
    "primary_strong": "#003782",
    "primary_tint": "#e6eef2",
    "accent": "#e73443",
    "text": "#313131",
    "bg": "#ffffff",
}
FONTS = ("poppins", "system")
SYSTEM_STACK = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
MAX_APP_NAME = 60
MAX_LOGO_BYTES = 1024 * 1024
MAX_LOGO_PIXELS = 4_000_000
LOGO_BOX = (512, 512)
FAVICON_SIZE = 64
CACHE_SECONDS = 30.0

_HEX = re.compile(r"^#[0-9a-f]{6}$")
_WHITE = "#ffffff"


@dataclass(frozen=True)
class ContrastPair:
    key: str
    fg: str  # a colour key, or a literal "#rrggbb"
    bg: str
    minimum: float


# Every pair of colours the stylesheet puts on top of each other (see app.css).
CONTRAST_PAIRS = (
    ContrastPair("body_text", "text", "bg", 4.5),
    ContrastPair("links", "primary", "bg", 4.5),
    ContrastPair("link_hover", "primary_strong", "bg", 4.5),
    ContrastPair("header_buttons", _WHITE, "primary", 4.5),
    ContrastPair("button_hover", _WHITE, "primary_strong", 4.5),
    ContrastPair("table_headers", "primary", "primary_tint", 4.5),
    ContrastPair("text_on_tint", "text", "primary_tint", 4.5),
    ContrastPair("accent_lines", "accent", "bg", 3.0),  # non-text: WCAG 1.4.11
)


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@dataclass(frozen=True)
class ContrastResult:
    pair: ContrastPair
    fg: str
    bg: str
    ratio: float

    @property
    def ok(self) -> bool:
        # Compare the rounded value people see, so "4.50:1" never shows as failing.
        return round(self.ratio, 2) >= self.pair.minimum


@dataclass(frozen=True)
class Branding:
    app_name: str = DEFAULT_APP_NAME
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    font: str = "poppins"
    logo: str | None = None  # sha256 of the stored PNG
    favicon: str | None = None

    @property
    def version(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:12]

    @property
    def logo_key(self) -> str | None:
        return f"branding/{self.logo}.png" if self.logo else None

    @property
    def favicon_key(self) -> str | None:
        return f"branding/favicon-{self.favicon}.png" if self.favicon else None

    def color(self, ref: str) -> str:
        return ref if ref.startswith("#") else self.colors[ref]

    def contrast(self) -> list[ContrastResult]:
        results = []
        for pair in CONTRAST_PAIRS:
            fg, bg = self.color(pair.fg), self.color(pair.bg)
            results.append(ContrastResult(pair, fg, bg, contrast_ratio(fg, bg)))
        return results

    def contrast_problems(self) -> list[ContrastResult]:
        return [result for result in self.contrast() if not result.ok]

    def css(self) -> str:
        lines = [
            f"  --color-{key.replace('_', '-')}: {value};" for key, value in self.colors.items()
        ]
        font = f'"Poppins", {SYSTEM_STACK}' if self.font == "poppins" else SYSTEM_STACK
        lines.append(f"  --font-sans: {font};")
        return ":root {\n" + "\n".join(lines) + "\n}\n"


def _from_stored(value: Any) -> Branding:
    if not isinstance(value, dict):
        return Branding()
    colors = dict(DEFAULT_COLORS)
    for key, color in (value.get("colors") or {}).items():
        if key in colors and isinstance(color, str) and _HEX.fullmatch(color):
            colors[key] = color
    font = value.get("font")
    return Branding(
        app_name=str(value.get("app_name") or DEFAULT_APP_NAME)[:MAX_APP_NAME],
        colors=colors,
        font=font if font in FONTS else "poppins",
        logo=value.get("logo"),
        favicon=value.get("favicon"),
    )


def validate(app_name: str, colors: dict[str, str], font: str) -> dict[str, str]:
    problems: dict[str, str] = {}
    if not app_name or len(app_name) > MAX_APP_NAME:
        problems["app_name"] = "app_name"
    for key in DEFAULT_COLORS:
        if not _HEX.fullmatch(colors.get(key, "")):
            problems[key] = "color"
    if font not in FONTS:
        problems["font"] = "font"
    return problems


def normalize_color(value: str) -> str:
    value = value.strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        value = "#" + "".join(ch * 2 for ch in value[1:])
    return value


class BrandingValidationError(ValueError):
    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__(", ".join(problems))
        self.problems = problems


def process_logo(data: bytes) -> tuple[bytes, bytes]:
    """Validate a PNG/WebP upload and return (logo PNG, favicon PNG)."""
    # Imported lazily; Pillow comes with WeasyPrint.
    from PIL import Image  # noqa: PLC0415

    if len(data) > MAX_LOGO_BYTES:
        raise DomainError("logo_too_large")
    is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (is_png or is_webp):
        raise DomainError("logo_format")
    try:
        with Image.open(io.BytesIO(data), formats=["PNG", "WEBP"]) as source:
            width, height = source.size
            if width * height > MAX_LOGO_PIXELS:
                raise DomainError("logo_too_large")
            source.load()
            image = source.convert("RGBA")
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("logo_invalid") from exc
    image.thumbnail(LOGO_BOX)
    logo = io.BytesIO()
    image.save(logo, format="PNG", optimize=True)
    icon = image.copy()
    icon.thumbnail((FAVICON_SIZE, FAVICON_SIZE))
    canvas = Image.new("RGBA", (FAVICON_SIZE, FAVICON_SIZE), (0, 0, 0, 0))
    canvas.paste(icon, ((FAVICON_SIZE - icon.width) // 2, (FAVICON_SIZE - icon.height) // 2))
    favicon = io.BytesIO()
    canvas.save(favicon, format="PNG", optimize=True)
    return logo.getvalue(), favicon.getvalue()


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


class BrandingService:
    def __init__(self, clock: Clock, audit: AuditService, storage: StorageBackend) -> None:
        self.clock = clock
        self.audit = audit
        self.storage = storage
        self._lock = threading.Lock()
        self._cached: tuple[float, Branding] | None = None

    # Reading ---------------------------------------------------------------------

    def load(self, db: Session) -> Branding:
        row = db.get(Setting, SETTING_KEY)
        return _from_stored(row.value if row else None)

    def current(self, make_session: Callable[[], Session]) -> Branding:
        """Cached for a short while; other replicas pick up a change within 30 seconds."""
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
        try:
            with make_session() as db:
                branding = self.load(db)
        except SQLAlchemyError:
            # Never break a page (or the error page) because the database is unavailable.
            log.warning("branding.unavailable")
            return Branding()
        with self._lock:
            self._cached = (time.monotonic(), branding)
        return branding

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def read(self, key: str) -> bytes:
        async def collect() -> bytes:
            return b"".join([chunk async for chunk in self.storage.open(key)])

        return call_async(collect)

    # Writing ---------------------------------------------------------------------

    def _store(self, db: Session, branding: Branding, by: Actor) -> None:
        value = asdict(branding)
        user_id = uuid.UUID(by.id) if by.type == "user" and by.id else None
        row = db.get(Setting, SETTING_KEY)
        now = self.clock.now()
        if row is None:
            db.add(Setting(key=SETTING_KEY, value=value, updated_at=now, updated_by_id=user_id))
        else:
            row.value = value
            row.updated_at = now
            row.updated_by_id = user_id

    def save(
        self, db: Session, *, actor: Actor, ip: str | None, app_name: str, colors: dict[str, str],
        font: str,
    ) -> Branding:  # fmt: skip
        problems = validate(app_name, colors, font)
        if problems:
            raise BrandingValidationError(problems)
        current = self.load(db)
        new = replace(current, app_name=app_name, colors=dict(colors), font=font)
        changes: dict[str, dict[str, Any]] = {}
        if new.app_name != current.app_name:
            changes["app_name"] = {"old": current.app_name, "new": new.app_name}
        if new.font != current.font:
            changes["font"] = {"old": current.font, "new": new.font}
        for key, value in new.colors.items():
            if current.colors[key] != value:
                changes[key] = {"old": current.colors[key], "new": value}
        if changes:
            self._store(db, new, actor)
            warnings = [result.pair.key for result in new.contrast_problems()]
            self.audit.append(
                db, "branding.changed", actor=actor, ip=ip,
                details={"changes": changes, "contrast_warnings": warnings},
            )  # fmt: skip
        db.commit()
        self.invalidate()
        return new

    def reset(self, db: Session, *, actor: Actor, ip: str | None) -> Branding:
        """Back to the default name, colours and font; the logo is kept."""
        current = self.load(db)
        new = Branding(logo=current.logo, favicon=current.favicon)
        self._store(db, new, actor)
        self.audit.append(db, "branding.reset", actor=actor, ip=ip)
        db.commit()
        self.invalidate()
        return new

    def set_logo(self, db: Session, *, actor: Actor, ip: str | None, data: bytes) -> Branding:
        logo, favicon = process_logo(data)
        logo_sha = hashlib.sha256(logo).hexdigest()
        favicon_sha = hashlib.sha256(favicon).hexdigest()
        current = self.load(db)
        new = replace(current, logo=logo_sha, favicon=favicon_sha)
        call_async(self.storage.put, new.logo_key, _one_chunk(logo))
        call_async(self.storage.put, new.favicon_key, _one_chunk(favicon))
        self._store(db, new, actor)
        self.audit.append(
            db, "branding.logo_changed", actor=actor, ip=ip,
            details={"sha256": logo_sha, "bytes": len(logo)},
        )  # fmt: skip
        db.commit()
        self.invalidate()
        self._delete_old(current, new)
        return new

    def remove_logo(self, db: Session, *, actor: Actor, ip: str | None) -> Branding:
        current = self.load(db)
        if current.logo is None:
            return current
        new = replace(current, logo=None, favicon=None)
        self._store(db, new, actor)
        self.audit.append(db, "branding.logo_removed", actor=actor, ip=ip)
        db.commit()
        self.invalidate()
        self._delete_old(current, new)
        return new

    def _delete_old(self, old: Branding, new: Branding) -> None:
        for key in (old.logo_key, old.favicon_key):
            if key and key not in (new.logo_key, new.favicon_key):
                call_async(self.storage.delete, key)
