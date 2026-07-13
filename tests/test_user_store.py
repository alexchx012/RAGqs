import sqlite3

import pytest

from app.security.user_store import UsernameAlreadyExistsError, UserStore


def test_new_user_starts_at_version_one(tmp_path):
    user = UserStore(tmp_path / "auth.sqlite3").create_user(
        username="alice", password_hash="h1", roles=["admin"], spaces=["*"]
    )
    assert user.version == 1


def test_old_users_table_gets_version_column_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, roles TEXT NOT NULL,
            spaces TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
        (
            "u1",
            "alice",
            "legacy-hash",
            '["viewer"]',
            '["default"]',
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    first = UserStore(db_path)
    migrated = first.get_by_id("u1")
    assert migrated is not None
    assert migrated.version == 1
    assert migrated.password_hash == "legacy-hash"
    assert migrated.roles == ["viewer"]
    assert migrated.spaces == ["default"]

    second = UserStore(db_path)
    assert second.get_by_id("u1") == migrated


def test_create_and_get_user_round_trips_all_fields(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    created = store.create_user(
        username="alice",
        password_hash="hashed-value",
        roles=["admin"],
        spaces=["*"],
    )

    assert store.get_by_username("alice") == created
    assert store.get_by_id(created.id) == created
    assert created.roles == ["admin"]
    assert created.spaces == ["*"]
    assert created.created_at


def test_get_by_username_returns_none_when_missing(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    assert store.get_by_username("missing") is None


def test_get_by_id_returns_none_when_missing(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    assert store.get_by_id("missing-id") is None


def test_count_users_reflects_number_of_rows(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    assert store.count_users() == 0

    store.create_user(username="alice", password_hash="h1", roles=["admin"], spaces=["*"])
    store.create_user(username="bob", password_hash="h2", roles=["viewer"], spaces=["default"])

    assert store.count_users() == 2


def test_create_user_rejects_duplicate_username(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    store.create_user(username="alice", password_hash="h1", roles=["admin"], spaces=["*"])

    with pytest.raises(UsernameAlreadyExistsError):
        store.create_user(
            username="alice", password_hash="h2", roles=["viewer"], spaces=["default"]
        )


def test_user_store_persists_across_instances(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    first_store = UserStore(db_path)
    first_store.create_user(username="alice", password_hash="h1", roles=["admin"], spaces=["*"])

    second_store = UserStore(db_path)

    assert second_store.count_users() == 1
    assert second_store.get_by_username("alice") is not None
