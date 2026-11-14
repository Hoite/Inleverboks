"""Pure ASGI middleware: security headers and a global request body limit."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "worker-src 'none'",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "camera",
        "geolocation",
        "gyroscope",
        "magnetometer",
        "microphone",
        "payment",
        "usb",
        "interest-cohort",
    )
)

HSTS_VALUE = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Adds the strict default headers to every HTTP response.

    A route may set its own Content-Security-Policy (for example the sandboxed preview
    frame); in that case the route's value is kept.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CSP)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "same-origin")
                headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("X-Frame-Options", "DENY")
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitError(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects request bodies above ``max_bytes``.

    Checks Content-Length up front and also counts streamed bytes, so chunked uploads
    cannot bypass the limit. Per-file and per-submission limits are enforced separately
    while streaming the upload.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _plain_response(send, 400, b"Bad Request")
                    return
                if declared > self.max_bytes:
                    await _plain_response(send, 413, b"Request Entity Too Large")
                    return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodySizeLimitError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodySizeLimitError:
            if not response_started:
                await _plain_response(send, 413, b"Request Entity Too Large")


async def _plain_response(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class CachedStaticFiles(StaticFiles):
    """Static files with long caching when requested with a content hash (``?v=``).

    The preview frame (``preview/``) runs in a sandboxed iframe with an opaque origin. It
    gets its own CSP (inline styles allowed there only, for docx-preview and SheetJS), and
    the libraries it loads get ``Access-Control-Allow-Origin: *`` because module imports
    and fetches from an opaque origin are cross-origin. These are public files.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if scope.get("query_string", b"").startswith(b"v=") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        if path.startswith(("preview/", "vendor/")):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        if path == "preview/frame.html":
            origin = _origin(scope)
            response.headers["Content-Security-Policy"] = PREVIEW_CSP.format(origin=origin)
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response


PREVIEW_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self' {origin}",
        "style-src 'self' {origin} 'unsafe-inline'",
        "img-src blob: data:",
        "font-src blob: data:",
        "connect-src 'self' {origin}",
        "worker-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
    ]
)


def _origin(scope: Scope) -> str:
    scheme = scope.get("scheme", "http")
    host = ""
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
            break
    if not host or any(ch in host for ch in " ;,'\"\n"):
        return ""
    return f"{scheme}://{host}"
