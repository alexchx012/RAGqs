from __future__ import annotations

import asyncio

import pytest

from app.documents.uploads import read_limited_upload
from app.platform.errors import PlatformError


class _ChunkedUpload:
    def __init__(self, content: bytes) -> None:
        self._remaining = content
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            chunk, self._remaining = self._remaining, b""
            return chunk
        chunk = self._remaining[:size]
        self._remaining = self._remaining[size:]
        return chunk


def test_document_upload_reader_stops_after_the_size_limit() -> None:
    upload = _ChunkedUpload(b"0123456789abcdef")

    async def read_upload() -> PlatformError:
        with pytest.raises(PlatformError) as error:
            await read_limited_upload(upload, max_bytes=8)
        return error.value

    error = asyncio.run(read_upload())

    assert error.code == "upload_too_large"
    assert error.status_code == 413
    assert upload.read_sizes == [9]
    assert upload._remaining == b"9abcdef"
