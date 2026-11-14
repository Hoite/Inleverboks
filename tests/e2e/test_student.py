"""End-to-end: the complete student flow in a real browser."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import select

from inleverboks.db.models import ExamRosterEntry, Submission, TemplateFile, User
from inleverboks.services.checks import FileCandidate, Rules, SlotRule, check_files
from inleverboks.services.exams import ExamInput, SlotInput
from inleverboks.services.passwords import hash_password
from tests.e2e.conftest import LiveServer
from tests.e2e.files import minimal_docx, minimal_pdf

pytestmark = pytest.mark.e2e

TEMPLATE = minimal_docx("Leeg sjabloon")


def seed_exam(server: LiveServer) -> tuple[str, str]:
    services = server.app.state.services
    slug = f"e2e-{uuid.uuid4().hex[:6]}"
    with server.app.state.sessionmaker() as db:
        owner = db.scalar(select(User).where(User.email == "e2e@school.nl"))
        if owner is None:
            owner = User(email="e2e@school.nl", name="E2E", password_hash=hash_password("x" * 20),
                         created_at=services.clock.now())  # fmt: skip
            db.add(owner)
            db.commit()
        now = services.clock.now()
        exam = services.exams.create(
            db,
            owner,
            ExamInput(
                name="Examen E2E",
                description=None,
                starts_at=now - timedelta(minutes=5),
                ends_at=now + timedelta(hours=1),
                slug=slug,
                extensions=["pdf", "docx", "sh", "txt"],
                slots=[
                    SlotInput(label="Verslag", pattern="Verslag_*.pdf"),
                    SlotInput(label="Bijlage", pattern="Bijlage_*.docx"),
                ],
            ),
            ip=None,
        )
        services.exams.publish(db, owner, exam, ip=None)
        sha = hashlib.sha256(TEMPLATE).hexdigest()
        exam.expected_files[1].template_file = TemplateFile(
            original_name="Bijlage_sjabloon.docx", storage_key=f"templates/{sha}",
            size=len(TEMPLATE), sha256=sha, uploaded_at=now,
        )  # fmt: skip
        db.add(ExamRosterEntry(exam_id=exam.id, student_number="123456", first_name="Jan",
                               infix="de", last_name="Vries"))  # fmt: skip
        db.commit()
        return slug, exam.access_code or ""


def make_files(tmp_path: Path) -> dict[str, Path]:
    files = {
        "Verslag_JdVries.pdf": minimal_pdf("Verslag van Jan"),
        "Bijlage_JdVries.docx": TEMPLATE,  # identical to the empty template: a warning
        "install.sh": b"#!/bin/sh\necho hallo\n",
    }
    paths = {}
    for name, content in files.items():
        path = tmp_path / name
        path.write_bytes(content)
        paths[name] = path
    return paths


def test_full_student_flow(page: Page, live_server: LiveServer, tmp_path: Path) -> None:
    slug, code = seed_exam(live_server)
    files = make_files(tmp_path)
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    page.goto(live_server + f"/{slug}")
    page.get_by_label("Toegangscode").fill(code.lower())
    page.get_by_role("button", name="Volgende").click()
    page.get_by_label("Studentnummer").fill("123456")
    page.get_by_role("button", name="Volgende").click()
    expect(page.get_by_text("Jan de V.")).to_be_visible()
    page.get_by_role("button", name="Ja, dat ben ik").click()

    page.locator("#pick-any").set_input_files([str(p) for p in files.values()])
    # Files land in the right slot by their expected name; the script becomes extra.
    expect(page.locator(".slot-card").nth(0)).to_contain_text("Verslag_JdVries.pdf")
    expect(page.locator(".slot-card").nth(1)).to_contain_text("Bijlage_JdVries.docx")
    expect(page.locator(".extra-card")).to_contain_text("install.sh")
    expected_hash = hashlib.sha256(files["install.sh"].read_bytes()).hexdigest()
    expect(page.locator(".extra-card .file-hash")).to_contain_text(expected_hash[:8])
    expect(page.locator(".slot-card").nth(1)).to_contain_text("Identiek aan het lege sjabloon")

    # PDF preview renders on a canvas inside the sandboxed frame.
    page.locator(".slot-card").nth(0).get_by_role("button", name="Voorbeeld").click()
    frame = page.frame_locator("#preview-frame")
    expect(frame.locator("canvas")).to_have_count(1, timeout=15000)
    page.get_by_role("button", name="Sluiten").click()
    # docx preview via docx-preview.
    page.locator(".slot-card").nth(1).get_by_role("button", name="Voorbeeld").click()
    expect(frame.get_by_text("Leeg sjabloon")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Sluiten").click()

    # Without the declaration nothing is sent.
    page.get_by_role("button", name="Inleveren").click()
    expect(page.locator("#status")).to_contain_text("Bevestig eerst de verklaring")
    page.get_by_label("Ik verklaar dat dit mijn eigen werk is").check()
    page.get_by_role("button", name="Inleveren").click()

    # One extra confirmation that lists exactly what deviates.
    dialog = page.locator("#confirm-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Je levert 3 bestanden in; er worden er 2 verwacht")
    expect(dialog).to_contain_text("Bijlage_JdVries.docx: Identiek aan het lege sjabloon")
    page.get_by_role("button", name="Ja, toch inleveren").click()

    receipt = page.locator("#receipt")
    expect(receipt).to_be_visible(timeout=15000)
    expect(receipt).to_contain_text("Bewaar dit ontvangstbewijs")
    receipt_code = receipt.locator(".receipt-code").first.inner_text()
    assert receipt_code.startswith("IB-")
    expect(receipt).to_contain_text(expected_hash[:8])

    with live_server.app.state.sessionmaker() as db:
        submission = db.scalar(select(Submission).where(Submission.receipt_code == receipt_code))
        assert submission is not None
        assert submission.on_roster
        assert len(submission.files) == 3

    # Back button and reload show nothing of the hand-in (shared computers).
    page.go_back()
    expect(page.get_by_label("Toegangscode")).to_be_visible()
    assert "Jan" not in page.content()
    assert receipt_code not in page.content()
    assert not [e for e in console_errors if "Content Security Policy" in e]


def test_browser_checks_match_server_checks(page: Page, live_server: LiveServer) -> None:
    """static/js/student/checks.js must give the same answers as services/checks.py."""
    report = SlotRule(
        id=uuid.uuid4(), label="Verslag", pattern="Verslag_*.docx", extensions=["docx"]
    )
    script = SlotRule(id=uuid.uuid4(), label="Script", pattern=None, extensions=None)
    template = hashlib.sha256(b"leeg").hexdigest()
    rules = Rules(
        slots=[report, script], extensions=["docx", "sh", "txt", "zip"], allow_extra_file=True,
        max_file_bytes=100, max_submission_bytes=150, max_files=4,
        template_hashes=frozenset({template}),
    )  # fmt: skip
    same = hashlib.sha256(b"same").hexdigest()
    cases: list[list[FileCandidate]] = [
        [],
        [FileCandidate("Verslag_a.docx", 10, template, report.id), FileCandidate("a.sh", 5, "a" * 64, script.id)],
        [FileCandidate("verslag.DOCX", 10, "b" * 64, report.id)],
        [FileCandidate("~$x.docx", 1, "c" * 64, None), FileCandidate("x.tmp", 0, "d" * 64, None)],
        [FileCandidate("a.txt", 5, same, None), FileCandidate("b.txt", 5, same, None)],
        [FileCandidate("groot.txt", 101, "e" * 64, None), FileCandidate("x.exe", 1, "f" * 64, None)],
        [FileCandidate("Verslag_1.docx", 1, "1" * 64, report.id), FileCandidate("Verslag_2.docx", 1, "2" * 64, report.id)],
        [FileCandidate("code.zip", 60, "3" * 64, None), FileCandidate("b.txt", 100, "4" * 64, None)],
    ]  # fmt: skip
    expected = []
    for files in cases:
        r = check_files(files, rules)
        expected.append({
            "errors": r.errors,
            "fileErrors": {str(k): v for k, v in r.file_errors.items()},
            "fileWarnings": {str(k): v for k, v in r.file_warnings.items()},
            "fileNotes": {str(k): v for k, v in r.file_notes.items()},
            "warnings": [{k: str(v) for k, v in w.items()} for w in r.warnings],
        })  # fmt: skip
    config: dict[str, Any] = {
        "slots": [{**asdict(s), "id": str(s.id)} for s in rules.slots],
        "extensions": rules.extensions, "allowExtra": rules.allow_extra_file,
        "maxFileBytes": rules.max_file_bytes, "maxSubmissionBytes": rules.max_submission_bytes,
        "maxFiles": rules.max_files, "templateHashes": sorted(rules.template_hashes),
    }  # fmt: skip
    js_cases = [
        [{"name": f.name, "size": f.size, "sha256": f.sha256, "slotId": str(f.slot_id) if f.slot_id else None} for f in files]
        for files in cases
    ]  # fmt: skip
    page.goto(live_server + "/")
    actual = page.evaluate(
        """async ([cases, config]) => {
            const { checkFiles } = await import('/static/js/student/checks.js');
            const obj = (m) => Object.fromEntries([...m.entries()].map(([k, v]) => [String(k), v]));
            return cases.map((files) => {
                const r = checkFiles(files, config);
                return { errors: r.errors, fileErrors: obj(r.fileErrors), fileWarnings: obj(r.fileWarnings),
                         fileNotes: obj(r.fileNotes),
                         warnings: r.warnings.map((w) => Object.fromEntries(Object.entries(w).map(([k, v]) => [k, String(v)]))) };
            });
        }""",
        [js_cases, config],
    )
    assert json.loads(json.dumps(actual)) == expected
