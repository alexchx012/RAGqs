"""Provider contracts and test doubles for the RAG foundation."""

from app.providers.contracts import (
    ChatModelProvider,
    CheckpointProvider,
    EmbeddingProvider,
    IngestionProvider,
    IngestionResult,
    RetrievalRequest,
    RetrievalResult,
    RetrievalSource,
    RetrieverProvider,
    SessionSummary,
    SessionStoreProvider,
    StoredMessage,
    VectorStoreProvider,
)
from app.providers.checkpoints import InMemoryCheckpointProvider, SQLiteCheckpointProvider
from app.providers.dashscope import DashScopeChatModelProvider, DashScopeEmbeddingProvider
from app.providers.fakes import (
    FakeChatModelProvider,
    FakeEmbeddingProvider,
    FakeIngestionProvider,
    FakeRetrieverProvider,
    FakeVectorStoreProvider,
    InMemorySessionStoreProvider,
)
from app.providers.ingestion import VectorIndexIngestionProvider
from app.providers.milvus import MilvusVectorStoreProvider
from app.providers.openai_compatible import (
    OpenAICompatibleChatModelProvider,
    OpenAICompatibleEmbeddingProvider,
)
from app.providers.postgres_session import PostgresSessionStoreProvider
from app.providers.retrieval import VectorStoreRetrieverProvider
from app.providers.sqlite_session import SQLiteSessionStoreProvider

__all__ = [
    "ChatModelProvider",
    "CheckpointProvider",
    "DashScopeChatModelProvider",
    "DashScopeEmbeddingProvider",
    "EmbeddingProvider",
    "FakeChatModelProvider",
    "FakeEmbeddingProvider",
    "FakeIngestionProvider",
    "FakeRetrieverProvider",
    "FakeVectorStoreProvider",
    "InMemorySessionStoreProvider",
    "InMemoryCheckpointProvider",
    "IngestionProvider",
    "IngestionResult",
    "MilvusVectorStoreProvider",
    "OpenAICompatibleChatModelProvider",
    "OpenAICompatibleEmbeddingProvider",
    "PostgresSessionStoreProvider",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalSource",
    "RetrieverProvider",
    "SessionSummary",
    "SessionStoreProvider",
    "SQLiteSessionStoreProvider",
    "SQLiteCheckpointProvider",
    "StoredMessage",
    "VectorIndexIngestionProvider",
    "VectorStoreRetrieverProvider",
    "VectorStoreProvider",
]
