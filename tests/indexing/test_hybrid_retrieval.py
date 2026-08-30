from __future__ import annotations

import pytest

from app.indexing import (
    DocumentVisibilityFact,
    GenerationManager,
    InMemoryIndexWriter,
    InMemorySparseIndexProvider,
    RetrievalHit,
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


def test_exact_match_route_can_allocate_equal_dense_and_sparse_candidates() -> None:
    assert allocate_candidate_quotas(
        10,
        ("dense", "sparse"),
        dense_weight=0.5,
        sparse_weight=0.5,
    ) == (5, 5)


def test_retrieval_resolves_library_profile_from_the_authorized_scope() -> None:
    provider = InMemorySparseIndexProvider(provider_name="sparse")
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        library_profile_resolver=lambda scope: "structured-table-csv",
    )

    result = service.search("text", principal="user_1")

    assert result.profile.library_profile_id == "structured-table-csv"


def test_retrieval_profile_applies_effort_to_baseline_without_mutating_it() -> None:
    baseline = RetrievalProfile(top_k=20, candidate_limit=50, effort="deep")

    transformed = baseline.with_effort_transform()

    assert (baseline.top_k, baseline.candidate_limit) == (20, 50)
    assert (transformed.top_k, transformed.candidate_limit) == (60, 150)


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


# ------------------------------------- tree result merge-back and budget wiring


class _ScoredReranker:
    def rerank(self, query, hits, profile):
        del query, profile
        return (
            tuple(
                RetrievalHit(hit.chunk, hit.score, hit.source, rerank_score=hit.score)
                for hit in hits
            ),
            None,
        )


class _RecordingMetrics:
    def __init__(self) -> None:
        self.samples: list[object] = []

    def record(self, sample) -> None:
        self.samples.append(sample)


def _meter(effort: str = "think"):
    from app.chat.budget import BudgetMeter, BudgetPolicy

    policy = BudgetPolicy.for_effort(
        effort,
        price_version="test-prices",
        max_estimated_cost_amount=100.0,
        pricer=lambda operation, tokens: 0.001,
    )
    return BudgetMeter(policy=policy)


def test_tree_results_merge_back_after_visibility_recheck() -> None:
    from types import SimpleNamespace

    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1", document_id="document_ok"), "attempt_1")
    _publish(provider, _chunk("chunk_2", document_id="document_denied"), "attempt_2")

    def tree_router(query, candidates, *, max_documents, rag_call_limit):
        del query, max_documents, rag_call_limit
        outcomes = []
        for hit in candidates:
            allowed = hit.chunk.document_id != "document_denied"
            outcomes.append(
                SimpleNamespace(
                    document_id=hit.chunk.document_id,
                    chunk_id=hit.chunk.chunk_id,
                    status="ok" if allowed else "ok",
                    result={"text": f"tree evidence for {hit.chunk.document_id}"},
                )
            )
        return SimpleNamespace(skipped=False, reason=None, documents=tuple(outcomes))

    def facts(candidate: IndexChunk, principal: object) -> DocumentVisibilityFact:
        del principal
        return DocumentVisibilityFact(
            candidate.document_id,
            candidate.space_id,
            "active",
            candidate.document_version_id,
            candidate.publication_id,
            "active",
            candidate.manifest_hash,
            candidate.document_id != "document_denied",
        )

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=facts,
        reranker=_ScoredReranker(),
        tree_router=tree_router,
    )
    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=4,
            candidate_limit=4,
            retrieval_context_items_per_space=4,
            effort="think",
            route_tree=True,
        ),
    )
    tree_hits = [hit for hit in result.hits if hit.source == "tree"]
    assert [hit.chunk.document_id for hit in tree_hits] == ["document_ok"]
    assert tree_hits[0].chunk.text == "tree evidence for document_ok"
    assert all(hit.chunk.document_id != "document_denied" for hit in result.hits)


