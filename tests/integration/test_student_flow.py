from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import (
    DeniedAttemptBucket,
    Exam,
    ExtensionGrant,
    ReceiptToken,
    Submission,
    User,
)
from inleverboks.main import create_app
from inleverboks.services.container import Services
from inleverboks.services.errors import DomainError
from inleverboks.services.exams import ExamInput, SlotInput
from inleverboks.services.student_flow import StudentState
from inleverboks.services.submissions import ReceivedFile
from tests.conftest import START, create_user, csrf_token, make_settings
from tests.student_helpers import (
    EMPTY_TEMPLATE,
    add_roster,
    events,
    form,
    good_files,
    make_exam,
    read_object,
    ready,
    slot_ids,
    stored_keys,
    upload,
)

# Entry and steps -----------------------------------------------------------------


def test_status_messages(
    client: TestClient, db: Session, services: Services, owner: User, clock: FrozenClock
) -> None:
    concept = services.exams.create(
        db, owner,
        ExamInput(name="Concept", description=None, starts_at=START, ends_at=START + timedelta(hours=1),
                  slug="concept-x", extensions=["pdf"], slots=[SlotInput(label="A")]),
        ip=None,
    )  # fmt: skip
    assert "nog niet opengezet" in client.get(f"/{concept.slug}").text
    exam = make_exam(db, services, owner, slug="later")
    clock.set(START - timedelta(minutes=30))
    assert "opent om 10:00" in client.get(f"/{exam.slug}").text
    clock.set(START + timedelta(hours=3))
    assert "Gesloten" in client.get(f"/{exam.slug}").text


def test_code_step(client: TestClient, db: Session, exam: Exam) -> None:
    page = client.get(f"/{exam.slug}")
    assert page.headers["cache-control"] == "no-store"
    assert 'name="code"' in page.text
    assert exam.access_code not in page.text  # type: ignore[operator]
    wrong = form(client, f"/{exam.slug}/code", {"code": "AAAAAA"})
    assert wrong.status_code == 400
    assert "toegangscode klopt niet" in wrong.text
    # The student number field is not reachable without the code.
    assert form(client, f"/{exam.slug}/student", {"student_number": "123456"}).status_code == 303
    assert 'name="code"' in client.get(f"/{exam.slug}").text
    ok = form(client, f"/{exam.slug}/code", {"code": (exam.access_code or "").lower()})
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"]
    assert f"Path=/{exam.slug}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert 'name="student_number"' in client.get(f"/{exam.slug}").text


def test_access_code_rate_limit(client: TestClient, exam: Exam) -> None:
    codes = [form(client, f"/{exam.slug}/code", {"code": "AAAAAA"}).status_code for _ in range(16)]
    assert codes[-1] == 429
    assert form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""}).status_code == 429


def test_denied_attempts_are_aggregated_per_minute(
    client: TestClient, db: Session, exam: Exam, services: Services, clock: FrozenClock
) -> None:
    for _ in range(3):
        form(client, f"/{exam.slug}/code", {"code": "AAAAAA"})
    assert len(events(db, "student.denied")) == 1
    bucket = db.scalar(select(DeniedAttemptBucket))
    assert bucket is not None
    assert bucket.count == 3
    clock.advance(minutes=1)
    form(client, f"/{exam.slug}/code", {"code": "AAAAAA"})
    assert len(events(db, "student.denied")) == 2
    services.flow.flush_denied(db, before=clock.now())
    summary = events(db, "student.denied_summary")
    assert len(summary) == 1
    assert summary[0].details["count"] == 3


def test_roster_confirmation(client: TestClient, db: Session, exam: Exam) -> None:
    add_roster(db, exam)
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    form(client, f"/{exam.slug}/student", {"student_number": "123456"})
    page = client.get(f"/{exam.slug}").text
    assert "Jan de V." in page
    assert "Vries" not in page  # only the abbreviated name is shown
    no = form(client, f"/{exam.slug}/bevestigen", {"answer": "no"})
    assert "Controleer je studentnummer of meld je bij de docent" in no.text
    form(client, f"/{exam.slug}/student", {"student_number": "123457"})
    assert "Zoë Å." in client.get(f"/{exam.slug}").text
    form(client, f"/{exam.slug}/bevestigen", {"answer": "yes"})
    assert "Zoë Ångström" in client.get(f"/{exam.slug}").text


def test_not_on_list(client: TestClient, db: Session, exam: Exam) -> None:
    add_roster(db, exam)
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    form(client, f"/{exam.slug}/student", {"student_number": "999999"})
    assert 'name="first_name"' in client.get(f"/{exam.slug}").text  # allowed by default

    exam.allow_unlisted = False
    db.commit()
    other = TestClient(client.app)
    form(other, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    response = form(other, f"/{exam.slug}/student", {"student_number": "999999"})
    assert response.status_code == 400
    assert "Controleer je studentnummer" in response.text


def test_invalid_student_number(client: TestClient, exam: Exam) -> None:
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    response = form(client, f"/{exam.slug}/student", {"student_number": "12"})
    assert response.status_code == 400
    assert "studentnummer is niet geldig" in response.text


def test_without_access_code(
    client: TestClient, db: Session, services: Services, owner: User
) -> None:
    exam = make_exam(db, services, owner, slug="zonder-code", access_code_enabled=False)
    assert 'name="student_number"' in client.get(f"/{exam.slug}").text


# IP whitelist behind a proxy -----------------------------------------------------


def _proxy_app(tmp_path: Path, database_url: str, clock: FrozenClock, trusted: str) -> FastAPI:
    settings = make_settings(tmp_path, database_url=database_url, trusted_proxies=trusted)
    app = create_app(settings, clock=clock)
    app.state.services.sleep = lambda _s: None
    return app


@pytest.mark.parametrize(
    ("trusted", "forwarded", "allowed"),
    [
        ("10.42.0.0/16", "192.168.5.20", True),  # real client IP from the trusted ingress
        ("10.42.0.0/16", "203.0.113.9", False),
        ("", "192.168.5.20", False),  # header ignored without TRUSTED_PROXIES
    ],
)
def test_ip_whitelist_behind_proxy(
    tmp_path: Path,
    database_url: str,
    clock: FrozenClock,
    trusted: str,
    forwarded: str,
    allowed: bool,
) -> None:
    app = _proxy_app(tmp_path, database_url, clock, trusted)
    services: Services = app.state.services
    with app.state.sessionmaker() as db:
        owner = create_user(db, clock, "docent@school.nl")
        exam = make_exam(
            db, services, owner, ip_restriction_enabled=True, ip_cidrs=["192.168.5.0/24"]
        )
        slug = exam.slug
    with TestClient(app, client=("10.42.0.7", 5000)) as client:
        page = client.get(f"/{slug}", headers={"x-forwarded-for": forwarded})
    if allowed:
        assert 'name="code"' in page.text
    else:
        assert "niet inleveren vanaf dit netwerk" in page.text
        with app.state.sessionmaker() as db:
            assert db.scalar(select(DeniedAttemptBucket.kind)) == "ip"


# Upload --------------------------------------------------------------------------


def test_successful_upload(client: TestClient, db: Session, exam: Exam, services: Services) -> None:
    ready(client, exam)
    files = good_files(exam)
    response = upload(client, exam, files)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert response.headers["cache-control"] == "no-store"
    assert "ib_student=" in response.headers["set-cookie"]  # cleared
    html = body["html"]
    code = re.search(r"IB-[A-Z2-9]{4}-[A-Z2-9]{4}", html)
    assert code
    assert "Bewaar dit ontvangstbewijs" in html
    assert hashlib.sha256(files[0][1]).hexdigest()[:8] in html

    submission = db.scalar(select(Submission))
    assert submission is not None
    assert submission.receipt_code == code.group(0)
    assert submission.status == "active"
    assert submission.attempt == 1
    assert submission.started_at == START
    assert submission.on_roster is False
    assert [f.original_name for f in submission.files] == [files[0][0], files[1][0]]
    for stored, (_, content, _slot) in zip(submission.files, files, strict=True):
        assert stored.storage_key is not None
        assert stored.storage_key.startswith(f"{exam.id}/{submission.id}/")
        assert read_object(services, stored.storage_key) == content
    assert not stored_keys(services, "tmp")
    db.refresh(exam)
    assert exam.requirements_locked_at is not None
    completed = events(db, "submission.completed")[0]
    assert completed.actor_label == "student 123456"
    assert completed.chain == str(exam.id)
    assert len(events(db, "submission.started")) == 1
    # The flow is reset: the page starts at the access code again.
    assert 'name="code"' in client.get(f"/{exam.slug}").text


def test_second_hand_in_is_refused(client: TestClient, exam: Exam) -> None:
    ready(client, exam)
    assert upload(client, exam, good_files(exam)).status_code == 200
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    response = form(client, f"/{exam.slug}/student", {"student_number": "123456"})
    assert response.status_code == 409
    assert "Je hebt al ingeleverd. Meld je bij de docent." in response.text


def test_hash_mismatch_rejects_everything(
    client: TestClient, db: Session, exam: Exam, services: Services
) -> None:
    ready(client, exam)
    files = good_files(exam)
    manifest = [
        {"slot_id": slot, "size": len(content), "sha256": "0" * 64 if i == 1 else hashlib.sha256(content).hexdigest()}
        for i, (_, content, slot) in enumerate(files)
    ]  # fmt: skip
    response = upload(client, exam, files, manifest=manifest)
    assert response.status_code == 400
    assert response.json()["message"] == "Upload mislukt, probeer het opnieuw."
    assert db.scalar(select(Submission)) is None
    assert stored_keys(services, str(exam.id)) == []
    assert stored_keys(services, "tmp") == []
    assert events(db, "submission.failed")[0].details["reason"] == "hash_mismatch"


def test_aborted_storage_leaves_nothing(
    client: TestClient, db: Session, exam: Exam, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready(client, exam)
    original = services.storage.put
    calls = {"n": 0}

    async def flaky_put(key: str, chunks: AsyncIterator[bytes]) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return await original(key, chunks)

    monkeypatch.setattr(services.storage, "put", flaky_put)
    response = upload(client, exam, good_files(exam))
    assert response.status_code == 500
    assert db.scalar(select(Submission)) is None
    assert stored_keys(services, "tmp") == []
    assert stored_keys(services, str(exam.id)) == []
    assert events(db, "submission.failed")[0].details["reason"] == "storage"


def test_idempotent_retry_returns_same_receipt(client: TestClient, db: Session, exam: Exam) -> None:
    ready(client, exam)
    cookie = client.cookies.get("ib_student", path=f"/{exam.slug}")
    key = uuid.uuid4().hex
    first = upload(client, exam, good_files(exam), key=key)
    assert first.status_code == 200
    # The response was lost; the browser retries with the same key and the old cookie.
    client.cookies.set("ib_student", cookie or "", path=f"/{exam.slug}")
    second = upload(client, exam, good_files(exam), key=key)
    assert second.status_code == 200
    code1 = re.search(r"IB-[A-Z2-9]{4}-[A-Z2-9]{4}", first.json()["html"])
    code2 = re.search(r"IB-[A-Z2-9]{4}-[A-Z2-9]{4}", second.json()["html"])
    assert code1 and code2 and code1.group(0) == code2.group(0)
    assert len(db.scalars(select(Submission)).all()) == 1


def test_concurrent_attempts_leave_exactly_one_active(
    db: Session, exam: Exam, services: Services, tmp_path: Path
) -> None:
    """Two devices pass all checks before either commits; the database decides."""
    state = StudentState(exam_id=str(exam.id), stage="ready", student_number="123456",
                         first_name="Jan", last_name="Vries")  # fmt: skip
    content = b"inhoud"
    path = tmp_path / "f"
    path.write_bytes(content)
    received = [ReceivedFile("a.txt", len(content), hashlib.sha256(content).hexdigest(), path)]
    manifest = json.dumps([{"slot_id": None, "size": len(content), "sha256": received[0].sha256}])

    def plan(key: str) -> Any:
        return services.submissions.prepare(
            db, exam, state, manifest_json=manifest, received=received, idempotency_key=key,
            declaration=True, warnings_confirmed=True, started_at=START, ip=None,
        )  # fmt: skip

    first, second = plan("device-a"), plan("device-b")
    services.submissions.commit(db, exam, first)
    with pytest.raises(DomainError, match="already_submitted"):
        services.submissions.commit(db, exam, second)
    db.expire_all()
    active = db.scalars(select(Submission).where(Submission.status == "active")).all()
    assert len(active) == 1


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ([("groot.txt", b"x" * (1024 * 1024 + 1), None)], "file_too_large"),
        ([("virus.exe", b"MZ", None)], "extension_not_allowed"),
        ([(f"f{i}.txt", b"x", None) for i in range(21)], "too_many_files"),
    ],
)
def test_server_enforces_limits(
    client: TestClient, exam: Exam, files: list[tuple[str, bytes, str | None]], error: str
) -> None:
    ready(client, exam)
    response = upload(client, exam, files, confirmed=True)
    assert response.status_code == 400
    assert response.json()["error"] == error


