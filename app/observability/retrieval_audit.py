"""Retrieval audit stores for traced RAG answers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RetrievalAuditRecord:
    """One persisted snapshot of a RAG answer and its selected retrieval context."""

    trace_id: str
    session_id: str
    space_id: str
    question: str
    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    retrieval: dict[str, Any] = field(default_factory=dict)
    audit_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=_utc_now_iso)


class InMemoryRetrievalAuditStore:
    """Process-local retrieval audit store for tests and lightweight development."""

    def __init__(self) -> None:
        self._records: list[RetrievalAuditRecord] = []

    def append(self, record: RetrievalAuditRecord) -> RetrievalAuditRecord:
        self._records.append(record)
        return record

    def list_records(
        self,
        *,
        session_id: str | None = None,
        space_id: str | None = None,
        trace_id: str | None = None,
        limit: int = 50,
    ) -> list[RetrievalAuditRecord]:
        if limit <= 0:
            return []

        records = _filter_records(
            reversed(self._records),
            session_id=session_id,
            space_id=space_id,
            trace_id=trace_id,
        )
        return records[:limit]


class SQLiteRetrievalAuditStore:
    """Durable retrieval audit store backed by a local SQLite file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def append(self, record: RetrievalAuditRecord) -> RetrievalAuditRecord:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO retrieval_audits (
                    audit_id,
                    trace_id,
                    session_id,
                    space_id,
                    question,
                    answer,
                    sources_json,
                    retrieval_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.audit_id,
                    record.trace_id,
                    record.session_id,
                    record.space_id,
                    record.question,
                    record.answer,
                    json.dumps(record.sources, ensure_ascii=False),
                    json.dumps(record.retrieval, ensure_ascii=False),
                    record.created_at,
                ),
            )
            connection.commit()
        return record

    def list_records(
        self,
        *,
        session_id: str | None = None,
        space_id: str | None = None,
        trace_id: str | None = None,
        limit: int = 50,
    ) -> list[RetrievalAuditRecord]:
        if limit <= 0:
            return []

        where_clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("space_id", space_id),
            ("trace_id", trace_id),
        ):
            if value:
                where_clauses.append(f"{column} = ?")
                params.append(value)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    audit_id,
                    trace_id,
                    session_id,
                    space_id,
                    question,
                    answer,
                    sources_json,
                    retrieval_json,
                    created_at
                FROM retrieval_audits
                {where_sql}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()

        return [_record_from_row(row) for row in rows]

    def close(self) -> None:
        """Compatibility hook for stores that keep open resources."""

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS retrieval_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    retrieval_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_retrieval_audits_session_id
                ON retrieval_audits(session_id, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_retrieval_audits_space_id
                ON retrieval_audits(space_id, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_retrieval_audits_trace_id
                ON retrieval_audits(trace_id)
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _filter_records(
    records: Iterable[RetrievalAuditRecord],
    *,
    session_id: str | None,
    space_id: str | None,
    trace_id: str | None,
) -> list[RetrievalAuditRecord]:
    return [
        record
        for record in records
        if (session_id is None or record.session_id == session_id)
        and (space_id is None or record.space_id == space_id)
        and (trace_id is None or record.trace_id == trace_id)
    ]


def _record_from_row(row: sqlite3.Row) -> RetrievalAuditRecord:
    return RetrievalAuditRecord(
        audit_id=row["audit_id"],
        trace_id=row["trace_id"],
        session_id=row["session_id"],
        space_id=row["space_id"],
        question=row["question"],
        answer=row["answer"],
        sources=_loads_json_list(row["sources_json"]),
        retrieval=_loads_json_dict(row["retrieval_json"]),
        created_at=row["created_at"],
    )


def _loads_json_list(value: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _loads_json_dict(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
