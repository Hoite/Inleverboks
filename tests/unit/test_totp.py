from __future__ import annotations

import re

import segno

from inleverboks.services.totp import QUIET_ZONE, TotpService


def test_qr_svg_scales_without_cropping() -> None:
    """The authenticator QR is resized by CSS: it needs a viewBox and no fixed size, and
    a quiet zone of 4 modules (a cropped code with a thin border did not scan)."""
    service = TotpService("x" * 40)
    uri = service.provisioning_uri("Q6P7GUE4LK4O3CDMPQLD3QYK4PHHTSV5", "docent@school.nl")
    svg = service.qr_svg(uri)
    assert QUIET_ZONE == 4
    assert 'width="' not in svg.split(">", 1)[0]
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert match is not None
    modules = segno.make(uri, error="m").symbol_size(scale=1, border=0)[0]
    assert int(match.group(1)) == (modules + 2 * QUIET_ZONE) * 5
