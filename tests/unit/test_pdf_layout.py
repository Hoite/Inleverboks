"""The PDFs keep long names, file names and hashes within the page width."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from inleverboks.services.branding import Branding
from inleverboks.web.templating import build_templates
from tests.conftest import make_settings
from tests.pdf_samples import audit_context, overflowing_boxes, pv_context, receipt_context


@pytest.fixture(scope="module", autouse=True)
def _pango() -> None:
    try:
        import weasyprint  # noqa: F401, PLC0415
    except OSError:
        pytest.skip("Pango is not installed locally (runs in CI and Docker)")


@pytest.mark.parametrize(
    ("template", "context"),
    [
        ("pdf/proces_verbaal.html.j2", pv_context),
        ("pdf/receipt.html.j2", receipt_context),
        ("pdf/audit.html.j2", audit_context),
    ],
)
def test_pdf_content_fits_the_page(
    tmp_path: Path, template: str, context: Callable[[], dict[str, Any]]
) -> None:
    env = build_templates(make_settings(tmp_path)).env
    values = {"app_name": "Inleverboks", "brand": Branding(), **context()}
    assert overflowing_boxes(env, template, values) == []
