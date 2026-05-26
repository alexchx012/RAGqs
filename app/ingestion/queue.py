"""Indexing queue abstractions for background ingestion workers."""

from __future__ import annotations

from queue import Empty, Queue
from typing import Protocol


class IndexingQueue(Protocol):
    """Queue boundary for indexing job ids."""

    @property
    def unfinished_count(self) -> int:
        """Return the number of queued or in-flight jobs."""

    def enqueue(self, job_id: str) -> bool:
        """Enqueue one job id and return whether it was accepted."""

    def dequeue(self, *, timeout_seconds: float) -> str | None:
        """Return the next job id or None when no job is available."""

    def task_done(self, job_id: str) -> None:
        """Mark one dequeued job as processed."""


class InMemoryIndexingQueue:
    """Process-local FIFO queue with waiting-job deduplication."""

    def __init__(self):
        self._queue: Queue[str] = Queue()
        self._queued_job_ids: set[str] = set()

    @property
    def unfinished_count(self) -> int:
        return self._queue.unfinished_tasks

    def enqueue(self, job_id: str) -> bool:
        if job_id in self._queued_job_ids:
            return False
        self._queued_job_ids.add(job_id)
        self._queue.put(job_id)
        return True

    def dequeue(self, *, timeout_seconds: float) -> str | None:
        try:
            job_id = self._queue.get(timeout=timeout_seconds)
        except Empty:
            return None
        self._queued_job_ids.discard(job_id)
        return job_id

    def task_done(self, job_id: str) -> None:
        self._queue.task_done()
