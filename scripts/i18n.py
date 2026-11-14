"""Maintain the Dutch catalog: ``uv run python scripts/i18n.py [--check]``.

Extracts all msgids from Python code and templates, adds new ones to messages.po
(keeping existing translations) and drops obsolete ones. With ``--check`` it only
reports missing translations and exits 1 if there are any (used in the test suite).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parent.parent
PO_FILE = ROOT / "src" / "inleverboks" / "i18n" / "nl" / "LC_MESSAGES" / "messages.po"


def extract() -> Catalog:
    with tempfile.TemporaryDirectory() as tmp:
        pot = Path(tmp) / "messages.pot"
        subprocess.run(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-m",
                "babel.messages.frontend",
                "extract",
                "-F",
                str(ROOT / "babel.cfg"),
                "--no-location",
                "--omit-header",
                "-k",
                "_",
                "-k",
                "gettext",
                "-o",
                str(pot),
                str(ROOT),
            ],
            check=True,
            capture_output=True,
            cwd=ROOT,
        )
        with pot.open("rb") as fh:
            return read_po(fh)


def missing_translations() -> list[str]:
    template = extract()
    with PO_FILE.open("rb") as fh:
        catalog = read_po(fh, locale="nl")
    missing: list[str] = []
    for message in template:
        if not message.id:
            continue
        translated = catalog.get(message.id)
        if translated is None or not translated.string:
            missing.append(str(message.id))
    return missing


def update() -> None:
    template = extract()
    with PO_FILE.open("rb") as fh:
        catalog = read_po(fh, locale="nl")
    catalog.update(template, no_fuzzy_matching=True)
    catalog.obsolete.clear()
    with PO_FILE.open("wb") as fh:
        write_po(fh, catalog, omit_header=False, no_location=True, width=100)


if __name__ == "__main__":
    if "--check" in sys.argv:
        missing = missing_translations()
        for msgid in missing:
            print(f"missing: {msgid!r}")
        sys.exit(1 if missing else 0)
    update()
    print(f"Updated {PO_FILE.relative_to(ROOT)}")
