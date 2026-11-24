"""Parser for the SBB qualification structure in XML ("Herziening dossiers").

SBB publishes the whole structure as one file (``dossiers.xml``, about 60 MB) at
kwalificatie-mijn.s-bb.nl → Downloads → XML. A ``dossier`` holds the basic part
(``basis/basistaken/basiskerntaken/kerntaak``, codes ``B1-K1``) and per outflow a
``profiel`` with its own core tasks (``P1-K1``) and crebo(s). Versions of a dossier are
chained by ``nrvoorganger``; a revision may give an outflow a new crebo, so we keep the
crebo of the predecessor as well.

The file is read as a stream (``iterparse``) and every dossier is cleared after use, so
memory stays small. A document type declaration is refused: the SBB file has none, and
refusing it rules out entity tricks.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import IO
from xml.etree.ElementTree import Element, ParseError, XMLPullParser

from sqlalchemy.orm import Session

from inleverboks.services.audit import Actor
from inleverboks.services.errors import DomainError
from inleverboks.services.qualifications import (
    CODE_RE,
    CoreTaskRecord,
    ImportResult,
    QualificationRecord,
    QualificationService,
    WorkProcessRecord,
)

DOWNLOAD_PAGE = "https://kwalificatie-mijn.s-bb.nl/Lijsten/Groep/10"

CHUNK = 1024 * 1024
MAX_XML_BYTES = 1024 * 1024 * 1024  # 1 GiB uncompressed: the real file is ~60 MB
MAX_PROLOG_BYTES = 4 * CHUNK


@dataclass
class ParseResult:
    records: list[QualificationRecord] = field(default_factory=list)
    dossiers: int = 0
    skipped_dossiers: int = 0  # not "Vastgesteld" (concepts)


@dataclass
class _Outflow:
    """One crebo inside one dossier version, before it becomes a record."""

    crebo: str
    names: list[str]
    level: int | None
    valid_from: date | None
    valid_until: date | None
    profile_ids: list[str]
    profile_previous_ids: list[str]
    tasks: dict[str, CoreTaskRecord]


def import_file(
    db: Session, qualifications: QualificationService, stream: IO[bytes], *, actor: Actor,
    ip: str | None, source: str,
) -> tuple[ParseResult, ImportResult]:  # fmt: skip
    """Parse an SBB file (XML or a zip with one XML) and import all its qualifications."""
    parsed = parse(open_xml(stream))
    if not parsed.records:
        raise DomainError("sbb_empty")
    return parsed, qualifications.import_records(
        db, parsed.records, actor=actor, ip=ip, source=source
    )


def open_xml(stream: IO[bytes]) -> IO[bytes]:
    """The XML stream itself, or the single ``.xml`` file inside a zip."""
    head = stream.read(4)
    stream.seek(0)
    if head != b"PK\x03\x04":
        return stream
    try:
        archive = zipfile.ZipFile(stream)
    except zipfile.BadZipFile as exc:
        raise DomainError("sbb_invalid") from exc
    members = [m for m in archive.infolist() if m.filename.lower().endswith(".xml")]
    if len(members) != 1:
        raise DomainError("sbb_invalid")
    if members[0].file_size > MAX_XML_BYTES:
        raise DomainError("sbb_invalid")
    return archive.open(members[0])


def parse(stream: IO[bytes]) -> ParseResult:
    """All qualifications (one record per crebo and dossier version) in an SBB file."""
    result = ParseResult()
    # (dossier nr, previous dossier nr, outflow, dossier title, dossier crebo, version label)
    raw: list[tuple[str, str, _Outflow, str, str | None, str]] = []
    previous_of: dict[str, str] = {}
    seen_root = False
    for element in _dossiers(stream):
        seen_root = True
        result.dossiers += 1
        if (element.get("status") or "").strip() != "Vastgesteld":
            result.skipped_dossiers += 1
            element.clear()
            continue
        nr = (element.get("nr") or "").strip()
        previous = (element.get("nrvoorganger") or "").strip()
        previous_of[nr] = previous
        title = _text(element.get("titel"))
        dossier_crebo, dossier_from = _dossier_crebo(element)
        label = _version_label(element.get("versie"), dossier_from)
        basic = _core_tasks(element.find("basis/basistaken/basiskerntaken"))
        for outflow in _outflows(element, basic):
            raw.append((nr, previous, outflow, title, dossier_crebo, label))
        element.clear()
    if not seen_root:
        raise DomainError("sbb_invalid")

    depth = _depths(previous_of)
    crebo_of_profile = {
        profile_id: outflow.crebo
        for _, _, outflow, *_ in raw
        for profile_id in outflow.profile_ids
    }  # fmt: skip
    # Versions of one crebo, oldest first: by place in the chain, then SBB's number
    # (two versions can share a predecessor, e.g. "Gewijzigd 2016" and "Gewijzigd 2016-2").
    raw.sort(key=lambda item: (depth.get(item[0], 0), _int(item[0]) or 0))
    revisions: dict[str, int] = {}
    labels: set[tuple[str, str]] = set()
    for nr, _, outflow, title, dossier_crebo, base_label in raw:
        previous_crebo = next(
            (
                crebo_of_profile[p]
                for p in outflow.profile_previous_ids
                if p in crebo_of_profile and crebo_of_profile[p] != outflow.crebo
            ),
            None,
        )
        label = base_label
        if (outflow.crebo, label) in labels:
            label = f"{label} ({nr})"
        labels.add((outflow.crebo, label))
        revision = revisions.get(outflow.crebo, -1) + 1
        revisions[outflow.crebo] = revision
        name = outflow.names[0] if len(set(outflow.names)) == 1 else title
        result.records.append(
            QualificationRecord(
                crebo=outflow.crebo, name=name[:300], dossier_version=label[:40],
                level=outflow.level, dossier_crebo=dossier_crebo, dossier_name=title[:300] or None,
                valid_from=outflow.valid_from, valid_until=outflow.valid_until,
                revision=revision, previous_crebo=previous_crebo,
                core_tasks=list(outflow.tasks.values()),
            )
        )  # fmt: skip
    return result


# Streaming ------------------------------------------------------------------------------


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    total = 0
    while chunk := stream.read(CHUNK):
        total += len(chunk)
        if total > MAX_XML_BYTES:
            raise DomainError("sbb_invalid")
        yield chunk


def _dossiers(stream: IO[bytes]) -> Iterator[Element]:
    """Yield each complete ``dossier`` element; refuse a DOCTYPE and non-SBB files."""
    parser: XMLPullParser[Element] = XMLPullParser(events=("start", "end"))
    depth = 0
    checked_root = False
    prolog = b""  # everything before the root element: a DOCTYPE can only be there
    for chunk in _chunks(stream):
        if not checked_root:
            prolog += chunk
            if b"<!DOCTYPE" in prolog or len(prolog) > MAX_PROLOG_BYTES:
                raise DomainError("sbb_invalid")
        try:
            parser.feed(chunk)
            for queued in parser.read_events():
                if len(queued) != 2 or not isinstance(queued[1], Element):  # start/end only
                    continue
                event, element = queued[0], queued[1]
                if event == "start":
                    if not checked_root:
                        if element.tag != "sbb":
                            raise DomainError("sbb_invalid")
                        checked_root = True
                    depth += 1
                    continue
                depth -= 1
                if element.tag == "dossier" and depth == 2:  # sbb/dossiers/dossier
                    yield element
                elif depth == 1:  # domeinen, competenties, …: not needed
                    element.clear()
        except ParseError as exc:
            raise DomainError("sbb_invalid") from exc
    try:
        parser.close()
    except ParseError as exc:
        raise DomainError("sbb_invalid") from exc


# Dossier parts --------------------------------------------------------------------------


def _outflows(dossier: Element, basic: list[CoreTaskRecord]) -> list[_Outflow]:
    """One entry per crebo; profiles that share a crebo (variants) are merged."""
    by_crebo: dict[str, _Outflow] = {}
    for profile in dossier.findall("profielen/profiel"):
        tasks = _core_tasks(profile.find("profielkerntaken"))
        level = _int(profile.get("niveau"))
        for crebo_el in profile.findall("creboinformatie/crebo"):
            crebo = _text(crebo_el.text)
            if not crebo.isdigit():
                continue
            outflow = by_crebo.get(crebo)
            if outflow is None:
                outflow = _Outflow(
                    crebo=crebo, names=[], level=level,
                    valid_from=_date(crebo_el.get("Geldig_vanaf")),
                    valid_until=_date(crebo_el.get("Datum_einde_opleiding")),
                    profile_ids=[], profile_previous_ids=[],
                    tasks={task.code: task for task in basic},
                )  # fmt: skip
                by_crebo[crebo] = outflow
            outflow.names.append(_text(profile.get("titel")))
            outflow.profile_ids.append(_text(profile.get("nr")))
            if previous := _text(profile.get("nrvoorganger")):
                outflow.profile_previous_ids.append(previous)
            for task in tasks:
                outflow.tasks.setdefault(task.code, task)
    return list(by_crebo.values())


def _core_tasks(parent: Element | None) -> list[CoreTaskRecord]:
    if parent is None:
        return []
    tasks = []
    for task in parent.findall("kerntaak"):
        code = _text(task.get("hoofdstuk"))
        if not CODE_RE.match(code):
            continue
        processes = []
        for process in task.iter("werkproces"):
            wp_code = _text(process.get("hoofdstuk"))
            if CODE_RE.match(wp_code) and wp_code.startswith(f"{code}-"):
                processes.append(WorkProcessRecord(wp_code, _text(process.get("titel"))[:500]))
        tasks.append(CoreTaskRecord(code, _text(task.get("titel"))[:500], processes))
    return tasks


def _dossier_crebo(dossier: Element) -> tuple[str | None, date | None]:
    for element in dossier.findall("dossiercrebos/dossiercrebo"):
        crebo = _text(element.text)
        if crebo.isdigit():
            return crebo, _date(element.get("Geldig_vanaf"))
    return None, None


def _version_label(version: str | None, valid_from: date | None) -> str:
    """``2015`` for a first version, ``2015, gewijzigd 2016`` after a change.

    The year is when the dossier (crebo) took effect; ``versie`` names later changes. A
    revision that starts a new dossier crebo is labelled with its own start year only.
    """
    start = str(valid_from.year) if valid_from else ""
    change = _text(version).lower()
    if not change:
        return start or "-"
    if change == f"gewijzigd {start}":
        return start
    return f"{start}, {change}" if start else change


def _depths(previous_of: dict[str, str]) -> dict[str, int]:
    """How many versions precede each dossier version in the file (0 = first)."""
    depths: dict[str, int] = {}
    for nr in previous_of:
        chain: list[str] = []
        current = nr
        while current in previous_of and current not in depths and current not in chain:
            chain.append(current)
            current = previous_of[current]
        base = depths.get(current, -1) if current in previous_of else -1
        for offset, item in enumerate(reversed(chain), start=1):
            depths[item] = base + offset
    return depths


def _text(value: str | None) -> str:
    return " ".join((value or "").split())


def _int(value: str | None) -> int | None:
    value = _text(value)
    return int(value) if value.isdigit() else None


def _date(value: str | None) -> date | None:
    value = _text(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y").date()  # noqa: DTZ007 (a plain date)
    except ValueError:
        return None
