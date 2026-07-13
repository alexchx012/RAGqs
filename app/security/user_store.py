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
    version: int = 1


class UsernameAlreadyExistsError(Exception):
    """Raised when creating a user with a username that already exists."""


class UserNotFoundError(Exception):
    """Raised when a requested user does not exist."""


class UserVersionConflictError(Exception):
    """Raised when a user changed after the caller read its version."""


class LastAdminProtectionError(Exception):
    """Raised when a mutation would remove the last administrator."""


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
            version=1,
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, password_hash, roles, spaces, created_at, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.username,
                        user.password_hash,
                        json.dumps(user.roles, ensure_ascii=False),
                        json.dumps(user.spaces, ensure_ascii=False),
                        user.created_at,
                        user.version,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as e:
                raise UsernameAlreadyExistsError(username) from e
        return user

    def get_by_username(self, username: str) -> UserRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, username, password_hash, roles, spaces, created_at, version
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def get_by_id(self, user_id: str) -> UserRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, username, password_hash, roles, spaces, created_at, version
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def list_users(self) -> list[UserRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT id, username, password_hash, roles, spaces, created_at, version
                FROM users
                ORDER BY username ASC, id ASC
                """).fetchall()
        return [_user_from_row(row) for row in rows]

    def update_user(
        self,
        *,
        user_id: str,
        expected_version: int,
        roles: list[str] | None = None,
        spaces: list[str] | None = None,
    ) -> UserRecord:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT id, username, password_hash, roles, spaces, created_at, version
                    FROM users
                    WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise UserNotFoundError(user_id)

                current = _user_from_row(row)
                if current.version != expected_version:
                    raise UserVersionConflictError(user_id)

                new_roles = list(current.roles) if roles is None else list(roles)
                new_spaces = list(current.spaces) if spaces is None else list(spaces)
                admin_count = self._count_admins(connection)
                if "admin" in current.roles and "admin" not in new_roles and admin_count == 1:
                    raise LastAdminProtectionError(user_id)

                cursor = connection.execute(
                    """
                    UPDATE users
                    SET roles = ?, spaces = ?, version = version + 1
                    WHERE id = ? AND version = ?
                    """,
                    (
                        json.dumps(new_roles, ensure_ascii=False),
                        json.dumps(new_spaces, ensure_ascii=False),
                        user_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise UserVersionConflictError(user_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return UserRecord(
            id=current.id,
            username=current.username,
            password_hash=current.password_hash,
            roles=new_roles,
            spaces=new_spaces,
            created_at=current.created_at,
            version=current.version + 1,
        )

    def delete_user(self, *, user_id: str, expected_version: int) -> UserRecord:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT id, username, password_hash, roles, spaces, created_at, version
                    FROM users
                    WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise UserNotFoundError(user_id)

                current = _user_from_row(row)
                if current.version != expected_version:
                    raise UserVersionConflictError(user_id)

                admin_count = self._count_admins(connection)
                if "admin" in current.roles and admin_count == 1:
                    raise LastAdminProtectionError(user_id)

                cursor = connection.execute(
                    "DELETE FROM users WHERE id = ? AND version = ?",
                    (user_id, expected_version),
                )
                if cursor.rowcount != 1:
                    raise UserVersionConflictError(user_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return current

    def count_users(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"]) if row is not None else 0

    @staticmethod
    def _count_admins(connection: sqlite3.Connection) -> int:
        rows = connection.execute("SELECT roles FROM users").fetchall()
        return sum("admin" in _loads_list(row["roles"]) for row in rows)

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    roles TEXT NOT NULL,
                    spaces TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                )
                """)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "version" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
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
        version=int(row["version"]),
    )


def _loads_list(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []
