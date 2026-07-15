"""Postgres-backed session store provider."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from app.providers.contracts import SessionSummary, StoredMessage

PostgresConnector = Callable[[str], Any]


class PostgresSessionStoreProvider:
    """Durable multi-instance session store backed by PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        connector: PostgresConnector | None = None,
    ):
        self.dsn = dsn
        self.connector = connector or _default_connector
        self._schema_initialized = False

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> StoredMessage:
        message = StoredMessage(role=role, content=content, metadata=dict(metadata or {}))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    """
                    INSERT INTO session_messages (
                        session_id, role, content, metadata_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        session_id,
                        message.role,
                        message.content,
                        json.dumps(message.metadata, ensure_ascii=False),
                        message.created_at,
                    ),
                )
        return message

    def get_messages(self, session_id: str) -> list[StoredMessage]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    """
                    SELECT role, content, metadata_json, created_at
                    FROM session_messages
                    WHERE session_id = %s
                    ORDER BY id ASC
                    """,
                    (session_id,),
                )
                rows = cursor.fetchall()
        return [_message_from_row(row) for row in rows]

    def list_sessions(self, query: str | None = None) -> list[SessionSummary]:
        normalized_query = query.strip().lower() if query else ""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    """
                    SELECT id, session_id, role, content, metadata_json, created_at
                    FROM session_messages
                    ORDER BY id ASC
                    """
                )
                rows = cursor.fetchall()

        grouped_messages: dict[str, list[StoredMessage]] = defaultdict(list)
        last_order: dict[str, int] = {}
        for row in rows:
            session_id = str(_row_value(row, "session_id", 1))
            grouped_messages[session_id].append(_message_from_row(row))
            last_order[session_id] = int(_row_value(row, "id", 0))

        summaries: list[SessionSummary] = []
        for session_id, messages in grouped_messages.items():
            summary = _build_session_summary(session_id, messages)
            if normalized_query and not _matches_session_query(
                normalized_query, session_id, summary, messages
            ):
                continue
            summaries.append(summary)

        return sorted(
            summaries,
            key=lambda summary: last_order.get(summary.session_id, 0),
            reverse=True,
        )

    def clear(self, session_id: str) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._ensure_schema(cursor)
                cursor.execute(
                    "DELETE FROM session_messages WHERE session_id = %s",
                    (session_id,),
                )
        return True

    def close(self) -> None:
        """Compatibility hook for providers that keep open resources."""

    def _connect(self) -> Any:
        return self.connector(self.dsn)

    def _ensure_schema(self, cursor: Any) -> None:
        if self._schema_initialized:
            return
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS session_messages (
                id BIGSERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_session_messages_session_id
            ON session_messages(session_id, id)
            """
        )
        self._schema_initialized = True


def _default_connector(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "SESSION_STORE_PROVIDER=postgres requires installing psycopg, "
            'for example: pip install "psycopg[binary]>=3.1"'
        ) from exc

    return psycopg.connect(dsn, row_factory=dict_row)


def _message_from_row(row: Any) -> StoredMessage:
    return StoredMessage(
        role=str(_row_value(row, "role", 0)),
        content=str(_row_value(row, "content", 1)),
        metadata=_loads_metadata(str(_row_value(row, "metadata_json", 2))),
        created_at=str(_row_value(row, "created_at", 3)),
    )


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row[key]
    return row[index]


def _loads_metadata(value: str) -> dict[str, object]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _build_session_summary(session_id: str, messages: list[StoredMessage]) -> SessionSummary:
    title_message = next((message for message in messages if message.role == "user"), messages[0])
    last_message = messages[-1]
    return SessionSummary(
        session_id=session_id,
        title=_truncate_text(title_message.content, max_length=80) or "New chat",
        message_count=len(messages),
        updated_at=last_message.created_at,
        last_message=_truncate_text(last_message.content, max_length=120),
    )


def _matches_session_query(
    query: str,
    session_id: str,
    summary: SessionSummary,
    messages: Iterable[StoredMessage],
) -> bool:
    del session_id, messages  # title-only match for sidebar search parity
    return query in (summary.title or "").lower()


def _truncate_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."
