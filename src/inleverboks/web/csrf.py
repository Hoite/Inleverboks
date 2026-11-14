"""CSRF protection: signed double-submit cookie plus an Origin check.

A random value lives in the HttpOnly cookie ``ib_csrf``. Forms carry
``HMAC(SECRET_KEY, cookie)`` in the hidden field ``csrf_token``; scripts send it in the
``X-CSRF-Token`` header (the upload uses the header, so its streamed body is never read
here). Without the server secret an attacker cannot produce a matching token, even if
they manage to plant a cookie.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inleverboks.services.security import new_token

COOKIE_NAME = "ib_csrf"
FIELD_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), f"csrf:{value}".encode(), hashlib.sha256).hexdigest()


class CsrfCookieMiddleware:
    """Makes sure every visitor has a CSRF cookie and exposes the form token."""

    def __init__(self, app: ASGIApp, *, secret: str, secure: bool) -> None:
        self.app = app
        self.secret = secret
        self.secure = secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        cookie_value = request.cookies.get(COOKIE_NAME)
        is_new = not cookie_value or len(cookie_value) > 128
        value: str = new_token(24) if is_new or cookie_value is None else cookie_value
        state = scope.setdefault("state", {})
        state["csrf_cookie"] = value
        state["csrf_token"] = sign(self.secret, value)

        async def send_with_cookie(message: Message) -> None:
            if is_new and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                cookie = f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax"
                if self.secure:
                    cookie += "; Secure"
                headers.append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def _same_origin(request: Request, base_url: str) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        return origin != "null"
    parts = urlsplit(origin)
    allowed = {urlsplit(base_url).netloc, request.url.netloc}
    return parts.netloc in allowed


async def verify_csrf(request: Request) -> None:
    """App-wide dependency; a no-op for safe methods."""
    if request.method in SAFE_METHODS:
        return
    expected: str | None = getattr(request.state, "csrf_token", None)
    if expected is None:
        raise HTTPException(status_code=403, detail="CSRF")
    if not _same_origin(request, request.app.state.settings.app_base_url):
        raise HTTPException(status_code=403, detail="CSRF origin")
    supplied = request.headers.get(HEADER_NAME)
    if supplied is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            field = form.get(FIELD_NAME)
            supplied = field if isinstance(field, str) else None
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="CSRF")
