from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.config import Settings
from inleverboks.db.models import (
    AuditEvent,
    Exam,
    ExtensionGrant,
    ProcesVerbaal,
    Submission,
    SubmissionFile,
    User,
)
from inleverboks.services.container import Services
from tests.conftest import START, create_user, enable_totp, login
from tests.student_helpers import (
    EMPTY_TEMPLATE,
    add_roster,
    form,
    ready,
    slot_ids,
    upload,
)


def events(db: Session, name: str) -> list[AuditEvent]:
    db.expire_all()
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.event == name).order_by(AuditEvent.seq))
    )


def hand_in(app: Any, exam: Exam, number: str, files: list[tuple[str, bytes, str | None]],
            *, first: str = "Piet", infix: str = "", last: str = "Jansen") -> str:  # fmt: skip
    student = TestClient(app)
    ready(student, exam, number, first=first, infix=infix, last=last)
    response = upload(student, exam, files, confirmed=True)
    assert response.status_code == 200, response.text
    html: str = response.json()["html"]
    assert re.search(r"IB-[A-Z2-9]{4}-[A-Z2-9]{4}", html)
    return html


@pytest.fixture
def teacher_client(client: TestClient, owner: User) -> TestClient:
    teacher = TestClient(client.app)
    login(teacher)
    return teacher


@pytest.fixture
def scenario(
    client: TestClient, db: Session, exam: Exam, owner: User, clock: FrozenClock
) -> dict[str, Any]:
    """Two students handed in: Jan (on the list) and Piet (not on the list).
    Zoë is on the list but has not handed in and has an extension."""
    add_roster(db, exam)
    db.add(
        ExtensionGrant(
            exam_id=exam.id,
            student_number="123457",
            ends_at=START + timedelta(hours=3),
            created_by_id=owner.id,
            created_at=START,
        )
    )
    db.commit()
    report, script = slot_ids(exam)
    shared = b"#!/bin/sh\necho gedeeld\n"
    jan = hand_in(
        client.app,
        exam,
        "123456",
        [
            ("verslag.docx", EMPTY_TEMPLATE, report),  # empty template + name mismatch
            ("install.sh", shared, script),
            ("notities.txt", b"notities", None),
            ("Notities.txt", b"andere notities", None),  # same name, different case
        ],
    )
    clock.advance(minutes=5)
    piet = hand_in(
        client.app,
        exam,
        "999999",
        [
            ("Examen_Linux_P3_PJansen.docx", b"PK echt werk", report),
            ("install.sh", shared, script),  # identical to Jan's script
        ],
    )
    return {"jan_html": jan, "piet_html": piet}


# Overview ------------------------------------------------------------------------


def test_overview_counters_rows_and_signals(
    teacher_client: TestClient, exam: Exam, scenario: dict[str, Any]
) -> None:
    page = teacher_client.get(f"/docent/examens/{exam.id}")
    assert page.status_code == 200
    text = page.text
    counters = re.findall(r'<span class="counter-value">(\d+)</span> ([a-z ]+)</li>', text)
    assert dict((label, int(n)) for n, label in counters) == {
        "ingeleverd": 2,
        "nog niet ingeleverd": 1,
        "geannuleerd": 0,
        "ongeldig": 0,
        "niet op lijst": 1,
    }
    for expected in [
        "Vries, Jan de",
        "Jansen, Piet",
        "Ångström, Zoë",
        "Leeg sjabloon",
        "Naam wijkt af",
        "Identiek aan andere student",
        "Niet op lijst",
        "Verlenging",
        "4/2",
        "2/2",
        "Nog niet ingeleverd",
    ]:
        assert expected in text, expected
    assert 'hx-trigger="every 10s"' in text  # live while open
    assert 'id="poll-overview"' in text


def test_overview_filter_sort_and_fragment(
    teacher_client: TestClient, exam: Exam, scenario: dict[str, Any]
) -> None:
    missing = teacher_client.get(f"/docent/examens/{exam.id}?status=missing").text
    assert "Ångström, Zoë" in missing
    assert "Jansen, Piet" not in missing
    unlisted = teacher_client.get(f"/docent/examens/{exam.id}?status=not_on_list").text
    assert "Jansen, Piet" in unlisted
    assert "Vries, Jan de" not in unlisted
    fragment = teacher_client.get(
        f"/docent/examens/{exam.id}", headers={"HX-Trigger": "poll-overview"}
    )
    assert fragment.text.lstrip().startswith("{#") is False
    assert "<html" not in fragment.text
    assert "Vries, Jan de" in fragment.text
    by_time = teacher_client.get(f"/docent/examens/{exam.id}?sort=time&desc=true").text
    assert by_time.index("Jansen, Piet") < by_time.index("Vries, Jan de")


