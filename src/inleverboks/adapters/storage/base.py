"""Storage backend interface. Implementations: ``local`` (filesystem) and ``s3``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


class StorageError(Exception):
    pass


class ObjectNotFoundError(StorageError):
    pass


@runtime_checkable
class StorageBackend(Protocol):
    """Stores opaque objects under keys such as ``{exam_id}/{submission_id}/{uuid}``.

    Uploads are first written to a temporary key and then promoted with :meth:`move`
    once fully verified, so a half-finished upload is never visible under its final key.
    """

    name: str

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        """Write all chunks to ``key``; returns the number of bytes written."""
        ...

    def open(self, key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Stream the object's bytes. Raises ObjectNotFoundError."""
        ...

    async def move(self, src: str, dst: str) -> None: ...

    async def delete(self, key: str) -> None:
        """Delete an object; deleting a missing key is not an error."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def size(self, key: str) -> int: ...

    def list_keys(self, prefix: str) -> AsyncIterator[str]: ...

    async def healthcheck(self) -> None:
        """Raise StorageError if the backend is not usable."""
        ...
