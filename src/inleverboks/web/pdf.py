"""HTML → PDF with WeasyPrint, using the same Jinja templates as the web pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment

STATIC_DIR = Path(__file__).parent / "static"


class PdfRenderer:
    def __init__(self, env: Environment) -> None:
        self.env = env

    def render(self, template: str, context: dict[str, Any]) -> bytes:
        # Imported lazily: WeasyPrint loads Pango at import time.
        from weasyprint import HTML  # noqa: PLC0415

        html = self.env.get_template(template).render(context)
        pdf: bytes = HTML(string=html, base_url=str(STATIC_DIR)).write_pdf()
        return pdf
