from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.usage.schema import usage_metadata


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


def test_spaces_route_returns_only_current_acl_permissions() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    service = IdentityAccessService(engine, configured.auth)
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    token = service.login(username="alice", password="Password1").access_token
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        response = client.get(
            "/v1/spaces?usage=retrieval",
            headers={"Authorization": f"Bearer {token}"},
        )
        invalid = client.get(
            "/v1/spaces?usage=invalid",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert {item["id"]: item["permission"] for item in response.json()["items"]} == {
        f"personal:{user['id']}": "manage",
        "public": "contribute",
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
