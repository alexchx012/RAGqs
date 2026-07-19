"""SQLite-backed department directory sharing the local auth database file."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class DepartmentRecord:
    """One department row. No version column: departments have no concurrent-write scenario."""

    id: str
    name: str
    description: str | None
    created_at: str


class DepartmentNotFoundError(Exception):
    """Raised when a requested department does not exist."""


class DepartmentNameAlreadyExistsError(Exception):
    """Raised when creating or renaming a department to an already-used name."""


class DepartmentNotEmptyError(Exception):
    """Raised when deleting a department that still has member users."""


class DepartmentStore:
    """Durable department directory backed by the same local SQLite database file as UserStore."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create(self, *, name: str, description: str | None) -> DepartmentRecord:
        department = DepartmentRecord(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            created_at=datetime.now(UTC).isoformat(),
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO departments (id, name, description, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (department.id, department.name, department.description, department.created_at),
                )
                connection.commit()
            except sqlite3.IntegrityError as e:
                raise DepartmentNameAlreadyExistsError(name) from e
        return department

    def get_by_id(self, department_id: str) -> DepartmentRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, name, description, created_at FROM departments WHERE id = ?",
                (department_id,),
            ).fetchone()
        return _department_from_row(row) if row is not None else None

    def list(self) -> list[DepartmentRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, name, description, created_at FROM departments "
                "ORDER BY name ASC, id ASC"
            ).fetchall()
        return [_department_from_row(row) for row in rows]

    def update(
        self, *, department_id: str, name: str | None = None, description: str | None = None
    ) -> DepartmentRecord:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT id, name, description, created_at FROM departments WHERE id = ?",
                    (department_id,),
                ).fetchone()
                if row is None:
                    raise DepartmentNotFoundError(department_id)
                current = _department_from_row(row)
                new_name = current.name if name is None else name
                new_description = current.description if description is None else description
                connection.execute(
                    "UPDATE departments SET name = ?, description = ? WHERE id = ?",
                    (new_name, new_description, department_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DepartmentNameAlreadyExistsError(new_name) from exc
            except Exception:
                connection.rollback()
                raise
        return DepartmentRecord(
            id=current.id,
            name=new_name,
            description=new_description,
            created_at=current.created_at,
        )

    def delete(self, department_id: str) -> None:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT id FROM departments WHERE id = ?", (department_id,)
                ).fetchone()
                if row is None:
                    raise DepartmentNotFoundError(department_id)
                users_table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'users'"
                ).fetchone()
                member_rows = (
                    []
                    if users_table is None
                    else connection.execute(
                        "SELECT department_ids_json FROM users"
                    ).fetchall()
                )
                for member_row in member_rows:
                    ids = _loads_department_ids(member_row["department_ids_json"])
                    if department_id in ids:
                        raise DepartmentNotEmptyError(department_id)
                connection.execute("DELETE FROM departments WHERE id = ?", (department_id,))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS departments (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL
                )
                """)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _department_from_row(row: sqlite3.Row) -> DepartmentRecord:
    return DepartmentRecord(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
    )


def _loads_department_ids(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []
