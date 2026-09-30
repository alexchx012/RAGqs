"""检索装配的父子组装（parent-child-chunking）：父块展开、同父折叠配额与 token、粒度与兼容。"""

from __future__ import annotations

from typing import Any

from app.indexing import (
    GenerationManager,
    InMemorySparseIndexProvider,
    RetrievalProfile,
    RetrievalScope,
    RetrievalService,
)
from app.indexing.models import IndexChunk
from tests.indexing.test_hybrid_retrieval import _facts, _publish

PARENT_TEXT = "text alpha one\n\ntext alpha two\n\ntext alpha three"


def _chunk(
    chunk_id: str,
    text: str,
    *,
    document_id: str = "document_1",
    parent_id: str | None = None,
    parent_text: str | None = None,
) -> IndexChunk:
    metadata: dict[str, Any] = {}
    if parent_id is not None:
        metadata = {"parent_id": parent_id, "parent_text": parent_text}
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_initial",
        publication_id=f"publication_{document_id}",
        document_id=document_id,
        document_version_id="version_1",
        space_id="space_1",
        text=text,
        embedding_text=text,
        locator={},
        snippet=text,
        media_kind="text/markdown",
        manifest_hash="manifest_1",
        metadata=metadata,
    )


def _siblings() -> list[IndexChunk]:
    return [
        _chunk(f"chunk_a{index}", text, parent_id="parent_1", parent_text=PARENT_TEXT)
        for index, text in enumerate(PARENT_TEXT.split("\n\n"), start=1)
    ]


def _service(*chunks: IndexChunk) -> RetrievalService:
    provider = InMemorySparseIndexProvider()
    for chunk in chunks:
        _publish(provider, chunk, f"attempt_{chunk.chunk_id}")
    return RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
    )


def _search(service: RetrievalService, query: str = "text", **profile: Any):
    values: dict[str, Any] = {"top_k": 10, "candidate_limit": 10}
    values.update(profile)
    return service.search(query, principal="user_1", profile=RetrievalProfile(**values))


def test_profile_defaults_fit_six_parent_blocks_per_space() -> None:
    profile = RetrievalProfile()

    assert profile.retrieval_context_tokens_per_space == 16000
    assert profile.retrieval_context_tokens_cap == 48000


def test_child_hit_returns_its_parent_block_as_context() -> None:
    child = _siblings()[0]

    result = _search(_service(child))

    assert [hit.chunk.chunk_id for hit in result.hits] == ["chunk_a1"]
    # 上下文正文是父块：包含同父其他子块的文本。
    assert result.hits[0].context_text == PARENT_TEXT
    assert "text alpha two" in (result.hits[0].context_text or "")


def test_sibling_hits_fold_into_one_quota_slot_and_one_parent_charge() -> None:
    other = _chunk("chunk_b1", "text beta", document_id="document_2")
    # 单边预算恰好容纳「父块一次 + 另一文档一次」：父块若按兄弟重复计费就会超限。
    budget = len(PARENT_TEXT) + len(other.text)

    result = _search(
        _service(*_siblings(), other),
        retrieval_context_items_per_space=2,
        retrieval_context_tokens_per_space=budget,
    )

    assert [hit.chunk.chunk_id for hit in result.hits] == ["chunk_a1", "chunk_b1"]
    assert [hit.context_text for hit in result.hits] == [PARENT_TEXT, "text beta"]
    assert not [
        item
        for item in result.degradations
        if item.get("code") == "retrieval_context_budget_exceeded"
    ]
    # 评测口径的精排前候选池不受折叠影响。
    assert {hit.chunk.chunk_id for hit in result.candidates} == {
        "chunk_a1",
        "chunk_a2",
        "chunk_a3",
        "chunk_b1",
    }


def test_sub_chunk_granularity_does_not_expand_to_parent() -> None:
    service = _service(*_siblings())

    result = _search(service, query='"text"')

    assert result.route_output is not None
    assert result.route_output["return_granularity"] == "sub_chunk"
    assert [hit.context_text for hit in result.hits] == PARENT_TEXT.split("\n\n")


def test_legacy_chunk_without_parent_is_delivered_as_itself() -> None:
    legacy = _chunk("chunk_legacy", "text legacy body")

    result = _search(_service(legacy))

    assert [hit.chunk.chunk_id for hit in result.hits] == ["chunk_legacy"]
    assert result.hits[0].context_text == "text legacy body"


def test_parent_that_no_longer_fits_falls_back_to_the_child() -> None:
    child = _siblings()[0]

    result = _search(
        _service(child),
        retrieval_context_tokens_per_space=len(child.text) + 1,
    )

    assert [hit.context_text for hit in result.hits] == [child.text]
    assert not [
        item
        for item in result.degradations
        if item.get("code") == "retrieval_context_budget_exceeded"
    ]
