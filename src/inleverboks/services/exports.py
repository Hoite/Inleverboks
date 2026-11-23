"""Exports for teachers: zip of all hand-ins (with manifest), CSV overview, integrity checks.

The zip is streamed (zipstream-ng, ZIP64 when needed): nothing is built in memory or on
disk. While a file is streamed it is hashed again and compared with the stored SHA-256;
the result goes into ``manifest.csv`` (the last entry) and into the audit log.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from anyio import from_thread
from zipstream import ZipStream

from inleverboks.adapters.storage import ObjectNotFoundError, StorageBackend
from inleverboks.db.models import Exam, Submission, SubmissionFile

_WINDOWS_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_CSV_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
MAX_FOLDER_NAME = 100
MAX_FILE_NAME = 150
STATUS_NL = {"active": "ingeleverd", "cancelled": "geannuleerd", "invalid": "ongeldig"}

MANIFEST_COLUMNS = [
    "studentnummer", "achternaam", "tussenvoegsel", "voornaam", "status", "poging",
    "inlevertijd_start", "inlevertijd_voltooid", "vakje", "werkproces", "bestandsnaam_origineel",
    "bestandsnaam_in_zip", "grootte_bytes", "sha256", "integriteit_ok", "waarschuwingen",
]  # fmt: skip


# Names ---------------------------------------------------------------------------


def windows_safe(name: str, max_length: int) -> str:
    """Remove characters Windows does not allow and trailing dots/spaces; NFC; limit."""
    name = unicodedata.normalize("NFC", name)
    name = _WINDOWS_FORBIDDEN.sub("", name).strip()
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10 and stem:
            name = stem[: max_length - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:max_length]
    name = name.rstrip(". ")
    return name or "bestand"


def student_folder(submission: Submission) -> str:
    """``{achternaam}, {voornaam}[ {tussenvoegsel}] ({studentnummer})``."""
    base = f"{submission.last_name}, {submission.first_name}"
    if submission.infix:
        base += f" {submission.infix}"
    base = windows_safe(base, MAX_FOLDER_NAME)
    return windows_safe(f"{base} ({submission.student_number})", MAX_FOLDER_NAME + 40)


def unique_name(name: str, used: set[str]) -> str:
    """Add `` (2)``, `` (3)``… before the extension; Windows is case-insensitive."""
    candidate = name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem} ({counter}).{ext}" if ext else f"{stem} ({counter})"
        counter += 1
    used.add(candidate.lower())
    return candidate


# CSV -----------------------------------------------------------------------------


def csv_safe(value: object) -> str:
    """Prevent formula injection when the CSV is opened in Excel."""
    text = "" if value is None else str(value)
    if text.startswith(_CSV_DANGEROUS):
        return "'" + text
    return text


def write_csv(columns: list[str], rows: list[list[object]]) -> bytes:
    """``;``-separated UTF-8 with BOM, so Dutch Excel opens it correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([csv_safe(v) for v in row])
    return "﻿".encode() + buffer.getvalue().encode("utf-8")


def local(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else ""


# Integrity -----------------------------------------------------------------------


@dataclass
class FileCheck:
    file: SubmissionFile
    ok: bool
    actual_sha256: str | None  # None when the object is missing


@dataclass
class IntegrityReport:
    checked_at: datetime
    checks: list[FileCheck] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.checks)

    @property
    def mismatches(self) -> list[FileCheck]:
        return [c for c in self.checks if not c.ok]

    def status_of(self, file: SubmissionFile) -> str:
        for check in self.checks:
            if check.file.id == file.id:
                if check.ok:
                    return "ja"
                return "nee (ontbreekt)" if check.actual_sha256 is None else "nee"
        return ""


async def hash_object(storage: StorageBackend, key: str) -> str | None:
    digest = hashlib.sha256()
    try:
        async for chunk in storage.open(key):
            digest.update(chunk)
    except ObjectNotFoundError:
        return None
    return digest.hexdigest()


async def check_integrity(
    storage: StorageBackend, files: list[SubmissionFile], checked_at: datetime
) -> IntegrityReport:
    report = IntegrityReport(checked_at=checked_at)
    for file in files:
        if not file.storage_key:
            continue  # removed by retention
        actual = await hash_object(storage, file.storage_key)
        report.checks.append(FileCheck(file=file, ok=actual == file.sha256, actual_sha256=actual))
    return report


