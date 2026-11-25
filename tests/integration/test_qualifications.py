"""Templates from the SBB qualification structure, part exams, codes in exports, the
overview per work process, resits, the replaced-dossier warning and the SBB import.

Most tests feed the importer with records (what the XML parser produces); the parser
itself is covered in ``tests/unit/test_sbb.py``.
"""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from inleverboks.adapters.clock import FrozenClock
from inleverboks.config import Settings, get_settings
from inleverboks.db.models import (
    AuditEvent,
    Exam,
    ExamRosterEntry,
    ExamTemplate,
    Qualification,
    User,
)
from inleverboks.services import sbb
from inleverboks.services.audit import Actor
from inleverboks.services.container import Services
from inleverboks.services.qualifications import (
    CoreTaskRecord,
    QualificationRecord,
    WorkProcessRecord,
)
from tests.conftest import TEST_SECRET, create_user, csrf_token, enable_totp, login, post
from tests.sbb_sample import sample_xml
from tests.student_helpers import form, ready, upload

DASH = chr(0x2013)  # en dash, as in suggested exam names


def record(
    version: str = "2024", w3_title: str = "Test een netwerk", **kw: Any
) -> QualificationRecord:
    return QualificationRecord(
        crebo="27016", name="ICT System Engineer", level=4, dossier_crebo="23271",
        dossier_name="ICT Systems and Devices", dossier_version=version,
        valid_from=kw.get("valid_from", date(2024, 8, 1)), valid_until=kw.get("valid_until"),
        core_tasks=[
            CoreTaskRecord("B1-K1", "Richt systemen in", [
                WorkProcessRecord("B1-K1-W1", "Inventariseert wensen"),
                WorkProcessRecord("B1-K1-W2", "Installeert systemen"),
                WorkProcessRecord("B1-K1-W3", w3_title),
            ]),
            CoreTaskRecord("B1-K2", "Beheert systemen", [WorkProcessRecord("B1-K2-W1", "Lost storingen op")]),
            CoreTaskRecord("P1-K1", "Ontwerpt infrastructuur", [WorkProcessRecord("P1-K1-W1", "Maakt een ontwerp")]),
        ],
    )  # fmt: skip


@pytest.fixture
def qualification(db: Session, services: Services) -> Qualification:
    services.qualifications.import_records(
        db, [record()], actor=Actor.system(), ip=None, source="test"
    )
    found: Qualification | None = db.scalar(select(Qualification))
    assert found is not None
    return found


@pytest.fixture
def teacher(client: TestClient, owner: User) -> TestClient:
    login(client)
    return client


@pytest.fixture
def admin(client: TestClient, db: Session, clock: FrozenClock, services: Services) -> TestClient:
    user = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, user)
    other = TestClient(client.app)
    login(other, "admin@school.nl", secret=secret, clock=clock)
    return other


def make_templates(client: TestClient, q: Qualification, *codes: str, shared: bool = False) -> Any:
    data: dict[str, Any] = {"core_task": list(codes)}
    if shared:
        data["shared"] = "true"
    return post(client, f"/examens/sjablonen/crebo/{q.id}", data)


def template_named(db: Session, code: str) -> ExamTemplate:
    db.expire_all()
    found = db.scalar(select(ExamTemplate).where(ExamTemplate.core_task_code == code))
    assert found is not None
    return found


def exam_form(**fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "description": "", "starts_at_date": "2026-11-13", "starts_at_time": "10:00",
        "ends_at_date": "2026-11-13", "ends_at_time": "12:00", "second_reviewer_id": "",
        "access_code_enabled": "true", "allow_extra_file": "", "extensions": "docx, pdf, zip",
        "max_file_mb": "10", "max_submission_mb": "50", "ip_cidrs": "", "ip_preset_id": "",
        "class_list_id": "",
    }  # fmt: skip
    data.update(fields)
    return data


def create_exam_from(
    client: TestClient, db: Session, template: ExamTemplate, wps: list[str]
) -> Exam:
    """Post the new-exam form the way the browser does after choosing the template."""
    slots = [s for s in template.slots if not wps or s.work_process_code in wps]
    fields: dict[str, Any] = {
        "name": "x", "slug": "", "source_template_id": str(template.id), "slot_count": str(len(slots)),
    }  # fmt: skip
    for i, slot in enumerate(slots):
        fields |= {f"slot_label_{i}": slot.label, f"slot_pattern_{i}": "", f"slot_extensions_{i}": "",
                   f"slot_id_{i}": str(slot.id), f"slot_wp_{i}": slot.work_process_code or ""}  # fmt: skip
    response = post(client, "/examens/nieuw", exam_form(**fields))
    assert response.status_code == 303, response.text
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None
    return exam


