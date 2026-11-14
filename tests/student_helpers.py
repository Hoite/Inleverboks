"""Shared helpers for tests that walk through the student flow."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import anyio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.db.models import AuditEvent, Exam, ExamRosterEntry, User
from inleverboks.services.container import Services
from inleverboks.services.exams import ExamInput, SlotInput
from tests.conftest import START, csrf_token

# The exam is open from START (09:00 UTC) until 11:00 UTC.
EMPTY_TEMPLATE = b"PK leeg sjabloon"


def make_exam(db: Session, services: Services, owner: User, **overrides: Any) -> Exam:
    data = ExamInput(
        name="Examen Linux P3",
        description=None,
        starts_at=START,
        ends_at=START + timedelta(hours=2),
        slug=overrides.pop("slug", "linux-p3"),
        extensions=["docx", "pdf", "sh", "txt", "zip", "tmp"],
        max_file_mb=1,
        max_submission_mb=2,
        slots=[
            SlotInput(label="Verslag", pattern="Examen_Linux_P3_*.docx", extensions=["docx"]),
            SlotInput(label="Script", pattern="*.sh"),
        ],
    )
    for key, value in overrides.items():
        setattr(data, key, value)
    exam = services.exams.create(db, owner, data, ip=None)
    services.exams.publish(db, owner, exam, ip=None)
    return exam


def add_roster(db: Session, exam: Exam) -> None:
    db.add_all(
        [
            ExamRosterEntry(exam_id=exam.id, student_number="123456", first_name="Jan", infix="de", last_name="Vries"),
            ExamRosterEntry(exam_id=exam.id, student_number="123457", first_name="Zoë", last_name="Ångström"),
        ]
    )  # fmt: skip
    db.commit()


def form(client: TestClient, url: str, data: dict[str, str]) -> Any:
    return client.post(url, data={"csrf_token": csrf_token(client), **data}, follow_redirects=False)


def ready(client: TestClient, exam: Exam, number: str = "123456", **names: str) -> None:
    """Walk through code, student number and name/confirmation."""
    assert form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""}).status_code == 303
    assert form(client, f"/{exam.slug}/student", {"student_number": number}).status_code == 303
    page = client.get(f"/{exam.slug}").text
    if 'action="/' + exam.slug + '/bevestigen"' in page:
        form(client, f"/{exam.slug}/bevestigen", {"answer": "yes"})
    else:
        form(
            client,
            f"/{exam.slug}/naam",
            {"first_name": names.get("first", "Jan"), "infix": names.get("infix", "de"),
             "last_name": names.get("last", "Vries")},
        )  # fmt: skip
    assert 'id="student-app"' in client.get(f"/{exam.slug}").text


def slot_ids(exam: Exam) -> list[str]:
    return [str(s.id) for s in exam.expected_files]


def upload(
    client: TestClient,
    exam: Exam,
    files: Sequence[tuple[str, bytes, str | None]],
    *,
    key: str | None = None,
    confirmed: bool = False,
    declaration: bool = True,
    manifest: list[dict[str, Any]] | None = None,
    csrf: bool = True,
) -> Any:
    manifest = manifest or [
        {"slot_id": slot, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for _, content, slot in files
    ]
    data = {
        "manifest": json.dumps(manifest),
        "idempotency_key": key or uuid.uuid4().hex,
        "declaration": "accepted" if declaration else "",
        "warnings_confirmed": "true" if confirmed else "false",
    }
    headers = {"x-csrf-token": csrf_token(client)} if csrf else {}
    return client.post(
        f"/{exam.slug}/inleveren",
        data=data,
        files=[
            ("files", (name, content, "application/octet-stream")) for name, content, _ in files
        ],
        headers=headers,
    )


def good_files(exam: Exam) -> list[tuple[str, bytes, str | None]]:
    report, script = slot_ids(exam)
    return [
        ("Examen_Linux_P3_JdVries.docx", b"PK echt verslag", report),
        ("install.sh", b"#!/bin/sh\necho hallo\n", script),
    ]


def events(db: Session, name: str) -> list[AuditEvent]:
    db.expire_all()
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.event == name).order_by(AuditEvent.seq))
    )


def stored_keys(services: Services, prefix: str) -> list[str]:
    async def collect() -> list[str]:
        return [k async for k in services.storage.list_keys(prefix)]

    return anyio.run(collect)


def read_object(services: Services, key: str) -> bytes:
    async def collect() -> bytes:
        return b"".join([c async for c in services.storage.open(key)])

    return anyio.run(collect)
