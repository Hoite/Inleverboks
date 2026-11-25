"""End-to-end: teacher pages with JavaScript (exam form, searchable selects, header)."""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import select

from inleverboks.db.models import ClassList, ClassListEntry, User, WhitelistEntry
from inleverboks.services.passwords import hash_password
from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e

PASSWORD = "Correct-Horse-Battery-42"


def seed_teacher(server: LiveServer) -> str:
    """A teacher with colleagues (for the second reviewer) and two class lists."""
    services = server.app.state.services
    email = f"docent-{uuid.uuid4().hex[:6]}@school.nl"
    with server.app.state.sessionmaker() as db:
        now = services.clock.now()
        if db.scalar(select(WhitelistEntry).where(WhitelistEntry.value == "school.nl")) is None:
            db.add(WhitelistEntry(kind="domain", value="school.nl", created_at=now))
        teacher = User(email=email, name="Docent E2E", password_hash=hash_password(PASSWORD),
                       created_at=now)  # fmt: skip
        db.add(teacher)
        for name in ("Ångström Jansen", "Bram de Boer", "Chantal Visser"):
            local = name.lower().replace(" ", ".").replace("å", "a")
            db.add(User(email=f"{local}-{uuid.uuid4().hex[:4]}@school.nl", name=name,
                        password_hash=hash_password(PASSWORD), created_at=now))  # fmt: skip
        db.flush()
        for list_name in ("ICT-2A", "ICT-3B"):
            class_list = ClassList(owner_id=teacher.id, name=list_name, created_at=now)
            class_list.entries.append(
                ClassListEntry(student_number="123456", first_name="Jan", last_name="Vries")
            )
            db.add(class_list)
        db.commit()
    return email


def login(page: Page, server: LiveServer, email: str) -> None:
    page.goto(server + "/login")
    page.get_by_label("E-mailadres").fill(email)
    page.get_by_label("Wachtwoord").fill(PASSWORD)
    page.get_by_role("button", name="Inloggen").click()
    expect(page).to_have_url(server + "/examens")


def test_searchable_selects(page: Page, live_server: LiveServer) -> None:
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    login(page, live_server, seed_teacher(live_server))
    page.goto(live_server + "/examens/nieuw")

    reviewer = page.get_by_role("combobox", name="Tweede beoordelaar (optioneel)")
    expect(reviewer).to_be_visible()
    expect(page.locator("select[name=second_reviewer_id]")).to_be_hidden()
    # Typing filters on name and e-mail, ignoring case and accents.
    reviewer.fill("angstrom")
    listbox = page.get_by_role("listbox")
    expect(listbox.get_by_role("option")).to_have_count(1)
    expect(listbox.get_by_role("option").first).to_contain_text("Ångström Jansen")
    reviewer.fill("bram.de")  # part of the e-mail address
    expect(listbox.get_by_role("option")).to_have_count(1)
    reviewer.press("Enter")
    expect(reviewer).to_have_attribute("aria-expanded", "false")
    selected = page.locator("select[name=second_reviewer_id] option:checked")
    expect(selected).to_contain_text("Bram de Boer")
    # No results, then Escape restores the chosen value.
    reviewer.fill("zzz")
    expect(page.locator(".combobox-empty").first).to_have_text("Geen resultaten")
    expect(page.locator(".combobox-empty").first).to_be_visible()
    expect(reviewer).to_have_attribute("aria-expanded", "false")
    reviewer.press("Escape")
    expect(reviewer).to_have_value(selected.inner_text())
    # Arrow keys move through the list; aria-activedescendant follows.
    reviewer.fill("")
    reviewer.press("ArrowDown")
    expect(listbox).to_be_visible()
    reviewer.press("ArrowDown")
    active = reviewer.get_attribute("aria-activedescendant")
    assert active
    expect(page.locator(f"#{active}")).to_have_class("combobox-option is-active")
    reviewer.press("Escape")

    # Class list: choosing one through the combobox keeps exam_form.js in sync.
    class_list = page.get_by_role("combobox", name="Klassenlijst (optioneel)")
    allow = page.get_by_label("Studenten toestaan die niet op de klassenlijst staan")
    expect(allow).to_be_disabled()
    class_list.fill("3b")
    class_list.press("Enter")
    expect(allow).to_be_enabled()
    class_list.fill("")
    class_list.blur()  # emptied: back to "no class list"
    expect(page.locator("select[name=class_list_id]")).to_have_value("")
    expect(allow).to_be_disabled()
    expect(allow).to_be_checked()

    # The form posts the select's value: the server stores the chosen reviewer.
    page.get_by_label("Naam", exact=True).fill("Combobox-examen")
    page.get_by_label("Label van bestand 1").fill("Verslag")
    page.get_by_role("button", name="Opslaan").last.click()
    expect(page).to_have_url(re.compile(r"/examens/[0-9a-f-]+/instellingen$"))
    expect(page.locator("dl.details")).to_contain_text("Bram de Boer")
    assert errors == []


