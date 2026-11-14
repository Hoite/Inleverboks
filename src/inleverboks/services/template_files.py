"""Storage of empty template documents ("sjablonen") per slot.

Template files are content-addressed (``templates/{sha256}``): duplicating an exam or
saving it as an exam template only adds a database row pointing at the same object. The
object is deleted when the last row referencing it is removed.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inleverboks.adapters.storage import StorageBackend
from inleverboks.db.models import TemplateFile
from inleverboks.services.errors import DomainError


@dataclass(frozen=True)
class StoredTemplate:
    storage_key: str
    sha256: str
    size: int


def template_key(sha256: str) -> str:
    return f"templates/{sha256}"


async def store_template(
    storage: StorageBackend, chunks: AsyncIterator[bytes], *, max_bytes: int
) -> StoredTemplate:
    """Stream to a temporary key while hashing; promote to the content address."""
    digest = hashlib.sha256()
    size = 0

    async def counted() -> AsyncIterator[bytes]:
        nonlocal size
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise DomainError("template_too_large")
            digest.update(chunk)
            yield chunk

    tmp_key = f"tmp/template-{uuid.uuid4().hex}"
    try:
        await storage.put(tmp_key, counted())
    except BaseException:
        await storage.delete(tmp_key)
        raise
    sha = digest.hexdigest()
    key = template_key(sha)
    if await storage.exists(key):
        await storage.delete(tmp_key)
    else:
        await storage.move(tmp_key, key)
    return StoredTemplate(storage_key=key, sha256=sha, size=size)


def unreferenced(db: Session, storage_key: str) -> bool:
    """True when no TemplateFile row points at this object any more."""
    count = db.scalar(
        select(func.count())
        .select_from(TemplateFile)
        .where(TemplateFile.storage_key == storage_key)
    )
    return not count
