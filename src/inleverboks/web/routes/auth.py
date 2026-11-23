"""Teacher login, logout, second factor, own account, invitations and reset links."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from inleverboks.db.models import User
from inleverboks.i18n import gettext as _
from inleverboks.services.auth import AuthContext, LoginOutcome, Stage
from inleverboks.services.errors import DomainError
from inleverboks.services.passwords import MIN_LENGTH
from inleverboks.web.deps import (
    SESSION_COOKIE,
    STAGE_PATHS,
    Db,
    OptionalAuth,
    Svc,
    Teacher,
    client_ip,
    require_stage,
)
from inleverboks.web.messages import describe
from inleverboks.web.templating import render

router = APIRouter()

MfaSession = Annotated[AuthContext, Depends(require_stage(Stage.MFA))]
PasswordStage = Annotated[AuthContext, Depends(require_stage(Stage.CHANGE_PASSWORD))]


def _safe_next(target: str | None) -> str:
    """Only allow local redirects (no open redirect)."""
    if not target:
        return "/docent"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return "/docent"
    return target


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=request.app.state.settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _after_stage(stage: Stage, next_url: str | None = None) -> str:
    return STAGE_PATHS.get(stage) or _safe_next(next_url)


# Login ---------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, ctx: OptionalAuth, next: str | None = None) -> Response:
    if ctx is not None and ctx.stage is Stage.FULL:
        return _redirect(_safe_next(next))
    return render(request, "auth/login.html.j2", next=_safe_next(next), errors=[], email="")


@router.post("/login")
def login(
    request: Request,
    db: Db,
    services: Svc,
    email: Annotated[str, Form(max_length=320)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    next: Annotated[str, Form()] = "",
) -> Response:
    result = services.auth.login(
        db, email, password, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    if result.outcome is LoginOutcome.OK and result.token and result.stage:
        response = _redirect(_after_stage(result.stage, next))
        _set_session_cookie(request, response, result.token)
        return response
    if result.delay:
        services.sleep(result.delay)
    if result.outcome is LoginOutcome.BLOCKED:
        errors = [_("Too many attempts. Try again later.")]
        status = 429
    else:
        errors = [_("E-mail address or password is incorrect.")]
        status = 400
    return render(
        request,
        "auth/login.html.j2",
        status_code=status,
        next=_safe_next(next),
        errors=errors,
        email=email,
    )


@router.get("/login/code")
def mfa_form(request: Request, ctx: MfaSession) -> Response:
    return render(request, "auth/mfa.html.j2", errors=[])


@router.post("/login/code")
def mfa(
    request: Request,
    ctx: MfaSession,
    db: Db,
    services: Svc,
    code: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    result = services.auth.verify_second_factor(db, ctx, code, ip=client_ip(request))
    if result.outcome is LoginOutcome.OK and result.token and result.stage:
        response = _redirect(_after_stage(result.stage))
        _set_session_cookie(request, response, result.token)
        return response
    if result.delay:
        services.sleep(result.delay)
    if result.outcome is LoginOutcome.BLOCKED:
        response = _redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
    return render(
        request,
        "auth/mfa.html.j2",
        status_code=400,
        errors=[_("The code is incorrect. Try again.")],
    )


@router.post("/logout")
def logout(request: Request, ctx: OptionalAuth, db: Db, services: Svc) -> Response:
    if ctx is not None:
        services.auth.logout(db, ctx, ip=client_ip(request))
    response = _redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# Own account ---------------------------------------------------------------------


def _password_page(
    request: Request, *, forced: bool, errors: list[str], status: int = 200
) -> Response:
    return render(
        request,
        "auth/password.html.j2",
        status_code=status,
        forced=forced,
        errors=errors,
        min_length=MIN_LENGTH,
    )


@router.get("/account/wachtwoord")
def password_form(request: Request, ctx: OptionalAuth) -> Response:
    if ctx is None:
        return _redirect("/login")
    if ctx.stage not in (Stage.FULL, Stage.CHANGE_PASSWORD):
        return _redirect(STAGE_PATHS[ctx.stage])
    return _password_page(request, forced=ctx.stage is Stage.CHANGE_PASSWORD, errors=[])


@router.post("/account/wachtwoord")
def change_password(
    request: Request,
    ctx: OptionalAuth,
    db: Db,
    services: Svc,
    current: Annotated[str, Form(max_length=1024)] = "",
    new: Annotated[str, Form(max_length=1024)] = "",
    confirm: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    if ctx is None:
        return _redirect("/login")
    if ctx.stage not in (Stage.FULL, Stage.CHANGE_PASSWORD):
        return _redirect(STAGE_PATHS[ctx.stage])
    forced = ctx.stage is Stage.CHANGE_PASSWORD
    if new != confirm:
        return _password_page(
            request, forced=forced, errors=describe(DomainError("password_mismatch")), status=400
        )
    try:
        services.users.change_password(
            db, ctx.user, current=current, new=new, keep_session_id=ctx.session.id,
            ip=client_ip(request),
        )  # fmt: skip
        stage = services.auth.advance(db, ctx)
        db.commit()
    except DomainError as exc:
        db.rollback()
        return _password_page(request, forced=forced, errors=describe(exc), status=400)
    return _redirect(STAGE_PATHS.get(stage, "/account?ok=password"))


@router.get("/account")
def account(
    request: Request, ctx: Teacher, services: Svc, db: Db, ok: str | None = None
) -> Response:
    return render(
        request,
        "auth/account.html.j2",
        ok=ok,
        totp_required=services.auth.totp_required(db, ctx.user),
        errors=[],
    )


def _totp_page(
    request: Request,
    ctx: AuthContext,
    services: Svc,
    db: Db,
    *,
    errors: list[str],
    status: int = 200,
) -> Response:
    user = ctx.user
    secret = services.users.pending_totp_secret(user) or services.users.start_totp_setup(db, user)
    uri = services.totp.provisioning_uri(secret, user.email)
    return render(
        request,
        "auth/totp_setup.html.j2",
        status_code=status,
        forced=ctx.stage is Stage.SETUP_TOTP,
        secret=secret,
        secret_groups=[secret[i : i + 4] for i in range(0, len(secret), 4)],
        qr_svg=services.totp.qr_svg(uri),
        errors=errors,
    )


@router.get("/account/tweestaps")
def totp_setup(request: Request, ctx: OptionalAuth, services: Svc, db: Db) -> Response:
    if ctx is None:
        return _redirect("/login")
    if ctx.stage not in (Stage.FULL, Stage.SETUP_TOTP):
        return _redirect(STAGE_PATHS[ctx.stage])
    if ctx.user.totp_enabled:
        return _redirect("/account")
    return _totp_page(request, ctx, services, db, errors=[])


@router.post("/account/tweestaps")
def totp_confirm(
    request: Request,
    ctx: OptionalAuth,
    services: Svc,
    db: Db,
    code: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    if ctx is None:
        return _redirect("/login")
    if ctx.stage not in (Stage.FULL, Stage.SETUP_TOTP):
        return _redirect(STAGE_PATHS[ctx.stage])
    try:
        codes = services.users.confirm_totp(db, ctx.user, code, ip=client_ip(request))
        services.auth.advance(db, ctx)
        db.commit()
    except DomainError as exc:
        db.rollback()
        return _totp_page(request, ctx, services, db, errors=describe(exc), status=400)
    return render(request, "auth/recovery_codes.html.j2", codes=codes)


@router.post("/account/herstelcodes")
def recovery_codes(request: Request, ctx: Teacher, services: Svc, db: Db) -> Response:
    try:
        codes = services.users.regenerate_recovery_codes(db, ctx.user, ip=client_ip(request))
    except DomainError:
        return _redirect("/account")
    return render(request, "auth/recovery_codes.html.j2", codes=codes)


@router.post("/account/tweestaps/uit")
def totp_disable(
    request: Request,
    ctx: Teacher,
    services: Svc,
    db: Db,
    password: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    try:
        services.users.disable_totp(db, ctx.user, password=password, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        return render(
            request,
            "auth/account.html.j2",
            status_code=400,
            ok=None,
            totp_required=services.auth.totp_required(db, ctx.user),
            errors=describe(exc),
        )
    return _redirect("/account?ok=totp_disabled")


# One-time links ------------------------------------------------------------------


def _link_page(
    request: Request, template: str, *, token: str, email: str, errors: list[str], status: int = 200
) -> Response:
    return render(
        request,
        template,
        status_code=status,
        token=token,
        email=email,
        errors=errors,
        min_length=MIN_LENGTH,
    )


def _token_limited(request: Request, services: Svc) -> bool:
    ip = client_ip(request) or "unknown"
    return services.limiter.status("token", ip).blocked


@router.get("/uitnodiging/{token}")
def invite_form(request: Request, token: str, db: Db, services: Svc) -> Response:
    if _token_limited(request, services):
        return render(
            request,
            "message.html.j2",
            status_code=429,
            title=_("Too many attempts. Try again later."),
        )
    try:
        invite = services.users.open_invite(db, token)
    except DomainError as exc:
        services.limiter.fail("token", client_ip(request) or "unknown")
        return render(request, "message.html.j2", status_code=404, title=describe(exc)[0])
    return _link_page(request, "auth/invite.html.j2", token=token, email=invite.email, errors=[])


@router.post("/uitnodiging/{token}")
def invite_accept(
    request: Request,
    token: str,
    db: Db,
    services: Svc,
    name: Annotated[str, Form(max_length=300)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    confirm: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    if _token_limited(request, services):
        return render(
            request,
            "message.html.j2",
            status_code=429,
            title=_("Too many attempts. Try again later."),
        )
    try:
        invite = services.users.open_invite(db, token)
        if password != confirm:
            raise DomainError("password_mismatch")
        services.users.accept_invite(db, token, name=name, password=password, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        if exc.code == "invite_invalid":
            services.limiter.fail("token", client_ip(request) or "unknown")
            return render(request, "message.html.j2", status_code=404, title=describe(exc)[0])
        return _link_page(
            request, "auth/invite.html.j2", token=token, email=invite.email,
            errors=describe(exc), status=400,
        )  # fmt: skip
    return render(
        request,
        "message.html.j2",
        title=_("Your account has been created."),
        body=_("You can now log in. Two-step verification can be enabled under Account."),
        link="/login",
        link_text=_("Log in"),
    )


@router.get("/reset/{token}")
def reset_form(request: Request, token: str, db: Db, services: Svc) -> Response:
    if _token_limited(request, services):
        return render(
            request,
            "message.html.j2",
            status_code=429,
            title=_("Too many attempts. Try again later."),
        )
    try:
        reset = services.users.open_reset(db, token)
    except DomainError as exc:
        services.limiter.fail("token", client_ip(request) or "unknown")
        return render(request, "message.html.j2", status_code=404, title=describe(exc)[0])
    target = db.get(User, reset.user_id)
    return _link_page(
        request, "auth/reset.html.j2", token=token, email=target.email if target else "", errors=[]
    )


@router.post("/reset/{token}")
def reset_submit(
    request: Request,
    token: str,
    db: Db,
    services: Svc,
    password: Annotated[str, Form(max_length=1024)] = "",
    confirm: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    if _token_limited(request, services):
        return render(
            request,
            "message.html.j2",
            status_code=429,
            title=_("Too many attempts. Try again later."),
        )
    try:
        services.users.open_reset(db, token)
        if password != confirm:
            raise DomainError("password_mismatch")
        services.users.use_reset(db, token, password=password, ip=client_ip(request))
    except DomainError as exc:
        db.rollback()
        if exc.code == "reset_invalid":
            services.limiter.fail("token", client_ip(request) or "unknown")
            return render(request, "message.html.j2", status_code=404, title=describe(exc)[0])
        return _link_page(
            request, "auth/reset.html.j2", token=token, email="", errors=describe(exc), status=400
        )
    return render(
        request,
        "message.html.j2",
        title=_("Your password has been changed."),
        link="/login",
        link_text=_("Log in"),
    )
