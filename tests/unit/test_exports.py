from __future__ import annotations

import uuid

import pytest

from inleverboks.db.models import Submission
from inleverboks.services.exports import (
    csv_safe,
    student_folder,
    unique_name,
    windows_safe,
    write_csv,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('Verslag: "eind" <v2>?.docx', "Verslag eind v2.docx"),
        ("map/naam\\x.txt", "mapnaamx.txt"),
        ("eindigt met punt. ", "eindigt met punt"),
        ("...", "bestand"),
    ],
)
def test_windows_safe(raw: str, expected: str) -> None:
    assert windows_safe(raw, 150) == expected


def test_windows_safe_keeps_extension_when_shortening() -> None:
    result = windows_safe("a" * 200 + ".docx", 50)
    assert len(result) == 50
    assert result.endswith(".docx")


def _submission(**kw: object) -> Submission:
    values: dict[str, object] = {
        "id": uuid.uuid4(), "student_number": "123456", "first_name": "Jan", "infix": "de",
        "last_name": "Vries", "attempt": 1, "status": "active",
    }  # fmt: skip
    values.update(kw)
    return Submission(**values)


def test_student_folder() -> None:
    assert student_folder(_submission()) == "Vries, Jan de (123456)"
    assert (
        student_folder(_submission(infix=None, last_name="Jansen", first_name="Piet"))
        == "Jansen, Piet (123456)"
    )
    assert student_folder(_submission(last_name="O:Brien?")) == "OBrien, Jan de (123456)"
    long = student_folder(_submission(last_name="x" * 300))
    assert long.endswith("(123456)")
    assert len(long) <= 140


def test_unique_name_is_case_insensitive() -> None:
    used: set[str] = set()
    assert unique_name("verslag.docx", used) == "verslag.docx"
    assert unique_name("Verslag.docx", used) == "Verslag (2).docx"
    assert unique_name("verslag.docx", used) == "verslag (3).docx"
    assert unique_name("README", used) == "README"
    assert unique_name("readme", used) == "readme (2)"


@pytest.mark.parametrize("value", ["=SUM(A1)", "+31612345678", "-1", "@cmd", "\tx"])
def test_csv_safe_prevents_formulas(value: str) -> None:
    assert csv_safe(value).startswith("'")


def test_write_csv_uses_bom_and_semicolons() -> None:
    data = write_csv(["a", "b"], [["Zoë", "x;y"], [None, 3]])
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert text.splitlines() == ["a;b", 'Zoë;"x;y"', ";3"]
