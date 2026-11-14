"""Backup and restore (spec §14.4).

A backup is ``inleverboks-backup-YYYYmmdd-HHMMSS.tar.gz`` with:
- ``database/inleverboks.db``: a consistent SQLite copy (SQLite backup API), or for
  PostgreSQL ``database/README.txt`` explaining ``pg_dump`` (the database is not included);
- ``files/…``: every stored object (local or S3), except temporary uploads;
- ``backup.json``: metadata.

Backups older than BACKUP_KEEP_DAYS are deleted, so deleted exam work also disappears from
the backups within that period.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import make_url

from inleverboks import __version__
from inleverboks.adapters.storage import StorageBackend
from inleverboks.services.asyncutil import call_async

PREFIX = "inleverboks-backup-"
PG_NOTE = """This installation uses PostgreSQL. The database is not part of this archive.
Make a database backup with pg_dump, for example:

    pg_dump --format=custom --file=inleverboks.dump "$DATABASE_URL"

and restore it with pg_restore before restoring the files from this archive.
"""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    objects: int
    database_included: bool


def _sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    tar.addfile(info, io.BytesIO(data))


async def _keys(storage: StorageBackend) -> list[str]:
    return [k async for k in storage.list_keys("") if not k.startswith("tmp/")]


def create_backup(
    database_url: str, storage: StorageBackend, target_dir: Path, now: datetime
) -> BackupResult:
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    final = target_dir / f"{PREFIX}{stamp}.tar.gz"
    partial = final.with_suffix(".partial")
    sqlite_path = _sqlite_path(database_url)
    keys = call_async(_keys, storage)
    with (
        tempfile.TemporaryDirectory(prefix="inleverboks-backup-") as tmp,
        tarfile.open(partial, "w:gz") as tar,
    ):
        if sqlite_path is not None:
            copy = Path(tmp) / "inleverboks.db"
            source = sqlite3.connect(sqlite_path)
            dest = sqlite3.connect(copy)
            with dest:
                source.backup(dest)  # consistent even while the app writes
            source.close()
            dest.close()
            tar.add(copy, arcname="database/inleverboks.db")
        else:
            _add_bytes(tar, "database/README.txt", PG_NOTE.encode(), now.timestamp())
        for key in keys:
            local = Path(tmp) / "object"
            with local.open("wb") as fh:

                async def write(key: str = key, fh: io.BufferedWriter = fh) -> None:
                    async for chunk in storage.open(key):
                        fh.write(chunk)

                call_async(write)
            tar.add(local, arcname=f"files/{key}")
        meta = {
            "created_at": now.isoformat(),
            "version": __version__,
            "objects": len(keys),
            "database": "sqlite" if sqlite_path else "postgresql (not included)",
        }
        _add_bytes(tar, "backup.json", json.dumps(meta, indent=2).encode(), now.timestamp())
    partial.rename(final)
    return BackupResult(path=final, objects=len(keys), database_included=sqlite_path is not None)


def prune_backups(target_dir: Path, now: datetime, keep_days: int) -> list[Path]:
    """Delete backups older than ``keep_days`` (by the timestamp in the file name)."""
    removed = []
    cutoff = now - timedelta(days=keep_days)
    for path in target_dir.glob(f"{PREFIX}*.tar.gz"):
        stamp = path.name.removeprefix(PREFIX).removesuffix(".tar.gz")
        try:
            created = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        if created < cutoff:
            path.unlink()
            removed.append(path)
    return removed


@dataclass(frozen=True)
class RestoreResult:
    objects: int
    database_restored: bool


def restore_backup(archive: Path, database_url: str, storage: StorageBackend) -> RestoreResult:
    """Restore files (overwriting) and, for SQLite, the database file.

    Stop the application first. The current SQLite file is kept as ``.before-restore``.
    """
    sqlite_path = _sqlite_path(database_url)
    restored = 0
    database_restored = False
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if ".." in name.split("/") or name.startswith("/"):
                continue  # never trust paths in an archive
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            if name == "database/inleverboks.db" and sqlite_path is not None:
                if sqlite_path.exists():
                    sqlite_path.replace(sqlite_path.with_name(sqlite_path.name + ".before-restore"))
                for suffix in ("-wal", "-shm"):
                    Path(str(sqlite_path) + suffix).unlink(missing_ok=True)
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)
                sqlite_path.write_bytes(extracted.read())
                database_restored = True
            elif name.startswith("files/"):
                key = name.removeprefix("files/")
                data = extracted.read()

                async def put(key: str = key, data: bytes = data) -> None:
                    async def chunks() -> AsyncIterator[bytes]:
                        yield data

                    await storage.delete(key)
                    await storage.put(key, chunks())

                call_async(put)
                restored += 1
    return RestoreResult(objects=restored, database_restored=database_restored)
