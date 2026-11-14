"""Download and verify vendored front-end files listed in ``vendor.lock.json``.

Usage:
    uv run python scripts/vendor.py           # download missing files, verify all checksums
    uv run python scripts/vendor.py --pin     # (re)compute sha256 for entries without one

Each entry names a package tarball (npm) or a plain file URL, the member to extract and
the destination under ``src/inleverboks/web/static/vendor/``. Files are never loaded from
a CDN at runtime; this script only runs at development time.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "vendor.lock.json"
VENDOR_DIR = ROOT / "src" / "inleverboks" / "web" / "static" / "vendor"


def _fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        msg = f"Refusing non-HTTPS URL: {url}"
        raise SystemExit(msg)
    request = urllib.request.Request(url, headers={"User-Agent": "inleverboks-vendor"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        data: bytes = response.read()
        return data


def _extract(entry: dict[str, Any], payload: bytes) -> bytes:
    member = entry.get("member")
    if not member:
        return payload
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        extracted = tar.extractfile(member)
        if extracted is None:
            msg = f"{member} not found in {entry['url']}"
            raise SystemExit(msg)
        return extracted.read()


def main() -> int:
    pin = "--pin" in sys.argv
    lock: dict[str, Any] = json.loads(LOCK.read_text())
    failures = 0
    for entry in lock["files"]:
        dest = VENDOR_DIR / entry["dest"]
        if not dest.exists() or pin:
            data = _extract(entry, _fetch(entry["url"]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        if not entry.get("sha256") or (pin and entry["sha256"] != digest):
            entry["sha256"] = digest
            print(f"pinned  {entry['dest']}  {digest}")
        elif entry["sha256"] != digest:
            print(f"MISMATCH {entry['dest']}: expected {entry['sha256']}, got {digest}")
            failures += 1
        else:
            print(f"ok      {entry['dest']}")
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
