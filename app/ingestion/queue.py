"""Indexing queue abstractions for background ingestion workers."""

from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Queue
from time import monotonic, sleep
from typing import Any, Protocol


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


PostgresConnector = Callable[[str], Any]


class PostgresIndexingQueue:
    """PostgreSQL-backed queue for multi-instance background indexing workers."""

    def __init__(
        self,
        dsn: str,
        *,
        connector: PostgresConnector | None = None,
        lease_timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 0.05,
    ):
        self.dsn = dsn
        self.connector = connector or _default_postgres_connector
        self.lease_timeout_seconds = max(1.0, float(lease_timeout_seconds))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._schema_initialized = False

    @property
    def unfinished_count(self) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    """
                    SELECT COUNT(*) AS unfinished_count
                    FROM indexing_queue_jobs
                    WHERE status IN ('pending', 'running')
                    """
                )
                row = cursor.fetchone()
        return int(_row_value(row, "unfinished_count", 0) or 0)

    def enqueue(self, job_id: str) -> bool:
        normalized_job_id = str(job_id).strip()
        if not normalized_job_id:
            return False

        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    """
                    INSERT INTO indexing_queue_jobs (job_id, status, enqueued_at)
                    VALUES (%s, 'pending', NOW())
                    ON CONFLICT(job_id) DO NOTHING
                    RETURNING job_id
                    """,
                    (normalized_job_id,),
                )
                row = cursor.fetchone()
        return bool(row)

    def dequeue(self, *, timeout_seconds: float) -> str | None:
        deadline = monotonic() + max(0.0, timeout_seconds)
        while True:
            job_id = self._claim_next_job()
            if job_id is not None:
                return job_id
            if monotonic() >= deadline:
                return None
            sleep(min(self.poll_interval_seconds, max(0.0, deadline - monotonic())))

    def task_done(self, job_id: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    "DELETE FROM indexing_queue_jobs WHERE job_id = %s",
                    (job_id,),
                )

    def close(self) -> None:
        """Compatibility hook for providers that keep open resources."""

    def _claim_next_job(self) -> str | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                self._reclaim_expired_leases(cursor)
                cursor.execute(
                    """
                    WITH next_job AS (
                        SELECT id
                        FROM indexing_queue_jobs
                        WHERE status = 'pending'
                        ORDER BY id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE indexing_queue_jobs
                    SET status = 'running',
                        claimed_at = NOW()
                    FROM next_job
                    WHERE indexing_queue_jobs.id = next_job.id
                    RETURNING indexing_queue_jobs.job_id
                    """
                )
                row = cursor.fetchone()
        return _row_value(row, "job_id") if row else None

    def _reclaim_expired_leases(self, cursor: Any) -> None:
        cursor.execute(
            """
            UPDATE indexing_queue_jobs
            SET status = 'pending',
                claimed_at = NULL
            WHERE status = 'running'
              AND claimed_at < (NOW() - (%s * INTERVAL '1 second'))
            """,
            (self.lease_timeout_seconds,),
        )

    def _connect(self) -> Any:
        return self.connector(self.dsn)

    def _ensure_schema(self, cursor: Any) -> None:
        if self._schema_initialized:
            return
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS indexing_queue_jobs (
                id BIGSERIAL PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                claimed_at TIMESTAMPTZ
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_indexing_queue_jobs_status_id
            ON indexing_queue_jobs(status, id)
            """
        )
        self._schema_initialized = True


def _default_postgres_connector(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "INDEXING_QUEUE_PROVIDER=postgres requires installing psycopg, "
            'for example: uv pip install -e ".[postgres]"'
        ) from exc

    return psycopg.connect(dsn, row_factory=dict_row)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return getattr(row, key, default)