# Search and templates --------------------------------------------------------------


def test_search_by_crebo_or_name(teacher: TestClient, qualification: Qualification) -> None:
    for query in ("27016", "system eng", "ICT SYSTEM"):
        page = teacher.get("/examens/sjablonen/crebo", params={"q": query}).text
        assert f'href="/examens/sjablonen/crebo/{qualification.id}"' in page, query
    assert "Geen kwalificaties gevonden." in teacher.get("/examens/sjablonen/crebo?q=bakker").text
    # HTMX asks for the results only.
    partial = teacher.get("/examens/sjablonen/crebo?q=27016", headers={"hx-request": "true"}).text
    assert "<html" not in partial and "ICT System Engineer" in partial


def test_search_shows_only_the_current_version(
    teacher: TestClient, db: Session, services: Services, qualification: Qualification
) -> None:
    services.qualifications.import_records(
        db, [record("2026", valid_from=date(2026, 8, 1))], actor=Actor.system(), ip=None, source="t"
    )
    newest = db.scalar(select(Qualification).where(Qualification.dossier_version == "2026"))
    assert newest is not None
    page = teacher.get("/examens/sjablonen/crebo?q=27016").text
    assert str(newest.id) in page and str(qualification.id) not in page


def test_no_import_yet(teacher: TestClient) -> None:
    assert "nog niet ingelezen" in teacher.get("/examens/sjablonen/crebo").text


