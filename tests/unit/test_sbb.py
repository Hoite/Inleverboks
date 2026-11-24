"""The parser for the SBB XML (structure of ``dossiers.xml``, "Herziening dossiers")."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from inleverboks.services import sbb
from inleverboks.services.errors import DomainError
from inleverboks.services.qualifications import QualificationRecord
from tests.sbb_sample import sample_xml


def _records() -> dict[tuple[str, str], QualificationRecord]:
    result = sbb.parse(io.BytesIO(sample_xml()))
    return {(r.crebo, r.dossier_version): r for r in result.records}


def test_parse_reads_outflows_versions_and_codes() -> None:
    result = sbb.parse(io.BytesIO(sample_xml()))
    assert result.dossiers == 5
    assert result.skipped_dossiers == 1  # the concept
    records = _records()
    assert set(records) == {
        ("29001", "2015"), ("29002", "2015"),
        ("29001", "2015, gewijzigd 2016"), ("29002", "2015, gewijzigd 2016"),
        ("29001", "2015, gewijzigd 2016-2"),
        ("29101", "2024"),  # a revision that starts a new dossier crebo: its own year only
    }  # fmt: skip
    first = records[("29001", "2015")]
    assert first.name == "Medewerker ICT"
    assert first.level == 3
    assert first.dossier_crebo == "29000"
    assert first.dossier_name == "ICT systemen"
    assert first.valid_from == date(2015, 8, 1)
    assert first.valid_until == date(2025, 8, 1)  # end of training, not of enrolment
    assert [t.code for t in first.core_tasks] == ["B1-K1", "B1-K2"]
    assert [wp.code for wp in first.core_tasks[0].work_processes] == ["B1-K1-W1", "B1-K1-W2"]
    assert first.core_tasks[0].title == "Richt systemen in"


def test_profiles_on_one_crebo_are_merged_under_the_dossier_name() -> None:
    merged = _records()[("29002", "2015")]
    assert merged.name == "ICT systemen"  # variants share the crebo: use the dossier title
    assert [t.code for t in merged.core_tasks] == ["B1-K1", "B1-K2", "P1-K1", "P2-K1"]
    single = _records()[("29002", "2015, gewijzigd 2016")]
    assert single.name == "Variant A"
    assert [t.code for t in single.core_tasks] == ["B1-K1", "B1-K2", "P1-K1"]


def test_revisions_order_versions_and_follow_new_crebos() -> None:
    records = _records()
    assert records[("29001", "2015")].revision == 0
    assert records[("29001", "2015, gewijzigd 2016")].revision == 1
    # Same predecessor as "gewijzigd 2016", higher SBB number: newer.
    assert records[("29001", "2015, gewijzigd 2016-2")].revision == 2
    new = records[("29101", "2024")]
    assert (new.revision, new.previous_crebo) == (0, "29001")
    assert records[("29001", "2015, gewijzigd 2016")].previous_crebo is None
    changed = records[("29001", "2015, gewijzigd 2016")].core_tasks[0].work_processes[1]
    assert changed.title == "Installeert en configureert systemen"


def test_zip_with_one_xml_is_accepted() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dossiers.xml", sample_xml())
    buffer.seek(0)
    assert len(sbb.parse(sbb.open_xml(buffer)).records) == 6


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not xml at all",
        b"<?xml version='1.0'?><other><dossiers/></other>",
        b"<sbb><dossiers><dossier>",  # truncated
        b'<?xml version="1.0"?><!DOCTYPE sbb [<!ENTITY a "aaaa">]><sbb>&a;</sbb>',
    ],
)
def test_invalid_files_are_refused(data: bytes) -> None:
    with pytest.raises(DomainError) as info:
        sbb.parse(sbb.open_xml(io.BytesIO(data)))
    assert info.value.code == "sbb_invalid"


def test_zip_must_hold_exactly_one_xml() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.xml", sample_xml())
        archive.writestr("b.xml", sample_xml())
    buffer.seek(0)
    with pytest.raises(DomainError):
        sbb.open_xml(buffer)
