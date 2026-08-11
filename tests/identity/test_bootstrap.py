from __future__ import annotations

from fastapi.testclient import TestClient

from app.identity.bootstrap import run_initial_admin_bootstrap
from app.identity.schema import identity_metadata
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
            "RAG_AUTH_ADMIN_ROSTER": "admin",
            "RAG_AUTH_BOOTSTRAP_USERNAME": "admin",
            "RAG_AUTH_BOOTSTRAP_PASSWORD": "Password1",
            "RAG_AUTH_BOOTSTRAP_REAL_NAME": "Initial Admin",
            "RAG_AUTH_BOOTSTRAP_DISPLAY_NAME": "Admin",
        }
    )


def test_bootstrap_command_seeds_an_empty_database_before_api_startup() -> None:
    configured = settings()
    runtime = build_runtime(configured)
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)

    admin = run_initial_admin_bootstrap(configured, runtime=runtime)
    app = create_platform_app(configured, runtime=runtime)

    assert admin["username"] == "admin"
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200
    runtime.close()
