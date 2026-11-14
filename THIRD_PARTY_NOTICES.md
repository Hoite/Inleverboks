# Licenties van derden

Inleverboks zelf valt onder de EUPL-1.2 (zie `LICENSE`).

## Meegeleverde JavaScript en lettertypen

Deze bestanden staan in `src/inleverboks/web/static/vendor/`. Ze worden tijdens het gebruik
nooit van een CDN geladen. De versie, de bron-URL en de SHA-256 per bestand staan in
`vendor.lock.json`.

| Component | Versie | Licentie | Bron |
|---|---|---|---|
| htmx | 2.0.11 | 0BSD | `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz` (`dist/htmx.min.js`) |
| *(volgt in fase 4 en 7: pdf.js, docx-preview, JSZip, SheetJS, Poppins)* | | | |

Bijwerken of controleren gaat met `uv run python scripts/vendor.py`. Een test controleert dat
elk bestand overeenkomt met de vastgelegde SHA-256.

## Python-afhankelijkheden

De Python-pakketten worden geïnstalleerd vanuit PyPI en zijn vastgepind in `uv.lock`. De
belangrijkste zijn: FastAPI (MIT), Starlette (BSD-3), Uvicorn (BSD-3), Jinja2 (BSD-3),
SQLAlchemy (MIT), Alembic (MIT), Pydantic (MIT), argon2-cffi (MIT), pyotp (MIT),
segno (BSD-3), WeasyPrint (BSD-3), zipstream-ng (LGPL-3.0), Typer (MIT), cryptography
(Apache-2.0/BSD), psycopg (LGPL-3.0), boto3 (Apache-2.0), APScheduler (MIT), structlog
(MIT/Apache-2.0), Babel (BSD-3) en charset-normalizer (MIT).

LGPL-pakketten (zipstream-ng en psycopg) worden ongewijzigd en dynamisch gebruikt, wat
verenigbaar is met de EUPL-1.2.
