"""Request dependencies: database session, services, authentication and authorization."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from inleverboks.policy import SystemAction, can
from inleverboks.services.auth import AuthContext, Stage
from inleverboks.services.container import Services

SESSION_COOKIE = "ib_session"

STAGE_PATHS: dict[Stage, str] = {
    Stage.MFA: "/login/code",
    Stage.CHANGE_PASSWORD: "/account/wachtwoord",
    Stage.SETUP_TOTP: "/account/tweestaps",
}


class RedirectError(Exception):
    """Raised by dependencies to send the browser elsewhere (HTTP 303)."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class ForbiddenError(Exception):
    pass


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.sessionmaker()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str | None:
    """Real client IP; X-Forwarded-For has already been resolved for trusted proxies."""
    return request.client.host if request.client else None


def is_background_poll(request: Request) -> bool:
    """HTMX polling elements use an id starting with ``poll``; they must not keep an
    idle session alive (PLAN B12)."""
    return request.headers.get("hx-trigger", "").startswith("poll")


Db = Annotated[Session, Depends(get_db)]
Svc = Annotated[Services, Depends(get_services)]


def optional_auth(request: Request, db: Db, services: Svc) -> AuthContext | None:
    if hasattr(request.state, "auth"):
        cached: AuthContext | None = request.state.auth
        return cached
    token = request.cookies.get(SESSION_COOKIE)
    ctx = services.auth.load(db, token, touch=not is_background_poll(request))
    request.state.auth = ctx
    return ctx


OptionalAuth = Annotated[AuthContext | None, Depends(optional_auth)]


def _login_redirect(request: Request) -> RedirectError:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectError("/login?next=" + quote(target, safe=""))


def require_stage(stage: Stage):  # type: ignore[no-untyped-def]  # dependency factory
    def dependency(request: Request, ctx: OptionalAuth) -> AuthContext:
        if ctx is None:
            raise _login_redirect(request)
        if ctx.stage is not stage:
            raise RedirectError(STAGE_PATHS.get(ctx.stage, "/docent"))
        return ctx

    return dependency


def require_teacher(request: Request, ctx: OptionalAuth) -> AuthContext:
    """A fully authenticated teacher (stage ``full``)."""
    if ctx is None:
        raise _login_redirect(request)
    if ctx.stage is not Stage.FULL:
        raise RedirectError(STAGE_PATHS[ctx.stage])
    return ctx


Teacher = Annotated[AuthContext, Depends(require_teacher)]


def require_admin(ctx: Teacher) -> AuthContext:
    if not can(ctx.user, SystemAction.MANAGE_USERS):
        raise ForbiddenError
    return ctx


Admin = Annotated[AuthContext, Depends(require_admin)]
