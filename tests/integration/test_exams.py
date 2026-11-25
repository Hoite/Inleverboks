from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import (
    AuditEvent,
    Exam,
    ExamRosterEntry,
    ExamTemplate,
    IpPreset,
    ProcesVerbaal,
    RetiredSlug,
    Submission,
    TemplateFile,
    User,
)
from inleverboks.services.container import Services
from inleverboks.services.exams import ExamInput, SlotInput
from inleverboks.services.security import HUMAN_ALPHABET
from tests.conftest import create_user, csrf_token, enable_totp, extract, login, post

# Fixed clock: Friday 13 November 2026, 10:00 Amsterdam (09:00 UTC).
T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def form(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Examen Linux P3",
        "description": "",
        "starts_at": "2026-11-13T10:00",
        "ends_at": "2026-11-13T12:00",
        "second_reviewer_id": "",
        "slug": "",
        "access_code_enabled": "true",
        "allow_unlisted": "true",
        "allow_extra_file": "true",
        "extensions": "docx, pdf, sh",
        "max_file_mb": "10",
        "max_submission_mb": "50",
        "slot_count": "2",
        "slot_label_0": "Verslag",
        "slot_pattern_0": "Examen_Linux_P3_*.docx",
        "slot_extensions_0": "docx",
        "slot_label_1": "Script",
        "slot_pattern_1": "",
        "slot_extensions_1": "",
        "ip_cidrs": "",
        "ip_preset_id": "",
        "class_list_id": "",
        "source_template_id": "",
    }
    data.update(overrides)
    return data


def events(db: Session, name: str, chain: str | None = None) -> list[AuditEvent]:
    db.expire_all()
    query = select(AuditEvent).where(AuditEvent.event == name)
    if chain:
        query = query.where(AuditEvent.chain == chain)
    return list(db.scalars(query.order_by(AuditEvent.ts, AuditEvent.seq)))


@pytest.fixture
def teacher(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "docent@school.nl", name="Docent")


@pytest.fixture
def tclient(client: TestClient, teacher: User) -> TestClient:
    assert login(client).headers["location"] == "/examens"
    return client


def other_client(
    client: TestClient, db: Session, clock: FrozenClock, email: str, **kw: Any
) -> TestClient:
    user = create_user(db, clock, email, name=email.split("@", maxsplit=1)[0], **kw)
    other = TestClient(client.app)
    secret = enable_totp(db, client.app.state.services, user) if user.is_admin else None  # type: ignore[attr-defined]
    login(other, email, secret=secret, clock=clock)
    return other


def create_exam(tclient: TestClient, **overrides: Any) -> Exam:
    response = post(tclient, "/examens/nieuw", form(**overrides))
    assert response.status_code == 303, response.text
    exam_id = uuid.UUID(response.headers["location"].split("/")[2])
    db: Session = tclient.app.state.sessionmaker()  # type: ignore[attr-defined]
    with db:
        exam = db.get(Exam, exam_id)
        assert exam is not None
        db.expunge(exam)
        return exam


# Create --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "day"),
    [
        (datetime(2026, 11, 13, 9, 7, tzinfo=UTC), "2026-11-16"),  # Friday → Monday
        (datetime(2026, 11, 12, 23, 30, tzinfo=UTC), "2026-11-16"),  # Fri 00:30 local → Mon
        (datetime(2026, 11, 14, 12, 0, tzinfo=UTC), "2026-11-16"),  # Saturday → Monday
        (datetime(2026, 11, 15, 12, 0, tzinfo=UTC), "2026-11-16"),  # Sunday → Monday
    ],
)
def test_new_exam_form_defaults(
    tclient: TestClient, clock: FrozenClock, now: datetime, day: str
) -> None:
    clock.set(now)
    login(tclient)  # a new session at that time
    page = tclient.get("/examens/nieuw")
    assert page.status_code == 200
    # The next working day from 09:00 to 17:00, as a date field plus a 24-hour time field.
    assert f'name="starts_at_date" type="date" required value="{day}"' in page.text
    assert 'name="starts_at_time" type="text" required value="09:00"' in page.text
    assert f'name="ends_at_date" type="date" required value="{day}"' in page.text
    assert 'name="ends_at_time" type="text" required value="17:00"' in page.text
    assert 'type="datetime-local"' not in page.text
    assert "docx, xlsx, pptx, pdf" in page.text  # default extensions
    # Searchable selects are plain selects for the server (js/combobox.js enhances them).
    assert '<select id="second_reviewer_id" name="second_reviewer_id" data-combobox' in page.text
    assert "&#34;noResults&#34;: &#34;Geen resultaten&#34;" in page.text


