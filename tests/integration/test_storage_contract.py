"""One test suite for every StorageBackend: local filesystem and S3.

S3 runs against moto by default. Set ``TEST_S3_ENDPOINT`` (plus TEST_S3_ACCESS_KEY and
TEST_S3_SECRET_KEY) to run the same suite against a real MinIO.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from moto import mock_aws

from inleverboks.adapters.storage import LocalStorage, ObjectNotFoundError, S3Storage, StorageError
from inleverboks.adapters.storage.base import StorageBackend

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(params=["local", "s3"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StorageBackend]:
    if request.param == "local":
        yield LocalStorage(tmp_path / "files")
        return
    endpoint = os.environ.get("TEST_S3_ENDPOINT")
    if endpoint:
        yield S3Storage(
            endpoint=endpoint,
            bucket=f"test-{uuid.uuid4().hex[:12]}",
            region="us-east-1",
            access_key=os.environ.get("TEST_S3_ACCESS_KEY", "inleverboks"),
            secret_key=os.environ.get("TEST_S3_SECRET_KEY", "inleverboks-dev-only"),
        )
        return
    with mock_aws():
        yield S3Storage(
            endpoint=None, bucket="test-bucket", region="us-east-1",
            access_key="testing", secret_key="testing",
        )  # fmt: skip


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _read(storage: StorageBackend, key: str) -> bytes:
    return b"".join([chunk async for chunk in storage.open(key)])


async def test_roundtrip(storage: StorageBackend) -> None:
    assert await storage.put("tmp/a1", _chunks(b"hello ", b"world")) == 11
    assert await storage.exists("tmp/a1")
    assert await storage.size("tmp/a1") == 11
    assert await _read(storage, "tmp/a1") == b"hello world"


async def test_empty_object(storage: StorageBackend) -> None:
    assert await storage.put("tmp/empty", _chunks()) == 0
    assert await _read(storage, "tmp/empty") == b""


async def test_large_object_streams_in_parts(storage: StorageBackend) -> None:
    chunk = bytes(range(256)) * 4096  # 1 MiB
    parts = [chunk] * 10  # 10 MiB: more than one S3 multipart part
    assert await storage.put("tmp/big", _chunks(*parts)) == 10 * len(chunk)
    data = await _read(storage, "tmp/big")
    assert data == chunk * 10


async def test_move_promotes_object(storage: StorageBackend) -> None:
    await storage.put("tmp/x", _chunks(b"data"))
    await storage.move("tmp/x", "exam/sub/file")
    assert not await storage.exists("tmp/x")
    assert await _read(storage, "exam/sub/file") == b"data"


async def test_move_refuses_existing_destination(storage: StorageBackend) -> None:
    await storage.put("tmp/x", _chunks(b"1"))
    await storage.put("exam/y", _chunks(b"2"))
    with pytest.raises(StorageError):
        await storage.move("tmp/x", "exam/y")
    assert await _read(storage, "exam/y") == b"2"


async def test_put_refuses_overwrite(storage: StorageBackend) -> None:
    await storage.put("exam/a", _chunks(b"1"))
    with pytest.raises(FileExistsError):
        await storage.put("exam/a", _chunks(b"2"))


async def test_missing_objects(storage: StorageBackend) -> None:
    assert not await storage.exists("exam/nope")
    with pytest.raises(ObjectNotFoundError):
        await _read(storage, "exam/nope")
    with pytest.raises(ObjectNotFoundError):
        await storage.size("exam/nope")
    with pytest.raises(ObjectNotFoundError):
        await storage.move("exam/nope", "exam/other")
    await storage.delete("exam/nope")  # deleting a missing key is fine


async def test_delete_and_list(storage: StorageBackend) -> None:
    for key in ("e1/s1/a", "e1/s1/b", "e1/s2/c", "e2/s3/d"):
        await storage.put(key, _chunks(b"x"))
    assert sorted([k async for k in storage.list_keys("e1")]) == ["e1/s1/a", "e1/s1/b", "e1/s2/c"]
    await storage.delete("e1/s1/a")
    assert sorted([k async for k in storage.list_keys("e1")]) == ["e1/s1/b", "e1/s2/c"]


async def test_failed_put_leaves_nothing(storage: StorageBackend) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("connection dropped")

    with pytest.raises(RuntimeError):
        await storage.put("tmp/broken", broken())
    assert not await storage.exists("tmp/broken")


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs", "a/../../b", "a//b", "a\\b", ""])
async def test_invalid_keys(storage: StorageBackend, key: str) -> None:
    with pytest.raises(StorageError):
        await storage.exists(key)


async def test_healthcheck(storage: StorageBackend) -> None:
    await storage.healthcheck()
