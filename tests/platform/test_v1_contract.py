from __future__ import annotations

import importlib
import sys

from fastapi.testclient import TestClient

from app.identity.schema import identity_metadata
from app.platform.database import core_metadata
from app.usage.schema import usage_metadata


def platform_environment(monkeypatch) -> None:
    monkeypatch.setenv("RAG_PLATFORM_PROFILE", "development")
    monkeypatch.setenv("RAG_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("RAG_OBJECT_STORAGE_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("RAG_OBJECT_STORAGE_BUCKET", "rag-dev")
    monkeypatch.setenv("RAG_PROVIDER_NAME", "fake")


def test_uvicorn_entrypoint_exposes_only_versioned_routes(monkeypatch) -> None:
    platform_environment(monkeypatch)
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    paths = {route.path for route in main.app.routes}

    assert "/v1/health" in paths
    assert all(path.startswith("/v1") for path in paths)
    assert "/api/chat" not in paths
    assert "/chat" not in paths

    # lifespan 会锁定业务日历（Task 12）：先建 usage 表，否则启动即失败。
    engine = main.app.state.platform_runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)

    with TestClient(main.app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "service", "request_id"}
