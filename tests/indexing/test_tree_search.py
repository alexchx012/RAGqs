from __future__ import annotations

from types import SimpleNamespace

from app.indexing import RetrievalHit
from app.indexing.models import IndexChunk
from app.indexing.tree_search import PageIndexTreeRouter
from app.platform.errors import PlatformError


def _hit(
    chunk_id: str,
    document_id: str,
    rerank_score: float | None,
) -> RetrievalHit:
    return RetrievalHit(
        chunk=IndexChunk(
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
        ),
        score=0.5,
        source="dense",
        rerank_score=rerank_score,
    )


class RecordingProvider:
    provider_name = "ds-v4-flash"

    def __init__(self, fail_documents: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_documents = fail_documents or set()

    def search_document(self, query: str, hit: RetrievalHit) -> dict[str, str]:
        self.calls.append(hit.chunk.document_id)
        if hit.chunk.document_id in self.fail_documents:
            raise RuntimeError("tree provider down")
        return {"document_id": hit.chunk.document_id, "answer": "ok"}


def test_tree_candidates_use_stable_score_order_and_first_chunk_per_document() -> None:
    provider = RecordingProvider()
    router = PageIndexTreeRouter(provider)
    candidates = [
        _hit("c1", "doc_b", 0.7),
        _hit("c2", "doc_a", 0.9),
        _hit("c3", "doc_a", 0.8),
        _hit("c4", "doc_c", 0.95),
    ]
    outcome = router("query", candidates, max_documents=2, rag_call_limit=4)
    assert not outcome.skipped
    assert provider.calls == ["doc_c", "doc_a"]
    assert all(item.status == "ok" for item in outcome.documents)


def test_missing_document_identity_degrades_without_tree_call() -> None:
    provider = RecordingProvider()
    router = PageIndexTreeRouter(provider)
    # Valid IndexChunks always carry identity; this exercises the router guard
    # for upstream candidates that lost document identity before tree routing.
    missing = SimpleNamespace(
        chunk=SimpleNamespace(document_id="", chunk_id="c1"),
        source="dense",
        score=0.5,
        rerank_score=None,
    )
    candidates = [missing, _hit("c2", "doc_a", 0.8)]
    outcome = router("query", candidates, max_documents=2, rag_call_limit=4)
    missing = [item for item in outcome.documents if item.status == "missing_document_identity"]
    assert len(missing) == 1
    assert provider.calls == ["doc_a"]


def test_tree_search_skips_entirely_without_final_scores() -> None:
    provider = RecordingProvider()
    router = PageIndexTreeRouter(provider)
    candidates = [_hit("c1", "doc_a", None), _hit("c2", "doc_b", 0.8)]
    outcome = router("query", candidates, max_documents=2, rag_call_limit=4)
    assert outcome.skipped
    assert outcome.reason == "rerank_degraded"
    assert provider.calls == []


def test_tree_budget_gate_and_parallel_document_failures() -> None:
    provider = RecordingProvider(fail_documents={"doc_a"})
    router = PageIndexTreeRouter(provider)
    candidates = [_hit("c1", "doc_a", 0.9), _hit("c2", "doc_b", 0.8)]
    outcome = router("query", candidates, max_documents=2, rag_call_limit=0)
    assert outcome.skipped
    assert outcome.reason == "budget_exhausted"
    assert provider.calls == []

    outcome = router("query", candidates, max_documents=2, rag_call_limit=4)
    assert not outcome.skipped
    degraded = [item for item in outcome.documents if item.status == "degraded"]
    assert [item.document_id for item in degraded] == ["doc_a"]
    assert degraded[0].reason == "unavailable"


def test_tree_router_rejects_local_or_unapproved_providers() -> None:
    local = RecordingProvider()
    local.provider_name = "local-model"
    try:
        PageIndexTreeRouter(local)
    except PlatformError as error:
        assert error.code == "tree_provider_invalid"
    else:
        raise AssertionError("local models must not be accepted")