def test_no_polling_when_closed(
    teacher_client: TestClient, exam: Exam, clock: FrozenClock, owner: User
) -> None:
    clock.set(START + timedelta(hours=5))
    login(teacher_client)
    assert 'hx-trigger="every 10s"' not in teacher_client.get(f"/docent/examens/{exam.id}").text


# Permissions ---------------------------------------------------------------------


def test_other_teacher_has_no_access(
    client: TestClient, db: Session, clock: FrozenClock, exam: Exam, scenario: dict[str, Any]
) -> None:
    create_user(db, clock, "ander@school.nl")
    other = TestClient(client.app)
    login(other, "ander@school.nl")
    submission = db.scalar(select(Submission))
    assert submission is not None
    file = submission.files[0]
    for url in [
        f"/docent/examens/{exam.id}",
        f"/docent/examens/{exam.id}/zip",
        f"/docent/examens/{exam.id}/overzicht.csv",
        f"/docent/examens/{exam.id}/bestanden/{file.id}",
        f"/docent/examens/{exam.id}/studenten/123456",
        f"/docent/examens/{exam.id}/auditlog",
    ]:
        response = other.get(url)
        assert response.status_code == 404, url
        assert b"PK" not in response.content


def test_second_reviewer_reads_but_cannot_change(
    client: TestClient, db: Session, clock: FrozenClock, exam: Exam, scenario: dict[str, Any]
) -> None:
    reviewer = create_user(db, clock, "tweede@school.nl")
    exam.second_reviewer_id = reviewer.id
    db.commit()
    rc = TestClient(client.app)
    login(rc, "tweede@school.nl")
    assert rc.get(f"/docent/examens/{exam.id}").status_code == 200
    assert rc.get(f"/docent/examens/{exam.id}/zip").status_code == 200
    assert rc.get(f"/docent/examens/{exam.id}/overzicht.csv").status_code == 200
    submission = db.scalar(select(Submission).where(Submission.student_number == "123456"))
    assert submission is not None
    page = rc.get(f"/docent/examens/{exam.id}/studenten/123456").text
    assert "Inlevering ongedaan maken" not in page
    response = form(
        rc, f"/docent/examens/{exam.id}/inleveringen/{submission.id}/annuleren", {"reason": "x"}
    )
    assert response.status_code == 403


def test_admin_may_download_but_not_cancel(
    client: TestClient,
    db: Session,
    clock: FrozenClock,
    services: Services,
    exam: Exam,
    scenario: dict[str, Any],
) -> None:
    admin = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    ac = TestClient(client.app)
    login(ac, "admin@school.nl", secret=secret, clock=clock)
    submission = db.scalar(select(Submission).where(Submission.student_number == "123456"))
    assert submission is not None
    file = submission.files[1]
    response = ac.get(f"/docent/examens/{exam.id}/bestanden/{file.id}")
    assert response.status_code == 200
    assert response.content == b"#!/bin/sh\necho gedeeld\n"
    response = form(
        ac, f"/docent/examens/{exam.id}/inleveringen/{submission.id}/ongeldig", {"reason": "x"}
    )
    assert response.status_code == 403


# Cancel and invalidate ------------------------------------------------------------


def test_cancel_keeps_files_and_allows_new_attempt(
    client: TestClient,
    teacher_client: TestClient,
    db: Session,
    exam: Exam,
    services: Services,
    scenario: dict[str, Any],
) -> None:
    submission = db.scalar(select(Submission).where(Submission.student_number == "999999"))
    assert submission is not None
    url = f"/docent/examens/{exam.id}/inleveringen/{submission.id}/annuleren"
    assert (
        form(teacher_client, url, {"reason": "  "})
        .headers["location"]
        .endswith("?error=reason_required")
    )
    response = form(teacher_client, url, {"reason": "Student wil verslag vervangen"})
    assert response.headers["location"].endswith("/studenten/999999?ok=cancelled")
    db.expire_all()
    assert submission.status == "cancelled"
    assert submission.status_reason == "Student wil verslag vervangen"
    assert all(f.storage_key for f in submission.files)
    event = events(db, "submission.cancelled")[0]
    assert event.details["reason"] == "Student wil verslag vervangen"
    # A second time is refused: only active hand-ins.
    assert (
        form(teacher_client, url, {"reason": "x"}).headers["location"].endswith("?error=not_active")
    )
    # The student hands in again: attempt 2.
    hand_in(client.app, exam, "999999", [("Examen_Linux_P3_v2.docx", b"PK v2", slot_ids(exam)[0])])
    db.expire_all()
    attempts = sorted(
        s.attempt
        for s in db.scalars(select(Submission).where(Submission.student_number == "999999"))
    )
    assert attempts == [1, 2]
    detail = teacher_client.get(f"/docent/examens/{exam.id}/studenten/999999").text
    assert "Poging 1" in detail and "Poging 2" in detail
    assert "Student wil verslag vervangen" in detail


