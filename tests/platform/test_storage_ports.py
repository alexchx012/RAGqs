from __future__ import annotations

import pytest

from app.platform.storage import MemoryObjectStore, ObjectMetadata, S3ObjectStore, StorageKeyError


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}

    def put_object(self, **kwargs) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        stored = self.objects[(Bucket, Key)]
        return {
            "Body": ChunkedBody((bytes(stored["Body"]),)),
            "ContentType": stored["ContentType"],
            "ContentLength": len(stored["Body"]),
            "Metadata": stored["Metadata"],
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        stored = self.objects[(Bucket, Key)]
        return {"ContentLength": len(stored["Body"])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del self.objects[(Bucket, Key)]


class ChunkedBody:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.chunk_size: int | None = None

    def read(self) -> bytes:
        raise AssertionError("S3 reads must not materialize the body with read()")

    def iter_chunks(self, chunk_size: int) -> tuple[bytes, ...]:
        self.chunk_size = chunk_size
        return self.chunks


class ChunkedS3Client:
    def __init__(self, body: ChunkedBody) -> None:
        self.body = body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        assert Bucket == "documents"
        assert Key == "documents/doc-1"
        return {
            "Body": self.body,
            "ContentType": "text/plain",
            "ContentLength": 6,
            "Metadata": {"checksum_sha256": "digest"},
        }


def test_object_store_can_be_replaced_with_deterministic_fake() -> None:
    store = MemoryObjectStore()
    metadata = ObjectMetadata(content_type="application/pdf", size_bytes=3)

    assert store.exists("documents/doc-1") is False
    store.put("documents/doc-1", b"pdf", metadata)

    assert store.exists("documents/doc-1") is True
    assert store.get("documents/doc-1") == (b"pdf", metadata)
    store.delete("documents/doc-1")
    assert store.exists("documents/doc-1") is False
    with pytest.raises(StorageKeyError):
        store.get("documents/doc-1")


def test_object_store_rejects_empty_keys_and_invalid_metadata() -> None:
    store = MemoryObjectStore()

    with pytest.raises(ValueError, match="key"):
        store.put("", b"data", ObjectMetadata(content_type="text/plain", size_bytes=4))
    with pytest.raises(ValueError, match="size"):
        store.put("file", b"data", ObjectMetadata(content_type="text/plain", size_bytes=3))


def test_s3_object_store_reads_content_in_bounded_chunks() -> None:
    body = ChunkedBody((b"abc", b"def"))
    store = S3ObjectStore("documents", client=ChunkedS3Client(body))

    content, metadata = store.get("documents/doc-1")

    assert content == b"abcdef"
    assert metadata == ObjectMetadata("text/plain", 6, "digest")
    assert body.chunk_size is not None


def test_s3_object_store_uses_controlled_bucket_and_preserves_metadata() -> None:
    client = FakeS3Client()
    store = S3ObjectStore("documents", client=client)
    metadata = ObjectMetadata("application/pdf", 3, "digest")

    store.put("documents/doc-1", b"pdf", metadata)

    assert client.objects[("documents", "documents/doc-1")]["Metadata"] == {
        "checksum_sha256": "digest"
    }
    assert store.exists("documents/doc-1") is True
    assert store.get("documents/doc-1") == (b"pdf", metadata)
    store.delete("documents/doc-1")
    assert store.exists("documents/doc-1") is False
    with pytest.raises(StorageKeyError):
        store.get("documents/doc-1")
