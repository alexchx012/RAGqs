"""Greenfield, rebuildable content processing and retrieval domain."""

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
from .persistence import SqlAlchemyGenerationManager, SqlAlchemyIndexingRepository
from .processing import ContentProcessor, IdentityCompression, OCRSamplePlan, ProcessingOutput
from .providers import (
    IndexWriter,
    InMemoryIndexWriter,
    InMemorySparseIndexProvider,
    OpenSearchSparseIndexProvider,
    SparseIndexProvider,
    StageResult,
    build_sparse_provider,
)
from .releases import RetrievalReleaseService
from .rerank import (
    RerankerRelease,
    StubRerankerModel,
    TwoStageReranker,
)
from .retrieval import (
    CitationService,
    NoopReranker,
    RetrievalService,
    ScoreReranker,
    intersect_scopes,
)
from .routing import MetadataPrefilter, RouteOutput, RuleQueryRouter, Subquestion
from .schema import INDEXING_TABLE_NAMES, indexing_metadata
from .service import IndexingService
from .tree_search import PageIndexTreeRouter, TreeDocumentOutcome, TreeSearchOutcome

__all__ = [
    "AllowedRetrievalScope",
    "CitationService",
    "ContentProcessor",
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
    "IdentityCompression",
    "INDEXING_TABLE_NAMES",
    "InMemoryEmbeddingProvider",
    "IndexChunk",
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
    "ProcessingOutput",
    "RetrievalHit",
    "RetrievalCandidate",
    "RetrievalProfile",
    "RetrievalResult",
    "RetrievalReleaseService",
    "RerankerRelease",
    "RetrievalScope",
    "RetrievalService",
    "ScoreReranker",
    "StubRerankerModel",
    "Subquestion",
    "TwoStageReranker",
    "MetadataPrefilter",
    "RouteOutput",
    "RuleQueryRouter",
    "PageIndexTreeRouter",
    "TreeDocumentOutcome",
    "TreeSearchOutcome",
    "SqlAlchemyGenerationManager",
    "SqlAlchemyIndexingRepository",
    "SparseIndexProvider",
    "StageResult",
    "build_sparse_provider",
    "indexing_metadata",
    "intersect_scopes",
]
