"""Conversation-history-aware retrieval acceptance tests (A2/A10 evidence).

A pronoun follow-up shares no terms with the topic; when the router fires the
conversation-history signal, the primary search branch must use the most
recent user query so the follow-up still retrieves topic documents.
"""

from __future__ import annotations

from app.indexing import (
    DocumentVisibilityFact,
    GenerationManager,
    IndexChunk,
    InMemoryIndexWriter,
    InMemorySparseIndexProvider,
    RetrievalProfile,
    RetrievalScope,
    RetrievalService,
)

TOPIC_QUESTION = "RAGqs 的检索架构是什么？"


def _chunk(chunk_id: str) -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_initial",
        publication_id="publication_1",
        document_id=f"document_{chunk_id}",
        document_version_id="version_1",
        space_id="space_1",
        text=f"{TOPIC_QUESTION} 的架构说明 {chunk_id}",
        embedding_text=f"{TOPIC_QUESTION} 的架构说明 {chunk_id}",
        locator={},
        snippet=f"{TOPIC_QUESTION} 摘要",
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
    provider.stage_chunks(attempt_id, chunk.publication_id, chunk.document_id, "version_1", [chunk])
    provider.publish_staged(attempt_id, chunk.publication_id)


class RecordingDense(InMemoryIndexWriter):
    """Dense backend that records every executed search query."""

    backend_kind = "dense"

    def __init__(self) -> None:
        super().__init__(provider_name="dense")
        self.executed_queries: list[str] = []

    def search(self, query: str, *args: object, **kwargs: object):
        self.executed_queries.append(query)
        return super().search(query, *args, **kwargs)  # type: ignore[arg-type]


def _service(dense: RecordingDense) -> RetrievalService:
    return RetrievalService(
        GenerationManager(),
        [dense, InMemorySparseIndexProvider(provider_name="sparse")],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )


def test_pronoun_followup_resolves_history_signal_into_search_query() -> None:
    dense = RecordingDense()
    _publish(dense, _chunk("topic"), "attempt_topic")
    service = _service(dense)

    result = service.search(
        "它呢？",
        principal="user_1",
        profile=RetrievalProfile(top_k=5, candidate_limit=5),
        recent_queries=(TOPIC_QUESTION,),
    )

    assert dense.executed_queries[0] == TOPIC_QUESTION
    assert result.route_output["query_history_ref"] == "conversation_history"
    assert [hit.chunk.chunk_id for hit in result.hits] == ["topic"]


def test_without_recent_queries_pronoun_searches_verbatim() -> None:
    dense = RecordingDense()
    service = _service(dense)

    service.search("它呢？", principal="user_1")

    assert dense.executed_queries == ["它呢？"]
