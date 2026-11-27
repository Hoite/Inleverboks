"""Basic WCAG checks on every page type, rendered server-side.

Not a replacement for a manual check with a screen reader, but it catches the common
regressions: a missing label, alt text, page language or heading, an unnamed button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import Exam, User
from inleverboks.services.audit import Actor
from inleverboks.services.classlists import ImportResult, ParsedEntry
from inleverboks.services.container import Services
from tests.conftest import create_user, enable_totp, login
from tests.integration.test_qualifications import record
from tests.student_helpers import add_roster, form, good_files, ready, upload

LABELLED_INPUTS = {"text", "email", "password", "number", "file", "url", "search", "tel",
                   "datetime-local", "date", "time", "color", "checkbox", "radio"}  # fmt: skip


@dataclass
class _Field:
    tag: str
    attrs: dict[str, str]
    in_label: bool


@dataclass
class Page(HTMLParser):
    lang: str | None = None
    h1: int = 0
    fields: list[_Field] = field(default_factory=list)
    label_for: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    _label_depth: int = 0
    _button: dict[str, str] | None = None
    _button_text: str = ""

    def __post_init__(self) -> None:
        super().__init__()

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: v or "" for k, v in attrs_list}
        if tag == "html":
            self.lang = attrs.get("lang")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "label":
            self._label_depth += 1
            if "for" in attrs:
                self.label_for.add(attrs["for"])
        elif tag == "img" and "alt" not in attrs:
            self.problems.append(f"img without alt: {attrs.get('src')}")
        elif tag in {"input", "select", "textarea"}:
            self.fields.append(_Field(tag, attrs, self._label_depth > 0))
        elif tag == "button":
            self._button, self._button_text = attrs, ""
        if attrs.get("tabindex", "0").lstrip("-").isdigit() and int(attrs.get("tabindex", "0")) > 0:
            self.problems.append(f"positive tabindex on <{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._label_depth -= 1
        elif tag == "button" and self._button is not None:
            if not self._button_text.strip() and not self._button.get("aria-label"):
                self.problems.append(f"button without a name: {self._button}")
            self._button = None

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button_text += data

    def check(self) -> list[str]:
        problems = list(self.problems)
        if self.lang != "nl":
            problems.append(f"html lang is {self.lang!r}")
        if self.h1 != 1:
            problems.append(f"{self.h1} h1 elements")
        for f in self.fields:
            kind = f.attrs.get("type", "text") if f.tag == "input" else f.tag
            if f.tag == "input" and kind not in LABELLED_INPUTS:
                continue
            if f.attrs.get("aria-hidden") == "true" or "aria-label" in f.attrs:
                continue
            if f.in_label or f.attrs.get("id") in self.label_for or "aria-labelledby" in f.attrs:
                continue
            problems.append(f"<{f.tag} name={f.attrs.get('name')!r}> has no label")
        return problems


def audit(client: TestClient, url: str) -> list[str]:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    page = Page()
    page.feed(response.text)
    return [f"{url}: {problem}" for problem in page.check()]


def test_public_and_student_pages(client: TestClient, db: Session, exam: Exam) -> None:
    add_roster(db, exam)
    problems = audit(client, "/login") + audit(client, f"/{exam.slug}")
    form(client, f"/{exam.slug}/code", {"code": exam.access_code or ""})
    problems += audit(client, f"/{exam.slug}")  # student number
    form(client, f"/{exam.slug}/student", {"student_number": "123456"})
    problems += audit(client, f"/{exam.slug}")  # confirm identity
    student = TestClient(client.app)
    ready(student, exam, "999999")
    problems += audit(student, f"/{exam.slug}")  # upload page (name step done)
    assert upload(student, exam, good_files(exam)).status_code == 200
    assert problems == []


def test_teacher_and_admin_pages(
    client: TestClient, db: Session, clock: FrozenClock, services: Services, exam: Exam, owner: User
) -> None:
    add_roster(db, exam)
    student = TestClient(client.app)
    ready(student, exam)
    upload(student, exam, good_files(exam))
    teacher = TestClient(client.app)
    login(teacher)
    template = services.exams.save_as_template(db, owner, exam, "Sjabloon", ip=None)
    services.qualifications.import_records(
        db, [record()], actor=Actor.system(), ip=None, source="t"
    )
    qualification = services.qualifications.search(db, "27016")[0]
    crebo_template = services.exams.templates_from_qualification(
        db, owner, qualification, ["B1-K1"], team_id=None, ip=None
    )[0]
    admin = create_user(db, clock, "admin@school.nl", admin=True)
    secret = enable_totp(db, services, admin)
    team = services.teams.create(db, admin, "ICT", "Applicatieontwikkeling", ip=None)
    services.teams.add_member(db, admin, team, owner.id, ip=None)
    services.teams.add_member(db, admin, team, admin.id, contact=True, ip=None)
    services.teams.add_crebo(db, admin, team, "27016", ip=None)
    class_list = services.classlists.save(
        db, admin, team.id, "ICT-2A", ImportResult(entries=[ParsedEntry(
            row_number=2, student_number="123456", first_name="Jan", infix="de",
            last_name="Vries", class_name="ICT-2A",
        )]), ip=None,
    )  # fmt: skip
    urls = [
        "/examens", "/examens/afgerond", "/examens/sjablonen", "/examens/nieuw",
        f"/examens/{exam.id}", f"/examens/{exam.id}/instellingen",
        f"/examens/{exam.id}/bewerken", f"/examens/{exam.id}/auditlog",
        f"/examens/{exam.id}/geavanceerd",
        f"/examens/{exam.id}/studenten/123456",
        "/examens/klassenlijsten", "/account",
        "/account/wachtwoord", "/account/tweestaps", "/examens/sjablonen/nieuw",
        f"/examens/sjablonen/{template.id}", f"/examens/sjablonen/{template.id}/bewerken",
        f"/examens/nieuw?sjabloon={template.id}",
        "/examens/sjablonen/crebo?q=27016", f"/examens/sjablonen/crebo/{qualification.id}",
        f"/examens/sjablonen/{crebo_template.id}",
        f"/examens/nieuw?sjabloon={crebo_template.id}&wp=B1-K1-W3",
        f"/examens/{exam.id}/herkansing",
        "/examens/gedeeld", "/examens/klassenlijsten?toon=alle",
        f"/examens/klassenlijsten/{class_list.id}",
    ]  # fmt: skip
    problems = [p for url in urls for p in audit(teacher, url)]

    team_template = services.exams.copy_template(db, admin, template, ip=None, team_id=team.id)
    problems += audit(teacher, f"/examens/teams/{team.id}")
    problems += audit(teacher, f"/examens/sjablonen/{team_template.id}")
    ac = TestClient(client.app)
    login(ac, "admin@school.nl", secret=secret, clock=clock)
    admin_urls = [
        "/admin/gebruikers", "/admin/whitelist", "/admin/ip-presets", "/admin/instellingen",
        "/admin/huisstijl", "/admin/kwalificaties", "/admin/auditlog", "/examens/alle",
        "/admin/ontvangstbewijs", f"/examens/{exam.id}/geavanceerd",
        f"/examens/alle?eigenaar={owner.id}", "/examens/sjablonen?toon=alle",
        f"/examens/sjablonen/{template.id}",  # another teacher's personal template
        f"/examens/klassenlijsten/{class_list.id}", "/examens/klassenlijsten/nieuw",
        f"/examens/klassenlijsten/{class_list.id}/vervangen",
        "/admin/onderwijsteams", f"/admin/onderwijsteams/{team.id}", "/examens/team",
        f"/examens/sjablonen/crebo?team={team.id}",
        f"/examens/sjablonen/crebo/{qualification.id}?team={team.id}",
        f"/examens/sjablonen/{team_template.id}",
        "/admin/gebruikers?toon=zonder-team", f"/examens/teams/{team.id}",
    ]  # fmt: skip
    problems += [p for url in admin_urls for p in audit(ac, url)]
    assert problems == []
