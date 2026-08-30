from __future__ import annotations

import pathlib
import tempfile

import pytest
from sqlalchemy import create_engine

from app.indexing import (
    DocumentVisibilityFact,
    GenerationManager,
    IndexChunk,
    IndexingService,
    InMemoryEmbeddingProvider,
    InMemorySparseIndexProvider,
    RetrievalProfile,
    RetrievalScope,
    RetrievalService,
)
from app.indexing.embedding import EmbeddingConfig
from app.indexing.models import RetrievalHit
from app.platform.config import load_platform_settings
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime


def _chunk(
    chunk_id: str,
    *,
    space_id: str = "space_1",
    document_id: str = "document_1",
    document_version_id: str = "version_1",
    publication_id: str = "publication_1",
) -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_initial",
        publication_id=publication_id,
        document_id=document_id,
        document_version_id=document_version_id,
        space_id=space_id,
        text=f"text {chunk_id}",
        embedding_text=f"text {chunk_id}",
        locator={},
        snippet=f"snippet {chunk_id}",
        media_kind="text/plain",
        manifest_hash=f"manifest_{chunk_id}",
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


def _production_settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
            "RAG_PROVIDER_NAME": "openai-compatible",
            "RAG_PROVIDER_API_KEY": "provider-secret",
            "RAG_EVALUATION_JUDGE_BASE_URL": "https://judge.example.test/v1",
            "RAG_EVALUATION_JUDGE_API_KEY": "judge-secret",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
            "RAG_AUTH_ADMIN_ROSTER": "admin",
            "RAG_BACKUP_TARGET_NAMESPACE": "ragqs-test-backups",
            "USER_DELETION_ARCHIVE_DIR": str(
                pathlib.Path(tempfile.gettempdir()) / "rag-test-archive"
            ),
        }
    )


def _publish(provider: InMemorySparseIndexProvider, chunk: IndexChunk, attempt_id: str) -> None:
    provider.stage_chunks(
        attempt_id,
        chunk.publication_id,
        chunk.document_id,
        chunk.document_version_id,
        [chunk],
    )
    provider.publish_staged(attempt_id, chunk.publication_id)


def test_graph_routing_receives_indexing_controlled_reader_lease() -> None:
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    lease = object()
    events: list[object] = []

    class GraphReader:
        def acquire_current_reader_lease(self, *, generation_id):
            events.append(("acquire", generation_id))
            return lease

        def release_reader_lease(self, value):
            events.append(("release", value))

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        graph_reader=GraphReader(),
        graph_router=lambda query, candidates, *, rag_call_limit, reader_lease: events.append(
            ("route", reader_lease, rag_call_limit)
        ),
    )

    service.search("text", principal="user_1", profile=RetrievalProfile(route_graph=True))

    assert events == [
        ("acquire", "generation_initial"),
        ("route", lease, 1),
        ("release", lease),
    ]


def test_graph_routing_without_indexing_reader_degrades_without_direct_read() -> None:
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    events: list[str] = []
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        graph_router=lambda *args, **kwargs: events.append("route"),
    )

    result = service.search("text", principal="user_1", profile=RetrievalProfile(route_graph=True))

    assert events == []
    assert result.degradations[-1] == {
        "code": "graph_degraded",
        "reason": "reader_unavailable",
    }


def test_indexing_service_citation_requires_a_request_context() -> None:
    service = IndexingService(visibility_facts=_facts)

    with pytest.raises(PlatformError) as error:
        service.resolve_citation(RetrievalHit(_chunk("chunk_1"), 1.0, "dense"), principal="user_1")

    assert error.value.code == "retrieval_request_required"


def test_retrieval_request_keeps_generation_lease_for_citation() -> None:
    manager = GenerationManager()
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    service = RetrievalService(
        manager,
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )
    observed: list[str] = []
    original_release = manager.release_reference_lease

    def record_release(lease_id: str) -> None:
        observed.append(lease_id)
        original_release(lease_id)

    manager.release_reference_lease = record_release  # type: ignore[method-assign]
    with service.open_request() as request:
        result = request.search("text", principal="user_1")
        citation = request.resolve_citation(result.hits[0], principal="user_1")
        assert citation["generation_id"] == result.generation_id == request.generation_id
        assert observed == []

    assert observed == [request.lease_id]


def test_context_space_k_rebalances_after_threshold() -> None:
    provider = InMemorySparseIndexProvider()
    chunks = (
        _chunk(
            "a1", space_id="space_a", publication_id="publication_a1", document_id="document_a1"
        ),
        _chunk(
            "a2", space_id="space_a", publication_id="publication_a2", document_id="document_a2"
        ),
        _chunk(
            "a3", space_id="space_a", publication_id="publication_a3", document_id="document_a3"
        ),
        _chunk(
            "b1", space_id="space_b", publication_id="publication_b1", document_id="document_b1"
        ),
    )
    for index, chunk in enumerate(chunks, start=1):
        _publish(provider, chunk, f"attempt_{index}")

    class OrderedReranker:
        def rerank(self, query, hits, profile):
            del query, profile
            ranking = {"a1": 4.0, "a2": 3.0, "a3": 2.0, "b1": 1.0}
            return (
                tuple(
                    RetrievalHit(
                        hit.chunk,
                        ranking[hit.chunk.chunk_id],
                        hit.source,
                        ranking[hit.chunk.chunk_id],
                    )
                    for hit in sorted(hits, key=lambda item: -ranking[item.chunk.chunk_id])
                ),
                None,
            )

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_a", "space_b"})),
        visibility_facts=_facts,
        reranker=OrderedReranker(),
    )

    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            top_k=4,
            candidate_limit=4,
            score_threshold=1.0,
            retrieval_context_items_per_space=2,
        ),
    )

    assert [hit.chunk.chunk_id for hit in result.hits] == ["a1", "a2", "b1", "a3"]


def test_retrieval_hides_sparse_provider_score_from_cross_backend_reranker() -> None:
    class ScoredProvider(InMemorySparseIndexProvider):
        def search(self, *args, **kwargs):
            page = super().search(*args, **kwargs)
            return {
                "items": [{**chunk.to_mapping(), "score": 0.75} for chunk in page.items],
                "cursor": page.cursor,
            }

    provider = ScoredProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    seen: list[float] = []

    class CapturingReranker:
        def rerank(self, query, hits, profile):
            del query, profile
            seen.extend(hit.score for hit in hits)
            return tuple(hits), None

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=CapturingReranker(),
    )

    service.search("text", principal="user_1")

    assert seen == [0.0]


def test_sparse_candidate_is_not_rejected_by_final_score_threshold() -> None:
    class ScoredSparseProvider(InMemorySparseIndexProvider):
        def search(self, *args, **kwargs):
            page = super().search(*args, **kwargs)
            return {
                "items": [{**chunk.to_mapping(), "score": 0.01} for chunk in page.items],
                "cursor": page.cursor,
            }

    provider = ScoredSparseProvider()
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
        profile=RetrievalProfile(score_threshold=0.9),
    )

    assert [hit.chunk.chunk_id for hit in result.hits] == ["chunk_1"]


def test_conservative_context_counter_does_not_underestimate_chinese_text() -> None:
    provider = InMemorySparseIndexProvider()
    chunk = _chunk("chunk_1")
    chunk = IndexChunk(
        **{**chunk.to_mapping(), "text": "中文没有空格", "embedding_text": "中文没有空格"}
    )
    _publish(provider, chunk, "attempt_1")
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        token_counter=len,
    )

    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(retrieval_context_tokens_per_space=2),
    )

    assert result.hits == ()
    assert result.degradations[-1]["code"] == "retrieval_context_budget_exceeded"


