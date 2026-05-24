"""In-memory provider implementations for tests and local development."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.providers.contracts import (
    IngestionResult,
    RetrievalRequest,
    RetrievalResult,
    SessionSummary,
    StoredMessage,
)


@dataclass
class FakeEmbeddingProvider:
    """Deterministic embedding provider that requires no external API."""

    dimensions: int = 8

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [round(byte / 255, 6) for byte in digest[: self.dimensions]]


class FakeChatModel:
    """Small async chat model surface compatible with unit tests."""

    def __init__(self, response: str):
        self.response = response

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        return AIMessage(content=self.response)


@dataclass
class FakeChatModelProvider:
    """Chat model provider that always returns a fixed answer."""

    response: str = "fake answer"

    def create_chat_model(self, streaming: bool = True) -> FakeChatModel:
        return FakeChatModel(self.response)


@dataclass
class FakeVectorStoreProvider:
    """In-memory vector store with simple token matching."""

    documents: list[Document] = field(default_factory=list)

    def add_documents(self, documents: list[Document]) -> list[str]:
        start = len(self.documents)
        self.documents.extend(documents)
        return [f"fake-doc-{index}" for index in range(start, start + len(documents))]

    def delete_by_source(self, source: str) -> int:
        before = len(self.documents)
        self.documents = [
            document
            for document in self.documents
            if document.metadata.get("_source") != source
        ]
        return before - len(self.documents)

    def delete_by_document_id(self, document_id: str) -> int:
        before = len(self.documents)
        self.documents = [
            document
            for document in self.documents
            if document.metadata.get("document_id") != document_id
        ]
        return before - len(self.documents)

    def similarity_search(
        self,
        query: str,
        k: int = 3,
        filters: dict[str, object] | None = None,
    ) -> list[Document]:
        query_terms = {term.lower() for term in query.split() if term.strip()}
        filters = filters or {}
        matches: list[Document] = []

        for document in self.documents:
            if any(document.metadata.get(key) != value for key, value in filters.items()):
                continue

            content_terms = {
                term.strip(".,:;!?()[]{}").lower()
                for term in document.page_content.split()
                if term.strip()
            }
            if not query_terms or query_terms & content_terms:
                matches.append(document)

        return matches[:k]


@dataclass
class FakeRetrieverProvider:
    """Retriever provider built on the fake vector store."""

    vector_store: FakeVectorStoreProvider

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        documents = self.vector_store.similarity_search(
            query=request.query,
            k=request.top_k or 3,
            filters=request.filters,
        )
        return RetrievalResult(
            query=request.query,
            documents=documents,
            debug={"provider": "fake", "returned": len(documents)},
        )


@dataclass
class InMemorySessionStoreProvider:
    """Simple session store for backend tests."""

    sessions: dict[str, list[StoredMessage]] = field(default_factory=dict)
    session_updated_order: dict[str, int] = field(default_factory=dict)
    _next_order: int = 0

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> StoredMessage:
        message = StoredMessage(role=role, content=content, metadata=dict(metadata or {}))
        self.sessions.setdefault(session_id, []).append(message)
        self._next_order += 1
        self.session_updated_order[session_id] = self._next_order
        return message

    def get_messages(self, session_id: str) -> list[StoredMessage]:
        return list(self.sessions.get(session_id, []))

    def list_sessions(self, query: str | None = None) -> list[SessionSummary]:
        normalized_query = query.strip().lower() if query else ""
        summaries: list[SessionSummary] = []

        for session_id, messages in self.sessions.items():
            if not messages:
                continue
            summary = _build_session_summary(session_id, messages)
            if normalized_query and not _matches_session_query(
                normalized_query, session_id, summary, messages
            ):
                continue
            summaries.append(summary)

        return sorted(
            summaries,
            key=lambda summary: self.session_updated_order.get(summary.session_id, 0),
            reverse=True,
        )

    def clear(self, session_id: str) -> bool:
        self.sessions.pop(session_id, None)
        self.session_updated_order.pop(session_id, None)
        return True


@dataclass
class FakeIngestionProvider:
    """Records ingestion calls without reading files."""

    indexed_paths: list[str] = field(default_factory=list)

    def index_file(self, file_path: str, space_id: str = "default") -> IngestionResult:
        self.indexed_paths.append(file_path)
        return IngestionResult(
            success=True,
            source=file_path,
            document_count=1,
            metadata={"spaceId": space_id},
        )

    def index_directory(self, directory_path: str, space_id: str = "default") -> IngestionResult:
        self.indexed_paths.append(directory_path)
        return IngestionResult(
            success=True,
            source=directory_path,
            document_count=1,
            metadata={"spaceId": space_id},
        )


def _build_session_summary(session_id: str, messages: list[StoredMessage]) -> SessionSummary:
    title_message = next((message for message in messages if message.role == "user"), messages[0])
    last_message = messages[-1]
    return SessionSummary(
        session_id=session_id,
        title=_truncate_text(title_message.content, max_length=80) or "New chat",
        message_count=len(messages),
        updated_at=last_message.created_at,
        last_message=_truncate_text(last_message.content, max_length=120),
    )


def _matches_session_query(
    query: str,
    session_id: str,
    summary: SessionSummary,
    messages: list[StoredMessage],
) -> bool:
    haystacks = [session_id, summary.title, summary.last_message]
    haystacks.extend(message.content for message in messages)
    return any(query in haystack.lower() for haystack in haystacks)


def _truncate_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."
