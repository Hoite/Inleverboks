"""ASGI entry for uvicorn: ``uvicorn inleverboks.asgi:app``."""

from inleverboks.main import create_app

app = create_app()
