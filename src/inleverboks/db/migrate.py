"""Alembic helpers, configured in code so no alembic.ini is needed at runtime."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from inleverboks.db.session import normalize_url

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", normalize_url(database_url).replace("%", "%%"))
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def current(database_url: str) -> None:
    command.current(alembic_config(database_url), verbose=False)


def revision(database_url: str, message: str) -> None:
    command.revision(alembic_config(database_url), message=message, autogenerate=True)
