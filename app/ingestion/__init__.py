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
from app.ingestion.worker import BackgroundIndexingWorker, BackgroundIndexingWorkerStats

__all__ = [
    "BackgroundIndexingWorker",
    "BackgroundIndexingWorkerStats",
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
