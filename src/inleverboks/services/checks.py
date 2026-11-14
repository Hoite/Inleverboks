"""Blocking checks and warnings for a set of files (spec §5.3).

The browser runs the same rules (``static/js/student/checks.js``) for immediate feedback,
but the server never trusts it: everything here is recomputed on upload.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field

from inleverboks.services.exams import matches_pattern

MAX_FILENAME_LENGTH = 200
EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Per-file warnings
EMPTY_TEMPLATE = "empty_template"
TEMP_FILE = "temp_file"
EMPTY_FILE = "empty_file"
DUPLICATE_FILE = "duplicate_file"
NAME_MISMATCH = "name_mismatch"
ZIP_UNCHECKED = "zip_unchecked"
# Submission-level warnings
COUNT_MISMATCH = "count_mismatch"
EMPTY_SLOT = "empty_slot"

_TEMP_RE = re.compile(r"^(~\$.*|.*\.tmp|\.~lock\..*#)$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_filename(name: str) -> str:
    """Original file name for display and the database: no path, no control characters,
    NFC-normalized (macOS sends NFD), limited length (extension kept)."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_RE.sub("", name).strip().strip(".")
    if not name:
        name = "bestand"
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: MAX_FILENAME_LENGTH - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_FILENAME_LENGTH]
    return name


def extension_of(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem else ""


@dataclass(frozen=True)
class SlotRule:
    id: uuid.UUID
    label: str
    pattern: str | None
    extensions: list[str] | None


@dataclass(frozen=True)
class Rules:
    slots: list[SlotRule]
    extensions: list[str]
    allow_extra_file: bool
    max_file_bytes: int
    max_submission_bytes: int
    max_files: int
    template_hashes: frozenset[str]


@dataclass(frozen=True)
class FileCandidate:
    name: str
    size: int
    sha256: str
    slot_id: uuid.UUID | None  # None = extra file


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)  # blocking, submission-level codes
    file_errors: dict[int, list[str]] = field(default_factory=dict)
    file_warnings: dict[int, list[str]] = field(default_factory=dict)
    file_notes: dict[int, list[str]] = field(default_factory=dict)  # informational only
    warnings: list[dict[str, str]] = field(default_factory=list)  # submission-level

    @property
    def blocked(self) -> bool:
        return bool(self.errors or any(self.file_errors.values()))

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings or any(self.file_warnings.values()))

    def summary(self, files: list[FileCandidate]) -> list[dict[str, str]]:
        """Flat list of all warnings for storing with the submission."""
        flat = [dict(w) for w in self.warnings]
        for index, codes in sorted(self.file_warnings.items()):
            flat += [{"code": code, "file": files[index].name} for code in codes]
        return flat


def _check_one(
    candidate: FileCandidate,
    slot: SlotRule | None,
    rules: Rules,
    used_slots: Counter[uuid.UUID],
    hashes: Counter[str],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if candidate.slot_id is not None and slot is None:
        errors.append("unknown_slot")
    if candidate.slot_id is None and not rules.allow_extra_file:
        errors.append("extra_not_allowed")
    if slot is not None:
        used_slots[slot.id] += 1
        if used_slots[slot.id] > 1:
            errors.append("slot_taken")
    allowed = (slot.extensions if slot and slot.extensions else None) or rules.extensions
    if extension_of(candidate.name) not in allowed:
        errors.append("extension_not_allowed")
    if candidate.size > rules.max_file_bytes:
        errors.append("file_too_large")
    if candidate.sha256 in rules.template_hashes:
        warnings.append(EMPTY_TEMPLATE)
    if _TEMP_RE.match(candidate.name):
        warnings.append(TEMP_FILE)
    if candidate.size == 0:
        warnings.append(EMPTY_FILE)
    if candidate.size > 0 and hashes[candidate.sha256] > 1:
        warnings.append(DUPLICATE_FILE)
    if slot is not None and slot.pattern and not matches_pattern(candidate.name, slot.pattern):
        warnings.append(NAME_MISMATCH)
    return errors, warnings


def check_files(files: list[FileCandidate], rules: Rules) -> CheckResult:
    result = CheckResult()
    slots = {slot.id: slot for slot in rules.slots}
    if not files:
        result.errors.append("no_files")
        return result
    if len(files) > rules.max_files:
        result.errors.append("too_many_files")
    if sum(f.size for f in files) > rules.max_submission_bytes:
        result.errors.append("submission_too_large")

    used_slots: Counter[uuid.UUID] = Counter()
    hashes = Counter(f.sha256 for f in files if f.size > 0)
    for index, candidate in enumerate(files):
        slot = slots.get(candidate.slot_id) if candidate.slot_id else None
        errors, warnings = _check_one(candidate, slot, rules, used_slots, hashes)
        if extension_of(candidate.name) == "zip":
            result.file_notes[index] = [ZIP_UNCHECKED]
        if errors:
            result.file_errors[index] = errors
        if warnings:
            result.file_warnings[index] = warnings

    for slot in rules.slots:
        if not used_slots[slot.id]:
            result.warnings.append({"code": EMPTY_SLOT, "slot": slot.label})
    expected = len(rules.slots)
    slotted = sum(used_slots.values())
    if expected and (slotted != expected or len(files) != expected):
        result.warnings.append(
            {"code": COUNT_MISMATCH, "expected": str(expected), "actual": str(len(files))}
        )
    return result