def test_no_files(client: TestClient, exam: Exam) -> None:
    ready(client, exam)
    boundary = "----inleverboks"
    fields = {
        "manifest": "[]", "idempotency_key": uuid.uuid4().hex,
        "declaration": "accepted", "warnings_confirmed": "true",
    }  # fmt: skip
    body = (
        "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            for k, v in fields.items()
        )
        + f"--{boundary}--\r\n"
    )
    response = client.post(
        f"/{exam.slug}/inleveren",
        content=body.encode(),
        headers={
            "content-type": f"multipart/form-data; boundary={boundary}",
            "x-csrf-token": csrf_token(client),
        },
    )
    assert response.json()["error"] == "no_files"


def test_submission_size_limit(client: TestClient, exam: Exam) -> None:
    ready(client, exam)
    files = [(f"deel{i}.txt", b"x" * (900 * 1024), None) for i in range(3)]
    response = upload(client, exam, files, confirmed=True)
    assert response.json()["error"] == "submission_too_large"


def test_slot_rules_are_enforced(client: TestClient, exam: Exam) -> None:
    ready(client, exam)
    report, _script = slot_ids(exam)
    files = [("Examen_Linux_P3_a.docx", b"1", report), ("Examen_Linux_P3_b.docx", b"2", report)]
    assert upload(client, exam, files, confirmed=True).json()["error"] == "slot_taken"
    files = [("verslag.pdf", b"1", report)]
    assert upload(client, exam, files, confirmed=True).json()["error"] == "extension_not_allowed"


