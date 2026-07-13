from types import SimpleNamespace

import pytest

from app.security.local_auth_service import LocalAuthError, LocalAuthService
from app.security.password import hash_password
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
