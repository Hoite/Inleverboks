"""Jinja2 environment with gettext, autoescaping and shared context."""

from __future__ import annotations

import hashlib
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from inleverboks import __version__
from inleverboks.config import Settings
from inleverboks.i18n import DEFAULT_LOCALE, get_translations
from inleverboks.services.branding import Branding

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@cache
def asset(path: str) -> str:
    """URL of a static file with a content hash, so browsers never use a stale copy."""
    file = (STATIC_DIR / path).resolve()
    if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
        msg = f"Unknown static file: {path}"
        raise ValueError(msg)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


def _context(request: Request) -> dict[str, Any]:
    auth = getattr(request.state, "auth", None)
    brand: Branding = request.app.state.brand()
    return {
        "brand": brand,
        "app_name": brand.app_name,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "auth": auth,
        "current_user": auth.user if auth else None,
        "current_path": request.url.path,
    }


def build_templates(settings: Settings) -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2", "xml"], default_for_string=True),
        undefined=StrictUndefined,
        extensions=["jinja2.ext.i18n"],
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.install_gettext_translations(get_translations(DEFAULT_LOCALE), newstyle=True)  # type: ignore[attr-defined]
    tz = settings.timezone

    def localtime(value: datetime | None, fmt: str = "%d-%m-%Y %H:%M") -> str:
        if value is None:
            return ""
        return value.astimezone(tz).strftime(fmt)

    env.filters["localtime"] = localtime
    env.globals.update(
        app_name="Inleverboks",
        app_version=__version__,
        lang=DEFAULT_LOCALE,
        demo_mode=settings.demo_mode,
        base_url=settings.app_base_url,
        asset=asset,
    )
    return Jinja2Templates(env=env, context_processors=[_context])


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    response: Response = templates.TemplateResponse(
        request, template, context, status_code=status_code
    )
    response.headers["Cache-Control"] = "no-store"
    return response