def test_warnings_need_confirmation_and_are_stored(
    client: TestClient, db: Session, exam: Exam
) -> None:
    ready(client, exam)
    report, script = slot_ids(exam)
    files = [
        ("verslag.docx", EMPTY_TEMPLATE, report),  # empty template + name mismatch
        ("~$verslag.tmp", b"lock", None),  # temp file
        ("leeg.txt", b"", None),  # empty
        ("install.sh", b"same", script),
        ("kopie.txt", b"same", None),  # duplicate content
    ]
    response = upload(client, exam, files, confirmed=False)
    assert response.json()["error"] == "warnings_not_confirmed"
    response = upload(client, exam, files, confirmed=True)
    assert response.status_code == 200, response.text
    submission = db.scalar(select(Submission))
    assert submission is not None
    by_name = {f.original_name: f.warnings for f in submission.files}
    assert by_name["verslag.docx"] == ["empty_template", "name_mismatch"]
    assert by_name["~$verslag.tmp"] == ["temp_file"]
    assert by_name["leeg.txt"] == ["empty_file"]
    assert by_name["install.sh"] == ["duplicate_file"]
    assert by_name["kopie.txt"] == ["duplicate_file"]
    codes = [w["code"] for w in submission.confirmed_warnings]
    assert "count_mismatch" in codes
    assert "empty_template" in codes
    assert "Identiek aan het lege sjabloon" in response.json()["html"]


def test_declaration_is_required(client: TestClient, db: Session, exam: Exam) -> None:
    ready(client, exam)
    response = upload(client, exam, good_files(exam), declaration=False)
    assert response.json()["error"] == "declaration_required"
    assert db.scalar(select(Submission)) is None


