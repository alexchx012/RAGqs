from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from app.platform.errors import PlatformError

from .models import IndexChunk, ProviderSearchPage, normalize_chunks


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StageResult:
    state: str
    attempt_id: str
    publication_id: str
    generation_id: str
    resource_ids: tuple[str, ...]
    content_hash: str
    fencing_token: int = 1

    def to_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "attempt_id": self.attempt_id,
            "publication_id": self.publication_id,
            "generation_id": self.generation_id,
            "resource_ids": list(self.resource_ids),
            "content_hash": self.content_hash,
            "fencing_token": self.fencing_token,
        }


class IndexWriter(Protocol):
    def stage_chunks(
        self,
        attempt_id: str,
        publication_id: str,
        document_id: str,
        document_version_id: str,
        chunks: Sequence[IndexChunk | Mapping[str, Any]],
        *,
        fencing_token: int = 1,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
        usage_context: object | None = None,
    ) -> StageResult: ...

    def publish_staged(
        self,
        attempt_id: str,
        publication_id: str,
        *,
        validator: Callable[[tuple[IndexChunk, ...]], bool] | None = None,
        fencing_token: int | None = None,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
    ) -> StageResult: ...

    def discard_staged(
        self,
        attempt_id: str,
        publication_id: str,
        *,
        fencing_token: int | None = None,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
    ) -> StageResult: ...

    def delete_document_version(
        self, document_id: str, document_version_id: str, *, generation_id: str | None = None
    ) -> int: ...

    def delete_document(self, document_id: str, *, generation_id: str | None = None) -> int: ...


class SparseIndexProvider(IndexWriter, Protocol):
    provider_name: str

    def search(
        self,
        query: str,
        space_ids: Sequence[str],
        top_k: int,
        cursor: str | None,
        *,
        generation_id: str | None = None,
    ) -> ProviderSearchPage: ...


@dataclass(frozen=True, slots=True)
class PreparedStage:
    chunks: tuple[IndexChunk, ...]
    generation_id: str
    resource_ids: tuple[str, ...]
    content_hash: str


def validate_stage_chunks(
    attempt_id: str,
    publication_id: str,
    document_id: str,
    document_version_id: str,
    chunks: Sequence[IndexChunk | Mapping[str, Any]],
    *,
    fencing_token: int = 1,
    expected_generation_id: str | None = None,
    stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
    content_hash: str | None = None,
) -> PreparedStage:
    if not attempt_id.strip() or not publication_id.strip():
        raise PlatformError("validation_error", "attempt and publication are required", {}, 422)
    if fencing_token < 1:
        raise PlatformError("fence_conflict", "fencing token is invalid", {}, 409)
    normalized = normalize_chunks(chunks)
    generation_ids = {item.generation_id for item in normalized}
    if len(generation_ids) > 1:
        raise PlatformError("generation_conflict", "chunks use multiple generations", {}, 409)
    generation_id = next(iter(generation_ids), expected_generation_id)
    if generation_id is None:
        raise PlatformError("validation_error", "generation identity is required", {}, 422)
    if expected_generation_id is not None and generation_id != expected_generation_id:
        raise PlatformError("generation_conflict", "chunk generation is not current", {}, 409)
    if any(
        item.document_id != document_id
        or item.document_version_id != document_version_id
        or item.publication_id != publication_id
        for item in normalized
    ):
        raise PlatformError("processing_receipt_conflict", "chunk identity is invalid", {}, 409)
    if any(not item.indexable for item in normalized):
        raise PlatformError("index_stage_failed", "a chunk is not indexable", {}, 409)
    manifest = tuple(dict(item) for item in (stage_resource_manifest or ()))
    resource_ids = tuple(
        str(item.get("resource_id", "")) for item in manifest if item.get("resource_id")
    )
    if manifest and len(resource_ids) != len(manifest):
        raise PlatformError("validation_error", "stage resource identity is invalid", {}, 422)
    payload = [item.to_mapping() for item in normalized]
    fingerprint = content_hash or _fingerprint(payload)
    return PreparedStage(
        normalized,
        generation_id,
        resource_ids
        or tuple(f"{attempt_id}:{publication_id}:{item.chunk_id}" for item in normalized),
        fingerprint,
    )


