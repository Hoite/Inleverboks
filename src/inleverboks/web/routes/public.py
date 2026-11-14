"""Public pages that need no login."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from inleverboks.web.templating import render

router = APIRouter()


@router.get("/", include_in_schema=False)
def index(request: Request) -> Response:
    response: Response = render(request, "index.html.j2")
    return response
