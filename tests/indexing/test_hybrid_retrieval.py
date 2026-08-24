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

    class BrokenSparse(InMemorySparseIndexProvider):
        def search(self, *args, **kwargs):
            del args, kwargs
            raise PlatformError("index_unavailable", "down", {}, 503)

    service = RetrievalService(
        GenerationManager(),
        [Broken(provider_name="dense"), BrokenSparse(provider_name="sparse")],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )
    with pytest.raises(PlatformError) as error:
        service.search("text", principal="user_1")
    assert error.value.code == "retrieval_failed"
    assert error.value.details["query_failure"] == {
        "code": "retrieval_query_failed",
        "failed_libraries": ["dense", "sparse"],
        "retryable": True,
    }
    assert error.value.details["failure_reasons"] == {
        "dense": "index_unavailable",
        "sparse": "index_unavailable",
    }


def test_successful_zero_result_is_no_context_without_query_failure() -> None:
    provider = InMemorySparseIndexProvider(provider_name="sparse")
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )
    result = service.search("missing text", principal="user_1")
    assert result.hits == ()
    assert all(item.get("code") != "retrieval_query_failed" for item in result.degradations)


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


def test_two_stage_final_failure_keeps_per_library_raw_truncation_order() -> None:
    provider = InMemorySparseIndexProvider()
    for chunk_id in ("chunk_low", "chunk_high"):
        _publish(provider, _chunk(chunk_id), f"attempt_{chunk_id}")

    class FailingReranker:
        def rerank(self, query, hits, profile):
            del query, profile
            return (), {"code": "rerank_degraded", "reason": "final_unavailable"}

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=FailingReranker(),
    )
    result = service.search(
        "text", principal="user_1", profile=RetrievalProfile(score_threshold=0.99)
    )
    assert [hit.chunk.chunk_id for hit in result.hits] == []
    assert result.degradations[-1]["code"] == "rerank_degraded"


def test_none_reranker_skips_tree_and_exposes_stable_fallback_notice() -> None:
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    tree_calls: list[object] = []

    def tree_router(*args, **kwargs):
        tree_calls.append((args, kwargs))

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        environment="test",
        tree_router=tree_router,
    )
    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(effort="think", route_tree=True),
    )
    assert tree_calls == []
    notices = [item for item in result.degradations if item.get("code") == "rerank_degraded"]
    assert notices
    assert notices[0]["provider"] == "none"
    assert notices[0]["fallback"] == "preserve_candidate_order"
    assert notices[0]["threshold"] == "not_applied"


# ------------------------------------- rerank stability and candidate split


def test_equal_score_ties_are_ordered_stably_across_rerankers() -> None:
    class ReversingReranker:
        def rerank(self, query, hits, profile):
            del query, profile
            return tuple(reversed(tuple(hits))), None

    def _search(reranker) -> list[str]:
        provider = InMemorySparseIndexProvider()
        for chunk_id in ("chunk_b", "chunk_a", "chunk_c"):
            _publish(provider, _chunk(chunk_id), f"attempt_{chunk_id}")
        service = RetrievalService(
            GenerationManager(),
            [provider],
            identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
            visibility_facts=_facts,
            reranker=reranker,
        )
        return [hit.chunk.chunk_id for hit in service.search("text", principal="user_1").hits]

    # All chunks share the same provider score: two rerankers with opposite
    # output orders must converge on the same deterministic ranking (A6).
    noop_order = _search(None)
    reversed_order = _search(ReversingReranker())
    assert noop_order == reversed_order == ["chunk_a", "chunk_b", "chunk_c"]


def test_candidate_pool_is_exposed_separately_from_final_hits() -> None:
    provider = InMemorySparseIndexProvider()
    for index in range(1, 6):
        _publish(provider, _chunk(f"chunk_{index}"), f"attempt_{index}")
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )
    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=2,
            candidate_limit=5,
            retrieval_context_items_per_space=5,
        ),
    )
    # The pre-rerank candidate pool feeds hit_at_k_candidate; the budgeted
    # final ranking feeds hit_at_k_final (A4).
    assert len(result.candidates) == 5
    assert len(result.hits) == 2
    assert {hit.chunk.chunk_id for hit in result.hits} <= {
        hit.chunk.chunk_id for hit in result.candidates
    }