def test_exam_form_start_moves_end_and_link_name(page: Page, live_server: LiveServer) -> None:
    login(page, live_server, seed_teacher(live_server))
    page.goto(live_server + "/examens/nieuw")
    start_date, start_time = page.locator("#starts_at_date"), page.locator("#starts_at_time")
    end_date, end_time = page.locator("#ends_at_date"), page.locator("#ends_at_time")
    expect(start_time).to_have_value("09:00")
    expect(end_time).to_have_value("17:00")
    # A later start moves the end along (same duration), also past midnight.
    start_time.fill("20:30")
    start_time.dispatch_event("change")
    expect(end_time).to_have_value("04:30")
    start_date.fill("2027-03-01")
    start_date.dispatch_event("change")
    expect(end_date).to_have_value("2027-03-02")
    # A changed end sets a new duration.
    end_date.fill("2027-03-01")
    end_time.fill("22:00")
    start_time.fill("09:00")
    start_time.dispatch_event("change")
    expect(end_time).to_have_value("10:30")
    expect(end_date).to_have_value("2027-03-01")

    # A suggested link name with a date follows the start date, until the teacher types one.
    slug = page.locator("#slug")
    suggested = page.locator("input[name=suggested_slug]")
    page.evaluate(
        """() => {
            document.getElementById('slug').value = 'examen-01032027';
            document.querySelector('input[name=suggested_slug]').value = 'examen-01032027';
        }"""
    )
    start_date.fill("2027-03-04")
    start_date.dispatch_event("change")
    expect(slug).to_have_value("examen-04032027")
    expect(suggested).to_have_value("examen-04032027")
    slug.fill("mijn-eigen-link")
    start_date.fill("2027-03-05")
    start_date.dispatch_event("change")
    expect(slug).to_have_value("mijn-eigen-link")


def test_link_prefix_fits(page: Page, live_server: LiveServer) -> None:
    login(page, live_server, seed_teacher(live_server))
    page.goto(live_server + "/examens/nieuw")
    prefix, slug = page.locator(".input-prefix .prefix-full"), page.locator("#slug")
    expect(prefix).to_be_visible()
    # The full base URL fits: as wide as its text, and the input stays usable.
    assert prefix.evaluate("el => el.scrollWidth <= el.clientWidth")
    for width in (1024, 375):
        page.set_viewport_size({"width": width, "height": 800})
        box = page.locator(".input-prefix").bounding_box()
        input_box = slug.bounding_box()
        assert box and input_box
        assert input_box["width"] >= 150  # never squeezed away
        assert input_box["x"] + input_box["width"] <= box["x"] + box["width"] + 1


def test_account_menu(page: Page, live_server: LiveServer) -> None:
    login(page, live_server, seed_teacher(live_server))
    menu = page.locator("details.account-menu")
    summary = menu.locator("summary")
    expect(summary).to_contain_text("Docent E2E")
    summary.click()
    expect(menu.get_by_role("link", name="Account")).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).not_to_have_attribute("open", "")
    expect(summary).to_be_focused()
    summary.click()
    page.locator("h1").click()  # a click elsewhere closes it
    expect(menu.get_by_role("link", name="Account")).to_be_hidden()
    summary.click()
    menu.get_by_role("button", name="Uitloggen").click()
    expect(page).to_have_url(live_server + "/login")
