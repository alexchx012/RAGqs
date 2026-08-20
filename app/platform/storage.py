from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class StorageKeyError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    content_type: str
    size_bytes: int
    checksum_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.content_type or self.size_bytes < 0:
            raise ValueError("object metadata content_type and size must be valid")


class ObjectStorePort(Protocol):
    def put(self, key: str, content: bytes, metadata: ObjectMetadata) -> None: ...

    def get(self, key: str) -> tuple[bytes, ObjectMetadata]: ...

    def copy(self, source_key: str, target_key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class MemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, ObjectMetadata]] = {}

    def put(self, key: str, content: bytes, metadata: ObjectMetadata) -> None:
        if not key:
            raise ValueError("object key must not be empty")
        if len(content) != metadata.size_bytes:
            raise ValueError("object metadata size does not match content")
        self._objects[key] = (bytes(content), metadata)

    def get(self, key: str) -> tuple[bytes, ObjectMetadata]:
        try:
            content, metadata = self._objects[key]
        except KeyError as exc:
            raise StorageKeyError(key) from exc
        return bytes(content), metadata

    def copy(self, source_key: str, target_key: str) -> None:
        content, metadata = self._objects[source_key]
        self._objects[target_key] = (content, metadata)

    def exists(self, key: str) -> bool:
        if not key:
            raise ValueError("object key must not be empty")
        return key in self._objects

    def delete(self, key: str) -> None:
        if key not in self._objects:
            raise StorageKeyError(key)
        del self._objects[key]


class S3ObjectStore:
    """S3-compatible object storage adapter with a fixed deployment bucket."""

    def __init__(
        self,
        bucket: str,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("object storage bucket must not be empty")
        self._bucket = bucket
        self._client = client
        self._client_factory = client_factory

    def _active_client(self) -> Any:
        if self._client is None:
            if self._client_factory is None:
                raise RuntimeError("S3 object store has no configured client")
            self._client = self._client_factory()
        return self._client

    @staticmethod
    def _validate(
        key: str, content: bytes | None = None, metadata: ObjectMetadata | None = None
    ) -> None:
        if not key:
            raise ValueError("object key must not be empty")
        if content is not None and metadata is not None and len(content) != metadata.size_bytes:
            raise ValueError("object metadata size does not match content")

    def put(self, key: str, content: bytes, metadata: ObjectMetadata) -> None:
        self._validate(key, content, metadata)
        custom_metadata = {}
        if metadata.checksum_sha256 is not None:
            custom_metadata["checksum_sha256"] = metadata.checksum_sha256
        try:
            self._active_client().put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=metadata.content_type,
                Metadata=custom_metadata,
            )
        except Exception as exc:
            raise StorageKeyError(key) from exc

    def get(self, key: str) -> tuple[bytes, ObjectMetadata]:
        self._validate(key)
        try:
            response = self._active_client().get_object(Bucket=self._bucket, Key=key)
            content = b"".join(
                chunk for chunk in response["Body"].iter_chunks(chunk_size=64 * 1024) if chunk
            )
            custom_metadata = response.get("Metadata", {})
            metadata = ObjectMetadata(
                content_type=str(response.get("ContentType") or "application/octet-stream"),
                size_bytes=int(response.get("ContentLength", len(content))),
                checksum_sha256=custom_metadata.get("checksum_sha256"),
            )
        except Exception as exc:
            raise StorageKeyError(key) from exc
        if len(content) != metadata.size_bytes:
            raise StorageKeyError(key)
        return content, metadata

    def copy(self, source_key: str, target_key: str) -> None:
        self._validate(source_key)
        self._validate(target_key)
        try:
            self._active_client().copy_object(
                Bucket=self._bucket,
                Key=target_key,
                CopySource={"Bucket": self._bucket, "Key": source_key},
            )
        except Exception as exc:
            raise StorageKeyError(source_key) from exc

    def exists(self, key: str) -> bool:
        self._validate(key)
        try:
            self._active_client().head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if self._is_missing_object_error(exc):
                return False
            raise StorageKeyError(key) from exc
        return True

    def delete(self, key: str) -> None:
        self._validate(key)
        try:
            self._active_client().delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise StorageKeyError(key) from exc

    @staticmethod
    def _is_missing_object_error(exc: Exception) -> bool:
        if isinstance(exc, KeyError):
            return True
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error")
        if not isinstance(error, dict):
            return False
        code = str(error.get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def build_object_store(
    *,
    endpoint: str,
    bucket: str,
    access_key: str | None,
    secret_key: str | None,
) -> S3ObjectStore:
    """Create a lazy S3-compatible adapter without performing network I/O at import time."""

    def create_client() -> Any:
        import boto3  # type: ignore[import-untyped]

        options: dict[str, Any] = {"service_name": "s3", "endpoint_url": endpoint}
        if access_key is not None:
            options["aws_access_key_id"] = access_key
        if secret_key is not None:
            options["aws_secret_access_key"] = secret_key
        return boto3.client(**options)

    return S3ObjectStore(bucket, client_factory=create_client)
