"""Indexing job state model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class IndexingJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class IndexingJob:
    """Tracks one document indexing attempt."""

    document_id: str
    source_path: str
    space_id: str = "default"
    job_id: str = field(default_factory=lambda: uuid4().hex)
    status: IndexingJobStatus = IndexingJobStatus.PENDING
    total_chunks: int = 0
    indexed_chunks: int = 0
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        source_path: str,
        space_id: str = "default",
        job_id: str | None = None,
        created_at: datetime | None = None,
    ) -> IndexingJob:
        return cls(
            document_id=document_id,
            source_path=source_path,
            space_id=space_id,
            job_id=job_id or uuid4().hex,
            created_at=created_at or datetime.now(UTC),
        )

    def start(self, *, now: datetime | None = None) -> None:
        if self.status is not IndexingJobStatus.PENDING:
            raise ValueError(f"Cannot start indexing job from status {self.status}")

        self.status = IndexingJobStatus.RUNNING
        self.started_at = now or datetime.now(UTC)

    def complete(
        self,
        *,
        total_chunks: int,
        indexed_chunks: int,
        errors: list[str] | None = None,
        now: datetime | None = None,
    ) -> None:
        if self.status is not IndexingJobStatus.RUNNING:
            raise ValueError(f"Cannot complete indexing job from status {self.status}")
        if total_chunks < 0 or indexed_chunks < 0:
            raise ValueError("Chunk counts must be non-negative")
        if indexed_chunks > total_chunks:
            raise ValueError("indexed_chunks cannot exceed total_chunks")

        self.total_chunks = total_chunks
        self.indexed_chunks = indexed_chunks
        self.errors = list(errors or [])
        self.completed_at = now or datetime.now(UTC)

        if not self.errors and indexed_chunks == total_chunks:
            self.status = IndexingJobStatus.SUCCEEDED
        elif indexed_chunks > 0:
            self.status = IndexingJobStatus.PARTIAL
        else:
            self.status = IndexingJobStatus.FAILED
