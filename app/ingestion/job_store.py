"""Indexing job storage interfaces and in-memory implementation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from app.ingestion.jobs import IndexingJob, IndexingJobStatus


class IndexingJobStore(Protocol):
    """Stores indexing jobs for status lookup and retry workflows."""

    def save(self, job: IndexingJob) -> IndexingJob:
        """Persist one job."""

    def get(self, job_id: str) -> IndexingJob | None:
        """Return one job by id."""

    def list(
        self,
        *,
        document_id: str | None = None,
        source_path: str | None = None,
        status: IndexingJobStatus | str | None = None,
    ) -> list[IndexingJob]:
        """Return jobs matching optional filters."""


@dataclass
class InMemoryIndexingJobStore:
    """Process-local job store for development and tests."""

    jobs: dict[str, IndexingJob] = field(default_factory=dict)

    def save(self, job: IndexingJob) -> IndexingJob:
        self.jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> IndexingJob | None:
        return self.jobs.get(job_id)

    def list(
        self,
        *,
        document_id: str | None = None,
        source_path: str | None = None,
        status: IndexingJobStatus | str | None = None,
    ) -> list[IndexingJob]:
        status_value = status.value if isinstance(status, IndexingJobStatus) else status
        result: list[IndexingJob] = []
        for job in self.jobs.values():
            if document_id is not None and job.document_id != document_id:
                continue
            if source_path is not None and job.source_path != source_path:
                continue
            if status_value is not None and job.status.value != status_value:
                continue
            result.append(job)
        return result


class SQLiteIndexingJobStore:
    """SQLite-backed job store for local durable indexing status."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def save(self, job: IndexingJob) -> IndexingJob:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO indexing_jobs (
                    job_id, document_id, source_path, space_id, status,
                    total_chunks, indexed_chunks, errors_json,
                    created_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    document_id = excluded.document_id,
                    source_path = excluded.source_path,
                    space_id = excluded.space_id,
                    status = excluded.status,
                    total_chunks = excluded.total_chunks,
                    indexed_chunks = excluded.indexed_chunks,
                    errors_json = excluded.errors_json,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at
                """,
                (
                    job.job_id,
                    job.document_id,
                    job.source_path,
                    job.space_id,
                    job.status.value,
                    job.total_chunks,
                    job.indexed_chunks,
                    json.dumps(job.errors, ensure_ascii=False),
                    job.created_at.isoformat(),
                    job.started_at.isoformat() if job.started_at else None,
                    job.completed_at.isoformat() if job.completed_at else None,
                ),
            )
            connection.commit()
        return job

    def get(self, job_id: str) -> IndexingJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM indexing_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_from_row(row) if row else None

    def list(
        self,
        *,
        document_id: str | None = None,
        source_path: str | None = None,
        status: IndexingJobStatus | str | None = None,
    ) -> list[IndexingJob]:
        clauses: list[str] = []
        values: list[str] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            values.append(document_id)
        if source_path is not None:
            clauses.append("source_path = ?")
            values.append(source_path)
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value if isinstance(status, IndexingJobStatus) else status)

        query = "SELECT * FROM indexing_jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence ASC"

        with closing(self._connect()) as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [_job_from_row(row) for row in rows]

    def close(self) -> None:
        """Compatibility hook for providers that keep open resources."""

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS indexing_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    indexed_chunks INTEGER NOT NULL,
                    errors_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_indexing_jobs_filters
                ON indexing_jobs(document_id, source_path, status)
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


PostgresConnector = Callable[[str], Any]


class PostgresIndexingJobStore:
    """PostgreSQL-backed job store for multi-instance indexing status."""

    def __init__(
        self,
        dsn: str,
        *,
        connector: PostgresConnector | None = None,
    ):
        self.dsn = dsn
        self.connector = connector or _default_postgres_connector
        self._schema_initialized = False

    def save(self, job: IndexingJob) -> IndexingJob:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    """
                    INSERT INTO indexing_jobs (
                        job_id, document_id, source_path, space_id, status,
                        total_chunks, indexed_chunks, errors_json,
                        created_at, started_at, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(job_id) DO UPDATE SET
                        document_id = excluded.document_id,
                        source_path = excluded.source_path,
                        space_id = excluded.space_id,
                        status = excluded.status,
                        total_chunks = excluded.total_chunks,
                        indexed_chunks = excluded.indexed_chunks,
                        errors_json = excluded.errors_json,
                        created_at = excluded.created_at,
                        started_at = excluded.started_at,
                        completed_at = excluded.completed_at
                    """,
                    _job_params(job),
                )
        return job

    def get(self, job_id: str) -> IndexingJob | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    "SELECT * FROM indexing_jobs WHERE job_id = %s",
                    (job_id,),
                )
                row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def list(
        self,
        *,
        document_id: str | None = None,
        source_path: str | None = None,
        status: IndexingJobStatus | str | None = None,
    ) -> list[IndexingJob]:
        clauses: list[str] = []
        values: list[str] = []
        if document_id is not None:
            clauses.append("document_id = %s")
            values.append(document_id)
        if source_path is not None:
            clauses.append("source_path = %s")
            values.append(source_path)
        if status is not None:
            clauses.append("status = %s")
            values.append(status.value if isinstance(status, IndexingJobStatus) else status)

        query = "SELECT * FROM indexing_jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence ASC"

        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(query, tuple(values))
                rows = cursor.fetchall()
        return [_job_from_row(row) for row in rows]

    def close(self) -> None:
        """Compatibility hook for providers that keep open resources."""

    def _connect(self) -> Any:
        return self.connector(self.dsn)

    def _ensure_schema(self, cursor: Any) -> None:
        if self._schema_initialized:
            return
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS indexing_jobs (
                sequence BIGSERIAL PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                space_id TEXT NOT NULL,
                status TEXT NOT NULL,
                total_chunks INTEGER NOT NULL,
                indexed_chunks INTEGER NOT NULL,
                errors_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_indexing_jobs_filters
            ON indexing_jobs(document_id, source_path, status)
            """
        )
        self._schema_initialized = True


