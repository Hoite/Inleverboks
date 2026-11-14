"""S3-compatible storage backend (AWS S3, MinIO, ...).

boto3 is synchronous, so calls run in a worker thread. Uploads use multipart upload
with 8 MiB parts, so large files are never held in memory as a whole.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from typing import Any

import boto3
from anyio import to_thread
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from inleverboks.adapters.storage.base import ObjectNotFoundError, StorageError

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*$")
PART_SIZE = 8 * 1024 * 1024


def _check_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key) or ".." in key.split("/"):
        msg = f"Invalid storage key: {key!r}"
        raise StorageError(msg)
    return key


def _is_missing(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


class S3Storage:
    name = "s3"

    def __init__(
        self,
        *,
        endpoint: str | None,
        bucket: str,
        region: str | None,
        access_key: str,
        secret_key: str,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region or "us-east-1",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        self._bucket_checked = False

    async def _call(self, method: str, **kwargs: Any) -> Any:
        if method not in {"head_bucket", "create_bucket"}:
            await self.ensure_bucket()
        fn = getattr(self._client, method)
        try:
            return await to_thread.run_sync(lambda: fn(**kwargs))
        except ClientError as exc:
            if _is_missing(exc):
                raise ObjectNotFoundError(kwargs.get("Key", "")) from exc
            raise StorageError(str(exc)) from exc
        except BotoCoreError as exc:
            raise StorageError(str(exc)) from exc

    async def ensure_bucket(self) -> None:
        if self._bucket_checked:
            return
        try:
            await self._call("head_bucket", Bucket=self.bucket)
        except ObjectNotFoundError:
            await self._call("create_bucket", Bucket=self.bucket)
        self._bucket_checked = True

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        _check_key(key)
        await self.ensure_bucket()
        if await self.exists(key):
            msg = f"Object exists: {key!r}"
            raise FileExistsError(msg)
        upload = await self._call("create_multipart_upload", Bucket=self.bucket, Key=key)
        upload_id = upload["UploadId"]
        parts: list[dict[str, Any]] = []
        buffer = bytearray()
        written = 0
        try:
            async for chunk in chunks:
                buffer += chunk
                written += len(chunk)
                while len(buffer) >= PART_SIZE:
                    await self._upload_part(key, upload_id, parts, bytes(buffer[:PART_SIZE]))
                    del buffer[:PART_SIZE]
            if buffer or not parts:
                await self._upload_part(key, upload_id, parts, bytes(buffer))
            await self._call(
                "complete_multipart_upload",
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            await self._call(
                "abort_multipart_upload", Bucket=self.bucket, Key=key, UploadId=upload_id
            )
            raise
        return written

    async def _upload_part(
        self, key: str, upload_id: str, parts: list[dict[str, Any]], data: bytes
    ) -> None:
        number = len(parts) + 1
        result = await self._call(
            "upload_part",
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=number,
            Body=data,
        )
        parts.append({"ETag": result["ETag"], "PartNumber": number})

    async def open(self, key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_key(key)
        result = await self._call("get_object", Bucket=self.bucket, Key=key)
        body = result["Body"]
        try:
            while chunk := await to_thread.run_sync(body.read, chunk_size):
                yield chunk
        finally:
            body.close()

    async def move(self, src: str, dst: str) -> None:
        _check_key(src)
        _check_key(dst)
        if await self.exists(dst):
            msg = f"Destination exists: {dst!r}"
            raise StorageError(msg)
        if not await self.exists(src):
            raise ObjectNotFoundError(src)
        await self._call(
            "copy_object",
            Bucket=self.bucket,
            Key=dst,
            CopySource={"Bucket": self.bucket, "Key": src},
        )
        await self._call("delete_object", Bucket=self.bucket, Key=src)

    async def delete(self, key: str) -> None:
        _check_key(key)
        with contextlib.suppress(ObjectNotFoundError):
            await self._call("delete_object", Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        _check_key(key)
        try:
            await self._call("head_object", Bucket=self.bucket, Key=key)
        except ObjectNotFoundError:
            return False
        return True

    async def size(self, key: str) -> int:
        _check_key(key)
        result = await self._call("head_object", Bucket=self.bucket, Key=key)
        return int(result["ContentLength"])

    async def list_keys(self, prefix: str) -> AsyncIterator[str]:
        if prefix:
            _check_key(prefix)
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            result = await self._call("list_objects_v2", **kwargs)
            for item in result.get("Contents", []):
                yield item["Key"]
            if not result.get("IsTruncated"):
                return
            token = result.get("NextContinuationToken")

    async def healthcheck(self) -> None:
        try:
            await self.ensure_bucket()
            await self._call("head_bucket", Bucket=self.bucket)
        except ObjectNotFoundError as exc:
            msg = "S3 bucket not found"
            raise StorageError(msg) from exc
