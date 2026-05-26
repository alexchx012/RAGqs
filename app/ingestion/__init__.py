"""Ingestion foundation primitives."""

from app.ingestion.job_store import (
    IndexingJobStore,
    InMemoryIndexingJobStore,
    PostgresIndexingJobStore,
    SQLiteIndexingJobStore,
)
from app.ingestion.jobs import IndexingJob, IndexingJobStatus
from app.ingestion.loaders import (
    DocumentLoader,
    DocumentLoaderRegistry,
    MarkdownDocumentLoader,
    TextDocumentLoader,
)
from app.ingestion.metadata import DocumentMetadataNormalizer
from app.ingestion.queue import IndexingQueue, InMemoryIndexingQueue
from app.ingestion.worker import BackgroundIndexingWorker, BackgroundIndexingWorkerStats

__all__ = [
    "BackgroundIndexingWorker",
    "BackgroundIndexingWorkerStats",
    "DocumentLoader",
    "DocumentLoaderRegistry",
    "DocumentMetadataNormalizer",
    "InMemoryIndexingJobStore",
    "InMemoryIndexingQueue",
    "IndexingJob",
    "IndexingJobStore",
    "IndexingJobStatus",
    "IndexingQueue",
    "MarkdownDocumentLoader",
    "PostgresIndexingJobStore",
    "SQLiteIndexingJobStore",
    "TextDocumentLoader",
]
