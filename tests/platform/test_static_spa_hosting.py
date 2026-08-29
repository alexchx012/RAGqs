from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.identity.schema import identity_metadata
from app.outbox.schema import outbox_metadata
from app.platform import app_factory as app_factory_module
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.observability import InMemoryObservabilityMetrics
from app.platform.runtime import PlatformRuntime, build_runtime
from app.usage.schema import usage_metadata


def _settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )


@contextmanager
def _platform_client(
    *, adapters: dict[str, Any] | None = None
) -> Iterator[tuple[TestClient, PlatformRuntime]]:
    configured = _settings()
    runtime = build_runtime(configured, adapters=adapters)
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    app = create_platform_app(configured, runtime=runtime)
    try:
        with TestClient(app) as client:
            yield client, runtime
    finally:
        runtime.close()


def _built_asset(relative_pattern: str) -> str:
    static_directory = Path(__file__).resolve().parents[2] / "static"
    asset = next((static_directory / "assets").glob(relative_pattern))
    return asset.relative_to(static_directory).as_posix()


def test_spa_serves_index_assets_and_frontend_routes() -> None:
    with _platform_client() as (client, _runtime):
        root = client.get("/")

        assert root.status_code == 200
        assert root.headers["content-type"].startswith("text/html")
        assert 'src="/static/assets/index-' in root.text
        assert 'href="/static/assets/index-' in root.text

        for pattern in ("*.js", "*.css", "*.mjs"):
            asset = client.get(f"/static/{_built_asset(pattern)}")
            assert asset.status_code == 200

        for route in ("/login", "/preview/some-id", "/v1foo", "/static-page"):
            response = client.get(route)
            assert response.status_code == 200
            assert response.text == root.text


def test_spa_preserves_api_static_and_non_get_semantics() -> None:
    with _platform_client() as (client, _runtime):
        health = client.get("/v1/health")
        openapi = client.get("/v1/openapi.json")
        docs = client.get("/v1/docs")
        unknown_api = client.get("/v1/not-registered")
        unknown_static = client.get("/static/not-built.js")
        non_get_frontend = client.post("/login")

    assert health.status_code == 200
    assert openapi.status_code == 200
    assert docs.status_code == 200
    assert unknown_api.status_code == 404
    assert unknown_api.json()["error"]["code"] == "not_found"
    assert unknown_api.json()["error"]["message"] == "The requested resource was not found"
    assert unknown_static.status_code == 404
    assert not unknown_static.headers["content-type"].startswith("text/html")
    assert non_get_frontend.status_code in {404, 405}
    assert not non_get_frontend.headers["content-type"].startswith("text/html")


def test_spa_routes_do_not_pollute_unknown_route_metrics() -> None:
    metrics = InMemoryObservabilityMetrics(
        now=lambda: datetime(2026, 8, 28, tzinfo=UTC),
        success_sample_rate=1,
    )
    with _platform_client(adapters={"observability_metrics": metrics}) as (client, _runtime):
        assert client.get("/login").status_code == 200
        assert client.get(f"/static/{_built_asset('*.js')}").status_code == 200
        assert client.get("/v1/not-registered").status_code == 404

    recorded_routes = [sample.route_template for sample in metrics.samples]
    assert "/{full_path:path}" in recorded_routes
    assert "/static" in recorded_routes
    assert recorded_routes.count("other") == 1


@pytest.mark.parametrize("directory_exists", [False, True])
def test_platform_app_starts_without_static_build(
    tmp_path: Path, monkeypatch, directory_exists: bool
) -> None:
    static_directory = tmp_path / "missing-build"
    if directory_exists:
        static_directory.mkdir()
    monkeypatch.setattr(app_factory_module, "_STATIC_DIRECTORY", static_directory)

    with _platform_client() as (client, _runtime):
        root = client.get("/")
        assert root.status_code == 404
        assert root.json()["error"]["code"] == "not_found"
        assert client.get("/v1/health").status_code == 200


def test_maintenance_gate_applies_to_static_spa_routes() -> None:
    class ClosedMaintenanceGate:
        @staticmethod
        def reads_closed() -> bool:
            return True

    with _platform_client(adapters={"maintenance_gate_reader": ClosedMaintenanceGate()}) as (
        client,
        _runtime,
    ):
        responses = (
            client.get("/"),
            client.get("/login"),
            client.get(f"/static/{_built_asset('*.js')}"),
        )

    for response in responses:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "maintenance_mode"