class _RecordingHardGateAlert:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish_hard_gate_exceeded(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        return "evt_retrieval_hard_gate_test"


def test_hard_gate_truncation_publishes_an_explicit_alert() -> None:
    # A3：超过跨库硬闸（总量 cap）发生截断时必须显式告警，不得静默。
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    alert = _RecordingHardGateAlert()
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        token_counter=len,
        hard_gate_alert=alert,
    )

    result = service.search(
        "text",
        principal="user_1",
        # "text chunk_1" 共 12 字符：放进单库预算（4）无法满足，总量硬闸 4 直接截断。
        profile=RetrievalProfile(
            retrieval_context_tokens_per_space=4, retrieval_context_tokens_cap=4
        ),
    )

    assert result.hits == ()
    assert result.degradations[-1]["code"] == "retrieval_context_budget_exceeded"
    assert len(alert.calls) == 1
    call = alert.calls[0]
    assert call["space_ids"] == ["space_1"]
    assert call["cap_tokens"] == 4
    assert call["used_tokens"] == 0
    assert call["excess_tokens"] == 12 - 4
    assert call["dropped_hit_count"] == 1


def test_per_space_budget_truncation_does_not_raise_the_hard_gate_alert() -> None:
    # 仅单库预算超限、总量硬闸未越限时只记录 degradation，不产生硬闸告警。
    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    alert = _RecordingHardGateAlert()
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        token_counter=len,
        hard_gate_alert=alert,
    )

    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            retrieval_context_tokens_per_space=2, retrieval_context_tokens_cap=100
        ),
    )

    assert result.hits == ()
    assert result.degradations[-1]["code"] == "retrieval_context_budget_exceeded"
    assert alert.calls == []


def test_hard_gate_alert_failure_does_not_break_retrieval() -> None:
    class _FailingAlert:
        def publish_hard_gate_exceeded(self, **kwargs: object) -> str:
            raise RuntimeError("outbox unavailable")

    provider = InMemorySparseIndexProvider()
    _publish(provider, _chunk("chunk_1"), "attempt_1")
    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        token_counter=len,
        hard_gate_alert=_FailingAlert(),
    )

    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(
            retrieval_context_tokens_per_space=4, retrieval_context_tokens_cap=4
        ),
    )

    assert result.hits == ()
    assert result.degradations[-1]["code"] == "retrieval_context_budget_exceeded"


def test_production_runtime_requires_image_ports_and_rejects_memory_adapters() -> None:
    settings = _production_settings()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    production_adapter = object()
    with pytest.raises(RuntimeError, match="image OCR and VLM"):
        build_runtime(
            settings,
            adapters={
                "database_engine": engine,
                "indexing_dense_writer": production_adapter,
                "indexing_sparse_provider": production_adapter,
                "indexing_reranker": production_adapter,
                "indexing_token_counter": len,
            },
        )

    with pytest.raises(RuntimeError, match="memory|test"):
        build_runtime(
            settings,
            adapters={
                "database_engine": engine,
                "indexing_dense_writer": InMemorySparseIndexProvider(provider_name="dense"),
                "indexing_sparse_provider": InMemorySparseIndexProvider(provider_name="sparse"),
                "indexing_reranker": production_adapter,
                "indexing_image_ocr": lambda content, context: "ocr",
                "indexing_image_describer": lambda content, context: "description",
                "indexing_token_counter": len,
            },
        )


def test_production_runtime_rejects_memory_embedding() -> None:
    settings = _production_settings()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    production_adapter = object()
    with pytest.raises(RuntimeError, match="memory|test"):
        build_runtime(
            settings,
            adapters={
                "database_engine": engine,
                "indexing_dense_writer": production_adapter,
                "indexing_sparse_provider": production_adapter,
                "indexing_reranker": production_adapter,
                "indexing_embedding": InMemoryEmbeddingProvider(
                    EmbeddingConfig(
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        api_key="test",
                        model="text-embedding-v4",
                        revision="text-embedding-v4",
                        dimension=8,
                        metric="cosine",
                    )
                ),
                "indexing_image_ocr": lambda content, context: "ocr",
                "indexing_image_describer": lambda content, context: "description",
                "indexing_token_counter": len,
            },
        )
