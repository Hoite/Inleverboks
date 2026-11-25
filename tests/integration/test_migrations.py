"""Migrations on SQLite keep existing data.

A batch migration recreates a table. With foreign keys on, dropping the old table
deletes child rows (ON DELETE CASCADE) or fails halfway because of other references,
leaving ``_alembic_tmp_*`` behind. These tests migrate a database that has data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from inleverboks.db import migrate

INITIAL = "75935fec392f"
USER, TEMPLATE, SLOT, EXAM, FILE, SUBMISSION = (c * 32 for c in "abcdef")
TABLES = ("exam_template", "exam_template_slot", "exam", "expected_file", "submission")


def _insert(db: sqlite3.Connection, table: str, **given: object) -> None:
    """Insert a row, with a placeholder for every required column not given."""
    values: dict[str, object] = {}
    for _cid, name, kind, notnull, default, _pk in db.execute(f"PRAGMA table_info({table})"):
        if name in given:
            values[name] = given[name]
        elif notnull and default is None:
            sql_type = str(kind).upper()
            if "INT" in sql_type or "BOOL" in sql_type:
                values[name] = 0
            elif "JSON" in sql_type:
                values[name] = "[]"
            elif "DATE" in sql_type:
                values[name] = "2026-01-01 00:00:00"
            else:
                values[name] = f"{table}-{name}"
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    query = f"INSERT INTO {table} ({columns}) VALUES ({marks})"  # noqa: S608 (fixed names)
    db.execute(query, list(values.values()))


def _seeded(tmp_path: Path) -> tuple[str, Path]:
    path = tmp_path / "old.db"
    url = f"sqlite:///{path}"
    migrate.upgrade(url, INITIAL)
    with sqlite3.connect(path) as db:
        _insert(db, "app_user", id=USER)
        _insert(db, "exam_template", id=TEMPLATE, owner_id=USER)
        _insert(db, "exam_template_slot", id=SLOT, template_id=TEMPLATE, position=1)
        _insert(db, "exam", id=EXAM, owner_id=USER, slug="oud-examen")
        _insert(db, "expected_file", id=FILE, exam_id=EXAM, position=1)
        _insert(db, "submission", id=SUBMISSION, exam_id=EXAM, status="active", attempt=1)
    return url, path


def _counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        query = "SELECT count(*) FROM {}"  # fixed table names
        return {t: db.execute(query.format(t)).fetchone()[0] for t in TABLES}


def _leftovers(path: Path) -> list[str]:
    with sqlite3.connect(path) as db:
        query = "SELECT name FROM sqlite_master WHERE name LIKE '%alembic_tmp%'"
        return [row[0] for row in db.execute(query)]


def test_upgrade_keeps_rows_of_recreated_tables(tmp_path: Path) -> None:
    url, path = _seeded(tmp_path)

    migrate.upgrade(url)

    assert _counts(path) == dict.fromkeys(TABLES, 1)
    assert _leftovers(path) == []
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_removes_copy_left_by_an_interrupted_batch(tmp_path: Path) -> None:
    url, path = _seeded(tmp_path)
    migrate.upgrade(url, "c3e8a1f04b27")
    with sqlite3.connect(path) as db:  # what a failed batch on "exam" leaves behind
        db.execute("CREATE TABLE _alembic_tmp_exam (id CHAR(32) NOT NULL PRIMARY KEY)")

    migrate.upgrade(url)

    assert _counts(path) == dict.fromkeys(TABLES, 1)
    assert _leftovers(path) == []
