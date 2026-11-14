"""Alembic environment. The URL is injected by ``inleverboks.db.migrate``."""

from __future__ import annotations

from alembic import context

import inleverboks.db.models  # noqa: F401  # registers all tables on Base.metadata
from inleverboks.db.base import Base
from inleverboks.db.session import build_engine

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        msg = "sqlalchemy.url is not configured"
        raise RuntimeError(msg)
    engine = build_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