def test_budget_counts_each_split_subquestion_and_tree_document() -> None:
    from types import SimpleNamespace

    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    tree_documents: list[SimpleNamespace] = []

    def tree_router(query, candidates, *, max_documents, rag_call_limit):
        del query, max_documents, rag_call_limit
        documents = tuple(
            SimpleNamespace(
                document_id=hit.chunk.document_id,
                chunk_id=hit.chunk.chunk_id,
                status="ok",
                result=None,
            )
            for hit in candidates
        )
        tree_documents.extend(documents)
        return SimpleNamespace(skipped=False, reason=None, documents=documents)

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=_ScoredReranker(),
        tree_router=tree_router,
    )
    meter = _meter("think")
    service.search(
        "第一部分；第二部分？第三部分",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=4,
            candidate_limit=4,
            retrieval_context_items_per_space=4,
            effort="think",
            route_tree=True,
        ),
        budget=meter,
    )
    # 3 split subquestions each count as one retrieval operation; the single
    # searchable tree document counts as one tree operation (A64).
    assert meter.rag_calls_used == 3 + len(tree_documents)


def test_budget_gate_rejection_skips_subquestion_and_sends_notice() -> None:
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=_ScoredReranker(),
    )
    meter = _meter("quick")
    meter.rag_calls_used = meter.policy.max_rag_calls
    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(top_k=4, candidate_limit=4),
        budget=meter,
    )
    assert result.hits == ()
    notices = [
        item
        for item in result.degradations
        if item.get("kind") == "retrieval_degraded"
        and item.get("detail", {}).get("reason") == "budget_exhausted"
    ]
    assert notices


def test_deep_strategy_failure_keeps_default_hybrid_candidates() -> None:
    class FailingHydeProvider(InMemorySparseIndexProvider):
        def __init__(self) -> None:
            super().__init__()
            self.seen_queries: list[str] = []

        def search(self, query, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.seen_queries.append(query)
            if query.startswith("检索假设："):
                raise RuntimeError("HyDE is unavailable")
            return super().search(query, *args, **kwargs)

    provider = FailingHydeProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
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
            top_k=4,
            candidate_limit=4,
            effort="deep",
            strategy_operations=("hyde",),
        ),
    )

    assert result.hits
    assert set(provider.seen_queries) == {"text", "检索假设：text"}
    assert {
        "code": "retrieval_degraded",
        "kind": "retrieval_degraded",
        "reason": "strategy_unavailable",
        "strategy": "hyde",
    } in result.degradations


def test_deep_combined_plan_stops_tree_work_at_ten_logical_rag_operations() -> None:
    from types import SimpleNamespace

    provider = InMemorySparseIndexProvider()
    for index in range(9):
        _publish(provider, _chunk(f"chunk_{index}", document_id=f"document_{index}"), str(index))
    tree_candidates: list[object] = []

    def tree_router(query, candidates, *, max_documents, rag_call_limit):
        del query, max_documents, rag_call_limit
        tree_candidates.extend(candidates)
        return SimpleNamespace(skipped=False, reason=None, documents=())

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=_ScoredReranker(),
        tree_router=tree_router,
    )
    meter = _meter("deep")
    service.search(
        "请帮我，第一项报销流程；第二项发票抬头",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=4,
            candidate_limit=4,
            effort="deep",
            strategy_operations=("rewrite", "split_subquestions", "hyde", "tree"),
        ),
        budget=meter,
    )

    assert meter.rag_calls_used == 10
    assert len(tree_candidates) == 3


def test_library_latency_and_tree_order_observability_routes() -> None:
    from types import SimpleNamespace

    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    metrics = _RecordingMetrics()

    def tree_router(query, candidates, *, max_documents, rag_call_limit):
        del query
        return SimpleNamespace(skipped=False, reason=None, documents=())

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=_ScoredReranker(),
        tree_router=tree_router,
        exact_match_metrics=metrics,
    )
    service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=4,
            candidate_limit=4,
            retrieval_context_items_per_space=4,
            effort="think",
            route_tree=True,
        ),
    )
    routes = {sample.route_template for sample in metrics.samples}
    assert "index_library_search_latency" in routes
    assert "index_tree_candidate_order" in routes