@pytest.mark.parametrize("may_resubmit", [True, False])
def test_invalidate(
    teacher_client: TestClient,
    client: TestClient,
    db: Session,
    exam: Exam,
    scenario: dict[str, Any],
    may_resubmit: bool,
) -> None:
    submission = db.scalar(select(Submission).where(Submission.student_number == "999999"))
    assert submission is not None
    data = {"reason": "Fraude vermoed"} | ({"may_resubmit": "true"} if may_resubmit else {})
    form(teacher_client, f"/docent/examens/{exam.id}/inleveringen/{submission.id}/ongeldig", data)
    db.expire_all()
    assert submission.status == "invalid"
    assert submission.may_resubmit is may_resubmit
    assert events(db, "submission.invalidated")[0].details["may_resubmit"] is may_resubmit
    student = TestClient(client.app)
    form(student, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    response = form(student, f"/{exam.slug}/student", {"student_number": "999999"})
    assert ("Je hebt al ingeleverd" in response.text) is (not may_resubmit)


# Downloads -----------------------------------------------------------------------


def test_file_download(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any]
) -> None:
    submission = db.scalar(select(Submission).where(Submission.student_number == "123456"))
    assert submission is not None
    file = submission.files[0]
    response = teacher_client.get(f"/docent/examens/{exam.id}/bestanden/{file.id}")
    assert response.content == EMPTY_TEMPLATE
    assert response.headers["content-disposition"].startswith('attachment; filename="verslag.docx"')
    assert response.headers["x-content-type-options"] == "nosniff"
    assert events(db, "download.file")[0].details["file_id"] == str(file.id)


def _zip(response: Any) -> zipfile.ZipFile:
    assert response.status_code == 200
    return zipfile.ZipFile(io.BytesIO(response.content))


def _manifest(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    raw = archive.read("manifest.csv")
    assert raw.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))


def test_zip_structure_and_manifest(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any]
) -> None:
    response = teacher_client.get(f"/docent/examens/{exam.id}/zip")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="linux-p3_2026-11-13.zip"'
    )
    archive = _zip(response)
    names = sorted(archive.namelist())
    assert names == [
        "Jansen, Piet (999999)/Examen_Linux_P3_PJansen.docx",
        "Jansen, Piet (999999)/install.sh",
        "Vries, Jan de (123456)/Notities (2).txt",
        "Vries, Jan de (123456)/install.sh",
        "Vries, Jan de (123456)/notities.txt",
        "Vries, Jan de (123456)/verslag.docx",
        "manifest.csv",
    ]
    assert archive.read("Vries, Jan de (123456)/verslag.docx") == EMPTY_TEMPLATE
    manifest = _manifest(archive)
    assert list(manifest[0]) == [
        "studentnummer",
        "achternaam",
        "tussenvoegsel",
        "voornaam",
        "status",
        "poging",
        "inlevertijd_start",
        "inlevertijd_voltooid",
        "vakje",
        "bestandsnaam_origineel",
        "bestandsnaam_in_zip",
        "grootte_bytes",
        "sha256",
        "integriteit_ok",
        "waarschuwingen",
    ]
    jan = [m for m in manifest if m["studentnummer"] == "123456"]
    assert {m["bestandsnaam_in_zip"] for m in jan} >= {"Vries, Jan de (123456)/Notities (2).txt"}
    assert all(m["integriteit_ok"] == "ja" for m in manifest)
    assert jan[0]["inlevertijd_start"] == "2026-11-13 10:00:00"
    assert "empty_template" in jan[0]["waarschuwingen"]
    assert events(db, "download.zip")
    db.expire_all()
    assert all(f.integrity_ok for f in db.scalars(select(SubmissionFile)))


