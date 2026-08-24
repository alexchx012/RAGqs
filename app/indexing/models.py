from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from app.platform.errors import PlatformError

GenerationStatus = Literal["staging", "active", "retired", "failed", "purging", "purged"]
ComponentState = Literal["staged", "ready", "disabled", "stale", "failed"]


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("validation_error", f"{name} is required", {}, 422)
    return value.strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class IndexChunk:
    """Immutable, generation-scoped unit exposed by providers after visibility checks."""

    chunk_id: str
    generation_id: str
    publication_id: str
    document_id: str
    document_version_id: str
    space_id: str
    text: str
    embedding_text: str
    locator: Mapping[str, Any]
    snippet: str | None
    media_kind: str
    manifest_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sparse_text: str | None = None
    indexable: bool = True

    def __post_init__(self) -> None:
        for value, name in (
            (self.chunk_id, "chunk_id"),
            (self.generation_id, "generation_id"),
            (self.publication_id, "publication_id"),
            (self.document_id, "document_id"),
            (self.document_version_id, "document_version_id"),
            (self.space_id, "space_id"),
            (self.media_kind, "media_kind"),
            (self.manifest_hash, "manifest_hash"),
        ):
            _required(value, name)
        if not isinstance(self.locator, Mapping):
            raise PlatformError("validation_error", "locator must be an object", {}, 422)
        if "bbox" in self.locator or "pixel_bbox" in self.locator:
            raise PlatformError("validation_error", "pixel locators are not supported", {}, 422)
        if not isinstance(self.metadata, Mapping):
            raise PlatformError("validation_error", "chunk metadata must be an object", {}, 422)
        if not isinstance(self.text, str) or not self.text.strip():
            raise PlatformError("validation_error", "chunk text is required", {}, 422)
        if not isinstance(self.embedding_text, str) or not self.embedding_text.strip():
            raise PlatformError("validation_error", "embedding text is required", {}, 422)

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        return self.generation_id, self.publication_id, self.chunk_id

    def to_mapping(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "generation_id": self.generation_id,
            "publication_id": self.publication_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "space_id": self.space_id,
            "text": self.text,
            "embedding_text": self.embedding_text,
            "sparse_text": self.sparse_text,
            "locator": dict(self.locator),
            "snippet": self.snippet,
            "media_kind": self.media_kind,
            "manifest_hash": self.manifest_hash,
            "metadata": dict(self.metadata),
            "indexable": self.indexable,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> IndexChunk:
        try:
            return cls(
                chunk_id=str(value["chunk_id"]),
                generation_id=str(value["generation_id"]),
                publication_id=str(value["publication_id"]),
                document_id=str(value["document_id"]),
                document_version_id=str(value["document_version_id"]),
                space_id=str(value["space_id"]),
                text=str(value["text"]),
                embedding_text=str(value.get("embedding_text", value["text"])),
                sparse_text=(str(value["sparse_text"]) if value.get("sparse_text") else None),
                locator=dict(value.get("locator", {})),
                snippet=(str(value["snippet"]) if value.get("snippet") is not None else None),
                media_kind=str(value.get("media_kind", "text")),
                manifest_hash=str(value["manifest_hash"]),
                metadata=dict(value.get("metadata", {})),
                indexable=bool(value.get("indexable", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformError("validation_error", "chunk is invalid", {}, 422) from exc


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Server-owned readable scope, optionally constrained per space."""

    space_ids: frozenset[str]
    document_ids: frozenset[str] | None = None
    documents_by_space: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.space_ids, frozenset):
            object.__setattr__(self, "space_ids", frozenset(str(item) for item in self.space_ids))
        if self.document_ids is not None and not isinstance(self.document_ids, frozenset):
            object.__setattr__(
                self,
                "document_ids",
                frozenset(str(item) for item in self.document_ids),
            )
        normalized = {
            str(space): frozenset(str(document) for document in documents)
            for space, documents in self.documents_by_space.items()
        }
        object.__setattr__(self, "documents_by_space", normalized)

    @property
    def is_empty(self) -> bool:
        if not self.space_ids or (self.document_ids is not None and not self.document_ids):
            return True
        for space_id in self.space_ids:
            scoped = self.documents_by_space.get(space_id)
            if scoped is None or scoped:
                return False
        return True

    def allows(self, *, space_id: str, document_id: str) -> bool:
        if space_id not in self.space_ids:
            return False
        if self.document_ids is not None and document_id not in self.document_ids:
            return False
        scoped = self.documents_by_space.get(space_id)
        return scoped is None or document_id in scoped

    @classmethod
    def from_value(cls, value: Any) -> RetrievalScope:
        if isinstance(value, RetrievalScope):
            return value
        if not isinstance(value, Mapping):
            raise PlatformError(
                "authorization_scope_invalid", "Allowed retrieval scope is invalid", {}, 403
            )
        spaces = value.get("space_ids", value.get("spaces", ()))
        documents = value.get("document_ids")
        by_space = value.get("documents_by_space", value.get("document_ids_by_space", {}))
        return cls(
            space_ids=frozenset(str(item) for item in spaces),
            document_ids=(
                frozenset(str(item) for item in documents) if documents is not None else None
            ),
            documents_by_space={
                str(space): frozenset(str(item) for item in values)
                for space, values in (by_space or {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class NarrowingScope:
    space_ids: frozenset[str] | None = None
    document_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.space_ids is not None and not isinstance(self.space_ids, frozenset):
            object.__setattr__(self, "space_ids", frozenset(str(item) for item in self.space_ids))
        if self.document_ids is not None and not isinstance(self.document_ids, frozenset):
            object.__setattr__(
                self, "document_ids", frozenset(str(item) for item in self.document_ids)
            )

    @classmethod
    def from_value(cls, value: Any) -> NarrowingScope | None:
        if value is None or isinstance(value, NarrowingScope):
            return value
        if not isinstance(value, Mapping):
            raise PlatformError("validation_error", "Narrowing scope is invalid", {}, 422)
        return cls(
            space_ids=(
                frozenset(str(item) for item in value["space_ids"])
                if value.get("space_ids") is not None
                else None
            ),
            document_ids=(
                frozenset(str(item) for item in value["document_ids"])
                if value.get("document_ids") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    profile_id: str = "default"
    version: str = "1"
    top_k: int = 10
    candidate_limit: int = 50
    effort: Literal["quick", "think", "deep"] = "quick"
    reranker_release: str = "default"
    tokenizer_version: str = "default"
    score_threshold: float | None = None
    retrieval_context_items_per_space: int = 5
    retrieval_context_tokens_per_space: int = 8000
    retrieval_context_tokens_cap: int = 24000
    expected_library_count: int = 1
    route_tree: bool = False
    route_graph: bool = False
    release_id: str | None = None
    config_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.profile_id, "profile_id")
        _required(self.version, "version")
        _required(self.tokenizer_version, "tokenizer_version")
        if self.top_k < 1 or self.candidate_limit < self.top_k:
            raise PlatformError("validation_error", "retrieval profile limits are invalid", {}, 422)
        if self.effort not in {"quick", "think", "deep"}:
            raise PlatformError("validation_error", "retrieval effort is invalid", {}, 422)
        if (
            self.retrieval_context_items_per_space < 1
            or self.retrieval_context_tokens_per_space < 1
            or self.retrieval_context_tokens_cap < 1
            or self.expected_library_count < 1
        ):
            raise PlatformError("validation_error", "retrieval context limits are invalid", {}, 422)
        if (
            self.retrieval_context_tokens_per_space * self.expected_library_count
            > self.retrieval_context_tokens_cap
        ):
            raise PlatformError(
                "startup_error",
                "retrieval context per-library cap exceeds total cap",
                {},
                500,
            )
        if self.release_id is not None:
            _required(self.release_id, "release_id")
        if not isinstance(self.config_snapshot, Mapping):
            raise PlatformError(
                "validation_error", "retrieval profile snapshot is invalid", {}, 422
            )


@dataclass(frozen=True, slots=True)
class ProviderSearchPage:
    items: tuple[IndexChunk | Mapping[str, Any], ...]
    cursor: str | None

    def __iter__(self):
        yield self.items
        yield self.cursor

    def __getitem__(self, key: str) -> Any:
        if key == "items":
            return self.items
        if key == "cursor":
            return self.cursor
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in {"items", "cursor"} else default


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk: IndexChunk
    score: float
    source: str
    rerank_score: float | None = None

    def to_mapping(self) -> dict[str, Any]:
        value = self.chunk.to_mapping()
        value.update({"score": self.score, "source": self.source})
        if self.rerank_score is not None:
            value["rerank_score"] = self.rerank_score
        return value


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: tuple[RetrievalHit, ...]
    generation_id: str
    profile: RetrievalProfile
    degradations: tuple[Mapping[str, Any], ...] = ()
    candidate_hits: tuple[RetrievalHit, ...] = ()
    route_output: Mapping[str, Any] | None = None

    @property
    def candidates(self) -> tuple[RetrievalHit, ...]:
        return self.candidate_hits or self.hits

    @property
    def preview_hits(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            {
                "document_id": hit.chunk.document_id,
                "document_version_id": hit.chunk.document_version_id,
                "publication_id": hit.chunk.publication_id,
                "chunk_id": hit.chunk.chunk_id,
                "locator": dict(hit.chunk.locator),
                "snippet": hit.chunk.snippet,
            }
            for hit in self.hits
        )


@dataclass(frozen=True, slots=True)
class Generation:
    generation_id: str
    status: GenerationStatus
    base_revision: int
    applied_revision: int
    manifest: Mapping[str, Any]
    created_at: datetime
    activated_at: datetime | None = None
    retired_at: datetime | None = None
    rollback_until_utc: datetime | None = None
    rollback_applied_revision: int | None = None
    graph_component_state: ComponentState = "disabled"

    def with_updates(self, **changes: Any) -> Generation:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class GenerationReferenceLease:
    lease_id: str
    generation_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GenerationComponentReaderLease:
    lease_id: str
    generation_id: str
    component_kind: str
    manifest_hash: str
    source_head_fence: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IndexGenerationGcReceipt:
    operation_id: str
    candidate_generation_id: str
    state: Literal["accepted", "blocked", "already_purged"]
    blocking_reasons: tuple[str, ...] = ()
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class DocumentVisibilityFact:
    document_id: str
    space_id: str
    lifecycle_status: str
    active_version_id: str | None
    active_publication_id: str | None
    publication_status: str | None
    manifest_hash: str | None
    readable: bool = True


def normalize_chunks(chunks: Sequence[IndexChunk | Mapping[str, Any]]) -> tuple[IndexChunk, ...]:
    return tuple(
        item if isinstance(item, IndexChunk) else IndexChunk.from_mapping(item) for item in chunks
    )


AllowedRetrievalScope = RetrievalScope
RetrievalCandidate = RetrievalHit


__all__ = [
    "AllowedRetrievalScope",
    "ComponentState",
    "DocumentVisibilityFact",
    "Generation",
    "GenerationComponentReaderLease",
    "GenerationReferenceLease",
    "GenerationStatus",
    "IndexChunk",
    "IndexGenerationGcReceipt",
    "NarrowingScope",
    "ProviderSearchPage",
    "RetrievalHit",
    "RetrievalCandidate",
    "RetrievalProfile",
    "RetrievalResult",
    "RetrievalScope",
    "normalize_chunks",
]
