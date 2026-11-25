"""Realistic worst-case contexts for the PDF templates, and a layout check.

The contexts use long names, file names, hashes and every optional column, so a test
can render them with WeasyPrint and assert that nothing runs off the page.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment

START = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
LONG_NAME = (
    "25604 Software Developer \u2013 B1-K1 Realiseert software, B1-K2 Werkt aan "
    "softwareontwikkelingsprojecten met een uitzonderlijk lange omschrijving"
)
LONG_FILE = (
    "Examen_Software_Developer_Kerntaak_1_Werkproces_3_Realiseert_onderdelen_van_"
    "software_Jan_Jansen_de_Vries_van_der_Berg_definitieve_versie_v12.docx"
)
HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _slot(position: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        position=position,
        work_process_code=f"B1-K1-W{position}",
        label=f"Werkproces {position}: Realiseert onderdelen van een softwareproduct en test",
        expected_name_pattern=f"Examen_SD_B1-K1-W{position}_Studentnummer_Achternaam_*.docx",
        extensions=["docx", "pdf", "xlsx", "pptx", "zip", "txt", "odt", "ods", "png", "jpg"],
        template_file=SimpleNamespace(sha256=HASH),
    )


def _file(index: int) -> dict[str, Any]:
    return {
        "name": f"{index}_{LONG_FILE}",
        "slot": "Werkproces 1: Realiseert onderdelen van een softwareproduct en test",
        "size": 1234567890,
        "size_human": "1,1 GB",
        "sha256": HASH,
        "warnings": ["name_mismatch", "extension_mismatch", "empty_template", "temp_file"],
        "integrity_ok": index != 1,
        "deleted": False,
    }


def pv_context() -> dict[str, Any]:
    slots = [_slot(n) for n in range(1, 6)]
    exam = SimpleNamespace(
        name=LONG_NAME,
        description="Omschrijving " * 30,
        qualification={
            "crebo": "25604",
            "name": "Software Developer met een lange kwalificatienaam",
            "level": 4,
            "dossier_version": "2025-2026 versie 3",
            "core_task_code": "B1-K1",
            "core_task_title": "Realiseert software " * 6,
        },
        starts_at=START,
        ends_at=START + timedelta(hours=4),
        access_code_enabled=True,
        ip_restriction_enabled=True,
        ip_cidrs=[f"10.{n}.0.0/16" for n in range(40)]
        + ["2001:db8:abcd:0012:0000:0000:0000:0000/64"],
        extensions=["docx", "pdf", "xlsx", "pptx", "zip", "txt", "odt", "ods", "png", "jpg"],
    )
    person = SimpleNamespace(name="Prof. dr. ir. Maximiliaan Theodorus van der Heijden-Oosterhuis")
    row = SimpleNamespace(
        display_name="Wilhelmina Alexandra Catharina van Oldenbarnevelt-Rijnsburger",
        student_number="1234567890123456",
        status="active",
        filled_slots={slots[0].id, slots[2].id},
        extension_until=START + timedelta(hours=6),
    )
    current = SimpleNamespace(
        started_at=START, completed_at=START + timedelta(minutes=5), receipt_code="ABCD-EFGH-JKLM"
    )
    earlier = SimpleNamespace(
        attempt=1, status="invalid", status_changed_at=START,
        status_reason="Verkeerd bestand ingeleverd " * 8,
    )  # fmt: skip
    students = [
        {
            "row": row,
            "current": current,
            "files": [_file(n) for n in range(1, 4)],
            "earlier": [{"submission": earlier, "changed_by": person, "files": [_file(9)]}],
        }
    ]
    particulars = [
        {
            "at": START, "kind": "invalid", "student": f"{row.display_name} ({row.student_number})",
            "detail": "Verkeerd bestand " * 10, "by": person.name, "may_resubmit": True,
        },
        {"at": START, "kind": "extension", "student": row.student_number, "detail": START,
         "by": person.name},
    ]  # fmt: skip
    integrity = SimpleNamespace(
        checked_at=START, checked=12,
        mismatches=[SimpleNamespace(file=SimpleNamespace(original_name=LONG_FILE))],
    )  # fmt: skip
    return {
        "exam": exam,
        "owner": person,
        "reviewer": person,
        "effective_end": START + timedelta(hours=6),
        "expected_files": slots,
        "counts": dict.fromkeys(("active", "cancelled", "invalid", "missing"), 3)
        | {"not_on_list": 1, "roster": 30},
        "has_roster": True,
        "students": students,
        "particulars": particulars,
        "integrity": integrity,
        "generated_at": START,
        "generated_by": person.name,
        "document_id": "PV-20261123-ABCDEF",
        "audit_head_hash": HASH,
        "final": True,
        "signal_labels": {},
        "row_status_labels": {"active": "Ingeleverd", "invalid": "Ongeldig"},
        "warning_labels": {},
    }


def receipt_context() -> dict[str, Any]:
    return {
        "receipt_code": "ABCD-EFGH-JKLM",
        "verification_code": "WXYZ-2345",
        "exam_name": LONG_NAME,
        "student_name": "Wilhelmina Alexandra Catharina van Oldenbarnevelt-Rijnsburger",
        "student_number": "1234567890123456",
        "started_at": START,
        "files": [_file(n) for n in range(1, 4)],
        "warnings": [{"code": "name_mismatch", "file": LONG_FILE}],
        "warning_labels": {},
    }


def audit_context() -> dict[str, Any]:
    event = SimpleNamespace(
        seq=123456, ts=START,
        actor_label="Prof. dr. ir. Maximiliaan Theodorus van der Heijden-Oosterhuis <m.heijden@school.nl>",
        ip="2001:0db8:85a3:0000:0000:8a2e:0370:7334", event="submission.completed.with.long.name",
        details={"file": LONG_FILE, "sha256": HASH, "names": [LONG_FILE] * 3},
    )  # fmt: skip
    return {
        "title": f"Auditlog: {LONG_NAME}",
        "events": [event] * 3,
        "generated_at": START,
        "checked": 3,
        "issues": [],
        "head": HASH,
    }


def overflowing_boxes(env: Environment, template: str, context: dict[str, Any]) -> list[str]:
    """Render with WeasyPrint and list every box that sticks out of the page's content
    area (or, for the page margin boxes such as the footer, out of the page)."""
    from weasyprint import HTML  # noqa: PLC0415  # needs Pango

    html = env.get_template(template).render(context)
    document = HTML(string=html).render()
    problems: list[str] = []
    tolerance = 0.5  # CSS px
    for number, page in enumerate(document.pages, start=1):
        page_box = page._page_box  # WeasyPrint has no public box API
        left = page_box.content_box_x()
        right = left + page_box.width
        for child in page_box.children:
            margin_box = type(child).__name__ == "MarginBox"
            low, high = (0.0, page.width) if margin_box else (left, right)
            for box in child.descendants():
                x = getattr(box, "position_x", None)
                width = box.margin_width() if hasattr(box, "margin_width") else None
                if x is None or width is None:
                    continue
                if x < low - tolerance or x + width > high + tolerance:
                    text = getattr(box, "text", "") or type(box).__name__
                    problems.append(f"page {number}: {text[:60]!r} ({x:.0f}-{x + width:.0f})")
    return problems
