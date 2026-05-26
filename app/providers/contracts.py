"""Provider contracts for replaceable RAG foundation components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from langchain_core.documents import Document

if TYPE_CHECKING:
    from app.observability.retrieval_audit import RetrievalAuditRecord


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RetrievalRequest:
    """Input to a retriever provider."""

    query: str
    top_k: int | None = None
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalSource:
    """Citation-friendly source metadata for retrieved documents."""

    index: int
    source_path: str
    file_name: str
    heading_path: str = ""
    chunk_id: str = ""
    document_id: str = ""
    score: float | None = None


@dataclass(frozen=True)
class RetrievalResult:
    """Structured retriever output used by agents, APIs, and evaluators."""

    query: str
    documents: list[Document]
    rewritten_query: str | None = None
    sources: list[RetrievalSource] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredMessage:
    """Provider-neutral session message."""

    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)


@dataclass(frozen=True)
class SessionSummary:
    """Sidebar-ready session metadata without loading the full transcript."""

    session_id: str
    title: str
    message_count: int
    updated_at: str
    last_message: str = ""


@dataclass(frozen=True)
class IngestionResult:
    """Provider-neutral ingestion outcome."""

    success: bool
    source: str
    document_count: int = 0
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embeds queries and documents without binding callers to a vendor SDK."""

    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document texts."""

    def embed_query(self, text: str) -> list[float]:
        """Embed one query string."""


@runtime_checkable
class ChatModelProvider(Protocol):
    """Factory for chat models used by LangChain or LangGraph."""

    def create_chat_model(self, streaming: bool = True) -> Any:
        """Create a chat model instance."""


@runtime_checkable
class CheckpointProvider(Protocol):
    """Factory for LangGraph checkpointers."""

    def create_checkpointer(self) -> Any:
        """Create or return a checkpointer instance for graph compilation."""


@runtime_checkable
class VectorStoreProvider(Protocol):
    """Minimal vector store boundary required by indexing and retrieval."""

    def add_documents(self, documents: list[Document]) -> list[str]:
        """Add documents and return stable ids."""

    def delete_by_source(self, source: str) -> int:
        """Delete indexed chunks for one source."""

    def delete_by_document_id(self, document_id: str) -> int:
        """Delete indexed chunks for one stable document id."""

    def similarity_search(
        self,
        query: str,
        k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        """Return documents matching the query."""


@runtime_checkable
class RetrieverProvider(Protocol):
    """Structured retrieval boundary for agents and evaluators."""

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Run retrieval and return structured documents plus debug data."""


@runtime_checkable
class SessionStoreProvider(Protocol):
    """Session history boundary independent from LangGraph checkpoint storage."""

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> StoredMessage:
        """Append one message to a session."""

    def get_messages(self, session_id: str) -> list[StoredMessage]:
        """Return messages for a session."""

    def list_sessions(self, query: str | None = None) -> list[SessionSummary]:
        """Return searchable session summaries sorted by most recently updated."""

    def clear(self, session_id: str) -> bool:
        """Clear one session."""


@runtime_checkable
class RetrievalAuditStoreProvider(Protocol):
    """Store selected retrieval context and generated answer traces."""

    def append(self, record: RetrievalAuditRecord) -> RetrievalAuditRecord:
        """Persist one retrieval audit record."""

    def list_records(
        self,
        *,
        session_id: str | None = None,
        space_id: str | None = None,
        trace_id: str | None = None,
        limit: int = 50,
    ) -> list[RetrievalAuditRecord]:
        """Return recent retrieval audit records matching the optional filters."""


@runtime_checkable
class IngestionProvider(Protocol):
    """Document ingestion boundary for files, directories, and future loaders."""

    def index_file(self, file_path: str, space_id: str = "default") -> IngestionResult:
        """Index one file."""

    def index_directory(self, directory_path: str, space_id: str = "default") -> IngestionResult:
        """Index a directory."""