def test_zip_includes_cancelled_only_on_request(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any]
) -> None:
    submission = db.scalar(select(Submission).where(Submission.student_number == "999999"))
    assert submission is not None
    form(
        teacher_client,
        f"/docent/examens/{exam.id}/inleveringen/{submission.id}/annuleren",
        {"reason": "x"},
    )
    without = _zip(teacher_client.get(f"/docent/examens/{exam.id}/zip"))
    assert not any(n.startswith("_geannuleerd/") for n in without.namelist())
    assert not any("Jansen" in n for n in without.namelist())
    with_cancelled = _zip(teacher_client.get(f"/docent/examens/{exam.id}/zip?geannuleerd=true"))
    assert "_geannuleerd/Jansen, Piet (999999) - poging 1/install.sh" in with_cancelled.namelist()
    assert not any(n.startswith("_ongeldig/") for n in with_cancelled.namelist())
    rows = [m for m in _manifest(with_cancelled) if m["studentnummer"] == "999999"]
    assert {m["status"] for m in rows} == {"geannuleerd"}


def test_zip_integrity_check_detects_tampering(
    teacher_client: TestClient,
    db: Session,
    exam: Exam,
    settings: Settings,
    scenario: dict[str, Any],
) -> None:
    submission = db.scalar(select(Submission).where(Submission.student_number == "999999"))
    assert submission is not None
    tampered = submission.files[0]
    assert tampered.storage_key is not None
    (Path(settings.storage_path) / tampered.storage_key).write_bytes(b"gemanipuleerd")
    archive = _zip(teacher_client.get(f"/docent/examens/{exam.id}/zip"))
    status = {
        m["bestandsnaam_origineel"] + m["studentnummer"]: m["integriteit_ok"]
        for m in _manifest(archive)
    }
    assert status["Examen_Linux_P3_PJansen.docx999999"] == "nee"
    assert status["install.sh999999"] == "ja"
    mismatch = events(db, "integrity.mismatch")
    assert len(mismatch) == 1
    assert mismatch[0].details["expected"] == hashlib.sha256(b"PK echt werk").hexdigest()
    db.expire_all()
    assert db.get(SubmissionFile, tampered.id).integrity_ok is False  # type: ignore[union-attr]


def test_student_zip(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any]
) -> None:
    archive = _zip(teacher_client.get(f"/docent/examens/{exam.id}/studenten/123456/zip"))
    assert (
        sorted(n.split("/")[0] for n in archive.namelist() if "/" in n)
        == ["Vries, Jan de (123456)"] * 4
    )
    assert events(db, "download.student")[0].details["student_number"] == "123456"


# CSV and proces-verbaal ------------------------------------------------------------


def test_overview_csv(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any]
) -> None:
    response = teacher_client.get(f"/docent/examens/{exam.id}/overzicht.csv")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="linux-p3_overzicht_2026-11-13.csv"'
    )
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    by_number = {r["studentnummer"]: r for r in rows}
    assert by_number["123456"]["status"] == "ingeleverd"
    assert by_number["123456"]["aantal_bestanden"] == "4"
    assert by_number["999999"]["op_lijst"] == "nee"
    assert by_number["123457"]["status"] == "nog niet ingeleverd"
    assert by_number["123457"]["verlenging_tot"] == "2026-11-13 13:00:00"
    assert "identical_to_other" in by_number["999999"]["signalen"]
    assert events(db, "export.csv")


def test_csv_neutralizes_formulas_in_names(
    client: TestClient, teacher_client: TestClient, exam: Exam
) -> None:
    hand_in(
        client.app, exam, "555555", [("a.txt", b"a", None)], first="=HYPERLINK(1)", last="@evil"
    )
    rows = list(
        csv.DictReader(
            io.StringIO(
                teacher_client.get(f"/docent/examens/{exam.id}/overzicht.csv").content.decode(
                    "utf-8-sig"
                )
            ),
            delimiter=";",
        )
    )
    row = next(r for r in rows if r["studentnummer"] == "555555")
    assert row["voornaam"] == "'=HYPERLINK(1)"
    assert row["achternaam"] == "'@evil"


