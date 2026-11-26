"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from anyio import to_thread
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from inleverboks import __version__
from inleverboks.adapters.clock import Clock
from inleverboks.adapters.ratelimit import RateLimiter
from inleverboks.adapters.storage import build_storage
from inleverboks.config import Settings, get_settings
from inleverboks.db.session import build_engine, build_sessionmaker
from inleverboks.i18n import gettext as _
from inleverboks.log import configure_logging
from inleverboks.services.container import Services, build_services
from inleverboks.services.errors import NotFoundError, PermissionDeniedError
from inleverboks.services.scheduler import AppScheduler, Jobs
from inleverboks.services.student_flow import StateCodec
from inleverboks.web.csrf import CsrfCookieMiddleware, verify_csrf
from inleverboks.web.deps import ForbiddenError, RedirectError
from inleverboks.web.middleware import (
    BodySizeLimitMiddleware,
    CachedStaticFiles,
    SecurityHeadersMiddleware,
)
from inleverboks.web.pdf import PdfRenderer
from inleverboks.web.routes import (
    admin,
    auth,
    branding,
    classlists,
    exams,
    health,
    legacy,
    public,
    review,
    student,
    teams,
)
from inleverboks.web.routes.review import render_pv_factory
from inleverboks.web.templating import build_templates, render

STATIC_DIR = Path(__file__).parent / "web" / "static"

log = structlog.get_logger(__name__)


def _bootstrap(app: FastAPI) -> None:
    services: Services = app.state.services
    with app.state.sessionmaker() as db:
        try:
            services.users.bootstrap_admin(db, services.env)
        except SQLAlchemyError as exc:
            # Typically: migrations not applied yet. /readyz will report the problem.
            log.error("bootstrap.failed", error=type(exc).__name__)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await to_thread.run_sync(_bootstrap, app)
        scheduler = None
        if settings.scheduler_enabled:
            scheduler = AppScheduler(app.state.jobs)
            scheduler.start()
        log.info("app.started", version=__version__, storage=settings.storage_backend)
        yield
        if scheduler is not None:
            scheduler.shutdown()
        app.state.engine.dispose()

    app = FastAPI(
        title="Inleverboks",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(verify_csrf)],
    )
    storage = build_storage(settings)
    app.state.settings = settings
    app.state.engine = build_engine(settings.database_url)
    app.state.sessionmaker = build_sessionmaker(app.state.engine)
    app.state.storage = storage
    app.state.services = build_services(settings, storage, clock=clock, limiter=limiter)
    app.state.templates = build_templates(settings)
    app.state.student_codec = StateCodec(settings.secret_key.get_secret_value())
    services = app.state.services
    app.state.brand = lambda: services.branding.current(app.state.sessionmaker)
    app.state.pdf = PdfRenderer(app.state.templates.env, app.state.brand)
    app.state.jobs = Jobs(
        app.state.services, app.state.sessionmaker, render_pv_factory(app.state.pdf)
    )

    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")
    # The student page at /{slug} is a catch-all and must come last.
    for module in (
        health, public, branding, auth, teams, exams, review, classlists, admin, legacy, student,
    ):  # fmt: skip
        app.include_router(module.router)

    @app.exception_handler(RedirectError)
    async def redirect(request: Request, exc: RedirectError) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": exc.url})
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> Response:
        return render(
            request, "message.html.j2", status_code=404, title=_("Page not found"),
            link="/", link_text=_("Back to the start page"),
        )  # fmt: skip

    @app.exception_handler(PermissionDeniedError)
    async def denied(request: Request, exc: PermissionDeniedError) -> Response:
        return render(
            request,
            "message.html.j2",
            status_code=403,
            title=_("You do not have access to this page."),
        )

    @app.exception_handler(ForbiddenError)
    async def forbidden(request: Request, exc: ForbiddenError) -> Response:
        response: Response = render(
            request,
            "message.html.j2",
            status_code=403,
            title=_("You do not have access to this page."),
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if "text/html" in request.headers.get("accept", "") and exc.status_code in (403, 404):
            title = (
                _("Page not found") if exc.status_code == 404 else _("This request was refused.")
            )
            response: Response = render(
                request, "message.html.j2", status_code=exc.status_code, title=title,
                link="/", link_text=_("Back to the start page"),
            )  # fmt: skip
            return response
        return Response(
            content=str(exc.detail),
            status_code=exc.status_code,
            media_type="text/plain; charset=utf-8",
            headers=getattr(exc, "headers", None),
        )

    # The last added middleware runs first: proxy headers → body limit → CSRF cookie →
    # security headers → app.
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.hsts_enabled)
    app.add_middleware(
        CsrfCookieMiddleware,
        secret=settings.secret_key.get_secret_value(),
        secure=settings.session_cookie_secure,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=[str(net) for net in settings.trusted_proxies] or [],
    )
    return app
