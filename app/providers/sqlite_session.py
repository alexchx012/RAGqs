"""SQLite-backed session store provider."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from app.providers.contracts import SessionSummary, StoredMessage


class SQLiteSessionStoreProvider:
    """Durable session store backed by a local SQLite database file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> StoredMessage:
        message = StoredMessage(role=role, content=content, metadata=dict(metadata or {}))
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO session_messages (
                    session_id, role, content, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message.role,
                    message.content,
                    json.dumps(message.metadata, ensure_ascii=False),
                    message.created_at,
                ),
            )
            connection.commit()
        return message

    def get_messages(self, session_id: str) -> list[StoredMessage]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT role, content, metadata_json, created_at
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()

        return [_message_from_row(row) for row in rows]

    def list_sessions(self, query: str | None = None) -> list[SessionSummary]:
        normalized_query = query.strip().lower() if query else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, role, content, metadata_json, created_at
                FROM session_messages
                ORDER BY id ASC
                """
            ).fetchall()

        grouped_messages: dict[str, list[StoredMessage]] = defaultdict(list)
        last_order: dict[str, int] = {}
        for row in rows:
            session_id = row["session_id"]
            grouped_messages[session_id].append(_message_from_row(row))
            last_order[session_id] = row["id"]

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
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM session_messages WHERE session_id = ?",
                (session_id,),
            )
            connection.commit()
        return True

    def close(self) -> None:
        """Compatibility hook for providers that keep open resources."""

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_id
                ON session_messages(session_id, id)
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _message_from_row(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        role=row["role"],
        content=row["content"],
        metadata=_loads_metadata(row["metadata_json"]),
        created_at=row["created_at"],
    )


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
    haystacks = [session_id, summary.title, summary.last_message]
    haystacks.extend(message.content for message in messages)
    return any(query in haystack.lower() for haystack in haystacks)


def _truncate_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."
