from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from inleverboks.services import slugs
from inleverboks.services.classlists import (
    apply_mapping,
    decode_csv,
    detect_delimiter,
    guess_mapping,
    read_rows,
)
from inleverboks.services.errors import DomainError
from inleverboks.services.exams import matches_pattern, pattern_extension, redate_slug
from inleverboks.services.ipranges import ip_allowed, parse_cidrs
from inleverboks.services.timeutil import (
    default_window,
    next_working_day,
    parse_local,
    round_to_quarter,
)
from inleverboks.web.downloads import content_disposition

AMS = ZoneInfo("Europe/Amsterdam")

# Slugs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Examen P2 K1 14-11-2026", "examen-p2-k1-14-11-2026"),
        ("Café Ørsted — Één!", "cafe-rsted-een"),
        ("  --Linux__P3--  ", "linux-p3"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugs.slugify(name) == slug


def test_generated_slug_format() -> None:
    for _ in range(200):
        value = slugs.generate_random()
        assert re.fullmatch(r"[a-z2-9]{3}-[a-z2-9]{3}-[a-z2-9]{3}", value)
        assert not set(value) & set("01ilo")
        slugs.validate_format(value)


@pytest.mark.parametrize("slug", ["ab", "-abc", "abc-", "a--b", "ABC", "abc def", "a" * 81, "ab/c"])
def test_invalid_slugs(slug: str) -> None:
    with pytest.raises(DomainError, match="slug_invalid"):
        slugs.validate_format(slug)


@pytest.mark.parametrize(
    "slug", ["login", "admin", "docent", "examens", "static", "healthz", "uitnodiging"]
)
def test_reserved_slugs(slug: str) -> None:
    with pytest.raises(DomainError, match="slug_reserved"):
        slugs.validate_format(slug)


# Expected names ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "pattern", "expected"),
    [
        ("Examen_Linux_P3_JdVries.docx", "Examen_Linux_P3_*.docx", True),
        ("examen_linux_p3_jdvries.DOCX", "Examen_Linux_P3_*.docx", True),
        ("Examen_Linux_P3_.docx", "Examen_Linux_P3_*.docx", True),
        ("Examen_Linux_P2_x.docx", "Examen_Linux_P3_*.docx", False),
        ("Examen_Linux_P3_x.docx.pdf", "Examen_Linux_P3_*.docx", False),
        ("script.sh", "script.sh", True),
        ("scriptXsh", "script.sh", False),  # the dot is literal
        ("a?b.txt", "a?b.txt", True),  # ? is literal, not a wildcard
        ("axb.txt", "a?b.txt", False),
        ("[1].txt", "[1].txt", True),
    ],
)
def test_matches_pattern(filename: str, pattern: str, expected: bool) -> None:
    assert matches_pattern(filename, pattern) is expected


def test_pattern_extension() -> None:
    assert pattern_extension("Verslag_*.DOCX") == "docx"
    assert pattern_extension("geen-extensie") is None
    assert pattern_extension(None) is None


# IP ranges -----------------------------------------------------------------------


