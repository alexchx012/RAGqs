from __future__ import annotations

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
from app.indexing.retrieval import NoopReranker
from app.indexing.routing import MetadataPrefilter, RuleQueryRouter
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


def test_plain_query_routes_no_rewrite_and_keeps_original_query() -> None:
    route = RuleQueryRouter().route("什么是检索增强生成")
    assert route.kind == "no_rewrite"
    assert route.original_query == "什么是检索增强生成"
    assert route.rewritten_query is None
    assert route.subquestions is None
    assert route.hyde_text is None
    assert route.search_queries() == ("什么是检索增强生成",)


def test_polite_prefix_produces_single_rewrite_with_original_reference() -> None:
    route = RuleQueryRouter().route("请帮我， 总结报销流程")
    assert route.kind == "rewrite"
    assert route.original_query == "请帮我， 总结报销流程"
    assert route.rewritten_query == "总结报销流程"
    assert route.search_queries() == ("总结报销流程",)


def test_multi_question_query_splits_into_ordered_unique_subquestions() -> None:
    route = RuleQueryRouter().route("报销流程是什么；发票抬头怎么填写？还有审批时限")
    assert route.kind == "split_subquestions"
    assert [item.id for item in route.subquestions or ()] == ["sq-1", "sq-2", "sq-3"]
    assert len(set(item.id for item in route.subquestions or ())) == 3
    assert route.search_queries() == ("报销流程是什么", "发票抬头怎么填写", "还有审批时限")


def test_aggregate_quote_ordinal_and_pronoun_signals_shape_route_fields() -> None:
    router = RuleQueryRouter()
    aggregate = router.route("总结这份文件")
    assert aggregate.return_granularity == "document_summary"
    quoted = router.route('什么是"数字化转型"')
    assert quoted.return_granularity == "sub_chunk"
    ordinal = router.route("第3章写了什么")
    assert ordinal.metadata_prefilter is not None
    assert ordinal.metadata_prefilter.ordinal_from == 3
    assert ordinal.metadata_prefilter.ordinal_to == 3
    pronoun = router.route("它的作者是谁", recent_queries=["介绍红楼梦"])
    assert pronoun.query_history_ref == "conversation_history"


def test_metadata_prefilter_only_narrows_and_validates_typed_fields() -> None:
    prefilter = MetadataPrefilter(
        published_from="2024-01-01",
        published_to="2024-12-31",
        document_types=("policy",),
    )
    assert prefilter.matches({"published_at": "2024-06-01", "document_type": "policy"})
    assert not prefilter.matches({"published_at": "2023-12-31", "document_type": "policy"})
    assert not prefilter.matches({"published_at": "2024-06-01", "document_type": "manual"})
    try:
        MetadataPrefilter(published_from="2024/01/01")
    except PlatformError as error:
        assert error.code == "validation_error"
    else:
        raise AssertionError("invalid date must be rejected")


def test_retrieval_service_runs_each_split_query_additively() -> None:
    provider = InMemorySparseIndexProvider(provider_name="sparse")
    _publish(provider, _chunk("a", document_id="document_a"), "a")
    _publish(provider, _chunk("b", document_id="document_b"), "b")
    seen: list[str] = []

    class CapturingReranker(NoopReranker):
        def rerank(self, query, hits, profile):
            seen.append(query)
            return super().rerank(query, hits, profile)

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=CapturingReranker(),
    )
    result = service.search(
        "报销流程是什么；发票抬头怎么填写",
        principal="user_1",
        profile=RetrievalProfile(candidate_limit=10),
    )
    assert result.route_output is not None
    assert result.route_output["kind"] == "split_subquestions"
    # Candidates are unioned first, then uniformly reranked with the original query.
    assert seen == ["报销流程是什么；发票抬头怎么填写"]
    assert {hit.chunk.chunk_id for hit in result.candidates} >= {"a", "b"}
