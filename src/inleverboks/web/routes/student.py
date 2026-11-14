"""Public hand-in page at ``/{slug}``. Phase 3: the status page; the hand-in flow
itself (code, student number, files, upload, receipt) follows in phase 4."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from inleverboks.services.errors import NotFoundError
from inleverboks.services.exams import ExamStatus
from inleverboks.services.slugs import SLUG_RE
from inleverboks.web.deps import Db, Svc
from inleverboks.web.templating import render

router = APIRouter()


@router.get("/{slug}", include_in_schema=False)
def exam_page(request: Request, slug: str, db: Db, services: Svc) -> Response:
    if not SLUG_RE.fullmatch(slug):
        raise NotFoundError()
    exam = services.exams.by_slug(db, slug)
    if exam is None:
        raise NotFoundError()
    now = services.clock.now()
    status = services.exams.status(db, exam, now)
    response = render(
        request,
        "student/status.html.j2",
        exam=exam,
        status=status.value,
        now=now,
        end=services.exams.effective_end(db, exam) if status is ExamStatus.OPEN else exam.ends_at,
        remaining=(exam.ends_at - now) if status is ExamStatus.OPEN else None,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
