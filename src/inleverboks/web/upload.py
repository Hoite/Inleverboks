"""Streaming multipart parser for the hand-in upload.

Reads the request body chunk by chunk, writes each file part to its own temporary file
while computing SHA-256 and enforcing the size limits as the bytes arrive. Nothing is
held in memory as a whole and nothing is unpacked or converted.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from inleverboks.services.submissions import ReceivedFile

MAX_FIELD_BYTES = 64 * 1024
MAX_FIELDS = 10


class UploadError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ParsedUpload:
    fields: dict[str, str] = field(default_factory=dict)
    files: list[ReceivedFile] = field(default_factory=list)
    temp_dir: Path | None = None

    def cleanup(self) -> None:
        for file in self.files:
            file.path.unlink(missing_ok=True)
        if self.temp_dir is not None:
            for leftover in self.temp_dir.glob("*"):
                leftover.unlink(missing_ok=True)
            self.temp_dir.rmdir()
            self.temp_dir = None


class _Collector:
    def __init__(
        self, result: ParsedUpload, *, max_file: int, max_total: int, max_files: int
    ) -> None:
        self.result = result
        self.max_file = max_file
        self.max_total = max_total
        self.max_files = max_files
        self.total = 0
        self._header_field = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}
        self._name = ""
        self._filename: str | None = None
        self._buffer = bytearray()
        self._fh: IO[bytes] | None = None
        self._path: Path | None = None
        self._hash: Any = None
        self._size = 0

    # Header callbacks
    def on_part_begin(self) -> None:
        self._headers = {}

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._header_field.lower()] = self._header_value
        self._header_field = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        self._name = params.get(b"name", b"").decode("utf-8", "replace")
        raw_filename = params.get(b"filename")
        self._filename = (
            raw_filename.decode("utf-8", "replace") if raw_filename is not None else None
        )
        if self._filename is not None:
            if len(self.result.files) >= self.max_files:
                raise UploadError("too_many_files")
            fd, path = tempfile.mkstemp(dir=self.result.temp_dir)
            self._fh = os.fdopen(fd, "wb")
            self._path = Path(path)
            self._hash = hashlib.sha256()
            self._size = 0
        else:
            if len(self.result.fields) >= MAX_FIELDS:
                raise UploadError("bad_request")
            self._buffer = bytearray()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._fh is not None:
            self._size += len(chunk)
            self.total += len(chunk)
            if self._size > self.max_file:
                raise UploadError("file_too_large")
            if self.total > self.max_total:
                raise UploadError("submission_too_large")
            self._hash.update(chunk)
            self._fh.write(chunk)
        else:
            self._buffer += chunk
            if len(self._buffer) > MAX_FIELD_BYTES:
                raise UploadError("bad_request")

    def on_part_end(self) -> None:
        if self._fh is not None and self._path is not None:
            self._fh.close()
            self.result.files.append(
                ReceivedFile(
                    filename=self._filename or "",
                    size=self._size,
                    sha256=self._hash.hexdigest(),
                    path=self._path,
                )
            )
            self._fh = None
            self._path = None
        else:
            self.result.fields[self._name] = self._buffer.decode("utf-8", "replace")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


async def parse_upload(
    request: Request, *, max_file: int, max_total: int, max_files: int
) -> ParsedUpload:
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UploadError("bad_request")
    result = ParsedUpload(temp_dir=Path(tempfile.mkdtemp(prefix="inleverboks-upload-")))
    collector = _Collector(result, max_file=max_file, max_total=max_total, max_files=max_files)
    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": collector.on_part_begin,
            "on_header_field": collector.on_header_field,
            "on_header_value": collector.on_header_value,
            "on_header_end": collector.on_header_end,
            "on_headers_finished": collector.on_headers_finished,
            "on_part_data": collector.on_part_data,
            "on_part_end": collector.on_part_end,
        },
    )
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    except UploadError:
        collector.close()
        result.cleanup()
        raise
    except Exception as exc:
        collector.close()
        result.cleanup()
        raise UploadError("bad_request") from exc
    return result