# Zip -----------------------------------------------------------------------------


@dataclass
class ZipItem:
    submission: Submission
    file: SubmissionFile
    arcname: str
    slot_label: str | None
    work_process_code: str | None = None


def plan_zip(
    exam: Exam,
    submissions: list[Submission],
    *,
    include_cancelled: bool,
    include_invalid: bool,
) -> list[ZipItem]:
    labels = {s.id: s.label for s in exam.expected_files}
    codes = {s.id: s.work_process_code for s in exam.expected_files if s.work_process_code}
    items: list[ZipItem] = []
    used: dict[str, set[str]] = {}
    for submission in sorted(
        submissions,
        key=lambda s: (s.last_name.lower(), s.first_name.lower(), s.student_number, s.attempt),
    ):
        folder = student_folder(submission)
        if submission.status == "cancelled":
            if not include_cancelled:
                continue
            folder = f"_geannuleerd/{folder} - poging {submission.attempt}"
        elif submission.status == "invalid":
            if not include_invalid:
                continue
            folder = f"_ongeldig/{folder} - poging {submission.attempt}"
        names = used.setdefault(folder.lower(), set())
        for file in sorted(submission.files, key=lambda f: f.position):
            if not file.storage_key:
                continue
            code = codes.get(file.expected_file_id) if file.expected_file_id else None
            # Files of a work process start with its code, so they sort per work process.
            original = f"{code}_{file.original_name}" if code else file.original_name
            name = unique_name(windows_safe(original, MAX_FILE_NAME), names)
            items.append(
                ZipItem(
                    submission=submission,
                    file=file,
                    arcname=f"{folder}/{name}",
                    slot_label=labels.get(file.expected_file_id) if file.expected_file_id else None,
                    work_process_code=code,
                )
            )
    return items


def manifest_rows(
    items: list[ZipItem], report: IntegrityReport, tz: ZoneInfo
) -> list[list[object]]:
    return [
        [
            item.submission.student_number,
            item.submission.last_name,
            item.submission.infix or "",
            item.submission.first_name,
            STATUS_NL.get(item.submission.status, item.submission.status),
            item.submission.attempt,
            local(item.submission.started_at, tz),
            local(item.submission.completed_at, tz),
            item.slot_label or "extra",
            item.work_process_code or "",
            item.file.original_name,
            item.arcname,
            item.file.size,
            item.file.sha256,
            report.status_of(item.file),
            ", ".join(item.file.warnings or []),
        ]
        for item in items
    ]


def _sync_chunks(storage: StorageBackend, key: str) -> Iterator[bytes]:
    """Pull an async storage stream from a worker thread (StreamingResponse runs sync
    iterators in a thread pool)."""
    stream = storage.open(key)

    async def next_chunk() -> bytes:
        return await anext(stream)

    while True:
        try:
            yield from_thread.run(next_chunk)
        except StopAsyncIteration:
            return


def build_zip(
    storage: StorageBackend,
    items: list[ZipItem],
    report: IntegrityReport,
    tz: ZoneInfo,
    on_finished: Callable[[IntegrityReport], None],
) -> ZipStream:
    zs = ZipStream(sized=False)

    def checked_file(item: ZipItem) -> Iterator[bytes]:
        digest = hashlib.sha256()
        found = True
        try:
            for chunk in _sync_chunks(storage, item.file.storage_key or ""):
                digest.update(chunk)
                yield chunk
        except ObjectNotFoundError:
            found = False
        actual = digest.hexdigest() if found else None
        report.checks.append(
            FileCheck(file=item.file, ok=actual == item.file.sha256, actual_sha256=actual)
        )

    def manifest() -> Iterator[bytes]:
        # Evaluated after all files were streamed, so the integrity results are known.
        yield write_csv(MANIFEST_COLUMNS, manifest_rows(items, report, tz))
        on_finished(report)

    for item in items:
        zs.add(checked_file(item), item.arcname)
    zs.add(manifest(), "manifest.csv")
    return zs
