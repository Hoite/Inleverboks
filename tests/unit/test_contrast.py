from __future__ import annotations

import pytest

from inleverboks.services.branding import Branding, contrast_ratio, normalize_color, validate


@pytest.mark.parametrize(
    ("fg", "bg", "expected"),
    [
        ("#000000", "#ffffff", 21.0),
        ("#ffffff", "#ffffff", 1.0),
        ("#005381", "#ffffff", 8.22),  # brand blue (PLAN §2a: 8.2:1)
        ("#e73443", "#ffffff", 4.23),  # brand red: only large text and non-text
        ("#313131", "#ffffff", 13.0),
        ("#767676", "#ffffff", 4.54),  # the classic "lightest AA grey"
    ],
)
def test_ratio(fg: str, bg: str, expected: float) -> None:
    assert contrast_ratio(fg, bg) == pytest.approx(expected, abs=0.01)
    assert contrast_ratio(bg, fg) == pytest.approx(expected, abs=0.01)


def test_defaults_pass() -> None:
    assert Branding().contrast_problems() == []


def test_failing_pairs() -> None:
    brand = Branding(colors={**Branding().colors, "primary": "#7fb2e5"})
    assert {r.pair.key for r in brand.contrast_problems()} == {
        "links",
        "header_buttons",
        "table_headers",
    }


def test_version_changes_with_content() -> None:
    assert Branding().version == Branding().version
    assert Branding(app_name="X").version != Branding().version


def test_validation_and_normalize() -> None:
    assert normalize_color(" #ABC ") == "#aabbcc"
    assert validate("Inleverboks", dict(Branding().colors), "poppins") == {}
    assert validate("x" * 61, {}, "nope").keys() >= {"app_name", "font", "primary", "bg"}


def test_css_never_contains_unvalidated_values() -> None:
    from inleverboks.services.branding import _from_stored  # noqa: PLC0415

    brand = _from_stored(
        {"colors": {"primary": "red;} body{display:none", "nope": "#000000"}, "font": "x"}
    )
    assert "--color-primary: #005381;" in brand.css()
    assert "nope" not in brand.css()
    assert brand.font == "poppins"
