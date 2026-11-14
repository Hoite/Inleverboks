"""Filesystem storage backend (default). Objects live under ``STORAGE_PATH``."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from anyio import to_thread

from inleverboks.adapters.storage.base import ObjectNotFoundError, StorageError

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*$")


class LocalStorage:
    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key) or ".." in key.split("/"):
            msg = f"Invalid storage key: {key!r}"
            raise StorageError(msg)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            msg = f"Storage key escapes root: {key!r}"
            raise StorageError(msg)
        return path

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        path = self._path(key)
        await to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))
        fh = await to_thread.run_sync(lambda: path.open("xb"))
        written = 0
        try:
            async for chunk in chunks:
                await to_thread.run_sync(fh.write, chunk)
                written += len(chunk)
            await to_thread.run_sync(fh.flush)
            await to_thread.run_sync(os.fsync, fh.fileno())
        except BaseException:
            fh.close()
            path.unlink(missing_ok=True)
            raise
        fh.close()
        return written

    async def open(self, key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            fh = await to_thread.run_sync(path.open, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        try:
            while chunk := await to_thread.run_sync(fh.read, chunk_size):
                yield chunk
        finally:
            fh.close()

    async def move(self, src: str, dst: str) -> None:
        src_path, dst_path = self._path(src), self._path(dst)

        def _move() -> None:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if dst_path.exists():
                msg = f"Destination exists: {dst!r}"
                raise StorageError(msg)
            try:
                src_path.rename(dst_path)
            except FileNotFoundError as exc:
                raise ObjectNotFoundError(src) from exc

        await to_thread.run_sync(_move)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await to_thread.run_sync(lambda: path.unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await to_thread.run_sync(path.is_file)

    async def size(self, key: str) -> int:
        path = self._path(key)
        try:
            return (await to_thread.run_sync(path.stat)).st_size
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    async def modified(self, key: str) -> datetime:
        path = self._path(key)
        try:
            stat = await to_thread.run_sync(path.stat)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        return datetime.fromtimestamp(stat.st_mtime, UTC)

    async def list_keys(self, prefix: str) -> AsyncIterator[str]:
        base = self._path(prefix) if prefix else self.root

        def _walk() -> list[str]:
            if not base.exists():
                return []
            return sorted(
                p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file()
            )

        for key in await to_thread.run_sync(_walk):
            yield key

    async def healthcheck(self) -> None:
        probe = self.root / f".health-{uuid.uuid4().hex}"

        def _probe() -> None:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"ok")
            probe.unlink()

        try:
            await to_thread.run_sync(_probe)
        except OSError as exc:
            msg = f"Storage not writable: {exc.strerror}"
            raise StorageError(msg) from exc
