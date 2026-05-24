"""Process-local knowledge-space and document registry."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from app.ingestion.jobs import IndexingJob


DEFAULT_SPACE_ID = "default"


class DocumentStatus(StrEnum):
    INDEXED = "indexed"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass
class KnowledgeSpace:
    space_id: str
    name: str
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class DocumentRecord:
    document_id: str
    space_id: str
    source_path: str
    file_name: str
    status: DocumentStatus
    latest_job_id: str = ""
    total_chunks: int = 0
    indexed_chunks: int = 0
    errors: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InMemoryKnowledgeCatalog:
    """Process-local catalog for knowledge spaces and indexed documents."""

    def __init__(self):
        self._spaces: dict[str, KnowledgeSpace] = {}
        self._documents: dict[tuple[str, str], DocumentRecord] = {}
        self.ensure_space(DEFAULT_SPACE_ID, name="Default")

    def ensure_space(
        self,
        space_id: str,
        *,
        name: str | None = None,
        description: str = "",
    ) -> KnowledgeSpace:
        normalized_space_id = _normalize_space_id(space_id)
        if normalized_space_id not in self._spaces:
            self._spaces[normalized_space_id] = KnowledgeSpace(
                space_id=normalized_space_id,
                name=name or normalized_space_id,
                description=description,
            )
        return self._spaces[normalized_space_id]

    def list_spaces(self) -> list[KnowledgeSpace]:
        return list(self._spaces.values())

    def upsert_from_job(self, job: IndexingJob) -> DocumentRecord:
        self.ensure_space(job.space_id)
        status = DocumentStatus.INDEXED if not job.errors else DocumentStatus.FAILED
        record = DocumentRecord(
            document_id=job.document_id,
            space_id=job.space_id,
            source_path=job.source_path,
            file_name=Path(job.source_path).name,
            status=status,
            latest_job_id=job.job_id,
            total_chunks=job.total_chunks,
            indexed_chunks=job.indexed_chunks,
            errors=list(job.errors),
            updated_at=datetime.now(UTC),
        )
        self._documents[(job.space_id, job.document_id)] = record
        return record

    def list_documents(self, space_id: str = DEFAULT_SPACE_ID) -> list[DocumentRecord]:
        normalized_space_id = _normalize_space_id(space_id)
        return [
            record
            for (record_space_id, _), record in self._documents.items()
            if record_space_id == normalized_space_id
        ]

    def get_document(self, space_id: str, document_id: str) -> DocumentRecord | None:
        return self._documents.get((_normalize_space_id(space_id), document_id))

    def mark_deleted(self, space_id: str, document_id: str) -> DocumentRecord:
        record = self.get_document(space_id, document_id)
        if record is None:
            raise ValueError(f"document not found: {space_id}/{document_id}")
        record.status = DocumentStatus.DELETED
        record.updated_at = datetime.now(UTC)
        return record


class SQLiteKnowledgeCatalog:
    """SQLite-backed catalog for local durable knowledge spaces and documents."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()
        self.ensure_space(DEFAULT_SPACE_ID, name="Default")

    def ensure_space(
        self,
        space_id: str,
        *,
        name: str | None = None,
        description: str = "",
    ) -> KnowledgeSpace:
        normalized_space_id = _normalize_space_id(space_id)
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM knowledge_spaces WHERE space_id = ?",
                (normalized_space_id,),
            ).fetchone()
            if existing:
                return _space_from_row(existing)

            space = KnowledgeSpace(
                space_id=normalized_space_id,
                name=name or normalized_space_id,
                description=description,
            )
            connection.execute(
                """
                INSERT INTO knowledge_spaces (
                    space_id, name, description, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    space.space_id,
                    space.name,
                    space.description,
                    space.created_at.isoformat(),
                ),
            )
            connection.commit()
            return space

    def list_spaces(self) -> list[KnowledgeSpace]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_spaces ORDER BY sequence ASC"
            ).fetchall()
        return [_space_from_row(row) for row in rows]

    def upsert_from_job(self, job: IndexingJob) -> DocumentRecord:
        self.ensure_space(job.space_id)
        status = DocumentStatus.INDEXED if not job.errors else DocumentStatus.FAILED
        record = DocumentRecord(
            document_id=job.document_id,
            space_id=_normalize_space_id(job.space_id),
            source_path=job.source_path,
            file_name=Path(job.source_path).name,
            status=status,
            latest_job_id=job.job_id,
            total_chunks=job.total_chunks,
            indexed_chunks=job.indexed_chunks,
            errors=list(job.errors),
            updated_at=datetime.now(UTC),
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO document_records (
                    document_id, space_id, source_path, file_name, status,
                    latest_job_id, total_chunks, indexed_chunks, errors_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(space_id, document_id) DO UPDATE SET
                    source_path = excluded.source_path,
                    file_name = excluded.file_name,
                    status = excluded.status,
                    latest_job_id = excluded.latest_job_id,
                    total_chunks = excluded.total_chunks,
                    indexed_chunks = excluded.indexed_chunks,
                    errors_json = excluded.errors_json,
                    updated_at = excluded.updated_at
                """,
                _document_record_values(record),
            )
            connection.commit()
        return record

    def list_documents(self, space_id: str = DEFAULT_SPACE_ID) -> list[DocumentRecord]:
        normalized_space_id = _normalize_space_id(space_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM document_records
                WHERE space_id = ?
                ORDER BY sequence ASC
                """,
                (normalized_space_id,),
            ).fetchall()
        return [_document_from_row(row) for row in rows]

    def get_document(self, space_id: str, document_id: str) -> DocumentRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM document_records
                WHERE space_id = ? AND document_id = ?
                """,
                (_normalize_space_id(space_id), document_id),
            ).fetchone()
        return _document_from_row(row) if row else None

    def mark_deleted(self, space_id: str, document_id: str) -> DocumentRecord:
        record = self.get_document(space_id, document_id)
        if record is None:
            raise ValueError(f"document not found: {space_id}/{document_id}")
        record.status = DocumentStatus.DELETED
        record.updated_at = datetime.now(UTC)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE document_records
                SET status = ?, updated_at = ?
                WHERE space_id = ? AND document_id = ?
                """,
                (
                    record.status.value,
                    record.updated_at.isoformat(),
                    record.space_id,
                    record.document_id,
                ),
            )
            connection.commit()
        return record

    def close(self) -> None:
        """Compatibility hook for catalogs that keep open resources."""

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_spaces (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    space_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS document_records (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latest_job_id TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    indexed_chunks INTEGER NOT NULL,
                    errors_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(space_id, document_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_document_records_space
                ON document_records(space_id, document_id)
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


PostgresConnector = Callable[[str], Any]


class PostgresKnowledgeCatalog:
    """PostgreSQL-backed catalog for multi-instance knowledge spaces and documents."""

    def __init__(
        self,
        dsn: str,
        *,
        connector: PostgresConnector | None = None,
    ):
        self.dsn = dsn
        self.connector = connector or _default_postgres_connector
        self._schema_initialized = False

    def ensure_space(
        self,
        space_id: str,
        *,
        name: str | None = None,
        description: str = "",
    ) -> KnowledgeSpace:
        normalized_space_id = _normalize_space_id(space_id)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._prepare(cursor)
                return self._ensure_space(
                    cursor,
                    normalized_space_id,
                    name=name,
                    description=description,
                )

    def list_spaces(self) -> list[KnowledgeSpace]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._prepare(cursor)
                cursor.execute("SELECT * FROM knowledge_spaces ORDER BY sequence ASC")
                rows = cursor.fetchall()
        return [_space_from_row(row) for row in rows]

    def upsert_from_job(self, job: IndexingJob) -> DocumentRecord:
        record = DocumentRecord(
            document_id=job.document_id,
            space_id=_normalize_space_id(job.space_id),
            source_path=job.source_path,
            file_name=Path(job.source_path).name,
            status=DocumentStatus.INDEXED if not job.errors else DocumentStatus.FAILED,
            latest_job_id=job.job_id,
            total_chunks=job.total_chunks,
            indexed_chunks=job.indexed_chunks,
            errors=list(job.errors),
            updated_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._prepare(cursor)
                self._ensure_space(cursor, record.space_id)
                cursor.execute(
                    """
                    INSERT INTO document_records (
                        document_id, space_id, source_path, file_name, status,
                        latest_job_id, total_chunks, indexed_chunks, errors_json, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(space_id, document_id) DO UPDATE SET
                        source_path = excluded.source_path,
                        file_name = excluded.file_name,
                        status = excluded.status,
                        latest_job_id = excluded.latest_job_id,
                        total_chunks = excluded.total_chunks,
                        indexed_chunks = excluded.indexed_chunks,
                        errors_json = excluded.errors_json,
                        updated_at = excluded.updated_at
                    """,
                    _document_record_values(record),
                )
        return record

    def list_documents(self, space_id: str = DEFAULT_SPACE_ID) -> list[DocumentRecord]:
        normalized_space_id = _normalize_space_id(space_id)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._prepare(cursor)
                cursor.execute(
                    """
                    SELECT * FROM document_records
                    WHERE space_id = %s
                    ORDER BY sequence ASC
                    """,
                    (normalized_space_id,),
                )
                rows = cursor.fetchall()
        return [_document_from_row(row) for row in rows]

    def get_document(self, space_id: str, document_id: str) -> DocumentRecord | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._prepare(cursor)
                cursor.execute(
                    """
                    SELECT * FROM document_records
                    WHERE space_id = %s AND document_id = %s
                    """,
                    (_normalize_space_id(space_id), document_id),
                )
                row = cursor.fetchone()
        return _document_from_row(row) if row else None

    def mark_deleted(self, space_id: str, document_id: str) -> DocumentRecord:
        record = self.get_document(space_id, document_id)
        if record is None:
            raise ValueError(f"document not found: {space_id}/{document_id}")
        record.status = DocumentStatus.DELETED
        record.updated_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._prepare(cursor)
                cursor.execute(
                    """
                    UPDATE document_records
                    SET status = %s, updated_at = %s
                    WHERE space_id = %s AND document_id = %s
                    """,
                    (
                        record.status.value,
                        record.updated_at.isoformat(),
                        record.space_id,
                        record.document_id,
                    ),
                )
        return record

    def close(self) -> None:
        """Compatibility hook for catalogs that keep open resources."""

    def _connect(self) -> Any:
        return self.connector(self.dsn)

    def _prepare(self, cursor: Any) -> None:
        self._ensure_schema(cursor)
        self._ensure_space(cursor, DEFAULT_SPACE_ID, name="Default")

    def _ensure_schema(self, cursor: Any) -> None:
        if self._schema_initialized:
            return
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_spaces (
                sequence BIGSERIAL PRIMARY KEY,
                space_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS document_records (
                sequence BIGSERIAL PRIMARY KEY,
                document_id TEXT NOT NULL,
                space_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                status TEXT NOT NULL,
                latest_job_id TEXT NOT NULL,
                total_chunks INTEGER NOT NULL,
                indexed_chunks INTEGER NOT NULL,
                errors_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(space_id, document_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_document_records_space
            ON document_records(space_id, document_id)
            """
        )
        self._schema_initialized = True

    def _ensure_space(
        self,
        cursor: Any,
        space_id: str,
        *,
        name: str | None = None,
        description: str = "",
    ) -> KnowledgeSpace:
        normalized_space_id = _normalize_space_id(space_id)
        cursor.execute(
            "SELECT * FROM knowledge_spaces WHERE space_id = %s",
            (normalized_space_id,),
        )
        existing = cursor.fetchone()
        if existing:
            return _space_from_row(existing)

        space = KnowledgeSpace(
            space_id=normalized_space_id,
            name=name or normalized_space_id,
            description=description,
        )
        cursor.execute(
            """
            INSERT INTO knowledge_spaces (
                space_id, name, description, created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                space.space_id,
                space.name,
                space.description,
                space.created_at.isoformat(),
            ),
        )
        return space


def _default_postgres_connector(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "DOCUMENT_CATALOG_PROVIDER=postgres requires installing psycopg, "
            'for example: pip install "psycopg[binary]>=3.1"'
        ) from exc

    return psycopg.connect(dsn, row_factory=dict_row)


def _normalize_space_id(space_id: str) -> str:
    normalized = (space_id or DEFAULT_SPACE_ID).strip()
    return normalized or DEFAULT_SPACE_ID


def _space_from_row(row: Any) -> KnowledgeSpace:
    return KnowledgeSpace(
        space_id=row["space_id"],
        name=row["name"],
        description=row["description"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _document_from_row(row: Any) -> DocumentRecord:
    return DocumentRecord(
        document_id=row["document_id"],
        space_id=row["space_id"],
        source_path=row["source_path"],
        file_name=row["file_name"],
        status=DocumentStatus(row["status"]),
        latest_job_id=row["latest_job_id"],
        total_chunks=row["total_chunks"],
        indexed_chunks=row["indexed_chunks"],
        errors=_loads_errors(row["errors_json"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _document_record_values(record: DocumentRecord) -> tuple:
    return (
        record.document_id,
        record.space_id,
        record.source_path,
        record.file_name,
        record.status.value,
        record.latest_job_id,
        record.total_chunks,
        record.indexed_chunks,
        json.dumps(record.errors, ensure_ascii=False),
        record.updated_at.isoformat(),
    )


def _loads_errors(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]
