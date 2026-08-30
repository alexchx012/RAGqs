"""Unit tests for the production retrieval port's citation resolution path."""

from __future__ import annotations

from typing import Any

from app.chat.ports import IndexingChatRetrievalPort
from app.indexing.models import IndexChunk, RetrievalHit, RetrievalResult


class _FakeRequest:
    def __init__(self) -> None:
        self.released = False

    def search(
        self, query: str, *, principal: Any, narrowing_scope: Any, profile: Any, budget: Any = None
    ) -> Any:
        del query, principal, narrowing_scope
        chunk = IndexChunk(
            chunk_id="chunk_1",
            generation_id="gen_index_1",
            publication_id="pub_1",
            document_id="doc_1",
            document_version_id="ver_1",
            space_id="space_1",
            text="text",
            embedding_text="embedding",
            locator={"page": 1},
            snippet="the answer is 42",
            media_kind="pdf",
            manifest_hash="hash_1",
        )
        return RetrievalResult(
            hits=(RetrievalHit(chunk=chunk, score=1.0, source="fake"),),
            generation_id="gen_index_1",
            profile=profile,
        )

    def resolve_citation(self, hit: RetrievalHit, *, principal: Any) -> dict[str, Any]:
        del principal
        return {
            "state": "available",
            "document_id": hit.chunk.document_id,
            "document_version_id": hit.chunk.document_version_id,
            "publication_id": hit.chunk.publication_id,
            "generation_id": hit.chunk.generation_id,
            "chunk_id": hit.chunk.chunk_id,
            "locator": dict(hit.chunk.locator),
            "snippet": hit.chunk.snippet,
        }

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.released = True


class _FakeIndexing:
    def __init__(self) -> None:
        self.request = _FakeRequest()

    def open_retrieval_request(self) -> _FakeRequest:
        return self.request


def test_resolves_citations_through_live_request_until_final_revalidation() -> None:
    indexing = _FakeIndexing()
    port = IndexingChatRetrievalPort(indexing)
    outcome = port.search(
        "hello",
        principal=None,
        narrowing_scope=None,
        profile_id="default",
        profile_version="1",
        effort="quick",
    )
    assert len(outcome.hits) == 1

    citations = port.resolve_citations(
        ({"document_id": "doc_1", "chunk_id": "chunk_1"},),
        principal=None,
    )
    assert [citation["document_id"] for citation in citations] == ["doc_1"]
    assert citations[0]["chunk_id"] == "chunk_1"
    assert "state" not in citations[0]
    assert indexing.request.released is False

    revalidated = port.revalidate_citations(citations, principal=None)
    assert revalidated == citations
    assert indexing.request.released is True


def test_missing_live_request_raises_conflict() -> None:
    from app.platform.errors import PlatformError

    port = IndexingChatRetrievalPort(_FakeIndexing())
    try:
        port.resolve_citations(
            ({"document_id": "doc_1", "chunk_id": "chunk_1"},),
            principal=None,
        )
    except PlatformError as error:
        assert error.code == "retrieval_request_required"
        assert error.status_code == 409
    else:
        raise AssertionError("resolve_citations without a live request must fail")