def test_create_exam(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    db_exam = db.get(Exam, exam.id)
    assert db_exam is not None
    assert db_exam.slug == "examen-linux-p3"
    assert db_exam.starts_at == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    assert db_exam.published_at is None
    assert db_exam.access_code is not None
    assert len(db_exam.access_code) == 6
    assert set(db_exam.access_code) <= set(HUMAN_ALPHABET)
    assert [s.label for s in db_exam.expected_files] == ["Verslag", "Script"]
    assert db_exam.expected_files[0].expected_name_pattern == "Examen_Linux_P3_*.docx"
    assert db_exam.max_file_bytes == 10 * 1024 * 1024
    created = events(db, "exam.created", str(exam.id))
    assert len(created) == 1
    assert created[0].details["exam"]["slots"][0]["label"] == "Verslag"
    assert str(db_exam.access_code) not in str(created[0].details)


def test_slug_suggestion_avoids_collision(tclient: TestClient) -> None:
    first = create_exam(tclient)
    second = create_exam(tclient)
    assert (first.slug, second.slug) == ("examen-linux-p3", "examen-linux-p3-2")


def test_add_and_remove_slot_rerender_without_saving(tclient: TestClient, db: Session) -> None:
    response = post(tclient, "/examens/nieuw", form(action="add_slot"))
    assert response.status_code == 200
    assert 'name="slot_label_2"' in response.text
    response = post(tclient, "/examens/nieuw", form(action="remove_slot:0"))
    assert 'value="Script"' in response.text
    assert 'value="Verslag"' not in response.text
    assert db.scalar(select(Exam)) is None


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"ends_at": "2026-11-13T09:00"}, "ends_at", "De eindtijd moet na de starttijd liggen"),
        ({"slug": "login"}, "slug", "gereserveerd"),
        ({"slug": "a--b"}, "slug", "a-z, 0-9"),
        ({"starts_at": "2027-03-28T02:30"}, "starts_at", "bestaat niet"),
        ({"name": ""}, "name", "Dit veld is verplicht"),
        ({"extensions": "d.o.c"}, "extensions", "niet geldig"),
        ({"max_file_mb": "500"}, "max_file_mb", "buiten het toegestane bereik"),
        ({"max_file_mb": "20", "max_submission_mb": "10"}, "max_file_mb", "niet groter zijn"),
        ({"slot_label_1": ""}, "slot_1_label", "Dit veld is verplicht"),
        ({"slot_pattern_0": "map/naam.docx"}, "slot_0_pattern", "niet geldig"),
        ({"ip_restriction_enabled": "true"}, "ip_cidrs", "minimaal één IP-bereik"),
        ({"ip_cidrs": "10.0.0.0/33"}, "ip_cidrs", "10.0.0.0/8"),
        ({"slot_count": "0", "allow_extra_file": ""}, "slots", "extra bestand toe"),
        ({"second_reviewer_id": str(uuid.uuid4())}, "second_reviewer_id", "niet geldig"),
    ],
)
def test_validation_errors(
    tclient: TestClient, db: Session, overrides: dict[str, str], field: str, message: str
) -> None:
    response = post(tclient, "/examens/nieuw", form(**overrides))
    assert response.status_code == 400
    assert f'id="{field}-error"' in response.text
    assert message in response.text
    assert db.scalar(select(Exam)) is None


def test_owner_cannot_be_second_reviewer(tclient: TestClient, teacher: User) -> None:
    response = post(tclient, "/examens/nieuw", form(second_reviewer_id=str(teacher.id)))
    assert response.status_code == 400


# Access control ------------------------------------------------------------------