def validate_stage_identity(
    result: StageResult,
    *,
    fencing_token: int | None,
    expected_generation_id: str | None,
    stage_resource_manifest: Sequence[Mapping[str, Any]] | None,
    content_hash: str | None,
) -> None:
    if fencing_token is not None and result.fencing_token != fencing_token:
        raise PlatformError("fence_conflict", "staged index fence is no longer current", {}, 409)
    if expected_generation_id is not None and result.generation_id != expected_generation_id:
        raise PlatformError(
            "generation_conflict", "staged index generation is no longer current", {}, 409
        )
    if content_hash is not None and result.content_hash != content_hash:
        raise PlatformError(
            "processing_receipt_conflict", "staged index content does not match", {}, 409
        )
    if stage_resource_manifest is not None:
        resource_ids = tuple(str(item.get("resource_id", "")) for item in stage_resource_manifest)
        if resource_ids != result.resource_ids:
            raise PlatformError(
                "processing_receipt_conflict",
                "staged index resources do not match",
                {},
                409,
            )


@dataclass(slots=True)
class _Stage:
    result: StageResult
    chunks: tuple[IndexChunk, ...]
    fingerprint: str
    fencing_token: int


class InMemoryIndexWriter:
    """Deterministic provider adapter used by the indexing boundary and tests.

    The adapter deliberately keeps staged and published namespaces separate. It
    models provider idempotency without pretending to be a vector engine.
    """

    provider_name = "memory"
    backend_kind = "dense"

    def __init__(self, *, provider_name: str = "memory") -> None:
        self.provider_name = provider_name
        self._lock = RLock()
        self._staged: dict[tuple[str, str], _Stage] = {}
        self._published: dict[tuple[str, str], tuple[IndexChunk, ...]] = {}
        self._terminal: dict[tuple[str, str], StageResult] = {}

    @staticmethod
    def _key(attempt_id: str, publication_id: str) -> tuple[str, str]:
        if not attempt_id.strip() or not publication_id.strip():
            raise PlatformError("validation_error", "attempt and publication are required", {}, 422)
        return attempt_id, publication_id

    def stage_chunks(
        self,
        attempt_id: str,
        publication_id: str,
        document_id: str,
        document_version_id: str,
        chunks: Sequence[IndexChunk | Mapping[str, Any]],
        *,
        fencing_token: int = 1,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
        usage_context: object | None = None,
    ) -> StageResult:
        del usage_context
        prepared = validate_stage_chunks(
            attempt_id,
            publication_id,
            document_id,
            document_version_id,
            chunks,
            fencing_token=fencing_token,
            expected_generation_id=expected_generation_id,
            stage_resource_manifest=stage_resource_manifest,
            content_hash=content_hash,
        )
        key = self._key(attempt_id, publication_id)
        with self._lock:
            existing = self._staged.get(key)
            if existing is not None:
                if existing.fingerprint != prepared.content_hash:
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "staged content conflicts with an existing attempt",
                        {},
                        409,
                    )
                return existing.result
            terminal = self._terminal.get(key)
            if terminal is not None and terminal.state == "discarded":
                return terminal
            result = StageResult(
                state="staged",
                attempt_id=attempt_id,
                publication_id=publication_id,
                generation_id=prepared.generation_id,
                resource_ids=prepared.resource_ids,
                content_hash=prepared.content_hash,
                fencing_token=fencing_token,
            )
            self._staged[key] = _Stage(
                result, prepared.chunks, prepared.content_hash, fencing_token
            )
            return result

    @staticmethod
    def _validate_identity(
        result: StageResult,
        *,
        fencing_token: int | None,
        expected_generation_id: str | None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None,
        content_hash: str | None,
    ) -> None:
        validate_stage_identity(
            result,
            fencing_token=fencing_token,
            expected_generation_id=expected_generation_id,
            stage_resource_manifest=stage_resource_manifest,
            content_hash=content_hash,
        )

    def publish_staged(
        self,
        attempt_id: str,
        publication_id: str,
        *,
        validator: Callable[[tuple[IndexChunk, ...]], bool] | None = None,
        fencing_token: int | None = None,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
    ) -> StageResult:
        key = self._key(attempt_id, publication_id)
        with self._lock:
            stage = self._staged.get(key)
            if stage is None:
                terminal = self._terminal.get(key)
                if terminal is not None:
                    self._validate_identity(
                        terminal,
                        fencing_token=fencing_token,
                        expected_generation_id=expected_generation_id,
                        stage_resource_manifest=stage_resource_manifest,
                        content_hash=content_hash,
                    )
                    return terminal
                published = self._published.get(key)
                if published is not None:
                    result = StageResult(
                        "published",
                        attempt_id,
                        publication_id,
                        published[0].generation_id,
                        tuple(
                            f"{attempt_id}:{publication_id}:{item.chunk_id}" for item in published
                        ),
                        _fingerprint([item.to_mapping() for item in published]),
                    )
                    self._validate_identity(
                        result,
                        fencing_token=fencing_token,
                        expected_generation_id=expected_generation_id,
                        stage_resource_manifest=stage_resource_manifest,
                        content_hash=content_hash,
                    )
                    return result
                raise PlatformError("index_stage_not_found", "staged index was not found", {}, 404)
            self._validate_identity(
                stage.result,
                fencing_token=fencing_token,
                expected_generation_id=expected_generation_id,
                stage_resource_manifest=stage_resource_manifest,
                content_hash=content_hash,
            )
            if validator is not None and not validator(stage.chunks):
                raise PlatformError(
                    "index_release_blocked", "documents validation rejected publish", {}, 409
                )
            self._published[key] = stage.chunks
            result = StageResult(
                "published",
                stage.result.attempt_id,
                stage.result.publication_id,
                stage.result.generation_id,
                stage.result.resource_ids,
                stage.result.content_hash,
                stage.result.fencing_token,
            )
            self._terminal[key] = result
            del self._staged[key]
            return result

    def discard_staged(
        self,
        attempt_id: str,
        publication_id: str,
        *,
        fencing_token: int | None = None,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
    ) -> StageResult:
        key = self._key(attempt_id, publication_id)
        with self._lock:
            stage = self._staged.get(key)
            if stage is not None:
                self._validate_identity(
                    stage.result,
                    fencing_token=fencing_token,
                    expected_generation_id=expected_generation_id,
                    stage_resource_manifest=stage_resource_manifest,
                    content_hash=content_hash,
                )
                result = StageResult(
                    "discarded",
                    stage.result.attempt_id,
                    stage.result.publication_id,
                    stage.result.generation_id,
                    stage.result.resource_ids,
                    stage.result.content_hash,
                    stage.result.fencing_token,
                )
                self._terminal[key] = result
                del self._staged[key]
                return result
            published = self._published.get(key)
            if published is not None:
                prior = self._terminal.get(key)
                if prior is not None:
                    self._validate_identity(
                        prior,
                        fencing_token=fencing_token,
                        expected_generation_id=expected_generation_id,
                        stage_resource_manifest=stage_resource_manifest,
                        content_hash=content_hash,
                    )
                result = StageResult(
                    "discarded",
                    attempt_id,
                    publication_id,
                    published[0].generation_id if published else "",
                    tuple(f"{attempt_id}:{publication_id}:{item.chunk_id}" for item in published),
                    _fingerprint([item.to_mapping() for item in published]),
                    prior.fencing_token if prior is not None else 1,
                )
                self._terminal[key] = result
                del self._published[key]
                return result
            terminal = self._terminal.get(key)
            if terminal is not None:
                self._validate_identity(
                    terminal,
                    fencing_token=fencing_token,
                    expected_generation_id=expected_generation_id,
                    stage_resource_manifest=stage_resource_manifest,
                    content_hash=content_hash,
                )
                return terminal
            return StageResult("discarded", attempt_id, publication_id, "", (), "", 1)

    def delete_document_version(
        self, document_id: str, document_version_id: str, *, generation_id: str | None = None
    ) -> int:
        with self._lock:
            keys = [
                key
                for key, chunks in self._published.items()
                if any(
                    item.document_id == document_id
                    and item.document_version_id == document_version_id
                    and (generation_id is None or item.generation_id == generation_id)
                    for item in chunks
                )
            ]
            removed = sum(len(self._published.pop(key, ())) for key in keys)
            return removed

    def delete_document(self, document_id: str, *, generation_id: str | None = None) -> int:
        with self._lock:
            keys = [
                key
                for key, chunks in self._published.items()
                if any(
                    item.document_id == document_id
                    and (generation_id is None or item.generation_id == generation_id)
                    for item in chunks
                )
            ]
            return sum(len(self._published.pop(key, ())) for key in keys)

    def search(
        self,
        query: str,
        space_ids: Sequence[str],
        top_k: int,
        cursor: str | None,
        *,
        generation_id: str | None = None,
    ) -> ProviderSearchPage:
        if top_k < 1:
            raise PlatformError("validation_error", "top_k must be positive", {}, 422)
        allowed_spaces = {str(item) for item in space_ids}
        normalized_query = query.casefold().strip()
        with self._lock:
            chunks = [
                item
                for values in self._published.values()
                for item in values
                if item.space_id in allowed_spaces
                and (generation_id is None or item.generation_id == generation_id)
            ]
        # Providers own ordering; this adapter uses a stable lexical order.
        chunks.sort(key=lambda item: item.chunk_id)
        if normalized_query:
            chunks.sort(
                key=lambda item: (
                    0 if normalized_query in item.text.casefold() else 1,
                    item.chunk_id,
                )
            )
        start = 0
        if cursor:
            try:
                start = int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise PlatformError(
                    "retrieval_degradation", "provider cursor is invalid", {}, 409
                ) from exc
        page = tuple(chunks[start : start + top_k])
        next_cursor = (
            base64.urlsafe_b64encode(str(start + len(page)).encode("ascii")).decode("ascii")
            if start + len(page) < len(chunks)
            else None
        )
        return ProviderSearchPage(page, next_cursor)

    def visible_chunks(self) -> tuple[IndexChunk, ...]:
        with self._lock:
            return tuple(item for values in self._published.values() for item in values)


class InMemorySparseIndexProvider(InMemoryIndexWriter):
    provider_name = "memory"
    backend_kind = "sparse"


class InMemoryOpenSearchSparseIndexProvider(InMemorySparseIndexProvider):
    provider_name = "opensearch"
    backend_kind = "sparse"


def build_sparse_provider(provider_name: str | None = None) -> SparseIndexProvider:
    configured = (provider_name or "meilisearch").strip().casefold()
    if configured == "meilisearch":
        return InMemorySparseIndexProvider(provider_name="meilisearch")
    if configured in {"opensearch", "opensearch+ik"}:
        return InMemoryOpenSearchSparseIndexProvider(provider_name="opensearch")
    raise PlatformError("provider_not_supported", "Sparse index provider is not supported", {}, 422)


__all__ = [
    "IndexWriter",
    "InMemoryIndexWriter",
    "InMemoryOpenSearchSparseIndexProvider",
    "InMemorySparseIndexProvider",
    "PreparedStage",
    "SparseIndexProvider",
    "StageResult",
    "build_sparse_provider",
    "validate_stage_chunks",
    "validate_stage_identity",
]
