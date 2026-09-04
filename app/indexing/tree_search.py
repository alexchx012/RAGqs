from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from app.platform.errors import PlatformError

from .models import RetrievalHit


class TreeSearchProviderPort(Protocol):
    """Remote PageIndex tree-search transport (for example deepseek-v4-flash-0731)."""

    provider_name: str

    def search_document(self, query: str, hit: RetrievalHit) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TreeDocumentOutcome:
    document_id: str
    chunk_id: str
    status: str
    result: Mapping[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TreeSearchOutcome:
    skipped: bool
    reason: str | None
    documents: tuple[TreeDocumentOutcome, ...] = ()


class PageIndexTreeRouter:
    """Parallel second-stage tree search driven only by final reranker scores."""

    def __init__(
        self,
        provider: TreeSearchProviderPort,
        *,
        max_workers: int = 4,
        allowed_provider: str = "deepseek-v4-flash-0731",
    ) -> None:
        if provider.provider_name != allowed_provider:
            raise PlatformError(
                "tree_provider_invalid",
                "tree search requires the configured remote provider",
                {"expected": allowed_provider, "actual": provider.provider_name},
                422,
            )
        self._provider = provider
        self._max_workers = max(1, max_workers)

    def __call__(
        self,
        query: str,
        candidates: Sequence[RetrievalHit],
        *,
        max_documents: int,
        rag_call_limit: int,
    ) -> TreeSearchOutcome:
        if max_documents < 1:
            raise PlatformError("validation_error", "tree document limit is invalid", {}, 422)
        if rag_call_limit < 1:
            return TreeSearchOutcome(True, "budget_exhausted")
        ordered = sorted(candidates, key=self._candidate_sort_key)
        documents: list[RetrievalHit] = []
        identity_degradations: list[TreeDocumentOutcome] = []
        seen_documents: set[str] = set()
        for hit in ordered:
            document_id = str(hit.chunk.document_id or "").strip()
            if not document_id:
                identity_degradations.append(
                    TreeDocumentOutcome(
                        document_id="",
                        chunk_id=hit.chunk.chunk_id,
                        status="missing_document_identity",
                        reason="missing_document_identity",
                    )
                )
                continue
            if document_id in seen_documents:
                continue
            seen_documents.add(document_id)
            documents.append(hit)
            if len(documents) >= min(max_documents, rag_call_limit):
                break
        if not documents or any(hit.rerank_score is None for hit in documents):
            return TreeSearchOutcome(True, "rerank_degraded", tuple(identity_degradations))

        def run(hit: RetrievalHit) -> TreeDocumentOutcome:
            try:
                result = self._provider.search_document(query, hit)
            except Exception as error:
                reason = error.code if isinstance(error, PlatformError) else "unavailable"
                return TreeDocumentOutcome(
                    document_id=str(hit.chunk.document_id),
                    chunk_id=hit.chunk.chunk_id,
                    status="degraded",
                    reason=reason,
                )
            return TreeDocumentOutcome(
                document_id=str(hit.chunk.document_id),
                chunk_id=hit.chunk.chunk_id,
                status="ok",
                result=result,
            )

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(documents))) as pool:
            outcomes = tuple(pool.map(run, documents))
        return TreeSearchOutcome(False, None, tuple(identity_degradations) + outcomes)

    @staticmethod
    def _candidate_sort_key(hit: RetrievalHit) -> tuple[float, str, str]:
        return (
            -(hit.rerank_score if hit.rerank_score is not None else float("inf")),
            hit.chunk.document_id,
            hit.chunk.chunk_id,
        )