def test_parse_cidrs() -> None:
    assert parse_cidrs("10.0.0.0/8, 192.168.1.5\n2001:db8::/32 ;10.0.0.0/8") == [
        "10.0.0.0/8",
        "192.168.1.5/32",
        "2001:db8::/32",
    ]
    assert parse_cidrs(["10.1.2.3/16", "172.16.0.0/12 192.168.0.0/16"]) == [
        "10.1.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    with pytest.raises(DomainError, match="ip_invalid"):
        parse_cidrs("10.0.0.0/33")
    with pytest.raises(DomainError, match="ip_too_many"):
        parse_cidrs([f"10.0.{i}.0/24" for i in range(51)])


def test_ip_allowed() -> None:
    cidrs = ["10.20.0.0/16", "2001:db8::/32"]
    assert ip_allowed("10.20.3.4", cidrs)
    assert not ip_allowed("10.21.0.1", cidrs)
    assert ip_allowed("2001:db8::1", cidrs)
    assert ip_allowed("::ffff:10.20.0.9", cidrs)  # IPv4-mapped IPv6
    assert not ip_allowed(None, cidrs)
    assert not ip_allowed("not-an-ip", cidrs)


# Time ----------------------------------------------------------------------------


def test_parse_local_converts_to_utc() -> None:
    assert parse_local("2026-11-13T09:00", AMS) == datetime(2026, 11, 13, 8, 0, tzinfo=UTC)
    assert parse_local("2026-07-01T09:00", AMS) == datetime(2026, 7, 1, 7, 0, tzinfo=UTC)


def test_parse_local_rejects_nonexistent_summer_time() -> None:
    # 29 March 2026: 02:00 → 03:00 in the Netherlands.
    with pytest.raises(DomainError, match="datetime_nonexistent"):
        parse_local("2026-03-29T02:30", AMS)


def test_parse_local_ambiguous_takes_first_occurrence() -> None:
    # 25 October 2026: 02:00-03:00 happens twice; the first one is summer time (UTC+2).
    assert parse_local("2026-10-25T02:30", AMS) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("value", ["", "2026-13-01T09:00", "morgen", "2026-11-13 09:00"])
def test_parse_local_invalid(value: str) -> None:
    with pytest.raises(DomainError, match="datetime_invalid"):
        parse_local(value, AMS)


@pytest.mark.parametrize(
    ("minute", "expected_hour", "expected_minute"),
    [(0, 9, 0), (7, 9, 0), (8, 9, 15), (22, 9, 15), (23, 9, 30), (53, 10, 0)],
)
def test_round_to_quarter(minute: int, expected_hour: int, expected_minute: int) -> None:
    rounded = round_to_quarter(datetime(2026, 11, 13, 9, minute, 42, tzinfo=AMS))
    assert (rounded.hour, rounded.minute, rounded.second) == (expected_hour, expected_minute, 0)


# Class list CSV ------------------------------------------------------------------


def test_decode_csv_encodings() -> None:
    text = "studentnummer;voornaam;achternaam\n123456;Zoë;Ångström\n"
    assert decode_csv(b"\xef\xbb\xbf" + text.encode()) == text
    assert decode_csv(text.encode()) == text
    assert decode_csv(text.encode("cp1252")) == text
    with pytest.raises(DomainError, match="csv_too_large"):
        decode_csv(b"x" * (2 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    ("line", "delimiter"),
    [("a;b;c", ";"), ("a,b,c", ","), ("a\tb\tc", "\t"), ("a;b,c", ";"), ("single", ";")],
)
def test_detect_delimiter(line: str, delimiter: str) -> None:
    assert detect_delimiter(line + "\n1;2;3") == delimiter


def test_guess_mapping() -> None:
    header = ["Leerlingnummer", "Roepnaam", "Tussenvoegsel", "Achternaam", "Klas", "E-mail"]
    assert guess_mapping(header) == {
        "student_number": 0,
        "first_name": 1,
        "infix": 2,
        "last_name": 3,
        "class_name": 4,
    }
    assert guess_mapping(["x", "y"])["student_number"] is None


def test_apply_mapping_reports_errors() -> None:
    text = (
        "nr;voornaam;tv;achternaam\n"
        "123456;Jan;de;Vries\n"
        "123456;Piet;;Jansen\n"
        "12;Kort;;Nummer\n"
        "654321;;;Zonder\n"
        ";Geen;;Nummer\n"
        "777777;Anna;van der;Berg\n"
    )
    rows = read_rows(text, ";")
    mapping = {
        "student_number": 0,
        "first_name": 1,
        "infix": 2,
        "last_name": 3,
        "class_name": None,
    }
    result = apply_mapping(rows, mapping, has_header=True, student_number_regex=r"^\d{4,12}$")
    assert [e.student_number for e in result.entries] == ["123456", "777777"]
    assert result.entries[1].infix == "van der"
    assert [(row, code) for row, code, _ in result.errors] == [
        (3, "duplicate_number"),
        (4, "invalid_number"),
        (5, "missing_name"),
        (6, "missing_number"),
    ]
    assert not result.ok
    with pytest.raises(DomainError, match="mapping_incomplete"):
        apply_mapping(
            rows, {**mapping, "last_name": None}, has_header=True, student_number_regex=".*"
        )


# Downloads -----------------------------------------------------------------------


def test_content_disposition_rfc5987() -> None:
    value = content_disposition('Verslag "Jan" Ørsted.docx')
    assert value.startswith("attachment; ")
    assert 'filename="Verslag _Jan_ rsted.docx"' in value
    assert "filename*=UTF-8''Verslag%20%22Jan%22%20%C3%98rsted.docx" in value
    assert "\n" not in content_disposition("a\nb.txt")


# Default window and suggested link names ---------------------------------------------


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 11, 23), date(2026, 11, 24)),  # Monday → Tuesday
        (date(2026, 11, 26), date(2026, 11, 27)),  # Thursday → Friday
        (date(2026, 11, 27), date(2026, 11, 30)),  # Friday → Monday
        (date(2026, 11, 28), date(2026, 11, 30)),  # Saturday → Monday
        (date(2026, 11, 29), date(2026, 11, 30)),  # Sunday → Monday
    ],
)
def test_next_working_day(today: date, expected: date) -> None:
    assert next_working_day(today) == expected


def test_default_window_is_9_to_17_local_also_around_daylight_saving() -> None:
    # Friday 27 March 2026, 23:30 local: Monday 30 March is in summer time (UTC+2).
    start, end = default_window(datetime(2026, 3, 27, 22, 30, tzinfo=UTC), AMS)
    assert start == datetime(2026, 3, 30, 7, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 30, 15, 0, tzinfo=UTC)
    # After midnight local time it is already the next day.
    start, _ = default_window(datetime(2026, 11, 26, 23, 30, tzinfo=UTC), AMS)  # Fri 00:30
    assert start == datetime(2026, 11, 30, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("slug", "redated"),
    [
        ("27016-b1-k1-16112026", "27016-b1-k1-03122026"),
        ("27016-b1-k1-w3-16112026-2", "27016-b1-k1-w3-03122026"),
        ("examen-linux-p3", None),
        ("code-123456789", None),  # nine digits: not a date
        ("12345678-examen", None),  # the date must be at the end
    ],
)
def test_redate_slug(slug: str, redated: str | None) -> None:
    assert redate_slug(slug, "03122026") == redated
