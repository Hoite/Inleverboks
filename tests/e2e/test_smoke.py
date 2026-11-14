from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_start_page_renders_without_csp_errors(page: Page, live_server: str) -> None:
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto(live_server + "/")
    expect(page.get_by_role("heading", name="Examenwerk inleveren")).to_be_visible()
    expect(page.locator("html")).to_have_attribute("lang", "nl")
    assert errors == []


def test_skip_link_is_first_focusable(page: Page, live_server: str) -> None:
    page.goto(live_server + "/")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Naar de inhoud")).to_be_focused()
