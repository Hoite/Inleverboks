"""House style: the public stylesheet, logo and favicon, and the admin screen."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from inleverboks.i18n import gettext as _
from inleverboks.services.audit import Actor
from inleverboks.services.branding import (
    CONTRAST_PAIRS,
    MAX_LOGO_BYTES,
    Branding,
    BrandingValidationError,
    normalize_color,
    validate,
)
from inleverboks.services.errors import DomainError
from inleverboks.web.deps import Admin, Db, Svc, client_ip
from inleverboks.web.messages import describe
from inleverboks.web.templating import render

router = APIRouter()

IMMUTABLE = "public, max-age=31536000, immutable"


def _cache_headers(request: Request, version: str) -> dict[str, str]:
    # Versioned URLs never change; an unversioned or stale URL is revalidated.
    fresh = request.query_params.get("v") == version
    return {
        "Cache-Control": IMMUTABLE if fresh else "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


@router.get("/huisstijl/stijl.css")
def stylesheet(request: Request) -> Response:
    brand: Branding = request.app.state.brand()
    return Response(
        brand.css(),
        media_type="text/css; charset=utf-8",
        headers=_cache_headers(request, brand.version),
    )


def _image(request: Request, services: Svc, key: str | None, version: str) -> Response:
    if key is None:
        return Response(status_code=404)
    return Response(
        services.branding.read(key),
        media_type="image/png",
        headers=_cache_headers(request, version),
    )


@router.get("/huisstijl/logo.png")
def logo(request: Request, services: Svc) -> Response:
    brand: Branding = request.app.state.brand()
    return _image(request, services, brand.logo_key, brand.version)


@router.get("/huisstijl/favicon.png")
def favicon(request: Request, services: Svc) -> Response:
    brand: Branding = request.app.state.brand()
    return _image(request, services, brand.favicon_key, brand.version)


# Admin screen --------------------------------------------------------------------


def pair_labels() -> dict[str, str]:
    return {
        "body_text": _("Text on background"),
        "links": _("Links and headings"),
        "link_hover": _("Links on hover"),
        "header_buttons": _("White text on the header and buttons"),
        "button_hover": _("White text on buttons on hover"),
        "table_headers": _("Primary colour on the light tint (table headers, tabs)"),
        "text_on_tint": _("Text on the light tint"),
        "accent_lines": _("Accent lines (not text, at least 3:1)"),
        "admin_link": _("Admin link in the header (fixed colours)"),
        "admin_tabs": _("Admin tabs on the background"),
        "admin_tab_current": _("Admin tab on the light tint (current tab)"),
        "tcp_button": _("Team-contact buttons (fixed colours)"),
        "tcp_button_hover": _("Team-contact buttons on hover"),
        "tcp_badge": _("Team-contact label"),
    }


def color_labels() -> dict[str, str]:
    return {
        "primary": _("Primary colour"),
        "primary_strong": _("Primary colour, dark (hover)"),
        "primary_tint": _("Light tint (panels, table headers)"),
        "accent": _("Accent colour"),
        "text": _("Text"),
        "bg": _("Background"),
    }


def _field_errors(problems: dict[str, str]) -> dict[str, str]:
    messages = {
        "app_name": _("Enter a name of at most 60 characters."),
        "color": _("Enter a colour as #rrggbb."),
        "font": _("Choose a font."),
    }
    return {key: messages[code] for key, code in problems.items()}


def _page(
    request: Request, services: Svc, db: Db, *, preview: Branding | None = None,
    errors: dict[str, str] | None = None, messages: list[str] | None = None, ok: str | None = None,
    status: int = 200, checked: bool = False,
) -> Response:  # fmt: skip
    saved = services.branding.load(db)
    shown = preview or saved
    return render(
        request,
        "admin/branding.html.j2",
        status_code=status,
        shown=shown,
        saved=saved,
        results=shown.contrast(),
        pairs=CONTRAST_PAIRS,
        pair_labels=pair_labels(),
        color_labels=color_labels(),
        errors=errors or {},
        messages=messages or [],
        ok=ok,
        checked=checked,
        max_logo_kb=MAX_LOGO_BYTES // 1024,
    )


@router.get("/admin/huisstijl")
def branding_form(
    request: Request, ctx: Admin, db: Db, services: Svc, ok: str | None = None
) -> Response:
    return _page(request, services, db, ok=ok)


@router.post("/admin/huisstijl")
def branding_save(
    request: Request, ctx: Admin, db: Db, services: Svc,
    app_name: Annotated[str, Form(max_length=200)] = "",
    font: Annotated[str, Form(max_length=20)] = "",
    action: Annotated[str, Form(max_length=20)] = "",
    color_primary: Annotated[str, Form(max_length=20)] = "",
    color_primary_strong: Annotated[str, Form(max_length=20)] = "",
    color_primary_tint: Annotated[str, Form(max_length=20)] = "",
    color_accent: Annotated[str, Form(max_length=20)] = "",
    color_text: Annotated[str, Form(max_length=20)] = "",
    color_bg: Annotated[str, Form(max_length=20)] = "",
) -> Response:  # fmt: skip
    raw = {
        "primary": color_primary, "primary_strong": color_primary_strong,
        "primary_tint": color_primary_tint, "accent": color_accent, "text": color_text,
        "bg": color_bg,
    }  # fmt: skip
    colors = {key: normalize_color(value) for key, value in raw.items()}
    app_name = " ".join(app_name.split())
    problems = validate(app_name, colors, font)
    current = services.branding.load(db)
    if problems:
        return _page(request, services, db, errors=_field_errors(problems), status=400)
    preview = Branding(
        app_name=app_name, colors=colors, font=font, logo=current.logo, favicon=current.favicon
    )
    if action == "check":
        return _page(request, services, db, preview=preview, checked=True)
    try:
        services.branding.save(
            db, actor=Actor.for_user(ctx.user), ip=client_ip(request), app_name=app_name,
            colors=colors, font=font,
        )  # fmt: skip
    except BrandingValidationError as exc:
        db.rollback()
        return _page(request, services, db, errors=_field_errors(exc.problems), status=400)
    return RedirectResponse("/admin/huisstijl?ok=saved", status_code=303)


@router.post("/admin/huisstijl/standaard")
def branding_reset(request: Request, ctx: Admin, db: Db, services: Svc) -> Response:
    services.branding.reset(db, actor=Actor.for_user(ctx.user), ip=client_ip(request))
    return RedirectResponse("/admin/huisstijl?ok=reset", status_code=303)


@router.post("/admin/huisstijl/logo")
def branding_logo(
    request: Request, ctx: Admin, db: Db, services: Svc,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:  # fmt: skip
    data = logo.file.read(MAX_LOGO_BYTES + 1) if logo is not None else b""
    try:
        if not data:
            raise DomainError("logo_missing")
        services.branding.set_logo(
            db, actor=Actor.for_user(ctx.user), ip=client_ip(request), data=data
        )
    except DomainError as exc:
        db.rollback()
        return _page(request, services, db, messages=describe(exc), status=400)
    return RedirectResponse("/admin/huisstijl?ok=logo", status_code=303)


@router.post("/admin/huisstijl/logo/verwijderen")
def branding_logo_remove(request: Request, ctx: Admin, db: Db, services: Svc) -> Response:
    services.branding.remove_logo(db, actor=Actor.for_user(ctx.user), ip=client_ip(request))
    return RedirectResponse("/admin/huisstijl?ok=logo_removed", status_code=303)
