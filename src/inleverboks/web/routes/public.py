"""Public pages that need no login."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from inleverboks.web.deps import OptionalAuth
from inleverboks.web.templating import render

router = APIRouter()


@router.get("/", include_in_schema=False)
def index(request: Request, ctx: OptionalAuth) -> Response:
    """The start page for students; a signed-in teacher keeps the normal header."""
    response: Response = render(request, "index.html.j2")
    return response