def test_templates_from_core_tasks(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    page = teacher.get(f"/examens/sjablonen/crebo/{qualification.id}").text
    assert "B1-K1 - Richt systemen in" in page and "B1-K1-W3" in page
    assert 'name="shared"' not in page  # only admins share
    response = make_templates(teacher, qualification, "B1-K1", "P1-K1")
    assert response.headers["location"] == "/examens/sjablonen?ok=created_from_crebo&n=2"
    template = template_named(db, "B1-K1")
    assert template.name == f"27016 ICT System Engineer {DASH} B1-K1 Richt systemen in"
    assert [s.label for s in template.slots] == [
        "B1-K1-W1 Inventariseert wensen", "B1-K1-W2 Installeert systemen", "B1-K1-W3 Test een netwerk",
    ]  # fmt: skip
    assert [s.work_process_code for s in template.slots] == ["B1-K1-W1", "B1-K1-W2", "B1-K1-W3"]
    assert not template.shared and template.qualification_id == qualification.id
    assert (
        "Sjablonen gemaakt: 2." in teacher.get("/examens/sjablonen?ok=created_from_crebo&n=2").text
    )
    assert (
        "Kies ten minste één kerntaak."
        in teacher.get(make_templates(teacher, qualification).headers["location"]).text
    )
    # A teacher cannot make a shared template.
    assert make_templates(teacher, qualification, "B1-K2", shared=True).status_code == 403


# Shared templates (managed by admins) ----------------------------------------------


def test_shared_templates_are_managed_by_admins(
    teacher: TestClient, admin: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(admin, qualification, "B1-K1", shared=True)
    template = template_named(db, "B1-K1")
    assert template.shared
    listing = teacher.get("/examens/sjablonen").text
    assert "Gedeelde sjablonen" in listing and template.name in listing
    detail = teacher.get(f"/examens/sjablonen/{template.id}").text
    assert "Sjabloon bewerken" not in detail and "Kopie maken" in detail
    assert teacher.get(f"/examens/sjablonen/{template.id}/bewerken").status_code == 403
    assert post(teacher, f"/examens/sjablonen/{template.id}/verwijderen").status_code == 403
    assert post(teacher, f"/examens/sjablonen/{template.id}/delen").status_code == 403
    upload_url = f"/examens/sjablonen/{template.id}/vakjes/{template.slots[0].id}/sjabloon"
    response = teacher.post(upload_url, data={"csrf_token": csrf_token(teacher)},
                            files={"file": ("a.docx", b"x", "application/octet-stream")})  # fmt: skip
    assert response.status_code == 403
    # Any teacher can use it and make a personal copy.
    assert teacher.get(f"/examens/nieuw?sjabloon={template.id}").status_code == 200
    copy = post(teacher, f"/examens/sjablonen/{template.id}/kopie")
    copy_id = uuid.UUID(copy.headers["location"].split("/")[3].split("?")[0])
    assert teacher.get(f"/examens/sjablonen/{copy_id}/bewerken").status_code == 200
    # The admin can take the sharing back.
    assert (
        post(admin, f"/examens/sjablonen/{template.id}/delen")
        .headers["location"]
        .endswith("unshared")
    )
    assert teacher.get(f"/examens/sjablonen/{template.id}").status_code == 404


def test_admin_shares_a_personal_template(
    teacher: TestClient, admin: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(admin, qualification, "B1-K2")
    template = template_named(db, "B1-K2")
    assert teacher.get(f"/examens/sjablonen/{template.id}").status_code == 404
    post(admin, f"/examens/sjablonen/{template.id}/delen", {"shared": "true"})
    assert teacher.get(f"/examens/sjablonen/{template.id}").status_code == 200
    db.expire_all()
    assert db.scalar(select(AuditEvent).where(AuditEvent.event == "exam_template.shared"))


# Exams in parts --------------------------------------------------------------------


def test_full_and_part_exam_names(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    template = template_named(db, "B1-K1")
    full = teacher.get(f"/examens/nieuw?sjabloon={template.id}").text
    assert f'value="27016 ICT System Engineer {DASH} B1-K1 Richt systemen in"' in full
    assert 'value="27016-b1-k1-16112026"' in full
    assert full.count('name="wp"') == 3
    part = teacher.get(f"/examens/nieuw?sjabloon={template.id}&wp=B1-K1-W3").text
    assert f'value="27016 ICT System Engineer {DASH} B1-K1-W3"' in part
    assert 'value="27016-b1-k1-w3-16112026"' in part
    assert 'name="slot_wp_0" value="B1-K1-W3"' in part and 'name="slot_wp_1"' not in part
    two = teacher.get(f"/examens/nieuw?sjabloon={template.id}&wp=B1-K1-W1&wp=B1-K1-W3").text
    assert 'value="27016-b1-k1-w1-w3-16112026"' in two


def test_exam_keeps_codes_and_a_server_side_snapshot(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    exam = create_exam_from(teacher, db, template_named(db, "B1-K1"), ["B1-K1-W2", "B1-K1-W3"])
    assert [s.work_process_code for s in exam.expected_files] == ["B1-K1-W2", "B1-K1-W3"]
    assert exam.qualification == {
        "crebo": "27016", "name": "ICT System Engineer", "level": 4, "dossier_version": "2024",
        "core_task_code": "B1-K1", "core_task_title": "Richt systemen in",
    }  # fmt: skip
    settings = teacher.get(f"/examens/{exam.id}/instellingen").text
    assert "Kwalificatie" in settings and "ICT System Engineer" in settings


def test_forged_work_process_code_is_dropped(teacher: TestClient, db: Session) -> None:
    fields = exam_form(name="Los", slug="", slot_count="1", slot_label_0="A", slot_pattern_0="",
                       slot_extensions_0="", slot_wp_0="<script>")  # fmt: skip
    response = post(teacher, "/examens/nieuw", fields)
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None and exam.expected_files[0].work_process_code is None
    assert exam.qualification is None


# Overview, exports and PV ----------------------------------------------------------


@pytest.fixture
def wp_exam(
    teacher: TestClient, db: Session, services: Services, qualification: Qualification
) -> Exam:
    """B1-K1 with three work processes; student 123456 hands in W1 and W3 only."""
    make_templates(teacher, qualification, "B1-K1")
    exam = create_exam_from(teacher, db, template_named(db, "B1-K1"), [])
    services.exams.publish(db, db.get(User, exam.owner_id), exam, ip=None)  # type: ignore[arg-type]
    db.add(ExamRosterEntry(exam_id=exam.id, student_number="123456", first_name="Jan", infix="de",
                           last_name="Vries"))  # fmt: skip
    db.add(ExamRosterEntry(exam_id=exam.id, student_number="123457", first_name="Zoë",
                           last_name="Bakker"))  # fmt: skip
    db.commit()
    student = TestClient(teacher.app)
    ready(student, exam, "123456")
    slots = {s.work_process_code: str(s.id) for s in exam.expected_files}
    response = upload(student, exam, [("wensen.docx", b"PK w1", slots["B1-K1-W1"]),
                                      ("test.pdf", b"%PDF w3", slots["B1-K1-W3"])], confirmed=True)  # fmt: skip
    assert response.status_code == 200, response.text
    db.expire_all()
    fresh = db.get(Exam, exam.id)
    assert fresh is not None
    return fresh


def test_overview_per_work_process(teacher: TestClient, wp_exam: Exam) -> None:
    page = teacher.get(f"/examens/{wp_exam.id}").text
    for code in ("B1-K1-W1", "B1-K1-W2", "B1-K1-W3"):
        assert f">{code}</abbr>" in page
    assert "B1-K1-W1 ingeleverd" in page and "B1-K1-W2 ontbreekt" in page
    assert "27016" in page  # qualification under the heading


def test_csv_zip_and_manifest_have_codes(teacher: TestClient, wp_exam: Exam) -> None:
    raw = teacher.get(f"/examens/{wp_exam.id}/overzicht.csv").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw), delimiter=";"))
    header = rows[0]
    assert header[-3:] == ["B1-K1-W1", "B1-K1-W2", "B1-K1-W3"]
    jan = next(r for r in rows if r[0] == "123456")
    assert jan[-3:] == ["ja", "nee", "ja"]
    zoe = next(r for r in rows if r[0] == "123457")
    assert zoe[-3:] == ["", "", ""]
    archive = zipfile.ZipFile(io.BytesIO(teacher.get(f"/examens/{wp_exam.id}/zip").content))
    names = archive.namelist()
    assert any(n.endswith("/B1-K1-W1_wensen.docx") for n in names)
    assert any(n.endswith("/B1-K1-W3_test.pdf") for n in names)
    manifest = list(
        csv.DictReader(io.StringIO(archive.read("manifest.csv").decode("utf-8-sig")), delimiter=";")
    )
    assert sorted(r["werkproces"] for r in manifest) == ["B1-K1-W1", "B1-K1-W3"]


def test_proces_verbaal_has_codes(
    app: FastAPI, teacher: TestClient, wp_exam: Exam, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = app.state.templates.env
    monkeypatch.setattr(
        app.state.pdf, "_render", lambda t, c: env.get_template(t).render(c).encode()
    )
    html = form(teacher, f"/examens/{wp_exam.id}/proces-verbaal", {}).text
    assert "ICT System Engineer" in html and "27016" in html and "dossier 2024" in html
    assert "Richt systemen in" in html
    assert '<td class="mono">B1-K1-W2</td>' in html
    assert "Werkprocessen:" in html


# Resit -----------------------------------------------------------------------------


def test_resit_in_one_click(teacher: TestClient, db: Session, wp_exam: Exam) -> None:
    page = post(teacher, f"/examens/{wp_exam.id}/herkansing", {"student_number": "123456"}).text
    assert "Herkansing van" in page and "123456" in page
    assert (
        'name="slot_wp_0" value="B1-K1-W2"' in page and 'name="slot_wp_1"' not in page
    )  # only W2 open
    assert f'name="source_exam_id" value="{wp_exam.id}"' in page
    assert 'name="roster_number" value="123456"' in page
    assert 'id="class_list_id"' not in page
    fields = exam_form(name=f"{wp_exam.name} {DASH} herkansing", slug="", slot_count="1",
                       slot_label_0="B1-K1-W2 Installeert systemen", slot_pattern_0="",
                       slot_extensions_0="", slot_id_0=str(wp_exam.expected_files[1].id),
                       slot_wp_0="B1-K1-W2", source_exam_id=str(wp_exam.id), roster_number="123456")  # fmt: skip
    response = post(teacher, "/examens/nieuw", fields)
    assert response.status_code == 303, response.text
    resit = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert resit is not None
    assert resit.allow_unlisted is False
    roster = db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == resit.id)).all()
    assert [(r.student_number, r.last_name) for r in roster] == [("123456", "Vries")]
    assert resit.qualification is not None and resit.qualification["core_task_code"] == "B1-K1"
    assert [s.work_process_code for s in resit.expected_files] == ["B1-K1-W2"]


def test_resit_for_several_students(teacher: TestClient, db: Session, wp_exam: Exam) -> None:
    page = teacher.get(f"/examens/{wp_exam.id}/herkansing").text
    # Both have something open: Jan misses W2, Zoë handed in nothing.
    assert page.count('name="student_number"') == 2 and page.count(" checked>") == 2
    form_page = post(teacher, f"/examens/{wp_exam.id}/herkansing",
                     {"student_number": ["123456", "123457"]}).text  # fmt: skip
    for i, code in enumerate(["B1-K1-W1", "B1-K1-W2", "B1-K1-W3"]):
        assert f'name="slot_wp_{i}" value="{code}"' in form_page  # union of what is open
    assert "voor 2 student(en)" in form_page


def test_resit_needs_students_and_rights(
    teacher: TestClient, db: Session, clock: FrozenClock, wp_exam: Exam
) -> None:
    response = post(teacher, f"/examens/{wp_exam.id}/herkansing", {"student_number": "000"})
    assert response.headers["location"].endswith("error=no_students_selected")
    create_user(db, clock, "ander@school.nl")
    other = TestClient(teacher.app)
    login(other, "ander@school.nl")
    assert (
        post(other, f"/examens/{wp_exam.id}/herkansing", {"student_number": "123456"}).status_code
        == 404
    )
    # A forged source exam of someone else is ignored.
    fields = exam_form(name="x", slug="", slot_count="0", allow_extra_file="true",
                       source_exam_id=str(wp_exam.id), roster_number="123456")  # fmt: skip
    created = post(other, "/examens/nieuw", fields)
    exam = db.get(Exam, uuid.UUID(created.headers["location"].split("/")[2]))
    assert exam is not None and exam.qualification is None
    assert not db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id)).all()


