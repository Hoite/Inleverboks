"""Safe file download responses: always an attachment, never sniffed or inlined."""

from __future__ import annotations

import unicodedata
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi.responses import Response, StreamingResponse


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII fallback and an RFC 5987 ``filename*``."""
    normalized = unicodedata.normalize("NFC", filename).replace("\r", "").replace("\n", "")
    fallback = (
        unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode() or "download"
    )
    fallback = fallback.replace("\\", "_").replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(normalized, safe='')}"


def download_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": content_disposition(filename),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }


def bytes_download(data: bytes, filename: str, media_type: str) -> Response:
    return Response(content=data, media_type=media_type, headers=download_headers(filename))


def stream_download(
    chunks: AsyncIterator[bytes], filename: str, *, size: int | None = None
) -> StreamingResponse:
    headers = download_headers(filename)
    if size is not None:
        headers["Content-Length"] = str(size)
    return StreamingResponse(chunks, media_type="application/octet-stream", headers=headers)
