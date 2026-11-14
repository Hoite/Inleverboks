"""Fill empty msgstr entries from a Python dict file: ``uv run python scripts/i18n_fill.py nl.py``.

Development helper; the file must define ``NL = {msgid: translation}``.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

from babel.messages.pofile import read_po, write_po

PO_FILE = Path(__file__).resolve().parent.parent / "src/inleverboks/i18n/nl/LC_MESSAGES/messages.po"


def main(path: str) -> None:
    translations: dict[str, str] = runpy.run_path(path)["NL"]
    with PO_FILE.open("rb") as fh:
        catalog = read_po(fh, locale="nl")
    filled = 0
    for message in catalog:
        if message.id and not message.string and message.id in translations:
            message.string = translations[str(message.id)]
            filled += 1
    with PO_FILE.open("wb") as fh:
        write_po(fh, catalog, no_location=True, width=100)
    print(f"Filled {filled} translations")


if __name__ == "__main__":
    main(sys.argv[1])
