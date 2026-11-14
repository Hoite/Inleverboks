from __future__ import annotations

import hashlib
import unicodedata
import uuid

import pytest

from inleverboks.db.models import Exam
from inleverboks.services.checks import (
    EMPTY_FILE_SHA256,
    FileCandidate,
    Rules,
    SlotRule,
    check_files,
    sanitize_filename,
)
from inleverboks.services.student_flow import StateCodec, StudentState, abbreviate, normalize_code
from tests.conftest import TEST_SECRET

REPORT = SlotRule(id=uuid.uuid4(), label="Verslag", pattern="Verslag_*.docx", extensions=["docx"])
SCRIPT = SlotRule(id=uuid.uuid4(), label="Script", pattern=None, extensions=None)
TEMPLATE = hashlib.sha256(b"leeg sjabloon").hexdigest()

RULES = Rules(
    slots=[REPORT, SCRIPT],
    extensions=["docx", "pdf", "sh", "txt", "zip"],
    allow_extra_file=True,
    max_file_bytes=1000,
    max_submission_bytes=1500,
    max_files=4,
    template_hashes=frozenset({TEMPLATE}),
)


def f(
    name: str, size: int = 10, sha: str | None = None, slot: SlotRule | None = None
) -> FileCandidate:
    return FileCandidate(
        name=name,
        size=size,
        sha256=sha or hashlib.sha256(name.encode()).hexdigest(),
        slot_id=slot.id if slot else None,
    )


def test_clean_submission_has_no_findings() -> None:
    result = check_files([f("Verslag_Jan.docx", slot=REPORT), f("install.sh", slot=SCRIPT)], RULES)
    assert not result.blocked
    assert not result.has_warnings


def test_blocking_errors() -> None:
    assert check_files([], RULES).errors == ["no_files"]
    too_many = [f(f"a{i}.txt") for i in range(5)]
    assert "too_many_files" in check_files(too_many, RULES).errors
    big = [f("a.txt", 900), f("b.txt", 900)]
    assert "submission_too_large" in check_files(big, RULES).errors
    result = check_files(
        [
            f("Verslag.pdf", slot=REPORT),  # slot only allows docx
            f("virus.exe"),
            f("groot.txt", 1001),
            f("Verslag_2.docx", slot=REPORT),  # second file in the same slot
        ],
        RULES,
    )
    assert result.file_errors[0] == ["extension_not_allowed"]
    assert result.file_errors[1] == ["extension_not_allowed"]
    assert result.file_errors[2] == ["file_too_large"]
    assert result.file_errors[3] == ["slot_taken"]
    assert result.blocked


def test_extra_files_can_be_disallowed() -> None:
    rules = Rules(**{**RULES.__dict__, "allow_extra_file": False})
    assert check_files([f("x.txt")], rules).file_errors[0] == ["extra_not_allowed"]


def test_unknown_slot_is_blocking() -> None:
    candidate = FileCandidate(name="a.txt", size=1, sha256="a" * 64, slot_id=uuid.uuid4())
    assert "unknown_slot" in check_files([candidate], RULES).file_errors[0]


@pytest.mark.parametrize(
    ("candidate", "warning"),
    [
        (f("Verslag_Jan.docx", sha=TEMPLATE, slot=REPORT), "empty_template"),
        (f("~$rslag_Jan.docx", slot=REPORT), "temp_file"),
        (f("werk.TMP"), "temp_file"),
        (f(".~lock.verslag.docx#"), "temp_file"),
        (f("leeg.txt", 0, EMPTY_FILE_SHA256), "empty_file"),
        (f("verslag.docx", slot=REPORT), "name_mismatch"),
    ],
)
def test_file_warnings(candidate: FileCandidate, warning: str) -> None:
    result = check_files([candidate, f("install.sh", slot=SCRIPT)], RULES)
    assert warning in result.file_warnings[0]
    assert result.has_warnings


def test_temp_extension_is_also_blocked_when_not_allowed() -> None:
    result = check_files([f("werk.tmp")], RULES)
    assert "temp_file" in result.file_warnings[0]
    assert "extension_not_allowed" in result.file_errors[0]


def test_duplicate_content_but_not_for_empty_files() -> None:
    same = hashlib.sha256(b"x").hexdigest()
    result = check_files(
        [f("Verslag_a.docx", sha=same, slot=REPORT), f("kopie.sh", sha=same, slot=SCRIPT)], RULES
    )
    assert result.file_warnings[0] == ["duplicate_file"]
    assert result.file_warnings[1] == ["duplicate_file"]
    empties = [f("a.txt", 0, EMPTY_FILE_SHA256), f("b.txt", 0, EMPTY_FILE_SHA256)]
    assert "duplicate_file" not in check_files(empties, RULES).file_warnings[0]


def test_count_and_empty_slot_warnings() -> None:
    result = check_files([f("Verslag_Jan.docx", slot=REPORT)], RULES)
    assert {"code": "empty_slot", "slot": "Script"} in result.warnings
    assert {"code": "count_mismatch", "expected": "2", "actual": "1"} in result.warnings
    extra = check_files(
        [f("Verslag_Jan.docx", slot=REPORT), f("a.sh", slot=SCRIPT), f("extra.txt")], RULES
    )
    assert extra.warnings == [{"code": "count_mismatch", "expected": "2", "actual": "3"}]


def test_zip_is_a_note_not_a_warning() -> None:
    result = check_files([f("Verslag_Jan.docx", slot=REPORT), f("code.zip", slot=SCRIPT)], RULES)
    assert result.file_notes[1] == ["zip_unchecked"]
    assert not result.has_warnings


def test_summary_lists_all_warnings() -> None:
    files = [f("verslag.docx", slot=REPORT)]
    result = check_files(files, RULES)
    assert result.summary(files) == [
        {"code": "empty_slot", "slot": "Script"},
        {"code": "count_mismatch", "expected": "2", "actual": "1"},
        {"code": "name_mismatch", "file": "verslag.docx"},
    ]


# File names ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\Users\\jan\\Verslag.docx", "Verslag.docx"),
        ("../../etc/passwd", "passwd"),
        ("na\x00am\x1f.txt", "naam.txt"),
        ("  .verborgen.  ", "verborgen"),
        ("", "bestand"),
        ("/", "bestand"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_normalizes_nfd_to_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "Café_Zoë.docx")
    assert nfd != "Café_Zoë.docx"
    assert sanitize_filename(nfd) == "Café_Zoë.docx"


def test_sanitize_filename_limits_length_and_keeps_extension() -> None:
    result = sanitize_filename("a" * 400 + ".docx")
    assert len(result) == 200
    assert result.endswith(".docx")


# Student state -------------------------------------------------------------------


def test_abbreviate() -> None:
    assert abbreviate("Jan", "de", "Vries") == "Jan de V."
    assert abbreviate("Zoë", None, "ångström") == "Zoë Å."
    assert abbreviate("Piet", "  ", "Jansen") == "Piet J."


def test_normalize_code() -> None:
    assert normalize_code(" nz av-xd ") == "NZAVXD"


def test_state_codec_rejects_tampering_and_other_exams() -> None:
    codec = StateCodec(TEST_SECRET)
    exam = Exam(id=uuid.uuid4())
    state = StudentState(exam_id=str(exam.id), stage="ready", student_number="123456")
    token = codec.dumps(state)
    assert codec.loads(token, exam) == state
    assert codec.loads(token[:-2] + "xx", exam) is None
    assert codec.loads(token, Exam(id=uuid.uuid4())) is None
    assert StateCodec("another-secret-key-0123456789abcdefgh").loads(token, exam) is None
    assert codec.loads(None, exam) is None
