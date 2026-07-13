"""SQLite-backed session token store for local_credentials auth."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class SessionRecord:
    """One session token row."""

    token: str
    user_id: str
    created_at: str
    expires_at: str
    revoked: bool


class SessionStore:
    """Durable session token store backed by a local SQLite database file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create_session(self, user_id: str, ttl_seconds: int) -> SessionRecord:
        now = datetime.now(UTC)
        session = SessionRecord(
            token=secrets.token_urlsafe(32),
            user_id=user_id,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
            revoked=False,
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (token, user_id, created_at, expires_at, revoked)
                VALUES (?, ?, ?, ?, 0)
                """,
                (session.token, session.user_id, session.created_at, session.expires_at),
            )
            connection.commit()
        return session

    def get_valid_session(self, token: str) -> SessionRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM auth_sessions WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        session = _session_from_row(row)
        if session.revoked:
            return None
        if datetime.fromisoformat(session.expires_at) <= datetime.now(UTC):
            return None
        return session

    def revoke_session(self, token: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked = 1 WHERE token = ?",
                (token,),
            )
            connection.commit()

    def revoke_all_for_user(self, user_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked = 1 WHERE user_id = ?",
                (user_id,),
            )
            connection.commit()

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        token=row["token"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked=bool(row["revoked"]),
    )
