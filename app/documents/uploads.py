from __future__ import annotations

from typing import Protocol

from app.platform.errors import PlatformError

_READ_CHUNK_SIZE = 64 * 1024


class UploadReader(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


async def read_limited_upload(file: UploadReader, *, max_bytes: int) -> bytes:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(min(_READ_CHUNK_SIZE, max_bytes - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise PlatformError(
                "upload_too_large",
                "Upload exceeds the maximum size",
                {"max_bytes": max_bytes},
                413,
            )
        chunks.append(chunk)