# Replaced dossier ------------------------------------------------------------------


def test_replaced_dossier_warning(
    teacher: TestClient, db: Session, services: Services, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    template = template_named(db, "B1-K1")
    assert "Dossier vervangen" not in teacher.get(f"/examens/sjablonen/{template.id}").text
    services.qualifications.import_records(
        db, [record("2026", w3_title="Test en documenteert een netwerk", valid_from=date(2026, 8, 1))],
        actor=Actor.system(), ip=None, source="t",
    )  # fmt: skip
    detail = teacher.get(f"/examens/sjablonen/{template.id}").text
    assert "Dossier vervangen" in detail and "(2026)" in detail
    assert "Test een netwerk → Test en documenteert een netwerk" in detail
    assert "Dossier vervangen" in teacher.get("/examens/sjablonen").text


def test_expired_dossier_warning(teacher: TestClient, db: Session, services: Services) -> None:
    services.qualifications.import_records(
        db, [record(valid_until=date(2026, 7, 31))], actor=Actor.system(), ip=None, source="t"
    )
    q = db.scalar(select(Qualification))
    assert q is not None
    make_templates(teacher, q, "B1-K2")
    detail = teacher.get(f"/examens/sjablonen/{template_named(db, 'B1-K2').id}").text
    assert "verlopen (31-07-2026)" in detail


def test_reimport_updates_in_place(
    db: Session, services: Services, qualification: Qualification
) -> None:
    result = services.qualifications.import_records(
        db, [record(w3_title="Nieuw")], actor=Actor.system(), ip=None, source="t"
    )
    assert (result.created, result.updated) == (0, 1)
    db.expire_all()
    assert len(db.scalars(select(Qualification)).all()) == 1


def test_duplicate_keeps_class_list_setting(
    teacher: TestClient, db: Session, wp_exam: Exam
) -> None:
    """Regression: duplicating passes the roster to create(), so allow_unlisted stays off."""
    wp_exam.allow_unlisted = False
    db.commit()
    response = post(teacher, f"/examens/{wp_exam.id}/dupliceren")
    copy = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert copy is not None
    assert copy.allow_unlisted is False
    assert copy.qualification == wp_exam.qualification
    assert [s.work_process_code for s in copy.expected_files] == [
        "B1-K1-W1",
        "B1-K1-W2",
        "B1-K1-W3",
    ]


# SBB import (admin screen and CLI) ------------------------------------------------


def _upload(client: TestClient, data: bytes, name: str = "dossiers.xml") -> Any:
    return client.post(
        "/admin/kwalificaties", data={"csrf_token": csrf_token(client)},
        files={"file": (name, data, "application/xml")}, follow_redirects=False,
    )  # fmt: skip


def test_admin_imports_the_sbb_file(admin: TestClient, teacher: TestClient, db: Session) -> None:
    assert teacher.get("/admin/kwalificaties").status_code == 403
    page = admin.get("/admin/kwalificaties").text
    assert "kwalificatie-mijn.s-bb.nl" in page
    response = _upload(admin, sample_xml())
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/kwalificaties?created=6&updated=0"
    page = admin.get(response.headers["location"]).text
    assert "6 nieuwe en 0 bijgewerkte" in page
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Qualification)) == 6
    event = db.scalar(select(AuditEvent).where(AuditEvent.event == "qualifications.imported"))
    assert event is not None
    assert event.details["source"] == "upload"
    # Importing again updates the same versions.
    response = _upload(admin, sample_xml())
    assert response.headers["location"] == "/admin/kwalificaties?created=0&updated=6"


