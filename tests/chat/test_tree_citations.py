"""Tree summaries remain context, while citations retain the source text."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.chat.ports import IndexingChatRetrievalPort
from app.indexing import GenerationManager, InMemorySparseIndexProvider, RetrievalScope
from app.indexing.retrieval import RetrievalService, ScoreReranker
from tests.indexing.test_hybrid_retrieval import _chunk, _facts, _publish


@pytest.mark.parametrize("tree_only", [False, True])
def test_tree_context_keeps_original_citations_through_publication(tree_only: bool) -> None:
    source = "text original source " * (1000 if tree_only else 1)
    chunk = replace(
        _chunk("child_1"), text=source, snippet=source, locator={"section_path": "chapter"}
    )
    provider = InMemorySparseIndexProvider()
    _publish(provider, chunk, "attempt_1")

    def tree_router(query, candidates, **kwargs):
        return SimpleNamespace(
            skipped=False,
            documents=(
                SimpleNamespace(
                    document_id=chunk.document_id,
                    status="ok",
                    result={"text": "GENERATED TREE ANSWER"},
                ),
            ),
        )

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        reranker=ScoreReranker(),
        tree_router=tree_router,
        token_counter=len,
    )
    port = IndexingChatRetrievalPort(SimpleNamespace(open_retrieval_request=service.open_request))
    result = port.search(
        "text",
        principal="user_1",
        narrowing_scope=None,
        profile_id="default",
        profile_version="1",
        effort="think",
        strategy_operations=("tree",),
    )
    tree = next(hit for hit in result.hits if hit.library == "tree")
    assert len(result.hits) == (1 if tree_only else 2)
    assert tree.context_text == "GENERATED TREE ANSWER"
    assert tree.snippet == source
    citations = port.resolve_citations(
        ({"document_id": chunk.document_id, "chunk_id": chunk.chunk_id},), principal="user_1"
    )
    assert len(citations) == 1
    assert citations[0]["snippet"] == source
    assert citations[0]["locator"] == chunk.locator
    if not tree_only:
        assert citations[0]["library"] != "tree"
    assert port.revalidate_citations(citations, principal="user_1") == citations
