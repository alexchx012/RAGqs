from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.identity.ports import NoopDepartmentWorkCheckPort
from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime


def settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
        }
    )


def test_admin_routes_manage_non_admin_users_and_expose_read_only_matrix() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
        department_work_check=NoopDepartmentWorkCheckPort(),
    )
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    admin_token = service.login(username="admin", password="Password1").access_token
    user_token = service.login(username="alice", password="Password1").access_token
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        department = client.post(
            "/v1/admin/departments",
            headers={"Authorization": f"Bearer {admin_token}", "Idempotency-Key": "dept-1"},
            json={"name": "Finance"},
        )
        assert department.status_code == 201

        created = client.post(
            "/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_token}", "Idempotency-Key": "user-1"},
            json={
                "username": "minister",
                "real_name": "Minister",
                "display_name": "Minister",
                "department_id": department.json()["id"],
                "role": "minister",
                "initial_password": "Password1",
            },
        )
        updated = client.patch(
            f"/v1/admin/users/{created.json()['id']}",
            headers={"Authorization": f"Bearer {admin_token}", "Idempotency-Key": "user-update-1"},
            json={"expected_version": 1, "role": "user", "department_id": None},
        )
        assert updated.status_code == 200
        deleted = client.request(
            "DELETE",
            f"/v1/admin/users/{created.json()['id']}",
            headers={"Authorization": f"Bearer {admin_token}", "Idempotency-Key": "user-delete-1"},
            json={"expected_version": 2},
        )
        renamed = client.patch(
            f"/v1/admin/departments/{department.json()['id']}",
            headers={"Authorization": f"Bearer {admin_token}", "Idempotency-Key": "dept-update-1"},
            json={"expected_version": 1, "name": "Treasury"},
        )
        deactivated = client.post(
            f"/v1/admin/departments/{department.json()['id']}/deactivate",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Idempotency-Key": "dept-deactivate-1",
            },
            json={"expected_version": 2},
        )
        users = client.get("/v1/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
        departments = client.get(
            "/v1/admin/departments?status=all", headers={"Authorization": f"Bearer {admin_token}"}
        )
        matrix = client.get(
            "/v1/admin/permission-matrix", headers={"Authorization": f"Bearer {admin_token}"}
        )
        forbidden_matrix = client.get(
            "/v1/admin/permission-matrix", headers={"Authorization": f"Bearer {user_token}"}
        )

    assert created.status_code == 201
    assert created.json()["role"] == "minister"
    assert updated.status_code == 200
    assert updated.json()["role"] == "user"
    assert deleted.status_code == 202
    assert deleted.json()["lifecycle_status"] == "pending_delete"
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Treasury"
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "inactive"
    assert users.status_code == 200
    assert {item["username"] for item in users.json()["items"]} == {"admin", "alice", "minister"}
    assert departments.status_code == 200
    assert departments.json()["items"][0]["allowed_actions"] == []
    assert matrix.status_code == 200
    assert matrix.json()["capabilities"]
    assert forbidden_matrix.status_code == 403
    assert forbidden_matrix.json()["error"]["code"] == "forbidden_target"
