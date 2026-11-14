"""Vendored front-end files must match the pinned checksums in vendor.lock.json."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "src" / "inleverboks" / "web" / "static" / "vendor"


def test_vendored_files_match_lock() -> None:
    lock = json.loads((ROOT / "vendor.lock.json").read_text())
    assert lock["files"]
    for entry in lock["files"]:
        path = VENDOR / entry["dest"]
        assert path.is_file(), f"missing {entry['dest']}: run scripts/vendor.py"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["dest"]
        assert entry["version"]
        assert entry["license"]


def test_no_cdn_references_in_templates() -> None:
    templates = ROOT / "src" / "inleverboks" / "web" / "templates"
    for template in templates.rglob("*.j2"):
        text = template.read_text()
        for marker in ("https://", "http://cdn", "//cdn", "unpkg.com", "jsdelivr"):
            assert marker not in text, f"{template.name} references {marker}"
