from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import admin_users
from app.api import auth as auth_api
from app.config import config as real_config
from app.security import auth as auth_module
from app.security.auth import AuthContext, require_permission
from app.security.local_auth_service import LocalAuthService
from app.security.password import hash_password
from app.security.session_store import SessionStore
from app.security.user_store import UserStore
from app.services.admin_user_service import AdminUserService


@pytest.fixture()
def local_auth_app(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.sqlite3"
    user_store = UserStore(db_path)
    session_store = SessionStore(db_path)
    user_store.create_user(
        username="alice",
        password_hash=hash_password("correct-password"),
        roles=["super_admin"],
        spaces=["*"],
    )
    service = LocalAuthService(user_store=user_store, session_store=session_store)

    monkeypatch.setattr(auth_api, "get_local_auth_service", lambda settings=None: service)
    monkeypatch.setattr(
        "app.security.local_auth_service.get_local_auth_service",
        lambda settings=None: service,
    )
    monkeypatch.setattr(
        auth_module,
        "config",
        SimpleNamespace(auth_enabled=True, auth_provider="local_credentials"),
    )

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")

    @app.get("/api/protected")
    async def protected(auth_context: AuthContext = Depends(require_permission("chat:write"))):
        return {"user_id": auth_context.user_id}

    return TestClient(app)


@pytest.fixture()
def local_auth_admin_app(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.sqlite3"
    user_store = UserStore(db_path)
    session_store = SessionStore(db_path)
    user_store.create_user(
        username="alice",
        password_hash=hash_password("correct-password"),
        roles=["super_admin"],
        spaces=["*"],
    )
    service = LocalAuthService(user_store=user_store, session_store=session_store)

    monkeypatch.setattr(auth_api, "get_local_auth_service", lambda settings=None: service)
    monkeypatch.setattr(
        "app.security.local_auth_service.get_local_auth_service",
        lambda settings=None: service,
    )
    monkeypatch.setattr(
        auth_module,
        "config",
        SimpleNamespace(auth_enabled=True, auth_provider="local_credentials"),
    )

    application = FastAPI()
    application.include_router(auth_api.router, prefix="/api")
    application.include_router(admin_users.router, prefix="/api")

    @application.get("/api/protected")
    async def protected(auth_context: AuthContext = Depends(require_permission("chat:write"))):
        return {"user_id": auth_context.user_id}

    application.dependency_overrides[admin_users.admin_user_service_dependency] = (
        lambda: AdminUserService(user_store=user_store, session_store=session_store)
    )
    return TestClient(application), TestClient(application), service


@pytest.fixture()
def local_auth_department_scoped_app(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.sqlite3"
    user_store = UserStore(db_path)
    session_store = SessionStore(db_path)
    user_store.create_user(
        username="alice",
        password_hash=hash_password("correct-password"),
        roles=["super_admin"],
        spaces=["*"],
    )
    user_store.create_user(
        username="carol",
        password_hash=hash_password("carol-password"),
        roles=["department_admin"],
        spaces=["docs"],
        department_ids=["dept-1"],
    )
    service = LocalAuthService(user_store=user_store, session_store=session_store)

    monkeypatch.setattr(auth_api, "get_local_auth_service", lambda settings=None: service)
    monkeypatch.setattr(
        "app.security.local_auth_service.get_local_auth_service",
        lambda settings=None: service,
    )
    monkeypatch.setattr(
        auth_module,
        "config",
        SimpleNamespace(auth_enabled=True, auth_provider="local_credentials"),
    )

    application = FastAPI()
    application.include_router(auth_api.router, prefix="/api")
    application.include_router(admin_users.router, prefix="/api")
    application.dependency_overrides[admin_users.admin_user_service_dependency] = (
        lambda: AdminUserService(user_store=user_store, session_store=session_store)
    )
    return (
        TestClient(application),
        TestClient(application),
        TestClient(application),
        service,
    )


# --- Requirement: Local credential login ---


def test_login_success_sets_cookie_and_returns_user_info(local_auth_app):
    response = local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )

    assert response.status_code == 200
    assert "rag_session" in response.cookies
    body = response.json()["data"]
    assert body["roles"] == ["super_admin"]
    assert body["spaces"] == ["*"]


def test_login_rejects_wrong_password(local_auth_app):
    response = local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert "rag_session" not in response.cookies


def test_login_rejects_unknown_username(local_auth_app):
    response = local_auth_app.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )

    assert response.status_code == 401
    assert "rag_session" not in response.cookies


def test_login_cookie_is_httponly_samesite_lax_and_not_secure_in_local_environment(
    local_auth_app,
):
    response = local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )

    set_cookie_header = response.headers.get("set-cookie", "").lower()
    assert "rag_session=" in set_cookie_header
    assert "httponly" in set_cookie_header
    assert "samesite=lax" in set_cookie_header
    assert "secure" not in set_cookie_header


def test_login_cookie_is_secure_when_deployment_environment_is_not_local(
    local_auth_app, monkeypatch
):
    monkeypatch.setattr(real_config, "deployment_environment", "production")

    response = local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )

    assert "secure" in response.headers.get("set-cookie", "").lower()


# --- Requirement: Session-based request authentication ---


def test_valid_session_cookie_grants_access_to_protected_route(local_auth_app):
    local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )

    response = local_auth_app.get("/api/protected")

    assert response.status_code == 200
    assert response.json()["user_id"]


def test_missing_session_cookie_is_rejected(local_auth_app):
    response = local_auth_app.get("/api/protected")

    assert response.status_code == 401


def test_expired_session_cookie_is_rejected(local_auth_app, tmp_path):
    db_path = tmp_path / "expired-auth.sqlite3"
    user_store = UserStore(db_path)
    session_store = SessionStore(db_path)
    user_store.create_user(
        username="bob", password_hash=hash_password("pw"), roles=["viewer"], spaces=["default"]
    )
    expired_session = session_store.create_session(
        user_store.get_by_username("bob").id, ttl_seconds=-1
    )
    local_auth_app.cookies.set("rag_session", expired_session.token)

    response = local_auth_app.get("/api/protected")

    assert response.status_code == 401


# --- Requirement: Logout invalidates session ---


def test_logout_invalidates_cookie_immediately(local_auth_app):
    local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )

    logout_response = local_auth_app.post("/api/auth/logout")
    protected_response = local_auth_app.get("/api/protected")

    assert logout_response.status_code == 200
    assert protected_response.status_code == 401


def test_logout_without_existing_session_still_returns_success(local_auth_app):
    response = local_auth_app.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json()["data"]["logged_out"] is True


# --- Requirement: Current user info endpoint ---


def test_me_endpoint_returns_user_info_when_logged_in(local_auth_app):
    local_auth_app.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )

    response = local_auth_app.get("/api/auth/me")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["roles"] == ["super_admin"]
    assert data["spaces"] == ["*"]


def test_me_endpoint_rejects_when_not_logged_in(local_auth_app):
    response = local_auth_app.get("/api/auth/me")

    assert response.status_code == 401


def test_role_update_is_visible_without_target_relogin(local_auth_admin_app):
    admin_client, target_client, service = local_auth_admin_app
    target = service.user_store.create_user(
        username="bob", password_hash=hash_password("bob-pw"), roles=["viewer"], spaces=["docs"]
    )
    target_login = target_client.post(
        "/api/auth/login", json={"username": "bob", "password": "bob-pw"}
    )
    assert target_login.status_code == 200
    assert "rag_session" in target_login.cookies

    admin_login = admin_client.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )
    assert admin_login.status_code == 200
    assert "rag_session" in admin_login.cookies

    before = target_client.get("/api/auth/me")
    assert before.status_code == 200
    assert before.json()["data"]["roles"] == ["viewer"]
    assert before.json()["data"]["spaces"] == ["docs"]

    changed = admin_client.patch(
        f"/api/admin/users/{target.id}",
        json={"expected_version": 1, "roles": ["maintainer"], "spaces": ["private"]},
    )
    assert changed.status_code == 200
    after = target_client.get("/api/auth/me")
    assert after.status_code == 200
    assert after.json()["data"]["roles"] == ["maintainer"]
    assert after.json()["data"]["spaces"] == ["private"]


def test_deleted_user_old_session_returns_401(local_auth_admin_app):
    admin_client, target_client, service = local_auth_admin_app
    target = service.user_store.create_user(
        username="bob", password_hash=hash_password("bob-pw"), roles=["viewer"], spaces=["docs"]
    )
    target_login = target_client.post(
        "/api/auth/login", json={"username": "bob", "password": "bob-pw"}
    )
    assert target_login.status_code == 200
    assert "rag_session" in target_login.cookies

    admin_login = admin_client.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-password"}
    )
    assert admin_login.status_code == 200
    assert "rag_session" in admin_login.cookies

    before_delete = target_client.get("/api/protected")
    assert before_delete.status_code == 200
    assert before_delete.json()["user_id"] == target.id

    deleted = admin_client.request(
        "DELETE", f"/api/admin/users/{target.id}", json={"expected_version": 1}
    )
    assert deleted.status_code == 200
    after_delete = target_client.get("/api/protected")
    assert after_delete.status_code == 401


def test_department_admin_real_login_filters_list_by_resolved_department(
    local_auth_department_scoped_app,
):
    _, dept_client, _, service = local_auth_department_scoped_app
    same_dept = service.user_store.create_user(
        username="dave", password_hash=hash_password("pw"), roles=["viewer"], spaces=["docs"],
        department_ids=["dept-1"],
    )
    service.user_store.create_user(
        username="erin", password_hash=hash_password("pw"), roles=["viewer"], spaces=["docs"],
        department_ids=["dept-2"],
    )

    login = dept_client.post(
        "/api/auth/login", json={"username": "carol", "password": "carol-password"}
    )
    assert login.status_code == 200

    listed = dept_client.get("/api/admin/users")
    assert listed.status_code == 200
    usernames = {user["username"] for user in listed.json()["data"]["users"]}
    assert usernames == {"carol", "dave"}
    assert same_dept.username in usernames


def test_department_admin_real_login_gets_404_for_cross_department_target(
    local_auth_department_scoped_app,
):
    _, dept_client, _, service = local_auth_department_scoped_app
    other_dept = service.user_store.create_user(
        username="erin", password_hash=hash_password("pw"), roles=["viewer"], spaces=["docs"],
        department_ids=["dept-2"],
    )

    login = dept_client.post(
        "/api/auth/login", json={"username": "carol", "password": "carol-password"}
    )
    assert login.status_code == 200

    response = dept_client.get(f"/api/admin/users/{other_dept.id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "administrator user not found"