def test_admin_import_refuses_other_files(admin: TestClient, db: Session) -> None:
    response = _upload(admin, b"<html><body>no</body></html>", "dossiers.xml")
    assert response.status_code == 400
    assert "geen SBB-kwalificatiebestand" in response.text
    response = admin.post(
        "/admin/kwalificaties", data={"csrf_token": csrf_token(admin)}, follow_redirects=False
    )
    assert response.status_code == 400
    assert db.scalar(select(func.count()).select_from(Qualification)) == 0


def test_a_new_crebo_after_a_revision_counts_as_replacement(
    teacher: TestClient, db: Session, services: Services
) -> None:
    records = sbb.parse(io.BytesIO(sample_xml())).records
    services.qualifications.import_records(db, records, actor=Actor.system(), ip=None, source="t")
    old = services.qualifications.current(db, "29001")
    assert old is not None
    assert old.dossier_version == "2015, gewijzigd 2016-2"  # newest of the 2016 changes
    status = services.qualifications.status(db, old)
    assert status.replaced_by is not None
    assert (status.replaced_by.crebo, status.replaced_by.dossier_version) == ("29101", "2024")
    # A template on the old crebo warns with the new crebo.
    make_templates(teacher, old, "B1-K1")
    template = template_named(db, "B1-K1")
    page = teacher.get(f"/examens/sjablonen/{template.id}").text
    assert "crebo 29101" in page
    # Search shows the replaced qualification after the current one, with a badge.
    results = teacher.get("/examens/sjablonen/crebo?q=Medewerker", headers={"HX-Request": "true"})
    assert results.text.index("29101") < results.text.index("29001")
    assert "vervangen door 29101" in results.text
    # The detail page of the old version links to the current one.
    current = services.qualifications.current(db, "29101")
    assert current is not None
    assert (
        f"/examens/sjablonen/crebo/{current.id}"
        in teacher.get(f"/examens/sjablonen/crebo/{old.id}").text
    )


