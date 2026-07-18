import pytest

from app.security.auth import AuthContext
from app.security.password import hash_password, verify_password
from app.security.session_store import SessionStore
from app.security.user_store import UserStore
from app.services.admin_user_service import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    AdminUserScopeError,
    AdminUserService,
    AdminUserValidationError,
    AdminUserVersionConflictError,
    LastAdminError,
)


def _build_service(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    users = UserStore(db_path)
    sessions = SessionStore(db_path)
    return AdminUserService(user_store=users, session_store=sessions), users, sessions


def test_create_hashes_password_and_returns_only_safe_fields(tmp_path):
    service, users, _ = _build_service(tmp_path)

    result = service.create_user(
        username="alice", password="secret", roles=["viewer"], spaces=["docs"]
    )

    assert set(result) == {"id", "username", "roles", "spaces", "department_id", "version", "created_at"}
    assert "password_hash" not in result
    stored = users.get_by_username("alice")
    assert stored is not None
    assert stored.password_hash != "secret"
    assert verify_password("secret", stored.password_hash)
    assert result["version"] == 1


def test_service_rejects_unknown_role_and_blank_username(tmp_path):
    service, _, _ = _build_service(tmp_path)

    with pytest.raises(AdminUserValidationError):
        service.create_user(username="alice", password="secret", roles=["root"], spaces=[])
    with pytest.raises(AdminUserValidationError):
        service.create_user(username="  ", password="secret", roles=[], spaces=[])


def test_duplicate_username_is_translated_to_service_error(tmp_path):
    service, _, _ = _build_service(tmp_path)
    service.create_user(username="alice", password="one", roles=[], spaces=[])

    with pytest.raises(AdminUserAlreadyExistsError):
        service.create_user(username="alice", password="two", roles=[], spaces=[])


def test_create_normalizes_values_and_preserves_password_whitespace(tmp_path):
    service, users, _ = _build_service(tmp_path)

    result = service.create_user(
        username="  alice  ",
        password=" secret ",
        roles=["VIEWER", "viewer", "SUPER_ADMIN"],
        spaces=[" docs ", "docs", "*"],
    )

    stored = users.get_by_id(result["id"])
    assert stored is not None
    assert stored.username == "alice"
    assert stored.roles == ["viewer", "super_admin"]
    assert stored.spaces == ["docs", "*"]
    assert verify_password(" secret ", stored.password_hash)


def test_service_rejects_empty_password_and_blank_space(tmp_path):
    service, _, _ = _build_service(tmp_path)

    with pytest.raises(AdminUserValidationError):
        service.create_user(username="alice", password="", roles=[], spaces=[])
    with pytest.raises(AdminUserValidationError):
        service.create_user(username="alice", password="secret", roles=[], spaces=[" "])


def test_list_and_get_return_safe_fields_and_missing_user_is_translated(tmp_path):
    service, _, _ = _build_service(tmp_path)
    created = service.create_user(
        username="alice", password="secret", roles=["viewer"], spaces=["docs"]
    )

    assert service.get_user(created["id"]) == created
    listed = service.list_users()
    assert listed == [created]
    assert all("password_hash" not in user for user in listed)

    with pytest.raises(AdminUserNotFoundError):
        service.get_user("missing")


def test_update_normalizes_values_and_returns_new_version(tmp_path):
    service, users, _ = _build_service(tmp_path)
    created = service.create_user(
        username="alice", password="secret", roles=["viewer"], spaces=["docs"]
    )

    updated = service.update_user(
        user_id=created["id"],
        expected_version=created["version"],
        roles=["SUPER_ADMIN", "super_admin"],
        spaces=[" private ", "private"],
    )

    assert updated["roles"] == ["super_admin"]
    assert updated["spaces"] == ["private"]
    assert updated["version"] == 2
    assert users.get_by_id(created["id"]).password_hash not in updated


def test_update_preserves_omitted_fields_via_store(tmp_path):
    service, _, _ = _build_service(tmp_path)
    created = service.create_user(
        username="alice", password="secret", roles=["viewer"], spaces=["docs"]
    )

    updated = service.update_user(
        user_id=created["id"], expected_version=created["version"], roles=["maintainer"]
    )

    assert updated["roles"] == ["maintainer"]
    assert updated["spaces"] == ["docs"]


def test_update_and_delete_translate_store_errors(tmp_path):
    service, _, _ = _build_service(tmp_path)
    created = service.create_user(username="alice", password="secret", roles=[], spaces=[])

    with pytest.raises(AdminUserNotFoundError):
        service.update_user(user_id="missing", expected_version=1, roles=[])
    with pytest.raises(AdminUserVersionConflictError):
        service.update_user(user_id=created["id"], expected_version=0, roles=[])

    with pytest.raises(AdminUserNotFoundError):
        service.delete_user(user_id="missing", expected_version=1)


def test_delete_revokes_sessions_only_after_success(tmp_path):
    service, users, sessions = _build_service(tmp_path)
    created = service.create_user(username="alice", password="secret", roles=[], spaces=[])
    session = sessions.create_session(created["id"], ttl_seconds=3600)

    result = service.delete_user(user_id=created["id"], expected_version=created["version"])

    assert result == {"deleted": True, "user_id": created["id"]}
    assert users.get_by_id(created["id"]) is None
    assert sessions.get_valid_session(session.token) is None


def test_delete_does_not_revoke_session_when_version_is_stale(tmp_path):
    service, _, sessions = _build_service(tmp_path)
    created = service.create_user(username="alice", password="secret", roles=[], spaces=[])
    session = sessions.create_session(created["id"], ttl_seconds=3600)
    service.update_user(user_id=created["id"], expected_version=created["version"], spaces=["docs"])

    with pytest.raises(AdminUserVersionConflictError):
        service.delete_user(user_id=created["id"], expected_version=created["version"])

    assert sessions.get_valid_session(session.token) is not None


def test_stale_service_delete_does_not_revoke_session(tmp_path):
    service, users, sessions = _build_service(tmp_path)
    users.create_user(
        username="admin", password_hash=hash_password("admin"), roles=["super_admin"], spaces=["*"]
    )
    target = users.create_user(
        username="bob", password_hash=hash_password("bob"), roles=["viewer"], spaces=[]
    )
    session = sessions.create_session(target.id, ttl_seconds=3600)
    users.update_user(
        user_id=target.id,
        expected_version=1,
        actor_is_super_admin=True,
        actor_department_id=None,
        spaces=["docs"],
    )

    with pytest.raises(AdminUserVersionConflictError):
        service.delete_user(user_id=target.id, expected_version=1)

    assert users.get_by_id(target.id) is not None
    assert sessions.get_valid_session(session.token) == session


def test_last_admin_errors_do_not_revoke_session(tmp_path):
    service, _, sessions = _build_service(tmp_path)
    created = service.create_user(
        username="admin", password="secret", roles=["super_admin"], spaces=["*"]
    )
    session = sessions.create_session(created["id"], ttl_seconds=3600)

    with pytest.raises(LastAdminError):
        service.update_user(
            user_id=created["id"], expected_version=created["version"], roles=["viewer"]
        )
    with pytest.raises(LastAdminError):
        service.delete_user(user_id=created["id"], expected_version=created["version"])

    assert sessions.get_valid_session(session.token) is not None


def test_super_admin_creates_department_admin_with_explicit_department(tmp_path):
    service, users, _ = _build_service(tmp_path)
    super_admin = AuthContext(user_id="root", roles={"super_admin"}, spaces={"*"})

    created = service.create_user(
        actor=super_admin, username="lead", password="secret",
        roles=["department_admin"], spaces=["docs"], department_id="dept-1",
    )

    assert created["department_id"] == "dept-1"
    assert users.get_by_id(created["id"]).department_ids == ["dept-1"]


def test_department_admin_cannot_create_user_with_admin_roles(tmp_path):
    service, _, _ = _build_service(tmp_path)
    dept_admin = AuthContext(
        user_id="lead", roles={"department_admin"}, spaces={"docs"}, department_id="dept-1"
    )

    with pytest.raises(AdminUserScopeError):
        service.create_user(
            actor=dept_admin, username="new-lead", password="secret",
            roles=["department_admin"], spaces=[], department_id="dept-1",
        )
    with pytest.raises(AdminUserScopeError):
        service.create_user(
            actor=dept_admin, username="new-root", password="secret",
            roles=["super_admin"], spaces=["*"],
        )


def test_department_admin_creates_user_defaults_to_own_department(tmp_path):
    service, users, _ = _build_service(tmp_path)
    dept_admin = AuthContext(
        user_id="lead", roles={"department_admin"}, spaces={"docs"}, department_id="dept-1"
    )

    created = service.create_user(
        actor=dept_admin, username="alice", password="secret", roles=["viewer"], spaces=["docs"]
    )

    assert created["department_id"] == "dept-1"
    assert users.get_by_id(created["id"]).department_ids == ["dept-1"]


def test_department_admin_creating_user_in_other_department_is_rejected(tmp_path):
    service, _, _ = _build_service(tmp_path)
    dept_admin = AuthContext(
        user_id="lead", roles={"department_admin"}, spaces={"docs"}, department_id="dept-1"
    )

    with pytest.raises(AdminUserScopeError):
        service.create_user(
            actor=dept_admin, username="alice", password="secret",
            roles=["viewer"], spaces=["docs"], department_id="dept-2",
        )


def test_creating_department_admin_without_department_returns_422(tmp_path):
    service, _, _ = _build_service(tmp_path)
    super_admin = AuthContext(user_id="root", roles={"super_admin"}, spaces={"*"})

    with pytest.raises(AdminUserValidationError):
        service.create_user(
            actor=super_admin, username="lead", password="secret",
            roles=["department_admin"], spaces=["docs"],
        )


def test_omitting_actor_behaves_like_unrestricted_super_admin(tmp_path):
    service, users, _ = _build_service(tmp_path)

    created = service.create_user(
        username="lead", password="secret", roles=["department_admin"],
        spaces=["docs"], department_id="dept-1",
    )

    assert created["department_id"] == "dept-1"
    assert users.get_by_id(created["id"]).roles == ["department_admin"]
