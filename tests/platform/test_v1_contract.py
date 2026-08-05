from __future__ import annotations

import importlib
import sys

from fastapi.testclient import TestClient


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

    with TestClient(main.app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "service", "request_id"}
