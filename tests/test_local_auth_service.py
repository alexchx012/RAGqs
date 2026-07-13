from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.security.local_auth_service import LocalAuthError, LocalAuthService
from app.security.password import hash_password, verify_password
from app.security.session_store import SessionStore
from app.security.user_store import UserStore


def _build_service(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    user_store = UserStore(db_path)
    session_store = SessionStore(db_path)
    service = LocalAuthService(user_store=user_store, session_store=session_store)
    user_store.create_user(
        username="alice",
        password_hash=hash_password("correct-password"),
        roles=["admin"],
        spaces=["*"],
    )
    return service


def test_login_succeeds_with_correct_credentials(tmp_path):
    service = _build_service(tmp_path)

    result = service.login("alice", "correct-password")

    assert result.user_id
    assert result.roles == {"admin"}
    assert result.spaces == {"*"}
    assert len(result.token) > 20


def test_login_rejects_wrong_password_with_generic_message(tmp_path):
    service = _build_service(tmp_path)

    with pytest.raises(LocalAuthError) as excinfo:
        service.login("alice", "wrong-password")

    assert str(excinfo.value) == "invalid username or password"


def test_login_rejects_unknown_username_with_same_generic_message(tmp_path):
    service = _build_service(tmp_path)

    with pytest.raises(LocalAuthError) as excinfo:
        service.login("nobody", "whatever")

    assert str(excinfo.value) == "invalid username or password"


def test_resolve_returns_auth_context_for_valid_token(tmp_path):
    service = _build_service(tmp_path)
    result = service.login("alice", "correct-password")

    context = service.resolve(result.token)

    assert context is not None
    assert context.user_id == result.user_id
    assert context.roles == {"admin"}
    assert context.spaces == {"*"}
    assert context.provider == "local_credentials"
    assert context.has_permission("chat:write")


def test_resolve_reads_updated_roles_and_spaces_without_relogin(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    users = UserStore(db_path)
    sessions = SessionStore(db_path)
    service = LocalAuthService(user_store=users, session_store=sessions)
    admin = users.create_user(
        username="admin",
        password_hash=hash_password("admin-pw"),
        roles=["admin"],
        spaces=["*"],
    )
    target = users.create_user(
        username="bob",
        password_hash=hash_password("bob-pw"),
        roles=["viewer"],
        spaces=["docs"],
    )
    login = service.login("bob", "bob-pw")

    users.update_user(
        user_id=target.id,
        expected_version=1,
        roles=["maintainer"],
        spaces=["private"],
    )

    context = service.resolve(login.token)
    assert context is not None
    assert context.user_id == target.id
    assert context.roles == {"maintainer"}
    assert context.spaces == {"private"}
    assert admin.id != target.id


def test_resolve_returns_none_for_invalid_token(tmp_path):
    service = _build_service(tmp_path)

    assert service.resolve("not-a-real-token") is None


def test_logout_invalidates_token_immediately(tmp_path):
    service = _build_service(tmp_path)
    result = service.login("alice", "correct-password")

    service.logout(result.token)

    assert service.resolve(result.token) is None


def test_seed_initial_admin_creates_account_on_first_startup(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed="admin:supersecret",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)

    service.seed_initial_admin()

    user = service.user_store.get_by_username("admin")
    assert user is not None
    assert user.roles == ["admin"]
    assert user.spaces == ["*"]


def test_seed_initial_admin_is_idempotent_when_users_already_exist(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed="admin:supersecret",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)
    service.user_store.create_user(
        username="existing",
        password_hash=hash_password("whatever"),
        roles=["viewer"],
        spaces=["default"],
    )

    service.seed_initial_admin()

    assert service.user_store.count_users() == 1
    assert service.user_store.get_by_username("admin") is None


def test_seed_initial_admin_skips_when_no_seed_configured(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(auth_local_db_path=str(db_path), auth_local_admin_seed=None)
    service = LocalAuthService(settings=settings)

    service.seed_initial_admin()

    assert service.user_store.count_users() == 0


def test_seed_initial_admin_skips_seed_missing_colon_separator(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed="adminonly",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)

    service.seed_initial_admin()

    assert service.user_store.count_users() == 0


def test_seed_initial_admin_skips_seed_with_empty_password(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed="admin:",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)

    service.seed_initial_admin()

    assert service.user_store.count_users() == 0


def test_seed_initial_admin_skips_seed_with_empty_username(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed=":secret",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)

    service.seed_initial_admin()

    assert service.user_store.count_users() == 0


def test_seed_initial_admin_swallows_race_when_admin_created_concurrently(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed="admin:supersecret",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)
    # 模拟另一个进程在 count_users() 早退检查通过之后、本进程执行 create_user
    # 之前抢先插入了同名 admin 账号（TOCTOU 竞态窗口）。
    service.user_store.create_user(
        username="admin",
        password_hash=hash_password("someone-elses-password"),
        roles=["admin"],
        spaces=["*"],
    )

    with patch.object(service.user_store, "count_users", return_value=0):
        service.seed_initial_admin()  # 不应向外抛出 UsernameAlreadyExistsError

    user = service.user_store.get_by_username("admin")
    assert user is not None
    assert verify_password("someone-elses-password", user.password_hash)


def test_seed_initial_admin_allows_colon_inside_password(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    settings = SimpleNamespace(
        auth_local_db_path=str(db_path),
        auth_local_admin_seed="admin:pa:ss",
        auth_session_ttl_seconds=3600,
    )
    service = LocalAuthService(settings=settings)

    service.seed_initial_admin()

    assert service.user_store.count_users() == 1
    user = service.user_store.get_by_username("admin")
    assert user is not None
    assert verify_password("pa:ss", user.password_hash)
