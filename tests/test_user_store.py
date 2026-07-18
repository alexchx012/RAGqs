import sqlite3

import pytest

from app.security.user_store import (
    LastAdminProtectionError,
    TooManyDepartmentsError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
    UserStore,
    UserVersionConflictError,
)


def test_new_user_starts_at_version_one(tmp_path):
    user = UserStore(tmp_path / "auth.sqlite3").create_user(
        username="alice", password_hash="h1", roles=["super_admin"], spaces=["*"]
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
        roles=["super_admin"],
        spaces=["*"],
    )

    assert store.get_by_username("alice") == created
    assert store.get_by_id(created.id) == created
    assert created.roles == ["super_admin"]
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

    store.create_user(username="alice", password_hash="h1", roles=["super_admin"], spaces=["*"])
    store.create_user(username="bob", password_hash="h2", roles=["viewer"], spaces=["default"])

    assert store.count_users() == 2


def test_create_user_rejects_duplicate_username(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    store.create_user(username="alice", password_hash="h1", roles=["super_admin"], spaces=["*"])

    with pytest.raises(UsernameAlreadyExistsError):
        store.create_user(
            username="alice", password_hash="h2", roles=["viewer"], spaces=["default"]
        )


def test_user_store_persists_across_instances(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    first_store = UserStore(db_path)
    first_store.create_user(username="alice", password_hash="h1", roles=["super_admin"], spaces=["*"])

    second_store = UserStore(db_path)

    assert second_store.count_users() == 1
    assert second_store.get_by_username("alice") is not None


def test_list_users_is_deterministic_and_reads_versions(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    store.create_user(username="zeta", password_hash="h1", roles=["viewer"], spaces=[])
    store.create_user(username="alice", password_hash="h2", roles=["super_admin"], spaces=["*"])

    assert [user.username for user in store.list_users()] == ["alice", "zeta"]
    assert all(user.version == 1 for user in store.list_users())


def test_update_user_increments_version_and_preserves_omitted_field(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    created = store.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=["docs"]
    )

    updated = store.update_user(user_id=created.id, expected_version=1, roles=["maintainer"])

    assert updated.roles == ["maintainer"]
    assert updated.spaces == ["docs"]
    assert updated.version == 2
    assert store.get_by_id(created.id).version == 2


def test_stale_update_rolls_back_without_overwriting_new_data(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    first = UserStore(db_path)
    created = first.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=["docs"]
    )
    second = UserStore(db_path)
    first.update_user(user_id=created.id, expected_version=1, roles=["maintainer"])

    with pytest.raises(UserVersionConflictError):
        second.update_user(user_id=created.id, expected_version=1, spaces=["private"])

    current = first.get_by_id(created.id)
    assert current.roles == ["maintainer"]
    assert current.spaces == ["docs"]
    assert current.version == 2


def test_last_admin_update_and_delete_leave_database_unchanged(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    admin = store.create_user(username="admin", password_hash="h1", roles=["super_admin"], spaces=["*"])

    with pytest.raises(LastAdminProtectionError):
        store.update_user(user_id=admin.id, expected_version=1, roles=["viewer"])
    assert store.get_by_id(admin.id).roles == ["super_admin"]
    assert store.get_by_id(admin.id).version == 1

    with pytest.raises(LastAdminProtectionError):
        store.delete_user(user_id=admin.id, expected_version=1)
    assert store.get_by_id(admin.id) is not None


def test_another_admin_allows_admin_demotion_and_regular_delete(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    first = store.create_user(username="first", password_hash="h1", roles=["super_admin"], spaces=["*"])
    second = store.create_user(username="second", password_hash="h2", roles=["super_admin"], spaces=["*"])
    ordinary = store.create_user(
        username="ordinary", password_hash="h3", roles=["viewer"], spaces=[]
    )

    demoted = store.update_user(user_id=first.id, expected_version=1, roles=["viewer"])
    assert demoted.roles == ["viewer"]
    assert store.get_by_id(first.id) == demoted
    assert store.get_by_id(second.id).roles == ["super_admin"]

    deleted = store.delete_user(user_id=ordinary.id, expected_version=1)
    assert deleted.id == ordinary.id
    assert store.get_by_id(ordinary.id) is None


def test_stale_delete_from_second_store_does_not_remove_user(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    first_store = UserStore(db_path)
    user = first_store.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=[]
    )
    second_store = UserStore(db_path)
    updated = first_store.update_user(user_id=user.id, expected_version=1, spaces=["docs"])

    with pytest.raises(UserVersionConflictError):
        second_store.delete_user(user_id=user.id, expected_version=user.version)

    assert first_store.get_by_id(user.id) == updated
    assert second_store.get_by_id(user.id) == updated
    assert first_store.count_users() == 1


def test_update_user_rejects_missing_user(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    with pytest.raises(UserNotFoundError):
        store.update_user(user_id="missing", expected_version=1, roles=["viewer"])


def test_delete_user_returns_deleted_record_and_removes_non_admin(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    created = store.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=["docs"]
    )
    count_before = store.count_users()

    deleted = store.delete_user(user_id=created.id, expected_version=created.version)

    assert deleted == created
    assert deleted.id == created.id
    assert deleted.username == created.username
    assert deleted.roles == created.roles
    assert deleted.spaces == created.spaces
    assert deleted.version == created.version
    assert store.get_by_id(created.id) is None
    assert store.count_users() == count_before - 1


def test_delete_user_rejects_missing_user(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    with pytest.raises(UserNotFoundError):
        store.delete_user(user_id="missing", expected_version=1)

    assert store.count_users() == 0


def test_update_user_empty_lists_clear_roles_and_spaces(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    created = store.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=["docs"]
    )

    updated = store.update_user(
        user_id=created.id, expected_version=created.version, roles=[], spaces=[]
    )

    assert updated.roles == []
    assert updated.spaces == []
    assert updated.version == created.version + 1
    assert store.get_by_id(created.id) == updated


def test_admin_role_string_is_migrated_to_super_admin_idempotently(tmp_path):
    db_path = tmp_path / "legacy_roles.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, roles TEXT NOT NULL,
            spaces TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
        )""")
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("u1", "root", "h1", '["admin"]', '["*"]', "2026-01-01T00:00:00+00:00", 1),
    )
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("u2", "bob", "h2", '["viewer", "admin"]', '["default"]', "2026-01-01T00:00:00+00:00", 1),
    )
    connection.commit()
    connection.close()

    first = UserStore(db_path)
    assert first.get_by_id("u1").roles == ["super_admin"]
    assert first.get_by_id("u2").roles == ["viewer", "super_admin"]

    second = UserStore(db_path)
    assert second.get_by_id("u1").roles == ["super_admin"]
    assert second.get_by_id("u2").roles == ["viewer", "super_admin"]


def test_create_user_supports_department_ids_and_derives_department_id(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    created = store.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=["docs"],
        department_ids=["dept-1"],
    )

    assert created.department_ids == ["dept-1"]
    assert created.department_id == "dept-1"
    assert store.get_by_id(created.id).department_id == "dept-1"
    assert store.get_by_username("alice").department_id == "dept-1"


def test_create_user_without_department_ids_defaults_to_empty_and_none(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    created = store.create_user(username="bob", password_hash="h1", roles=["viewer"], spaces=[])

    assert created.department_ids == []
    assert created.department_id is None


def test_create_user_rejects_more_than_one_department_id(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")

    with pytest.raises(TooManyDepartmentsError):
        store.create_user(
            username="alice", password_hash="h1", roles=["viewer"], spaces=[],
            department_ids=["dept-1", "dept-2"],
        )


def test_update_user_can_set_and_clear_department_ids(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    created = store.create_user(username="alice", password_hash="h1", roles=["viewer"], spaces=[])

    assigned = store.update_user(user_id=created.id, expected_version=1, department_ids=["dept-1"])
    assert assigned.department_id == "dept-1"

    cleared = store.update_user(user_id=created.id, expected_version=2, department_ids=[])
    assert cleared.department_id is None


def test_update_user_omitted_department_ids_preserves_current_value(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    created = store.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=[],
        department_ids=["dept-1"],
    )

    updated = store.update_user(user_id=created.id, expected_version=1, roles=["maintainer"])

    assert updated.department_id == "dept-1"


def test_update_user_rejects_more_than_one_department_id(tmp_path):
    store = UserStore(tmp_path / "auth.sqlite3")
    created = store.create_user(username="alice", password_hash="h1", roles=["viewer"], spaces=[])

    with pytest.raises(TooManyDepartmentsError):
        store.update_user(
            user_id=created.id, expected_version=1, department_ids=["dept-1", "dept-2"]
        )


def test_old_users_table_gets_department_ids_column_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy_department.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, roles TEXT NOT NULL,
            spaces TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
        )""")
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("u1", "alice", "legacy-hash", '["viewer"]', '["default"]', "2026-01-01T00:00:00+00:00", 1),
    )
    connection.commit()
    connection.close()

    first = UserStore(db_path)
    migrated = first.get_by_id("u1")
    assert migrated.department_ids == []
    assert migrated.department_id is None
    assert migrated.password_hash == "legacy-hash"

    second = UserStore(db_path)
    assert second.get_by_id("u1") == migrated
