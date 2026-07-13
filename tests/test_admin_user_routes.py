import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_users
from app.security.auth import AuthContext, get_current_auth_context
from app.security.session_store import SessionStore
from app.security.user_store import UserStore
from app.services.admin_user_service import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    AdminUserService,
    AdminUserValidationError,
    AdminUserVersionConflictError,
    LastAdminError,
)


def _client_for(tmp_path, *, roles):
    users = UserStore(tmp_path / "auth.sqlite3")
    sessions = SessionStore(tmp_path / "auth.sqlite3")
    service = AdminUserService(user_store=users, session_store=sessions)
    service.create_user(username="target", password="secret", roles=["viewer"], spaces=["docs"])
    application = FastAPI()
    application.include_router(admin_users.router, prefix="/api")
    application.dependency_overrides[admin_users.admin_user_service_dependency] = lambda: service
    application.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles=set(roles), spaces={"*"}
    )
    return TestClient(application), service, users, sessions


def test_admin_can_list_and_view_safe_user_data(tmp_path):
    client, _, _, _ = _client_for(tmp_path, roles={"admin"})

    listed = client.get("/api/admin/users")

    assert listed.status_code == 200
    user = listed.json()["data"]["users"][0]
    assert set(user) == {"id", "username", "roles", "spaces", "version", "created_at"}
    assert "password_hash" not in listed.text

    detail = client.get(f"/api/admin/users/{user['id']}")

    assert detail.status_code == 200
    assert detail.json()["data"]["user"] == user
    assert "password_hash" not in detail.text


def test_admin_can_create_and_delete_user_with_json_expected_version(tmp_path):
    client, _, users, _ = _client_for(tmp_path, roles={"admin"})

    created = client.post(
        "/api/admin/users",
        json={"username": "new-user", "password": "pw", "roles": [], "spaces": []},
    )

    assert created.status_code == 200
    user = created.json()["data"]["user"]
    assert set(user) == {"id", "username", "roles", "spaces", "version", "created_at"}
    assert user["username"] == "new-user"
    assert "password_hash" not in created.text

    deleted = client.request(
        "DELETE",
        f"/api/admin/users/{user['id']}",
        json={"expected_version": user["version"]},
        headers={"If-Match": '"999"'},
    )

    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"deleted": True, "user_id": user["id"]}
    assert users.get_by_id(user["id"]) is None
    assert "password_hash" not in deleted.text


def test_patch_stale_version_returns_409_without_overwrite(tmp_path):
    client, service, users, _ = _client_for(tmp_path, roles={"admin"})
    user_id = next(iter(users.list_users())).id

    assert (
        client.patch(
            f"/api/admin/users/{user_id}",
            json={"expected_version": 1, "roles": ["maintainer"]},
        ).status_code
        == 200
    )

    stale = client.patch(
        f"/api/admin/users/{user_id}",
        json={"expected_version": 1, "spaces": ["private"]},
    )

    assert stale.status_code == 409
    current = service.get_user(user_id)
    assert current["roles"] == ["maintainer"]
    assert current["spaces"] == ["docs"]
    assert current["version"] == 2
    assert "password_hash" not in stale.text


def test_service_domain_errors_map_to_stable_http_statuses(tmp_path):
    client, _, users, _ = _client_for(tmp_path, roles={"admin"})
    user_id = next(iter(users.list_users())).id

    invalid = client.post(
        "/api/admin/users",
        json={"username": "invalid", "password": "pw", "roles": ["unknown"], "spaces": []},
    )
    duplicate = client.post(
        "/api/admin/users",
        json={"username": "target", "password": "pw", "roles": [], "spaces": []},
    )
    missing = client.get("/api/admin/users/missing")
    version_conflict = client.patch(
        f"/api/admin/users/{user_id}",
        json={"expected_version": 2, "roles": []},
    )

    assert invalid.status_code == 422
    assert duplicate.status_code == 409
    assert missing.status_code == 404
    assert version_conflict.status_code == 409
    assert invalid.json()["detail"] == "invalid administrator user input"
    assert duplicate.json()["detail"] == "administrator user already exists"
    assert missing.json()["detail"] == "administrator user not found"
    assert version_conflict.json()["detail"] == "administrator user version conflict"
    assert all(
        "password_hash" not in response.text
        for response in (invalid, duplicate, missing, version_conflict)
    )