def test_cli_sbb_import(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: Session
) -> None:
    from inleverboks import cli  # noqa: PLC0415

    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("STORAGE_PATH", settings.storage_path)
    get_settings.cache_clear()
    try:
        path = tmp_path / "sbb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("dossiers.xml", sample_xml())
        result = CliRunner().invoke(cli.app, ["sbb-import", str(path)])
        assert result.exit_code == 0, result.output
        assert "5 dossier(s) read, 1 not adopted" in result.stdout
        assert "6 new and 0 updated" in result.stdout
        bad = tmp_path / "bad.xml"
        bad.write_bytes(b"<nope/>")
        result = CliRunner().invoke(cli.app, ["sbb-import", str(bad)])
        assert result.exit_code == 2
    finally:
        get_settings.cache_clear()
    assert db.scalar(select(func.count()).select_from(Qualification)) == 6


# Expected files of a template-based exam are read-only ------------------------------


def test_template_exam_ignores_forged_slots(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    template = template_named(db, "B1-K1")
    fields: dict[str, Any] = {
        "name": "x", "slug": "", "source_template_id": str(template.id), "slot_count": "2",
        "slot_label_0": "Gewijzigd", "slot_pattern_0": "*.exe", "slot_extensions_0": "exe",
        "slot_wp_0": "B1-K1-W3", "slot_label_1": "Extra vakje", "slot_wp_1": "",
    }  # fmt: skip
    response = post(teacher, "/examens/nieuw", exam_form(**fields))
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None
    # Only the chosen work process (a part exam), with the template's own label.
    w3 = next(s for s in template.slots if s.work_process_code == "B1-K1-W3")
    assert [(s.label, s.expected_name_pattern) for s in exam.expected_files] == [
        (w3.label, w3.expected_name_pattern)
    ]
    assert exam.source_template_id == template.id

    # Later edits show a read-only list with a link to the template and ignore slot changes.
    edit = teacher.get(f"/examens/{exam.id}/bewerken").text
    assert 'name="slot_label_0"' not in edit
    assert 'value="add_slot"' not in edit
    assert "Deze verwachte bestanden komen uit het examensjabloon" in edit
    assert f'href="/examens/sjablonen/{template.id}"' in edit
    fields |= {"name": "y", "slug": exam.slug, "slot_count": "1", "slot_label_0": "Anders"}
    assert post(teacher, f"/examens/{exam.id}/bewerken", exam_form(**fields)).status_code == 303
    db.expire_all()
    exam = db.get(Exam, exam.id)
    assert exam is not None and exam.name == "y"
    assert [s.label for s in exam.expected_files] == [w3.label]


def test_template_exam_becomes_editable_when_the_template_is_gone(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    template = template_named(db, "B1-K1")
    exam = create_exam_from(teacher, db, template, [])
    assert 'name="slot_label_0"' not in teacher.get(f"/examens/{exam.id}/bewerken").text
    post(teacher, f"/examens/sjablonen/{template.id}/verwijderen")
    db.expire_all()
    stored = db.get(Exam, exam.id)
    assert stored is not None and stored.source_template_id is None
    assert 'name="slot_label_0"' in teacher.get(f"/examens/{exam.id}/bewerken").text


def test_duplicate_and_resit_of_a_template_exam(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    template = template_named(db, "B1-K1")
    exam = create_exam_from(teacher, db, template, ["B1-K1-W1"])
    copy_id = post(teacher, f"/examens/{exam.id}/dupliceren").headers["location"].split("/")[2]
    copy = db.get(Exam, uuid.UUID(copy_id))
    assert copy is not None
    assert copy.source_template_id == template.id  # still read-only
    assert [s.work_process_code for s in copy.expected_files] == ["B1-K1-W1"]
    # A resit keeps its own selection of open expected files (editable, as before).
    db.add(ExamRosterEntry(exam_id=exam.id, student_number="123456", first_name="Jan",
                           last_name="Vries"))  # fmt: skip
    db.commit()
    resit = post(teacher, f"/examens/{exam.id}/herkansing", {"student_number": "123456"})
    assert 'name="slot_label_0"' in resit.text


# Link name with the planned date --------------------------------------------------------


def test_kept_suggested_slug_follows_the_start_date(
    teacher: TestClient, db: Session, qualification: Qualification
) -> None:
    make_templates(teacher, qualification, "B1-K1")
    template = template_named(db, "B1-K1")
    page = teacher.get(f"/examens/nieuw?sjabloon={template.id}").text
    assert 'name="suggested_slug" value="27016-b1-k1-16112026"' in page  # the planned date
    common = {"name": "x", "source_template_id": str(template.id), "slot_count": "0",
              "starts_at_date": "2026-12-03", "starts_at_time": "09:00",
              "ends_at_date": "2026-12-03", "ends_at_time": "17:00"}  # fmt: skip
    kept = {"slug": "27016-b1-k1-16112026", "suggested_slug": "27016-b1-k1-16112026"}
    response = post(teacher, "/examens/nieuw", exam_form(**common, **kept))
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None and exam.slug == "27016-b1-k1-03122026"
    # Again for the same day: the next free name.
    response = post(teacher, "/examens/nieuw", exam_form(**common, **kept))
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None and exam.slug == "27016-b1-k1-03122026-2"
    # A link name the teacher typed stays as it is.
    own = {"slug": "mijn-eigen-link", "suggested_slug": "27016-b1-k1-16112026"}
    response = post(teacher, "/examens/nieuw", exam_form(**common, **own))
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None and exam.slug == "mijn-eigen-link"