@pytest.fixture
def captured_pdf(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def render(template: str, context: dict[str, Any]) -> bytes:
        calls.append((template, context))
        return b"%PDF-1.7 fake"

    monkeypatch.setattr(app.state.pdf, "render", render)
    return calls


def test_proces_verbaal_context_and_record(
    teacher_client: TestClient,
    db: Session,
    exam: Exam,
    services: Services,
    scenario: dict[str, Any],
    captured_pdf: list[tuple[str, dict[str, Any]]],
) -> None:
    submission = db.scalar(select(Submission).where(Submission.student_number == "999999"))
    assert submission is not None
    form(
        teacher_client,
        f"/docent/examens/{exam.id}/inleveringen/{submission.id}/annuleren",
        {"reason": "Verzoek"},
    )
    response = form(teacher_client, f"/docent/examens/{exam.id}/proces-verbaal", {})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="linux-p3_PV-')
    template, context = captured_pdf[0]
    assert template == "pdf/proces_verbaal.html.j2"
    assert context["counts"]["active"] == 1
    assert context["counts"]["cancelled"] == 1
    assert context["integrity"].checked == 6
    assert not context["integrity"].mismatches
    assert {p["kind"] for p in context["particulars"]} == {"cancelled", "extension"}
    pv = db.scalar(select(ProcesVerbaal))
    assert pv is not None
    assert pv.document_id == context["document_id"]
    assert pv.audit_head_hash == context["audit_head_hash"]
    assert not pv.is_final
    event = events(db, "export.pv")[0]
    assert event.prev_hash == pv.audit_head_hash  # the PV is tied to the log at export time
    import anyio  # noqa: PLC0415

    assert anyio.run(services.storage.exists, pv.storage_key)


def test_proces_verbaal_renders_with_weasyprint(
    teacher_client: TestClient, exam: Exam, scenario: dict[str, Any]
) -> None:
    try:
        import weasyprint  # noqa: F401, PLC0415
    except OSError:
        pytest.skip("Pango is not installed locally (runs in CI and Docker)")
    response = form(teacher_client, f"/docent/examens/{exam.id}/proces-verbaal", {})
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


# Receipt verification ------------------------------------------------------------


def _codes(html: str) -> tuple[str, str]:
    codes = re.findall(r'class="mono receipt-code">([^<]+)<', html)
    return codes[0], codes[1]


def test_verify_receipt(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any]
) -> None:
    receipt, verification = _codes(scenario["jan_html"])
    page = form(
        teacher_client,
        "/docent/ontvangstbewijs",
        {"receipt_code": receipt.lower(), "verification_code": verification},
    )
    assert page.status_code == 200
    assert "De codes horen bij een opgeslagen inlevering" in page.text
    assert "Vries, Jan de" in page.text
    wrong = form(
        teacher_client,
        "/docent/ontvangstbewijs",
        {"receipt_code": receipt, "verification_code": "AAAA-AAAA"},
    )
    assert wrong.status_code == 404
    checks = events(db, "receipt.verified")
    assert [e.details["valid"] for e in checks] == [True, False]
    assert checks[0].chain == str(exam.id)


def test_verify_receipt_requires_access(
    client: TestClient, db: Session, clock: FrozenClock, exam: Exam, scenario: dict[str, Any]
) -> None:
    create_user(db, clock, "ander@school.nl")
    other = TestClient(client.app)
    login(other, "ander@school.nl")
    receipt, verification = _codes(scenario["jan_html"])
    response = form(
        other,
        "/docent/ontvangstbewijs",
        {"receipt_code": receipt, "verification_code": verification},
    )
    assert response.status_code == 404
    assert "Vries" not in response.text


# Audit log -----------------------------------------------------------------------


def test_exam_audit_log_view_and_export(
    teacher_client: TestClient, db: Session, exam: Exam, scenario: dict[str, Any], captured_pdf: Any
) -> None:
    page = teacher_client.get(f"/docent/examens/{exam.id}/auditlog?event=submission")
    assert page.status_code == 200
    assert "Hash-keten intact" in page.text
    assert "submission.completed" in page.text
    assert "exam.created" not in page.text
    export = teacher_client.get(f"/docent/examens/{exam.id}/auditlog?format=csv")
    rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0]["gebeurtenis"] == "exam.created"
    assert rows[-1]["gebeurtenis"] == "export.audit"
    assert teacher_client.get(f"/docent/examens/{exam.id}/auditlog?format=pdf").content.startswith(
        b"%PDF"
    )


def test_admin_system_log_export(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    admin = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    login(client, "admin@school.nl", secret=secret, clock=clock)
    export = client.get("/admin/auditlog?format=csv")
    rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig")), delimiter=";"))
    assert any(r["gebeurtenis"] == "login.succeeded" for r in rows)
    assert events(db, "export.audit")[0].details["chain"] == "system"
