from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.documents.schema import documents_metadata, documents_table
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
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
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
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


def test_spaces_route_reports_real_per_space_document_counts() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    service = IdentityAccessService(engine, configured.auth)
    user = service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    department = service.create_department(actor=admin, name="Finance", idempotency_key="dep-1")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for space_id in (
            f"personal:{user['id']}",
            f"department:{department['id']}",
            "public",
        ):
            connection.execute(
                documents_table.insert().values(
                    id=f"doc_{space_id.split(':')[-1]}",
                    space_id=space_id,
                    lifecycle_status="active",
                    active_version_id=None,
                    pending_version_id=None,
                    active_operation_job_id=None,
                    deletion_id=None,
                    version=1,
                    name="Document",
                    normalized_name="document",
                    media_kind="text/plain",
                    created_by_user_id=str(user["id"]),
                    uploaded_at_utc=now,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        connection.execute(
            documents_table.insert().values(
                id="doc_public_deleted",
                space_id="public",
                lifecycle_status="deleted",
                active_version_id=None,
                pending_version_id=None,
                active_operation_job_id=None,
                deletion_id=None,
                version=1,
                name="Deleted document",
                normalized_name="deleted-document",
                media_kind="text/plain",
                created_by_user_id=str(user["id"]),
                uploaded_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
    token = service.login(username="admin", password="Password1").access_token
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        response = client.get(
            "/v1/spaces?usage=manage",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert {item["id"]: item["document_count"] for item in response.json()["items"]} == {
        f"personal:{user['id']}": 1,
        f"department:{department['id']}": 1,
        "public": 1,
    }