def test_service_error_detail_does_not_echo_sensitive_exception_text(tmp_path):
    application = FastAPI()
    application.include_router(admin_users.router, prefix="/api")

    class LeakingService:
        def create_user(self, **kwargs):
            raise AdminUserAlreadyExistsError("password_hash=should-not-leak")

    application.dependency_overrides[admin_users.admin_user_service_dependency] = (
        lambda: LeakingService()
    )
    application.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles={"admin"}, spaces={"*"}
    )

    response = TestClient(application).post(
        "/api/admin/users",
        json={"username": "new", "password": "pw", "roles": [], "spaces": []},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "administrator user already exists"
    assert "password_hash" not in response.text
    assert "should-not-leak" not in response.text


@pytest.mark.parametrize(
    ("method", "path", "payload", "error_type", "expected_status", "expected_detail"),
    [
        (
            "POST",
            "/api/admin/users",
            {"username": "new", "password": "pw", "roles": [], "spaces": []},
            AdminUserValidationError,
            422,
            "invalid administrator user input",
        ),
        (
            "GET",
            "/api/admin/users/missing",
            None,
            AdminUserNotFoundError,
            404,
            "administrator user not found",
        ),
        (
            "POST",
            "/api/admin/users",
            {"username": "new", "password": "pw", "roles": [], "spaces": []},
            AdminUserAlreadyExistsError,
            409,
            "administrator user already exists",
        ),
        (
            "PATCH",
            "/api/admin/users/target",
            {"expected_version": 1, "roles": []},
            AdminUserVersionConflictError,
            409,
            "administrator user version conflict",
        ),
        (
            "DELETE",
            "/api/admin/users/target",
            {"expected_version": 1},
            LastAdminError,
            409,
            "cannot remove last administrator",
        ),
    ],
)
def test_service_error_details_are_fixed_and_safe(
    method, path, payload, error_type, expected_status, expected_detail
):
    application = FastAPI()
    application.include_router(admin_users.router, prefix="/api")
    raw_message = "raw exception password_hash=should-not-leak"

    class RaisingService:
        def _raise(self):
            raise error_type(raw_message)

        def create_user(self, **kwargs):
            self._raise()

        def get_user(self, user_id):
            self._raise()

        def update_user(self, **kwargs):
            self._raise()

        def delete_user(self, **kwargs):
            self._raise()

    application.dependency_overrides[admin_users.admin_user_service_dependency] = (
        lambda: RaisingService()
    )
    application.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles={"admin"}, spaces={"*"}
    )

    request = TestClient(application)
    if payload is None:
        response = request.request(method, path)
    else:
        response = request.request(method, path, json=payload)

    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "password_hash" not in response.text
    assert raw_message not in response.text


def test_request_validation_returns_422_and_keeps_explicit_empty_updates(tmp_path):
    client, service, users, _ = _client_for(tmp_path, roles={"admin"})
    user_id = next(iter(users.list_users())).id

    missing_password = client.post(
        "/api/admin/users",
        json={"username": "new", "roles": [], "spaces": []},
    )
    empty_update = client.patch(
        f"/api/admin/users/{user_id}",
        json={"expected_version": 1},
    )
    invalid_delete_version = client.request(
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"expected_version": 0},
    )
    clear_update = client.patch(
        f"/api/admin/users/{user_id}",
        json={"expected_version": 1, "roles": [], "spaces": []},
    )

    assert missing_password.status_code == 422
    assert empty_update.status_code == 422
    assert invalid_delete_version.status_code == 422
    assert clear_update.status_code == 200
    assert service.get_user(user_id)["roles"] == []
    assert service.get_user(user_id)["spaces"] == []


