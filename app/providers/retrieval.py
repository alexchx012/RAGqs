"""Retriever provider implementations."""

from __future__ import annotations

from dataclasses import dataclass

from app.providers.contracts import RetrievalRequest, RetrievalResult, VectorStoreProvider


@dataclass
class VectorStoreRetrieverProvider:
    """Retriever provider that delegates to a vector store provider."""

    vector_store_provider: VectorStoreProvider
    default_top_k: int = 3

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        top_k = request.top_k or self.default_top_k
        documents = self.vector_store_provider.similarity_search(
            query=request.query,
            k=top_k,
            filters=request.filters,
        )
        return RetrievalResult(
            query=request.query,
            documents=documents,
            debug={
                "provider": "vector_store",
                "top_k": top_k,
                "filters": request.filters,
                "returned": len(documents),
            },
        )
