"""Greenfield, rebuildable content processing and retrieval domain."""

from .contextual import CONTEXTUAL_MODEL, ContextualDocument, ContextualRetrievalService
from .contextual_provider import DashScopeContextualRetriever
from .embedding import (
    EmbeddingConfig,
    EmbeddingProvider,
    EmbeddingUsageContext,
    InMemoryEmbeddingProvider,
    OpenAICompatibleEmbedding,
)
from .generation import GenerationManager, IndexGenerationService
from .graph import (
    GraphComponentCoordinator,
    GraphComponentReleaseReceipt,
    GraphComponentStageGrant,
    GraphComponentStageReceipt,
    IndexGenerationComponentInput,
)
from .meilisearch import MeilisearchSparseIndexProvider
from .milvus import MilvusIndexWriter
from .models import (
    AllowedRetrievalScope,
    DocumentVisibilityFact,
    Generation,
    GenerationComponentReaderLease,
    GenerationReferenceLease,
    IndexChunk,
    IndexGenerationGcReceipt,
    NarrowingScope,
    RetrievalCandidate,
    RetrievalHit,
    RetrievalProfile,
    RetrievalResult,
    RetrievalScope,
)
from .opensearch import (
    HttpOpenSearchClient,
)
from .opensearch import OpenSearchSparseIndexProvider as RealOpenSearchSparseIndexProvider
from .persistence import SqlAlchemyGenerationManager, SqlAlchemyIndexingRepository
from .prefix_cache import PrefixCacheManager
from .processing import ContentProcessor, IdentityCompression, OCRSamplePlan, ProcessingOutput
from .providers import (
    IndexWriter,
    InMemoryIndexWriter,
    InMemoryOpenSearchSparseIndexProvider,
    InMemorySparseIndexProvider,
    SparseIndexProvider,
    StageResult,
    build_sparse_provider,
)
from .releases import RetrievalReleaseService
from .retrieval import (
    CitationService,
    NoopReranker,
    RetrievalService,
    ScoreReranker,
    intersect_scopes,
)
from .schema import INDEXING_TABLE_NAMES, indexing_metadata
from .service import IndexingService

OpenSearchSparseIndexProvider = RealOpenSearchSparseIndexProvider

__all__ = [
    "AllowedRetrievalScope",
    "CitationService",
    "CONTEXTUAL_MODEL",
    "ContentProcessor",
    "ContextualDocument",
    "ContextualRetrievalService",
    "DashScopeContextualRetriever",
    "DocumentVisibilityFact",
    "EmbeddingConfig",
    "EmbeddingProvider",
    "EmbeddingUsageContext",
    "Generation",
    "GenerationComponentReaderLease",
    "GenerationManager",
    "GenerationReferenceLease",
    "GraphComponentCoordinator",
    "GraphComponentReleaseReceipt",
    "GraphComponentStageGrant",
    "GraphComponentStageReceipt",
    "HttpOpenSearchClient",
    "IdentityCompression",
    "INDEXING_TABLE_NAMES",
    "InMemoryEmbeddingProvider",
    "IndexChunk",
    "InMemoryOpenSearchSparseIndexProvider",
    "IndexGenerationComponentInput",
    "IndexGenerationGcReceipt",
    "IndexGenerationService",
    "IndexingService",
    "IndexWriter",
    "InMemoryIndexWriter",
    "InMemorySparseIndexProvider",
    "MeilisearchSparseIndexProvider",
    "MilvusIndexWriter",
    "NarrowingScope",
    "NoopReranker",
    "OCRSamplePlan",
    "OpenAICompatibleEmbedding",
    "OpenSearchSparseIndexProvider",
    "PrefixCacheManager",
    "ProcessingOutput",
    "RetrievalHit",
    "RetrievalCandidate",
    "RetrievalProfile",
    "RetrievalResult",
    "RetrievalReleaseService",
    "RetrievalScope",
    "RetrievalService",
    "ScoreReranker",
    "SqlAlchemyGenerationManager",
    "SqlAlchemyIndexingRepository",
    "SparseIndexProvider",
    "StageResult",
    "build_sparse_provider",
    "indexing_metadata",
    "intersect_scopes",
]