def test_other_teacher_gets_404(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    exam = create_exam(tclient)
    other = other_client(tclient, db, clock, "ander@school.nl")
    for url in (
        f"/examens/{exam.id}",
        f"/examens/{exam.id}/bewerken",
        f"/examens/{exam.id}/presentatie",
        f"/examens/{exam.id}/qr.png",
    ):
        assert other.get(url).status_code == 404, url
    assert post(other, f"/examens/{exam.id}/publiceren").status_code == 404
    assert post(other, f"/examens/{exam.id}/bewerken", form(name="Gekaapt")).status_code == 404


def test_second_reviewer_can_view_but_not_edit(
    tclient: TestClient, db: Session, clock: FrozenClock
) -> None:
    reviewer = create_user(db, clock, "tweede@school.nl", name="Tweede")
    exam = create_exam(tclient, second_reviewer_id=str(reviewer.id))
    rclient = TestClient(tclient.app)
    login(rclient, "tweede@school.nl")
    assert rclient.get(f"/examens/{exam.id}").status_code == 200
    assert rclient.get(f"/examens/{exam.id}/instellingen").status_code == 200
    assert "Examen Linux P3" in rclient.get("/examens/gedeeld").text
    assert rclient.get(f"/examens/{exam.id}/bewerken").status_code == 403
    assert post(rclient, f"/examens/{exam.id}/code").status_code == 403
    response = post(
        rclient, f"/examens/{exam.id}/verlengingen",
        {"student_number": "123456", "ends_at": "2026-11-13T13:00"},
    )  # fmt: skip
    assert response.status_code == 403


def test_admin_sees_all_and_transfers_ownership(
    tclient: TestClient, db: Session, clock: FrozenClock, teacher: User
) -> None:
    exam = create_exam(tclient)
    aclient = other_client(tclient, db, clock, "admin@school.nl", admin=True)
    assert "Examen Linux P3" in aclient.get("/examens/alle").text
    assert aclient.get(f"/examens/{exam.id}").status_code == 200
    assert aclient.get(f"/examens/{exam.id}/bewerken").status_code == 403
    new_owner = create_user(db, clock, "nieuw@school.nl", name="Nieuw")
    response = post(aclient, f"/examens/{exam.id}/eigenaar", {"owner_id": str(new_owner.id)})
    assert response.headers["location"] == f"/examens/{exam.id}/geavanceerd?ok=owner"
    assert "Eigendom overgedragen." in aclient.get(response.headers["location"]).text
    db.expire_all()
    assert db.get(Exam, exam.id).owner_id == new_owner.id  # type: ignore[union-attr]
    assert events(db, "exam.ownership_transferred", "system")
    assert events(db, "exam.ownership_transferred", str(exam.id))
    # The previous owner lost access.
    assert tclient.get(f"/examens/{exam.id}").status_code == 404
    # Teachers cannot use the admin tab or transfer.
    assert tclient.get("/examens/alle").status_code == 403


# Edit, publish, status -----------------------------------------------------------


def test_update_records_old_and_new_values(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    response = post(
        tclient,
        f"/examens/{exam.id}/bewerken",
        form(
            name="Examen Linux P3 herkansing",
            ends_at="2026-11-13T12:30",
            slug=exam.slug,
            slot_id_0=str(_slot_ids(db, exam)[0]),
            slot_id_1=str(_slot_ids(db, exam)[1]),
        ),
    )
    assert response.status_code == 303
    changes = events(db, "exam.updated", str(exam.id))[-1].details["changes"]
    assert changes["name"] == {"old": "Examen Linux P3", "new": "Examen Linux P3 herkansing"}
    assert changes["ends_at"]["new"].startswith("2026-11-13T11:30")
    assert "slots" not in changes


def _slot_ids(db: Session, exam: Exam) -> list[uuid.UUID]:
    db.expire_all()
    fresh = db.get(Exam, exam.id)
    assert fresh is not None
    return [s.id for s in fresh.expected_files]


def test_status_and_public_page(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    clock.set(T0 - timedelta(hours=1))  # 09:00 local
    exam = create_exam(tclient)
    anonymous = TestClient(tclient.app)
    page = anonymous.get(f"/{exam.slug}")
    assert page.status_code == 200
    assert "nog niet opengezet door de docent" in page.text
    assert page.headers["cache-control"] == "no-store"

    post(tclient, f"/examens/{exam.id}/publiceren")
    assert events(db, "exam.published", str(exam.id))
    assert "Nog niet geopend (opent om 10:00)" in anonymous.get(f"/{exam.slug}").text

    clock.set(T0 + timedelta(minutes=30))
    page = anonymous.get(f"/{exam.slug}")
    assert "Resterende tijd" in page.text
    assert "1:30" in page.text

    clock.set(T0 + timedelta(hours=2))
    assert "Gesloten" in anonymous.get(f"/{exam.slug}").text
    assert anonymous.get("/bestaat-niet").status_code == 404
    # The page never shows the access code.
    assert exam.access_code not in anonymous.get(f"/{exam.slug}").text  # type: ignore[operator]


def test_unpublish_only_without_submissions(
    tclient: TestClient, db: Session, clock: FrozenClock
) -> None:
    clock.set(T0 - timedelta(hours=1))
    exam = create_exam(tclient)
    post(tclient, f"/examens/{exam.id}/publiceren")
    _add_submission(db, exam, clock)
    response = post(tclient, f"/examens/{exam.id}/concept")
    assert response.headers["location"].endswith("?error=has_submissions")


def test_regenerate_access_code(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    post(tclient, f"/examens/{exam.id}/code")
    db.expire_all()
    new_code = db.get(Exam, exam.id).access_code  # type: ignore[union-attr]
    assert new_code != exam.access_code
    event = events(db, "exam.access_code_regenerated", str(exam.id))[0]
    assert str(new_code) not in str(event.details)


def test_changing_slug_of_published_exam_retires_old_slug(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    post(tclient, f"/examens/{exam.id}/publiceren")
    ids = _slot_ids(db, exam)
    response = post(
        tclient, f"/examens/{exam.id}/bewerken",
        form(slug="linux-nieuw", slot_id_0=str(ids[0]), slot_id_1=str(ids[1])),
    )  # fmt: skip
    assert response.status_code == 303
    assert db.get(RetiredSlug, "examen-linux-p3") is not None
    assert TestClient(tclient.app).get("/examen-linux-p3").status_code == 404
    response = post(tclient, "/examens/nieuw", form(slug="examen-linux-p3"))
    assert response.status_code == 400
    assert "al in gebruik" in response.text


# Requirements lock ---------------------------------------------------------------


def _add_submission(db: Session, exam: Exam, clock: FrozenClock) -> None:
    db.add(
        Submission(
            exam_id=exam.id, student_number="123456", first_name="Jan", last_name="Vries",
            on_roster=False, attempt=1, status="active", started_at=clock.now(),
            completed_at=clock.now(), declaration_accepted=True,
            idempotency_key=uuid.uuid4().hex, receipt_code=f"IB-{uuid.uuid4().hex[:8]}",
            verification_code_hash="x" * 64,
        )
    )  # fmt: skip
    db.commit()


def test_file_requirements_lock_after_first_submission(
    tclient: TestClient, db: Session, clock: FrozenClock
) -> None:
    exam = create_exam(tclient)
    _add_submission(db, exam, clock)
    page = tclient.get(f"/examens/{exam.id}/bewerken")
    assert "bestandseisen kunnen niet meer worden gewijzigd" in page.text
    ids = _slot_ids(db, exam)
    response = post(
        tclient,
        f"/examens/{exam.id}/bewerken",
        form(
            slug=exam.slug, ends_at="2026-11-13T13:00", extensions="pdf", max_file_mb="1",
            slot_count="1", slot_id_0=str(ids[0]), slot_label_0="Anders",
        ),
    )  # fmt: skip
    assert response.status_code == 303
    db.expire_all()
    fresh = db.get(Exam, exam.id)
    assert fresh is not None
    assert [s.label for s in fresh.expected_files] == ["Verslag", "Script"]
    assert fresh.extensions == ["docx", "pdf", "sh"]
    assert fresh.max_file_bytes == 10 * 1024 * 1024
    assert fresh.ends_at == datetime(2026, 11, 13, 12, 0, tzinfo=UTC)  # times may change
    response = tclient.post(
        f"/examens/{exam.id}/vakjes/{ids[0]}/sjabloon",
        data={"csrf_token": _csrf(tclient)},
        files={"file": ("leeg.docx", b"template", "application/octet-stream")},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("?error=requirements_locked")


def _csrf(client: TestClient) -> str:
    return csrf_token(client)


# Template files ------------------------------------------------------------------


def _upload(client: TestClient, exam: Exam, slot_id: uuid.UUID, data: bytes, name: str) -> Any:
    return client.post(
        f"/examens/{exam.id}/vakjes/{slot_id}/sjabloon",
        data={"csrf_token": _csrf(client)},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )


def test_template_files_are_content_addressed(
    tclient: TestClient, db: Session, services: Services
) -> None:
    exam = create_exam(tclient)
    ids = _slot_ids(db, exam)
    content = b"PK\x03\x04 empty template"
    response = _upload(tclient, exam, ids[0], content, "Examen_Linux_P3_sjabloon.docx")
    assert response.headers["location"].endswith("?ok=template_uploaded")
    template = db.scalar(select(TemplateFile))
    assert template is not None
    sha = hashlib.sha256(content).hexdigest()
    assert template.sha256 == sha
    assert template.storage_key == f"templates/{sha}"
    assert events(db, "exam.template_file_uploaded", str(exam.id))[0].details["sha256"] == sha

    download = tclient.get(f"/examens/{exam.id}/vakjes/{ids[0]}/sjabloon")
    assert download.content == content
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"

    key = template.storage_key
    # A duplicate shares the stored object; removing one reference keeps it.
    copy_location = post(tclient, f"/examens/{exam.id}/dupliceren").headers["location"]
    copy_id = uuid.UUID(copy_location.split("/")[2])
    response = post(tclient, f"/examens/{exam.id}/vakjes/{ids[0]}/sjabloon/verwijderen")
    assert response.headers["location"].endswith("?ok=template_removed")
    assert _exists(services, key)
    copy_slot = _slot_ids(db, db.get(Exam, copy_id))[0]  # type: ignore[arg-type]
    post(tclient, f"/examens/{copy_id}/vakjes/{copy_slot}/sjabloon/verwijderen")
    assert not _exists(services, key)


def _exists(services: Services, key: str) -> bool:
    return anyio.run(services.storage.exists, key)


def test_template_file_too_large(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient, max_file_mb="1")
    ids = _slot_ids(db, exam)
    response = _upload(tclient, exam, ids[0], b"x" * (1024 * 1024 + 1), "groot.docx")
    assert response.headers["location"].endswith("?error=template_too_large")
    assert db.scalar(select(TemplateFile)) is None


# Duplicate, templates, delete ----------------------------------------------------


def test_duplicate_exam(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    exam = create_exam(tclient)
    clock.advance(days=3)
    login(tclient)  # the session expired meanwhile
    response = post(tclient, f"/examens/{exam.id}/dupliceren")
    copy_id = uuid.UUID(response.headers["location"].split("/")[2])
    copy = db.get(Exam, copy_id)
    assert copy is not None
    assert copy.name == "Examen Linux P3 (kopie)"
    assert copy.slug == "examen-linux-p3-kopie"
    assert copy.access_code != exam.access_code
    assert copy.published_at is None
    # The next working day (Tuesday) at 09:00 local, with the duration of the original.
    assert copy.starts_at == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    assert copy.ends_at - copy.starts_at == timedelta(hours=2)
    assert [s.label for s in copy.expected_files] == ["Verslag", "Script"]
    assert events(db, "exam.duplicated", str(copy.id))[0].details["source_exam_id"] == str(exam.id)


def test_duplicate_keeps_class_list_and_setting(tclient: TestClient, db: Session) -> None:
    """Regression (v0.1.1/v0.1.2): the copy got its roster after create(), which then saw
    no class list and turned "allow students who are not on the list" on."""
    exam = create_exam(tclient)
    db.add_all(
        [
            ExamRosterEntry(exam_id=exam.id, student_number="123456", first_name="Jan",
                            infix="de", last_name="Vries", class_name="ICT-2A"),
            ExamRosterEntry(exam_id=exam.id, student_number="123457", first_name="Zoë",
                            last_name="Bakker"),
        ]
    )  # fmt: skip
    db.get(Exam, exam.id).allow_unlisted = False  # type: ignore[union-attr]
    db.commit()
    response = post(tclient, f"/examens/{exam.id}/dupliceren")
    copy = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert copy is not None
    assert copy.allow_unlisted is False
    roster = db.scalars(
        select(ExamRosterEntry).where(ExamRosterEntry.exam_id == copy.id)
        .order_by(ExamRosterEntry.student_number)
    ).all()  # fmt: skip
    assert [(r.student_number, r.infix, r.class_name) for r in roster] == [
        ("123456", "de", "ICT-2A"), ("123457", None, None),
    ]  # fmt: skip
    created = events(db, "exam.created", str(copy.id))[0]
    assert created.details["exam"]["allow_unlisted"] is False


def test_save_as_template_and_create_from_it(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient, access_code_enabled="", allow_unlisted="")
    ids = _slot_ids(db, exam)
    _upload(tclient, exam, ids[0], b"sjabloon", "leeg.docx")
    post(tclient, f"/examens/{exam.id}/sjabloon-opslaan", {"name": "Linux P3"})
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    assert template.name == "Linux P3"
    assert "Linux P3" in tclient.get("/examens/sjablonen").text

    page = tclient.get(f"/examens/nieuw?sjabloon={template.id}")
    assert '<td class="mono">Examen_Linux_P3_*.docx</td>' in page.text  # read-only
    assert f'value="{template.id}"' in page.text
    response = post(
        tclient, "/examens/nieuw",
        form(name="Linux P3 herkansing", access_code_enabled="", allow_unlisted="",
             source_template_id=str(template.id)),
    )  # fmt: skip
    new_id = uuid.UUID(response.headers["location"].split("/")[2])
    new_exam = db.get(Exam, new_id)
    assert new_exam is not None
    assert new_exam.expected_files[0].template_file is not None
    assert (
        new_exam.expected_files[0].template_file.sha256 == hashlib.sha256(b"sjabloon").hexdigest()
    )


def test_templates_are_personal(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    exam = create_exam(tclient)
    post(tclient, f"/examens/{exam.id}/sjabloon-opslaan", {"name": "Mijn sjabloon"})
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    other = other_client(tclient, db, clock, "ander@school.nl")
    assert other.get(f"/examens/nieuw?sjabloon={template.id}").status_code == 404
    assert "Mijn sjabloon" not in other.get("/examens/sjablonen").text


def test_delete_concept(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    response = post(tclient, f"/examens/{exam.id}/verwijderen")
    assert response.headers["location"] == "/examens"
    db.expire_all()
    assert db.get(Exam, exam.id) is None
    assert db.get(RetiredSlug, exam.slug) is not None
    assert events(db, "exam.deleted", "system")
    # The exam's own audit chain survives the deletion.
    assert events(db, "exam.created", str(exam.id))


def test_published_exam_cannot_be_deleted(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    post(tclient, f"/examens/{exam.id}/publiceren")
    response = post(tclient, f"/examens/{exam.id}/verwijderen")
    assert response.headers["location"].endswith("?error=not_concept")
    assert db.get(Exam, exam.id) is not None


# Extensions ----------------------------------------------------------------------


def test_extensions(
    tclient: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    exam = create_exam(tclient)
    post(tclient, f"/examens/{exam.id}/publiceren")
    url = f"/examens/{exam.id}/verlengingen"
    assert (
        "error=student_number_invalid"
        in post(tclient, url, {"student_number": "12", "ends_at": "2026-11-13T13:00"}).headers[
            "location"
        ]
    )
    assert (
        "error=extension_not_later"
        in post(tclient, url, {"student_number": "123456", "ends_at": "2026-11-13T11:30"}).headers[
            "location"
        ]
    )
    assert (
        "ok=extension"
        in post(tclient, url, {"student_number": "123456", "ends_at": "2026-11-13T12:30"}).headers[
            "location"
        ]
    )

    fresh = db.get(Exam, exam.id)
    assert fresh is not None
    assert services.exams.effective_end(db, fresh) == datetime(2026, 11, 13, 11, 30, tzinfo=UTC)
    assert services.exams.end_for_student(db, fresh, "123456") == datetime(
        2026, 11, 13, 11, 30, tzinfo=UTC
    )
    assert services.exams.end_for_student(db, fresh, "999999") == fresh.ends_at
    # After the regular end, the exam stays open until the last extension ends.
    clock.set(datetime(2026, 11, 13, 11, 15, tzinfo=UTC))
    assert services.exams.status(db, fresh) == "open"
    clock.set(datetime(2026, 11, 13, 11, 30, tzinfo=UTC))
    assert services.exams.status(db, fresh) == "closed"
    login(tclient)

    grant = events(db, "exam.extension_granted", str(exam.id))[0]
    assert grant.details["student_number"] == "123456"
    page = tclient.get(f"/examens/{exam.id}/instellingen")
    grant_id = extract(r"/verlengingen/([0-9a-f-]{36})/verwijderen", page.text)
    post(tclient, f"/examens/{exam.id}/verlengingen/{grant_id}/verwijderen")
    assert services.exams.effective_end(db, fresh) == fresh.ends_at
    assert events(db, "exam.extension_revoked", str(exam.id))


# Class lists ---------------------------------------------------------------------

CSV = (
    "Studentnummer;Voornaam;Tussenvoegsel;Achternaam;Klas\n"
    "123456;Jan;de;Vries;ICT-2A\n"
    "123457;Zoë;;Ångström;ICT-2A\n"
)


def _upload_csv(client: TestClient, data: bytes, name: str = "ICT-2A") -> Any:
    return client.post(
        "/examens/klassenlijsten/nieuw",
        data={"csrf_token": _csrf(client), "name": name},
        files={"file": ("lijst.csv", data, "text/csv")},
        follow_redirects=False,
    )


def _mapping_form(page: str, action: str, **overrides: str) -> dict[str, str]:
    values = {
        "csv_b64": extract(r'name="csv_b64" value="([^"]+)"', page),
        "delimiter": extract(r'name="delimiter" value="([^"]+)"', page),
        "name": extract(r'id="name" name="name" [^>]*value="([^"]*)"', page),
        "has_header": "true",
        "map_student_number": "0", "map_first_name": "1", "map_infix": "2",
        "map_last_name": "3", "map_class_name": "4",
        "action": action,
    }  # fmt: skip
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "encoded",
    [CSV.encode("utf-8"), b"\xef\xbb\xbf" + CSV.encode("utf-8"), CSV.encode("cp1252")],
    ids=["utf8", "utf8-bom", "cp1252"],
)
def test_class_list_import(tclient: TestClient, db: Session, encoded: bytes) -> None:
    page = _upload_csv(tclient, encoded)
    assert page.status_code == 200
    # Columns are recognised from the header.
    assert '<option value="0" selected>Studentnummer</option>' in page.text
    checked = post(tclient, "/examens/klassenlijsten/importeren", _mapping_form(page.text, "check"))
    assert "2 geldige rijen" in checked.text
    assert "Ångström" in checked.text
    saved = post(tclient, "/examens/klassenlijsten/importeren", _mapping_form(page.text, "save"))
    assert saved.status_code == 303
    detail = tclient.get(saved.headers["location"])
    assert "Zoë" in detail.text
    assert "Ångström" in detail.text
    assert events(db, "class_list.imported")[0].details["rows"] == 2


def test_class_list_import_comma_and_errors(tclient: TestClient, db: Session) -> None:
    data = b"nr,voornaam,achternaam\n123456,Jan,Vries\n123456,Piet,Jansen\n"
    page = _upload_csv(tclient, data)
    assert 'name="delimiter" value=","' in page.text
    mapping = {"map_student_number": "0", "map_first_name": "1", "map_infix": "",
               "map_last_name": "2", "map_class_name": ""}  # fmt: skip
    checked = post(
        tclient, "/examens/klassenlijsten/importeren", _mapping_form(page.text, "check", **mapping)
    )
    assert "Dubbel studentnummer" in checked.text
    assert "Klassenlijst opslaan" not in checked.text
    saved = post(
        tclient, "/examens/klassenlijsten/importeren", _mapping_form(page.text, "save", **mapping)
    )
    assert saved.status_code == 400
    assert "Herstel eerst de fouten" in saved.text


def test_roster_snapshot_and_privacy(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    page = _upload_csv(tclient, CSV.encode())
    saved = post(tclient, "/examens/klassenlijsten/importeren", _mapping_form(page.text, "save"))
    class_list_id = saved.headers["location"].rsplit("/", 1)[1]
    exam = create_exam(tclient, class_list_id=class_list_id)
    roster = list(db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id)))
    assert {r.student_number for r in roster} == {"123456", "123457"}

    # The library list can be deleted; the exam keeps its snapshot.
    post(tclient, f"/examens/klassenlijsten/{class_list_id}/verwijderen")
    db.expire_all()
    assert db.scalars(select(ExamRosterEntry).where(ExamRosterEntry.exam_id == exam.id)).all()

    # Presentation mode shows the counter but never names.
    present = tclient.get(f"/examens/{exam.id}/presentatie")
    assert "0 van 2 ingeleverd" in present.text
    assert "Vries" not in present.text
    counter = tclient.get(f"/examens/{exam.id}/presentatie/teller")
    assert "0 van 2 ingeleverd" in counter.text

    other = other_client(tclient, db, clock, "ander@school.nl")
    assert other.get(f"/examens/klassenlijsten/{class_list_id}").status_code == 404


def test_set_roster_on_existing_exam(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    page = _upload_csv(tclient, CSV.encode())
    saved = post(tclient, "/examens/klassenlijsten/importeren", _mapping_form(page.text, "save"))
    class_list_id = saved.headers["location"].rsplit("/", 1)[1]
    post(tclient, f"/examens/{exam.id}/klassenlijst", {"class_list_id": class_list_id})
    assert events(db, "exam.roster_set", str(exam.id))[-1].details["entries"] == 2
    post(tclient, f"/examens/{exam.id}/klassenlijst", {"class_list_id": ""})
    db.expire_all()
    assert not db.scalars(select(ExamRosterEntry)).all()


# QR and presentation -------------------------------------------------------------


def test_qr_downloads(tclient: TestClient) -> None:
    exam = create_exam(tclient)
    png = tclient.get(f"/examens/{exam.id}/qr.png")
    assert png.content.startswith(b"\x89PNG")
    assert png.headers["content-disposition"].startswith(
        'attachment; filename="examen-linux-p3-qr.png"'
    )
    svg = tclient.get(f"/examens/{exam.id}/qr.svg")
    assert svg.content.startswith(b"<svg")
    assert b"<script" not in svg.content


def test_presentation_page(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    page = tclient.get(f"/examens/{exam.id}/presentatie")
    assert page.status_code == 200
    assert exam.access_code in page.text  # type: ignore[operator]
    assert "testserver/examen-linux-p3" in page.text
    assert 'hx-trigger="every 10s"' in page.text
    assert "0 ingeleverd" in page.text
    assert "<script>" not in page.text  # no inline scripts (CSP)


# IP presets ----------------------------------------------------------------------


def test_ip_presets(
    client: TestClient, db: Session, clock: FrozenClock, services: Services
) -> None:
    admin = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    login(client, "admin@school.nl", secret=secret, clock=clock)
    assert (
        post(
            client,
            "/admin/ip-presets",
            {"name": "Schoolnetwerk", "cidrs": "10.20.0.0/16\n10.30.0.1"},
        ).status_code
        == 303
    )
    assert (
        post(client, "/admin/ip-presets", {"name": "Fout", "cidrs": "geen-ip"}).status_code == 400
    )
    assert (
        post(
            client, "/admin/ip-presets", {"name": "Schoolnetwerk", "cidrs": "10.0.0.0/8"}
        ).status_code
        == 400
    )
    preset = db.scalar(select(IpPreset))
    assert preset is not None
    preset_id = preset.id
    assert preset.cidrs == ["10.20.0.0/16", "10.30.0.1/32"]

    exam = create_exam(
        client,
        ip_restriction_enabled="true",
        ip_preset_id=str(preset.id),
        ip_cidrs="192.168.1.0/24",
    )
    fresh = db.get(Exam, exam.id)
    assert fresh is not None
    assert fresh.ip_cidrs == ["192.168.1.0/24", "10.20.0.0/16", "10.30.0.1/32"]

    post(client, f"/admin/ip-presets/{preset_id}/verwijderen")
    db.expire_all()
    assert db.get(IpPreset, preset_id) is None
    assert db.get(Exam, exam.id).ip_cidrs  # type: ignore[union-attr]  # copied, not linked
    assert len(events(db, "ip_preset.created")) == 1
    assert len(events(db, "ip_preset.deleted")) == 1


def test_teacher_cannot_manage_ip_presets(tclient: TestClient) -> None:
    assert tclient.get("/admin/ip-presets").status_code == 403
    assert (
        post(tclient, "/admin/ip-presets", {"name": "x", "cidrs": "10.0.0.0/8"}).status_code == 403
    )


# Dashboard -----------------------------------------------------------------------


def test_dashboard_lists_own_exams(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    create_exam(tclient)
    page = tclient.get("/examens")
    assert "Examen Linux P3" in page.text
    assert "Concept" in page.text
    other = other_client(tclient, db, clock, "ander@school.nl")
    assert "Examen Linux P3" not in other.get("/examens").text


def test_service_create_without_http(db: Session, services: Services, teacher: User) -> None:
    """The service can be used directly (CLI, demo-seed)."""
    data = ExamInput(
        name="Direct",
        description=None,
        starts_at=T0,
        ends_at=T0 + timedelta(hours=1),
        extensions=["pdf"],
        slots=[SlotInput(label="Werk")],
    )
    exam = services.exams.create(db, teacher, data, ip=None)
    assert exam.slug == "direct"
    assert services.exams.status(db, exam) == "concept"


# Beta feedback (v0.1.1) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("10:00", "10:00"), ("9:30", "09:30"), ("09.30", "09:30"), ("1430", "14:30")],
)
def test_date_and_24_hour_time_fields(
    tclient: TestClient, db: Session, typed: str, stored: str
) -> None:
    data = form(starts_at_date="2026-11-13", starts_at_time=typed,
                ends_at_date="2026-11-13", ends_at_time="18:00")  # fmt: skip
    del data["starts_at"], data["ends_at"]
    exam = create_exam(tclient, **{k: v for k, v in data.items() if k not in form()} | data)
    hour, minute = map(int, stored.split(":"))
    assert exam.starts_at == datetime(2026, 11, 13, hour - 1, minute, tzinfo=UTC)


@pytest.mark.parametrize("typed", ["2:30 PM", "24:00", "10:75", "", "abc"])
def test_invalid_times_are_refused(tclient: TestClient, typed: str) -> None:
    data = form(starts_at_date="2026-11-13", starts_at_time=typed,
                ends_at_date="2026-11-13", ends_at_time="18:00")  # fmt: skip
    del data["starts_at"], data["ends_at"]
    response = post(tclient, "/examens/nieuw", data)
    assert response.status_code == 400
    assert "Vul een geldige datum en tijd in." in response.text
    assert f'name="starts_at_time" type="text" required value="{typed}"' in response.text


def test_extension_with_split_fields(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    url = f"/examens/{exam.id}/verlengingen"
    page = tclient.get(f"/examens/{exam.id}/instellingen").text
    assert 'name="ends_at_time" type="text" required value="12:00"' in page  # prefilled
    assert "<h2>Verlengingen</h2>" in page
    assert "Nog geen verlengingen gegeven." in page
    response = post(tclient, url, {"student_number": "123456", "ends_at_date": "2026-11-13",
                                   "ends_at_time": "13:15"})  # fmt: skip
    assert response.headers["location"].endswith("ok=extension#verlengingen")
    assert events(db, "exam.extension_granted")


def test_allow_unlisted_forced_without_class_list(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient, allow_unlisted="")
    db.expire_all()
    fresh = db.get(Exam, exam.id)
    assert fresh is not None and fresh.allow_unlisted is True
    page = tclient.get(f"/examens/{exam.id}/bewerken").text
    assert 'id="allow_unlisted" name="allow_unlisted" value="true"' in page
    assert "checked disabled" in page
    # Editing (the disabled box is not submitted) keeps it on.
    post(tclient, f"/examens/{exam.id}/bewerken", form(slug=exam.slug, allow_unlisted=""))
    db.expire_all()
    assert db.get(Exam, exam.id).allow_unlisted is True  # type: ignore[union-attr]


def test_settings_show_allowed_extensions(tclient: TestClient) -> None:
    exam = create_exam(tclient)
    page = tclient.get(f"/examens/{exam.id}/instellingen").text
    assert "Toegestane extensies" in page
    assert "Alle toegestane extensies" in page  # slot 2 has no own extensions


def test_footer_shows_version(tclient: TestClient) -> None:
    from inleverboks import __version__  # noqa: PLC0415

    assert f"v{__version__}</span>" in tclient.get("/examens").text


def test_template_editor_create_edit_and_use(
    tclient: TestClient, db: Session, services: Services
) -> None:
    page = tclient.get("/examens/sjablonen/nieuw").text
    assert 'class="mode-template"' in page
    assert "Je bewerkt een sjabloon, geen examen." in page
    assert 'name="starts_at_date"' not in page and 'name="slug"' not in page
    data = form(name="Python P1", slot_count="1", slot_label_0="Code", slot_pattern_0="*.py",
                slot_extensions_0="py", extensions="py, md")  # fmt: skip
    response = post(tclient, "/examens/sjablonen/nieuw", data)
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    assert response.headers["location"] == f"/examens/sjablonen/{template.id}?ok=created"
    assert template.config["extensions"] == ["py", "md"]
    assert events(db, "exam_template.created")

    # Template file on the template's own page.
    slot_id = template.slots[0].id
    upload = tclient.post(
        f"/examens/sjablonen/{template.id}/vakjes/{slot_id}/sjabloon",
        data={"csrf_token": csrf_token(tclient)},
        files={"file": ("leeg.py", b"# leeg\n", "text/x-python")}, follow_redirects=False,
    )  # fmt: skip
    assert upload.headers["location"].endswith("ok=template_uploaded")
    detail = tclient.get(f"/examens/sjablonen/{template.id}").text
    assert "leeg.py" in detail and "Nieuw examen met dit sjabloon" in detail

    # Edit: add a slot before the existing one; the file stays with its slot.
    edit = tclient.get(f"/examens/sjablonen/{template.id}/bewerken").text
    assert f'name="slot_id_0" value="{slot_id}"' in edit
    post(tclient, f"/examens/sjablonen/{template.id}/bewerken", form(
        name="Python P1 (2026)", slot_count="2", slot_label_0="Uitleg", slot_pattern_0="*.md",
        slot_extensions_0="md", slot_id_0="", slot_label_1="Code", slot_pattern_1="*.py",
        slot_extensions_1="py", slot_id_1=str(slot_id), extensions="py, md"))  # fmt: skip
    db.expire_all()
    template = db.get(ExamTemplate, template.id)
    assert template is not None
    assert template.name == "Python P1 (2026)"
    assert [s.label for s in template.slots] == ["Uitleg", "Code"]
    assert template.slots[1].template_file is not None
    assert (
        events(db, "exam_template.updated")[0].details["changes"]["name"]["new"]
        == "Python P1 (2026)"
    )

    # New exam: the picker is at the top; the expected files come from the template
    # (read-only), so removing one in the POST has no effect. The file follows its slot.
    new = tclient.get(f"/examens/nieuw?sjabloon={template.id}").text
    assert 'id="sjabloon" name="sjabloon"' in new
    assert "Gebaseerd op sjabloon Python P1 (2026)." in new
    assert 'name="slot_label_0"' not in new
    slot_ids = [str(s.id) for s in template.slots]
    response = post(tclient, "/examens/nieuw", form(
        name="Python P1 herkansing", source_template_id=str(template.id), slot_count="1",
        slot_label_0="Code", slot_pattern_0="*.py", slot_extensions_0="py",
        slot_id_0=slot_ids[1], extensions="py, md"))  # fmt: skip
    exam = db.get(Exam, uuid.UUID(response.headers["location"].split("/")[2]))
    assert exam is not None
    assert exam.source_template_id == template.id
    assert [s.label for s in exam.expected_files] == ["Uitleg", "Code"]
    assert exam.expected_files[1].template_file is not None
    assert exam.expected_files[1].template_file.original_name == "leeg.py"


def test_template_edit_removes_slot_file(tclient: TestClient, db: Session) -> None:
    post(tclient, "/examens/sjablonen/nieuw", form(name="T", slot_count="1", slot_label_0="A",
         slot_pattern_0="", slot_extensions_0=""))  # fmt: skip
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    tclient.post(
        f"/examens/sjablonen/{template.id}/vakjes/{template.slots[0].id}/sjabloon",
        data={"csrf_token": csrf_token(tclient)}, files={"file": ("a.txt", b"a", "text/plain")},
    )  # fmt: skip
    post(tclient, f"/examens/sjablonen/{template.id}/bewerken",
         form(name="T", slot_count="0", allow_extra_file="true"))  # fmt: skip
    db.expire_all()
    assert not db.scalars(select(TemplateFile)).all()


def test_template_validation_and_privacy(
    tclient: TestClient, db: Session, clock: FrozenClock
) -> None:
    response = post(tclient, "/examens/sjablonen/nieuw", form(name="", extensions=""))
    assert response.status_code == 400
    assert 'aria-invalid="true"' in response.text
    post(tclient, "/examens/sjablonen/nieuw", form(name="Privé"))
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    other = other_client(tclient, db, clock, "ander@school.nl")
    for url in (f"/examens/sjablonen/{template.id}", f"/examens/sjablonen/{template.id}/bewerken"):
        assert other.get(url).status_code == 404
    assert (
        post(other, f"/examens/sjablonen/{template.id}/bewerken", form(name="x")).status_code == 404
    )


def test_save_as_template_opens_the_template(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    response = post(tclient, f"/examens/{exam.id}/sjabloon-opslaan", {"name": "Kopie"})
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    assert response.headers["location"] == f"/examens/sjablonen/{template.id}?ok=saved_from_exam"
    overview = tclient.get("/examens/sjablonen").text
    assert "badge-template" in overview and "Sjabloon uit crebo" in overview


# Old /docent URLs --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/docent", "/examens"),
        ("/docent/", "/examens"),
        ("/docent/gedeeld", "/examens/gedeeld"),
        ("/docent/sjablonen/nieuw", "/examens/sjablonen/nieuw"),
        ("/docent/klassenlijsten?x=1", "/examens/klassenlijsten?x=1"),
        ("/docent/examens/nieuw?sjabloon=abc&wp=B1", "/examens/nieuw?sjabloon=abc&wp=B1"),
        ("/docent/ontvangstbewijs", "/admin/ontvangstbewijs"),
    ],
)
def test_old_teacher_urls_redirect_permanently(client: TestClient, old: str, new: str) -> None:
    response = client.get(old, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (301, new)


def test_old_exam_urls_keep_working(tclient: TestClient) -> None:
    exam = create_exam(tclient)
    old = tclient.get(f"/docent/examens/{exam.id}/instellingen?ok=saved", follow_redirects=False)
    assert old.status_code == 301
    assert old.headers["location"] == f"/examens/{exam.id}/instellingen?ok=saved"
    assert "Opgeslagen." in tclient.get(f"/docent/examens/{exam.id}/instellingen?ok=saved").text
    # Other methods keep their method and body (308).
    response = post(tclient, f"/docent/examens/{exam.id}/code")
    assert (response.status_code, response.headers["location"]) == (
        308, f"/examens/{exam.id}/code",
    )  # fmt: skip
    assert post(tclient, response.headers["location"]).status_code == 303


def test_examens_is_a_reserved_link_name(tclient: TestClient) -> None:
    response = post(tclient, "/examens/nieuw", form(slug="examens"))
    assert response.status_code == 400
    assert "Deze linknaam is gereserveerd" in response.text


# Advanced tab ------------------------------------------------------------------------


def test_advanced_tab_holds_the_rare_actions(tclient: TestClient, db: Session) -> None:
    exam = create_exam(tclient)
    settings = tclient.get(f"/examens/{exam.id}/instellingen").text
    advanced = tclient.get(f"/examens/{exam.id}/geavanceerd")
    assert advanced.status_code == 200
    assert f'href="/examens/{exam.id}/geavanceerd" aria-current="page">Geavanceerd' in advanced.text
    for action in ("dupliceren", "verwijderen", "sjabloon-opslaan"):
        assert f"/examens/{exam.id}/{action}" in advanced.text
        assert f"/examens/{exam.id}/{action}" not in settings
    assert 'id="bewaartermijn"' in advanced.text
    assert f"/examens/{exam.id}/bewaren" in advanced.text
    overview = tclient.get(f"/examens/{exam.id}").text
    assert 'id="bewaartermijn"' not in overview
    assert f"/examens/{exam.id}/bewaren" not in overview
    # Ownership is only for admins.
    assert f"/examens/{exam.id}/eigenaar" not in advanced.text
    # Back to concept and its error land on the advanced tab.
    post(tclient, f"/examens/{exam.id}/publiceren")
    response = post(tclient, f"/examens/{exam.id}/concept")
    assert response.headers["location"] == f"/examens/{exam.id}/geavanceerd?ok=unpublished"
    post(tclient, f"/examens/{exam.id}/publiceren")
    response = post(tclient, f"/examens/{exam.id}/verwijderen")
    assert response.headers["location"] == f"/examens/{exam.id}/geavanceerd?error=not_concept"


def test_advanced_tab_permissions(tclient: TestClient, db: Session, clock: FrozenClock) -> None:
    exam = create_exam(tclient)
    other = other_client(tclient, db, clock, "ander@school.nl")
    assert other.get(f"/examens/{exam.id}/geavanceerd").status_code == 404  # no access
    reviewer = create_user(db, clock, "tweede@school.nl", name="Tweede")
    db.expire_all()
    stored = db.get(Exam, exam.id)
    assert stored is not None
    stored.second_reviewer_id = reviewer.id
    db.commit()
    rclient = TestClient(tclient.app)
    login(rclient, "tweede@school.nl")
    page = rclient.get(f"/examens/{exam.id}/geavanceerd").text
    assert "Alleen de eigenaar van het examen kan deze acties gebruiken." in page
    assert f"/examens/{exam.id}/dupliceren" not in page
    assert f"/examens/{exam.id}/bewaren" not in page


def test_delete_concept_made_from_template_after_unpublish(
    tclient: TestClient, db: Session, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proces-verbaal downloaded while published goes with the concept (it used to make
    the delete fail on its foreign key)."""
    monkeypatch.setattr(tclient.app.state.pdf, "render", lambda *_args: b"%PDF-1.7 test")  # type: ignore[attr-defined]
    exam = create_exam(tclient, access_code_enabled="", allow_unlisted="")
    ids = _slot_ids(db, exam)
    _upload(tclient, exam, ids[0], b"sjabloon", "leeg.docx")
    post(tclient, f"/examens/{exam.id}/sjabloon-opslaan", {"name": "Linux P3"})
    template = db.scalar(select(ExamTemplate))
    assert template is not None
    response = post(
        tclient, "/examens/nieuw",
        form(name="Linux P3 vanuit sjabloon", access_code_enabled="", allow_unlisted="",
             source_template_id=str(template.id)),
    )  # fmt: skip
    assert response.status_code == 303, response.text[:500]
    new_id = uuid.UUID(response.headers["location"].split("/")[2])
    assert post(tclient, f"/examens/{new_id}/publiceren").status_code == 303
    assert post(tclient, f"/examens/{new_id}/proces-verbaal").status_code == 200
    pv = db.scalar(select(ProcesVerbaal).where(ProcesVerbaal.exam_id == new_id))
    assert pv is not None
    pv_key, document_id = pv.storage_key, pv.document_id
    assert _exists(services, pv_key)
    assert post(tclient, f"/examens/{new_id}/concept").status_code == 303

    response = post(tclient, f"/examens/{new_id}/verwijderen")

    assert response.status_code == 303
    assert response.headers["location"] == "/examens"
    db.expire_all()
    assert db.get(Exam, new_id) is None
    assert db.scalar(select(ProcesVerbaal)) is None
    assert not _exists(services, pv_key)
    deleted = events(db, "exam.deleted", str(new_id))[0]
    assert deleted.details["proces_verbaal"] == [document_id]
    template_key = db.scalars(select(TemplateFile.storage_key)).first()
    assert template_key is not None and _exists(services, template_key)  # still in use
