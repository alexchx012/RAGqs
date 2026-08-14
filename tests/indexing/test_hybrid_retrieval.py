from __future__ import annotations

import pytest

from app.indexing import (
    DocumentVisibilityFact,
    GenerationManager,
    InMemoryIndexWriter,
    InMemorySparseIndexProvider,
    RetrievalProfile,
    RetrievalScope,
    RetrievalService,
)
from app.indexing.models import IndexChunk
from app.indexing.retrieval import allocate_candidate_quotas
from app.platform.errors import PlatformError


def _chunk(chunk_id: str, document_id: str = "document_1") -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_initial",
        publication_id="publication_1",
        document_id=document_id,
        document_version_id="version_1",
        space_id="space_1",
        text=f"text {chunk_id}",
        embedding_text=f"text {chunk_id}",
        locator={},
        snippet=chunk_id,
        media_kind="text/plain",
        manifest_hash="manifest_1",
    )


def _facts(candidate: IndexChunk, principal: object) -> DocumentVisibilityFact:
    del principal
    return DocumentVisibilityFact(
        candidate.document_id,
        candidate.space_id,
        "active",
        candidate.document_version_id,
        candidate.publication_id,
        "active",
        candidate.manifest_hash,
        True,
    )


def _publish(provider: InMemoryIndexWriter, chunk: IndexChunk, attempt_id: str) -> None:
    provider.stage_chunks(
        attempt_id,
        chunk.publication_id,
        chunk.document_id,
        chunk.document_version_id,
        [chunk],
    )
    provider.publish_staged(attempt_id, chunk.publication_id)


def test_library_baseline_allocates_vector_seventy_sparse_thirty() -> None:
    assert allocate_candidate_quotas(10, ("dense", "sparse")) == (7, 3)
    assert allocate_candidate_quotas(10, ("sparse", "dense")) == (3, 7)
    assert allocate_candidate_quotas(8, ("dense",)) == (8,)


def test_hybrid_search_unions_quota_limited_candidates_before_rerank() -> None:
    dense = InMemoryIndexWriter(provider_name="dense")
    sparse = InMemorySparseIndexProvider(provider_name="sparse")
    for index in range(1, 11):
        _publish(dense, _chunk(f"dense_{index}", document_id=f"document_d{index}"), f"d{index}")
        _publish(sparse, _chunk(f"sparse_{index}", document_id=f"document_s{index}"), f"s{index}")
    seen: list[tuple[str, ...]] = []

    class Reranker:
        def rerank(self, query, hits, profile):
            del query, profile
            seen.append(tuple(f"{hit.source}:{hit.chunk.chunk_id}" for hit in hits))
            return tuple(hits), None

    service = RetrievalService(
        GenerationManager(),
        [dense, sparse],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=Reranker(),
    )
    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=10,
            candidate_limit=10,
            retrieval_context_items_per_space=10,
        ),
    )
    sources = {item.split(":", 1)[0] for item in seen[0]}
    assert sources == {"dense", "sparse"}
    assert sum(item.startswith("dense:") for item in seen[0]) == 7
    assert sum(item.startswith("sparse:") for item in seen[0]) == 3
    assert len(result.hits) == 10


def test_visibility_filters_staged_and_identity_mismatch_before_rerank() -> None:
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("ok"), "attempt_ok")
    hidden = _chunk("hidden", document_id="document_hidden")
    _publish(provider, hidden, "attempt_hidden")

    def facts(candidate: IndexChunk, principal: object) -> DocumentVisibilityFact:
        if candidate.chunk_id == "hidden":
            return DocumentVisibilityFact(
                candidate.document_id,
                candidate.space_id,
                "active",
                candidate.document_version_id,
                None,
                "staged",
                candidate.manifest_hash,
                True,
            )
        return _facts(candidate, principal)

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=facts,
    )
    result = service.search("text", principal="user_1")
    assert [hit.chunk.chunk_id for hit in result.hits] == ["ok"]


def test_dense_failure_degrades_to_sparse() -> None:
    class BrokenDense(InMemoryIndexWriter):
        backend_kind = "dense"

        def search(self, *args, **kwargs):
            del args, kwargs
            raise PlatformError("milvus_unavailable", "down", {}, 503)

    dense = BrokenDense(provider_name="milvus")
    sparse = InMemorySparseIndexProvider(provider_name="meilisearch")
    _publish(sparse, _chunk("sparse_1"), "attempt_1")
    service = RetrievalService(
        GenerationManager(),
        [dense, sparse],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )
    result = service.search("text", principal="user_1")
    assert [hit.chunk.chunk_id for hit in result.hits] == ["sparse_1"]
    assert result.degradations[0]["code"] == "retrieval_degraded"
    assert result.degradations[0]["failed"] == ("dense",)


def test_both_backends_failing_fails_the_query() -> None:
    class Broken(InMemoryIndexWriter):
        def search(self, *args, **kwargs):
            del args, kwargs
            raise PlatformError("index_unavailable", "down", {}, 503)

    service = RetrievalService(
        GenerationManager(),
        [Broken(provider_name="dense"), Broken(provider_name="sparse")],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )
    with pytest.raises(PlatformError) as error:
        service.search("text", principal="user_1")
    assert error.value.code == "retrieval_failed"


def test_reranker_failure_keeps_original_order() -> None:
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")

    class BrokenReranker:
        def rerank(self, query, hits, profile):
            del query, hits, profile
            raise RuntimeError("rerank down")

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=BrokenReranker(),
    )
    result = service.search("text", principal="user_1")
    assert [hit.chunk.chunk_id for hit in result.hits] == ["chunk_1"]
    assert result.degradations[-1]["code"] == "rerank_degraded"
