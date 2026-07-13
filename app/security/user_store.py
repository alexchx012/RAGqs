"""SQLite-backed local user credential store for local_credentials auth."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class UserRecord:
    """One local user account row. Not an AuthContext — callers convert as needed."""

    id: str
    username: str
    password_hash: str
    roles: list[str] = field(default_factory=list)
    spaces: list[str] = field(default_factory=list)
    created_at: str = ""


class UsernameAlreadyExistsError(Exception):
    """Raised when creating a user with a username that already exists."""


class UserStore:
    """Durable user credential store backed by a local SQLite database file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        roles: list[str],
        spaces: list[str],
    ) -> UserRecord:
        user = UserRecord(
            id=str(uuid.uuid4()),
            username=username,
            password_hash=password_hash,
            roles=list(roles),
            spaces=list(spaces),
            created_at=datetime.now(UTC).isoformat(),
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (id, username, password_hash, roles, spaces, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.username,
                        user.password_hash,
                        json.dumps(user.roles, ensure_ascii=False),
                        json.dumps(user.spaces, ensure_ascii=False),
                        user.created_at,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as e:
                raise UsernameAlreadyExistsError(username) from e
        return user

    def get_by_username(self, username: str) -> UserRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def get_by_id(self, user_id: str) -> UserRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def count_users(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"]) if row is not None else 0

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    roles TEXT NOT NULL,
                    spaces TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _user_from_row(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        roles=_loads_list(row["roles"]),
        spaces=_loads_list(row["spaces"]),
        created_at=row["created_at"],
    )


def _loads_list(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []
