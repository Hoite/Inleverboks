"""Alembic environment. The URL is injected by ``inleverboks.db.migrate``."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

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
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            _prepare_sqlite(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=sqlite,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            _check_foreign_keys(connection)
    engine.dispose()


def _prepare_sqlite(connection: Connection) -> None:
    """Make batch migrations safe on SQLite.

    A batch migration copies a table, drops the original and renames the copy. With
    foreign keys on, that DROP is an implicit DELETE: child rows with ON DELETE CASCADE
    disappear and other references make it fail halfway. So foreign keys are off while
    migrating (the setting is ignored inside a transaction, hence the commit), and
    checked afterwards. Copies left behind by an interrupted batch are removed: they are
    only ever a transient part of a migration.
    """
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.commit()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        msg = "could not turn off SQLite foreign keys for the migration"
        raise RuntimeError(msg)
    leftovers = (
        connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '\\_alembic\\_tmp\\_%'"
            " ESCAPE '\\'"
        )
        .scalars()
        .all()
    )
    for name in leftovers:
        connection.exec_driver_sql(f'DROP TABLE "{name}"')
    connection.commit()


def _check_foreign_keys(connection: Connection) -> None:
    problems = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if problems:
        tables = sorted({str(row[0]) for row in problems})
        msg = f"foreign key check failed after migrating: {', '.join(tables)}"
        raise RuntimeError(msg)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