def _default_postgres_connector(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "INDEXING_JOB_STORE_PROVIDER=postgres requires installing psycopg, "
            'for example: pip install "psycopg[binary]>=3.1"'
        ) from exc

    return psycopg.connect(dsn, row_factory=dict_row)


def _job_params(job: IndexingJob) -> tuple:
    return (
        job.job_id,
        job.document_id,
        job.source_path,
        job.space_id,
        job.status.value,
        job.total_chunks,
        job.indexed_chunks,
        json.dumps(job.errors, ensure_ascii=False),
        job.created_at.isoformat(),
        job.started_at.isoformat() if job.started_at else None,
        job.completed_at.isoformat() if job.completed_at else None,
    )


def _job_from_row(row: Any) -> IndexingJob:
    return IndexingJob(
        document_id=_row_value(row, "document_id"),
        source_path=_row_value(row, "source_path"),
        space_id=_row_value(row, "space_id"),
        job_id=_row_value(row, "job_id"),
        status=IndexingJobStatus(_row_value(row, "status")),
        total_chunks=_row_value(row, "total_chunks"),
        indexed_chunks=_row_value(row, "indexed_chunks"),
        errors=_loads_errors(_row_value(row, "errors_json")),
        created_at=_parse_datetime(_row_value(row, "created_at")),
        started_at=_parse_optional_datetime(_row_value(row, "started_at")),
        completed_at=_parse_optional_datetime(_row_value(row, "completed_at")),
    )


def _row_value(row: Any, key: str) -> Any:
    return row[key]


def _loads_errors(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _parse_optional_datetime(value: str | None):
    return _parse_datetime(value) if value else None


def _parse_datetime(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
