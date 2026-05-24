"""Ingestion foundation primitives."""

from app.ingestion.jobs import IndexingJob, IndexingJobStatus
from app.ingestion.job_store import (
    InMemoryIndexingJobStore,
    IndexingJobStore,
    PostgresIndexingJobStore,
    SQLiteIndexingJobStore,
)
from app.ingestion.loaders import (
    DocumentLoader,
    DocumentLoaderRegistry,
    MarkdownDocumentLoader,
    TextDocumentLoader,
)
from app.ingestion.metadata import DocumentMetadataNormalizer

__all__ = [
    "DocumentLoader",
    "DocumentLoaderRegistry",
    "DocumentMetadataNormalizer",
    "InMemoryIndexingJobStore",
    "IndexingJob",
    "IndexingJobStore",
    "IndexingJobStatus",
    "MarkdownDocumentLoader",
    "PostgresIndexingJobStore",
    "SQLiteIndexingJobStore",
    "TextDocumentLoader",
]
