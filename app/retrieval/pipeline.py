"""Composable retrieval pipeline with extension points."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from langchain_core.documents import Document

from app.providers.contracts import (
    ChatModelProvider,
    RetrievalRequest,
    RetrievalResult,
    RetrievalSource,
    RetrieverProvider,
)


class QueryRewriter(Protocol):
    def rewrite(self, query: str) -> str:
        """Return a rewritten query."""


class Reranker(Protocol):
    def rerank(self, query: str, documents: list[Document]) -> list[Document]:
        """Return documents in preferred order."""


class ContextCompressor(Protocol):
    def compress(self, query: str, documents: list[Document]) -> list[Document]:
        """Return a smaller set of documents or shorter contents."""


@dataclass
class StaticQueryRewriter:
    """Deterministic query rewriter for tests and simple profiles."""

    rewrites: dict[str, str] = field(default_factory=dict)

    def rewrite(self, query: str) -> str:
        return self.rewrites.get(query, query)


@dataclass
class StaticReranker:
    """Metadata-key reranker for deterministic local behavior."""

    key: str = "score"
    reverse: bool = True

    def rerank(self, query: str, documents: list[Document]) -> list[Document]:
        return sorted(
            documents,
            key=lambda document: document.metadata.get(self.key, 0) or 0,
            reverse=self.reverse,
        )


@dataclass
class StaticContextCompressor:
    """Trims document count and optional document text length."""

    max_documents: int | None = None
    max_characters: int | None = None

    def compress(self, query: str, documents: list[Document]) -> list[Document]:
        selected = documents[: self.max_documents] if self.max_documents is not None else documents
        if self.max_characters is None:
            return selected
        return [
            Document(
                page_content=document.page_content[: self.max_characters],
                metadata=dict(document.metadata),
            )
            for document in selected
        ]


@dataclass
class LLMQueryRewriter:
    """Chat-model-backed query rewriter for production retrieval quality tuning."""

    chat_model_provider: ChatModelProvider

    def rewrite(self, query: str) -> str:
        model = self.chat_model_provider.create_chat_model(streaming=False)
        response = model.invoke(
            [
                (
                    "system",
                    "Rewrite the question into a concise retrieval query. "
                    "Preserve named entities, product names, dates, and constraints. "
                    "Return only the rewritten query.",
                ),
                ("human", query),
            ]
        )
        rewritten = _message_content(response).strip()
        return rewritten or query


@dataclass
class LLMReranker:
    """Chat-model-backed listwise reranker for retrieved document chunks."""

    chat_model_provider: ChatModelProvider
    max_content_characters: int = 700

    def rerank(self, query: str, documents: list[Document]) -> list[Document]:
        if len(documents) <= 1:
            return documents

        identifiers = [
            _rerank_identifier(index, document)
            for index, document in enumerate(documents, start=1)
        ]
        model = self.chat_model_provider.create_chat_model(streaming=False)
        response = model.invoke(
            [
                (
                    "system",
                    "Rerank the document chunks for the user's question. "
                    "Return only the chunk identifiers in descending relevance order, "
                    "separated by commas. Do not explain.",
                ),
                (
                    "human",
                    _rerank_prompt(
                        query=query,
                        documents=documents,
                        identifiers=identifiers,
                        max_content_characters=self.max_content_characters,
                    ),
                ),
            ]
        )
        requested_order = _parse_rerank_identifiers(_message_content(response))
        if not requested_order:
            return documents

        lookup: dict[str, Document] = {}
        for index, (identifier, document) in enumerate(zip(identifiers, documents), start=1):
            lookup[identifier] = document
            lookup[str(index)] = document

        selected: list[Document] = []
        selected_ids: set[int] = set()
        for identifier in requested_order:
            document = lookup.get(identifier)
            if document is None:
                continue
            document_identity = id(document)
            if document_identity in selected_ids:
                continue
            selected.append(document)
            selected_ids.add(document_identity)

        if not selected:
            return documents

        selected_ids = {id(document) for document in selected}
        selected.extend(document for document in documents if id(document) not in selected_ids)
        return selected


@dataclass
class LLMContextCompressor:
    """Chat-model-backed context compressor that preserves document metadata."""

    chat_model_provider: ChatModelProvider
    max_characters: int = 1200

    def compress(self, query: str, documents: list[Document]) -> list[Document]:
        model = self.chat_model_provider.create_chat_model(streaming=False)
        compressed_documents: list[Document] = []
        for document in documents:
            response = model.invoke(
                [
                    (
                        "system",
                        "Compress the document chunk for the user's question. "
                        "Keep only facts useful for answering, preserve source-grounded wording, "
                        "and return only the compressed context.",
                    ),
                    (
                        "human",
                        f"Question:\n{query}\n\nDocument chunk:\n{document.page_content}",
                    ),
                ]
            )
            compressed_content = _message_content(response).strip()
            if not compressed_content:
                compressed_content = document.page_content
            compressed_documents.append(
                Document(
                    page_content=compressed_content[: self.max_characters],
                    metadata=dict(document.metadata),
                )
            )
        return compressed_documents


@dataclass
class RetrievalPipeline:
    """RetrieverProvider that composes rewrite, retrieval, rerank, compression, and sources."""

    primary_retriever: RetrieverProvider
    additional_retrievers: list[RetrieverProvider] = field(default_factory=list)
    query_rewriter: QueryRewriter | None = None
    reranker: Reranker | None = None
    compressor: ContextCompressor | None = None
    default_top_k: int = 3

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        total_started = perf_counter()
        stages: list[str] = []
        timings_ms: dict[str, float] = {}
        top_k = request.top_k or self.default_top_k
        rewritten_query = request.query

        if self.query_rewriter is not None:
            started = perf_counter()
            rewritten_query = self.query_rewriter.rewrite(request.query)
            timings_ms["rewrite"] = _elapsed_ms(started)
            stages.append("rewrite")

        started = perf_counter()
        stages.append("retrieve")
        retriever_request = RetrievalRequest(
            query=rewritten_query,
            top_k=top_k,
            filters=request.filters,
        )
        retriever_results = [
            retriever.retrieve(retriever_request)
            for retriever in [self.primary_retriever, *self.additional_retrievers]
        ]
        timings_ms["retrieve"] = _elapsed_ms(started)

        started = perf_counter()
        documents, deduplicated = _deduplicate_documents(
            document
            for result in retriever_results
            for document in result.documents
        )
        timings_ms["deduplicate"] = _elapsed_ms(started)
        stages.append("deduplicate")

        if self.reranker is not None:
            started = perf_counter()
            documents = self.reranker.rerank(rewritten_query, documents)
            timings_ms["rerank"] = _elapsed_ms(started)
            stages.append("rerank")

        if self.compressor is not None:
            started = perf_counter()
            documents = self.compressor.compress(rewritten_query, documents)
            timings_ms["compress"] = _elapsed_ms(started)
            stages.append("compress")

        started = perf_counter()
        documents = documents[:top_k]
        sources = [_source_from_document(index, document) for index, document in enumerate(documents, 1)]
        timings_ms["sources"] = _elapsed_ms(started)
        timings_ms["total"] = _elapsed_ms(total_started)
        stages.append("sources")

        return RetrievalResult(
            query=request.query,
            rewritten_query=rewritten_query if rewritten_query != request.query else None,
            documents=documents,
            sources=sources,
            debug={
                "top_k": top_k,
                "profile": _profile_name(retriever_results),
                "retriever_count": len(retriever_results),
                "retrievers": [result.debug for result in retriever_results],
                "deduplicated": deduplicated,
                "stages": stages,
                "timings_ms": timings_ms,
            },
        )


def _deduplicate_documents(documents) -> tuple[list[Document], int]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[Document] = []
    deduplicated = 0
    for document in documents:
        metadata = document.metadata
        chunk_id = str(metadata.get("chunk_id") or "")
        key = (
            ("chunk_id", chunk_id)
            if chunk_id
            else (
                "content",
                str(metadata.get("source_path") or metadata.get("_source") or metadata.get("source", "")),
                document.page_content,
            )
        )
        if key in seen:
            deduplicated += 1
            continue
        seen.add(key)
        unique.append(document)
    return unique, deduplicated


def _profile_name(results: list[RetrievalResult]) -> str:
    for result in results:
        profile = result.debug.get("profile")
        if profile:
            return str(profile)
    return "default"


def _source_from_document(index: int, document: Document) -> RetrievalSource:
    metadata = document.metadata
    heading_path = metadata.get("heading_path") or _heading_path(metadata)
    score = metadata.get("score")
    return RetrievalSource(
        index=index,
        source_path=str(metadata.get("source_path") or metadata.get("_source") or metadata.get("source") or ""),
        file_name=str(metadata.get("file_name") or metadata.get("_file_name") or ""),
        heading_path=heading_path,
        chunk_id=str(metadata.get("chunk_id") or ""),
        document_id=str(metadata.get("document_id") or ""),
        score=float(score) if isinstance(score, int | float) else None,
    )


def _heading_path(metadata: dict) -> str:
    headings = [metadata[key] for key in ("h1", "h2", "h3", "h4") if metadata.get(key)]
    return " > ".join(headings)


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)


def _rerank_identifier(index: int, document: Document) -> str:
    for key in ("chunk_id", "document_id"):
        value = document.metadata.get(key)
        if value:
            return str(value)
    return str(index)


def _rerank_prompt(
    *,
    query: str,
    documents: list[Document],
    identifiers: list[str],
    max_content_characters: int,
) -> str:
    chunks = []
    for identifier, document in zip(identifiers, documents):
        content = " ".join(document.page_content.split())
        chunks.append(f"[{identifier}]\n{content[:max_content_characters]}")
    return "Question:\n" + query + "\n\nDocument chunks:\n" + "\n\n".join(chunks)


def _parse_rerank_identifiers(content: str) -> list[str]:
    stripped = content.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]

    normalized = stripped.replace("\n", ",").replace(";", ",")
    identifiers: list[str] = []
    for item in normalized.split(","):
        identifier = item.strip().strip("[](){}\"'` ")
        if identifier:
            identifiers.append(identifier)
    return identifiers


def _message_content(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)