def test_upload_requires_csrf_and_session(client: TestClient, exam: Exam) -> None:
    ready(client, exam)
    assert upload(client, exam, good_files(exam), csrf=False).status_code == 403
    fresh = TestClient(client.app)
    response = upload(fresh, exam, good_files(exam))
    assert response.status_code == 409
    assert response.json()["error"] == "session"


def test_file_names_are_sanitized(client: TestClient, db: Session, exam: Exam) -> None:
    ready(client, exam)
    nfd = unicodedata.normalize("NFD", "Café.txt")
    response = upload(client, exam, [(f"C:\\temp\\{nfd}", b"x", None)], confirmed=True)
    assert response.status_code == 200, response.text
    stored = db.scalar(select(Submission))
    assert stored is not None
    assert stored.files[0].original_name == "Café.txt"


# Time window ---------------------------------------------------------------------


def test_upload_started_before_end_is_accepted(
    db: Session, exam: Exam, services: Services, clock: FrozenClock, tmp_path: Path
) -> None:
    """The moment of handing in is the server time at the start of the request."""
    clock.set(START + timedelta(hours=2, seconds=5))  # finished after the end
    state = StudentState(exam_id=str(exam.id), stage="ready", student_number="123456",
                         first_name="Jan", last_name="Vries")  # fmt: skip
    path = tmp_path / "f"
    path.write_bytes(b"x")
    received = [ReceivedFile("a.txt", 1, hashlib.sha256(b"x").hexdigest(), path)]
    manifest = json.dumps([{"slot_id": None, "size": 1, "sha256": received[0].sha256}])
    kwargs: dict[str, Any] = {
        "manifest_json": manifest, "received": received, "declaration": True,
        "warnings_confirmed": True, "ip": None,
    }  # fmt: skip
    started = START + timedelta(hours=2) - timedelta(seconds=1)
    plan = services.submissions.prepare(
        db, exam, state, idempotency_key="k1", started_at=started, **kwargs
    )
    receipt = services.submissions.commit(db, exam, plan)
    assert receipt.submission.started_at == started
    assert receipt.submission.completed_at == clock.now()
    with pytest.raises(DomainError, match="closed"):
        services.submissions.prepare(
            db, exam, state, idempotency_key="k2", started_at=START + timedelta(hours=2), **kwargs
        )


def test_extension_lets_one_student_hand_in_later(
    client: TestClient, db: Session, exam: Exam, owner: User, clock: FrozenClock
) -> None:
    db.add(ExtensionGrant(exam_id=exam.id, student_number="123457", ends_at=START + timedelta(hours=3),
                          created_by_id=owner.id, created_at=START))  # fmt: skip
    db.commit()
    clock.set(START + timedelta(hours=2, minutes=10))
    # Regular students: closed after entering the student number.
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    response = form(client, f"/{exam.slug}/student", {"student_number": "123456"})
    assert "Gesloten" in response.text
    other = TestClient(client.app)
    ready(other, exam, "123457", first="Zoë", infix="", last="Ångström")
    assert upload(other, exam, good_files(exam)).status_code == 200


# Resubmission rules --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "may_resubmit", "allowed"),
    [("cancelled", False, True), ("invalid", True, True), ("invalid", False, False)],
)
def test_resubmission_rules(
    client: TestClient, db: Session, exam: Exam, status: str, may_resubmit: bool, allowed: bool
) -> None:
    ready(client, exam)
    assert upload(client, exam, good_files(exam)).status_code == 200
    first = db.scalar(select(Submission))
    assert first is not None
    first.status = status
    first.may_resubmit = may_resubmit
    db.commit()
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    response = form(client, f"/{exam.slug}/student", {"student_number": "123456"})
    if not allowed:
        assert "Je hebt al ingeleverd" in response.text
        return
    ready_after = form(
        client, f"/{exam.slug}/naam", {"first_name": "Jan", "infix": "de", "last_name": "Vries"}
    )
    assert ready_after.status_code == 303
    assert upload(client, exam, good_files(exam)).status_code == 200
    db.expire_all()
    attempts = sorted(s.attempt for s in db.scalars(select(Submission)))
    assert attempts == [1, 2]
    # The files of the earlier attempt are kept.
    assert all(f.storage_key for s in db.scalars(select(Submission)) for f in s.files)


# Students can never read handed-in files -----------------------------------------


def test_no_student_route_returns_files(
    client: TestClient, db: Session, exam: Exam, services: Services
) -> None:
    ready(client, exam)
    assert upload(client, exam, good_files(exam)).status_code == 200
    submission = db.scalar(select(Submission))
    assert submission is not None
    key = submission.files[0].storage_key
    content = good_files(exam)[0][1]
    candidates = [
        f"/{exam.slug}/{key}",
        f"/{exam.slug}/bestanden",
        f"/{exam.slug}/inleveren",
        f"/static/../files/{key}",
        f"/static/%2e%2e/files/{key}",
        f"/files/{key}",
        f"/docent/examens/{exam.id}",
    ]
    for url in candidates:
        response = client.get(url, follow_redirects=False)
        assert content not in response.content, url
    # Structural check: the student router only has these GET endpoints.
    from inleverboks.web.routes import student as student_routes  # noqa: PLC0415

    student_gets = sorted(
        route.path  # type: ignore[attr-defined]
        for route in student_routes.router.routes
        if "GET" in getattr(route, "methods", set())
    )
    assert student_gets == ["/{slug}", "/{slug}/opnieuw"]


# Receipt PDF ---------------------------------------------------------------------


@pytest.fixture
def fake_pdf(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []

    def render(template: str, context: dict[str, Any]) -> bytes:
        rendered.append(context)
        return b"%PDF-1.7 fake"

    monkeypatch.setattr(app.state.pdf, "render", render)
    return rendered


def _receipt_form(html: str) -> dict[str, str]:
    return {
        "token": re.search(r'name="token" value="([^"]+)"', html).group(1),  # type: ignore[union-attr]
        "verification_code": re.search(r'name="verification_code" value="([^"]*)"', html).group(1),  # type: ignore[union-attr]
    }


def test_receipt_pdf_token_is_single_use(
    client: TestClient, exam: Exam, fake_pdf: list[dict[str, Any]]
) -> None:
    ready(client, exam)
    html = upload(client, exam, good_files(exam)).json()["html"]
    data = _receipt_form(html)
    response = form(client, "/ontvangstbewijs", data)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="ontvangstbewijs-IB-'
    )
    assert fake_pdf[0]["verification_code"] == data["verification_code"]
    assert len(fake_pdf[0]["files"]) == 2
    assert form(client, "/ontvangstbewijs", data).status_code == 404


def test_receipt_pdf_token_expires(
    client: TestClient, db: Session, exam: Exam, clock: FrozenClock, fake_pdf: list[dict[str, Any]]
) -> None:
    ready(client, exam)
    data = _receipt_form(upload(client, exam, good_files(exam)).json()["html"])
    assert db.scalar(select(ReceiptToken)) is not None
    clock.advance(minutes=15)
    assert form(client, "/ontvangstbewijs", data).status_code == 404


def test_receipt_pdf_hides_wrong_verification_code(
    client: TestClient, exam: Exam, fake_pdf: list[dict[str, Any]]
) -> None:
    ready(client, exam)
    data = _receipt_form(upload(client, exam, good_files(exam)).json()["html"])
    form(client, "/ontvangstbewijs", {**data, "verification_code": "AAAA-AAAA"})
    assert fake_pdf[0]["verification_code"] is None


def test_receipt_pdf_renders_with_weasyprint(client: TestClient, exam: Exam) -> None:
    try:
        import weasyprint  # noqa: F401, PLC0415
    except OSError:
        pytest.skip("Pango is not installed locally (runs in CI and Docker)")
    ready(client, exam)
    data = _receipt_form(upload(client, exam, good_files(exam)).json()["html"])
    response = form(client, "/ontvangstbewijs", data)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_now_is_frozen(clock: FrozenClock) -> None:
    assert clock.now() == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
