import sqlite3

import pytest

from app.security.department_store import (
    DepartmentNameAlreadyExistsError,
    DepartmentNotEmptyError,
    DepartmentNotFoundError,
    DepartmentStore,
)


def test_create_and_get_department_round_trips_fields(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")

    created = store.create(name="工程部", description="负责研发")

    assert store.get_by_id(created.id) == created
    assert created.name == "工程部"
    assert created.description == "负责研发"
    assert created.created_at


def test_get_by_id_returns_none_when_missing(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")

    assert store.get_by_id("missing") is None


def test_list_departments_is_sorted_by_name(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    store.create(name="运维部", description=None)
    store.create(name="工程部", description=None)

    assert [d.name for d in store.list()] == ["工程部", "运维部"]


def test_create_rejects_duplicate_name(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    store.create(name="工程部", description=None)

    with pytest.raises(DepartmentNameAlreadyExistsError):
        store.create(name="工程部", description="重复")


def test_update_changes_name_and_preserves_omitted_description(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    created = store.create(name="工程部", description="负责研发")

    updated = store.update(department_id=created.id, name="研发部")

    assert updated.name == "研发部"
    assert updated.description == "负责研发"
    assert store.get_by_id(created.id).name == "研发部"


def test_update_rejects_missing_department(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")

    with pytest.raises(DepartmentNotFoundError):
        store.update(department_id="missing", name="x")


def test_update_rejects_rename_to_existing_name(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    store.create(name="工程部", description=None)
    other = store.create(name="运维部", description=None)

    with pytest.raises(DepartmentNameAlreadyExistsError):
        store.update(department_id=other.id, name="工程部")


def test_delete_empty_department_succeeds_without_users_table(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    created = store.create(name="工程部", description=None)

    store.delete(created.id)

    assert store.get_by_id(created.id) is None


def test_delete_rejects_missing_department(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")

    with pytest.raises(DepartmentNotFoundError):
        store.delete("missing")


from app.security.user_store import UserStore


def test_delete_rejects_department_with_member_users(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    departments = DepartmentStore(db_path)
    users = UserStore(db_path)
    department = departments.create(name="工程部", description=None)
    users.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=[],
        department_ids=[department.id],
    )

    with pytest.raises(DepartmentNotEmptyError):
        departments.delete(department.id)

    assert departments.get_by_id(department.id) is not None


def test_delete_matches_department_id_exactly_not_as_substring(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    departments = DepartmentStore(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO departments (id, name, description, created_at) VALUES (?, ?, ?, ?)",
        ("5", "五部", None, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO departments (id, name, description, created_at) VALUES (?, ?, ?, ?)",
        ("15", "十五部", None, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            roles TEXT NOT NULL, spaces TEXT NOT NULL, created_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, department_ids_json TEXT NOT NULL DEFAULT '[]'
        )""")
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u1", "alice", "h1", "[]", "[]", "2026-01-01T00:00:00+00:00", 1, '["15"]'),
    )
    connection.commit()
    connection.close()

    departments.delete("5")

    assert departments.get_by_id("5") is None
    with pytest.raises(DepartmentNotEmptyError):
        departments.delete("15")
