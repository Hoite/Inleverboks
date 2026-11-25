"""Permanent redirects for the old teacher URLs under ``/docent`` (renamed to ``/examens``).

Bookmarks, QR codes on printed instructions and links in e-mails keep working. GET and
HEAD get a 301; other methods a 308, so the browser repeats the same method and body.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


def new_path(rest: str) -> str:
    """``examens/{id}/x`` → ``/examens/{id}/x``; ``ontvangstbewijs`` → the admin page;
    everything else (``sjablonen``, ``klassenlijsten``, ``gedeeld``, …) → ``/examens/…``."""
    rest = rest.strip("/")
    if rest == "ontvangstbewijs":
        return "/admin/ontvangstbewijs"
    if rest == "examens" or rest.startswith("examens/"):
        rest = rest.removeprefix("examens").lstrip("/")
    return f"/examens/{rest}" if rest else "/examens"


def _redirect(request: Request, rest: str) -> RedirectResponse:
    target = new_path(rest)
    if request.url.query:
        target += "?" + request.url.query
    status = 301 if request.method in {"GET", "HEAD"} else 308
    return RedirectResponse(target, status_code=status)


@router.api_route("/docent", methods=METHODS, include_in_schema=False)
def legacy_root(request: Request) -> RedirectResponse:
    return _redirect(request, "")


@router.api_route("/docent/{rest:path}", methods=METHODS, include_in_schema=False)
def legacy_path(request: Request, rest: str) -> RedirectResponse:
    return _redirect(request, rest)