def test_last_admin_error_maps_to_409(tmp_path):
    client, _, users, _ = _client_for(tmp_path, roles={"admin"})
    created = client.post(
        "/api/admin/users",
        json={"username": "only-admin", "password": "pw", "roles": ["admin"], "spaces": ["*"]},
    ).json()["data"]["user"]

    response = client.patch(
        f"/api/admin/users/{created['id']}",
        json={"expected_version": created["version"], "roles": ["viewer"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "cannot remove last administrator"
    assert users.get_by_id(created["id"]).roles == ["admin"]
    assert created["id"] not in response.text
    assert "password_hash" not in response.text


def test_delete_missing_user_returns_fixed_404_detail(tmp_path):
    client, _, _, _ = _client_for(tmp_path, roles={"admin"})

    response = client.request("DELETE", "/api/admin/users/missing", json={"expected_version": 1})

    assert response.status_code == 404
    assert response.json()["detail"] == "administrator user not found"
    assert "missing" not in response.text
    assert "password_hash" not in response.text


def test_delete_stale_version_returns_fixed_409_without_deletion(tmp_path):
    client, _, users, _ = _client_for(tmp_path, roles={"admin"})
    user_id = next(iter(users.list_users())).id

    assert (
        client.patch(
            f"/api/admin/users/{user_id}",
            json={"expected_version": 1, "spaces": ["updated"]},
        ).status_code
        == 200
    )

    response = client.request("DELETE", f"/api/admin/users/{user_id}", json={"expected_version": 1})

    assert response.status_code == 409
    assert response.json()["detail"] == "administrator user version conflict"
    assert users.get_by_id(user_id) is not None
    assert user_id not in response.text
    assert "password_hash" not in response.text


def test_delete_last_admin_returns_fixed_409_without_deletion(tmp_path):
    client, _, users, _ = _client_for(tmp_path, roles={"admin"})
    created_response = client.post(
        "/api/admin/users",
        json={"username": "only-admin", "password": "pw", "roles": ["admin"], "spaces": ["*"]},
    )
    created_response.raise_for_status()
    created = created_response.json()["data"]["user"]

    response = client.request(
        "DELETE",
        f"/api/admin/users/{created['id']}",
        json={"expected_version": created["version"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "cannot remove last administrator"
    record = users.get_by_id(created["id"])
    assert record is not None
    assert record.roles == ["admin"]
    assert created["id"] not in response.text
    assert "password_hash" not in response.text


def test_non_admin_gets_403_for_all_five_endpoints(tmp_path):
    client, _, users, _ = _client_for(tmp_path, roles={"viewer"})
    user_id = next(iter(users.list_users())).id
    before = users.count_users()

    responses = [
        client.get("/api/admin/users"),
        client.get(f"/api/admin/users/{user_id}"),
        client.post(
            "/api/admin/users",
            json={"username": "new", "password": "pw", "roles": [], "spaces": []},
        ),
        client.patch(f"/api/admin/users/{user_id}", json={"expected_version": 1, "roles": []}),
        client.request("DELETE", f"/api/admin/users/{user_id}", json={"expected_version": 1}),
    ]

    assert [response.status_code for response in responses] == [403] * 5
    assert users.count_users() == before


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/admin/users", None),
        ("GET", "/api/admin/users/target", None),
        (
            "POST",
            "/api/admin/users",
            {"username": "new", "password": "pw", "roles": [], "spaces": []},
        ),
        ("PATCH", "/api/admin/users/target", {"expected_version": 1, "roles": []}),
        ("DELETE", "/api/admin/users/target", {"expected_version": 1}),
    ],
)
def test_permission_dependency_runs_before_service_dependency(method, path, payload):
    application = FastAPI()
    application.include_router(admin_users.router, prefix="/api")

    def fail_if_service_is_resolved():
        raise AssertionError("service must not be resolved for an unauthorized request")

    application.dependency_overrides[admin_users.admin_user_service_dependency] = (
        fail_if_service_is_resolved
    )
    application.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles={"viewer"}, spaces={"*"}
    )

    client = TestClient(application)
    if payload is None:
        response = client.request(method, path)
    else:
        response = client.request(method, path, json=payload)

    assert response.status_code == 403
