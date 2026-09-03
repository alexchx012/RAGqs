from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Query
from fastapi.testclient import TestClient

from app.identity.schema import identity_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError, map_exception
from app.platform.http_contract import (
    batch_item_error,
    paginated_response,
    request_error_payload,
)
from app.usage.schema import usage_metadata


def settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )


def test_request_and_batch_error_shapes_are_stable() -> None:
    error = PlatformError("validation_error", "Invalid input", {"field": "name"}, 422)

    assert request_error_payload(error, "req_123") == {
        "error": {
            "code": "validation_error",
            "message": "Invalid input",
            "details": {"field": "name"},
            "request_id": "req_123",
        }
    }
    assert batch_item_error(error) == {
        "error": {
            "code": "validation_error",
            "message": "Invalid input",
            "details": {"field": "name"},
        }
    }


def test_every_response_carries_nosniff_and_frame_options_headers() -> None:
    app = create_platform_app(settings())
    engine = app.state.platform_runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)

    with TestClient(app) as client:
        health = client.get("/v1/health")
        unknown = client.get("/v1/not-registered")

    assert health.status_code == 200
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert unknown.status_code == 404
    assert unknown.headers["x-content-type-options"] == "nosniff"
    assert unknown.headers["x-frame-options"] == "DENY"


def test_pagination_helpers_keep_v1_contract_shape() -> None:
    assert paginated_response([{"id": "doc-1"}], total=3, page=2, page_size=1) == {
        "items": [{"id": "doc-1"}],
        "total": 3,
        "page": 2,
        "page_size": 1,
    }


def test_fastapi_validation_http_and_internal_errors_use_contract() -> None:
    app = create_platform_app(settings())
    engine = app.state.platform_runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)

    @app.get("/v1/test-error")
    async def test_error() -> None:
        raise PlatformError("forbidden", "Not allowed", {}, 403)

    @app.get("/v1/test-validation")
    async def test_validation(count: Annotated[int, Query(ge=1)]) -> dict[str, int]:
        return {"count": count}

    @app.get("/v1/test-internal")
    async def test_internal() -> None:
        raise RuntimeError("password=secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        forbidden = client.get("/v1/test-error")
        validation = client.get("/v1/test-validation", params={"count": "0"})
        internal = client.get("/v1/test-internal")

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"
    assert validation.status_code == 422
    assert validation.json()["error"]["code"] == "validation_error"
    assert isinstance(validation.json()["error"]["details"], dict)
    assert internal.status_code == 500
    assert "secret" not in internal.text
    assert internal.json()["error"]["code"] == "internal_error"


def test_unexpected_error_keeps_request_id_and_logs_it_while_context_is_active(caplog) -> None:
    app = create_platform_app(settings())
    engine = app.state.platform_runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)

    @app.get("/v1/correlated-internal")
    async def correlated_internal() -> None:
        raise RuntimeError("password=secret")

    with caplog.at_level(logging.ERROR, logger="app.platform.app_factory"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/v1/correlated-internal")

    request_id = response.headers["x-request-id"]
    assert request_id.startswith("req_")
    assert response.status_code == 500
    assert response.json()["error"]["request_id"] == request_id
    assert "password=secret" not in response.text
    records = [
        record
        for record in caplog.records
        if record.name == "app.platform.app_factory"
        and record.getMessage() == "Unhandled request exception"
    ]
    assert records[-1].request_id == request_id


def test_shared_platform_conflicts_have_stable_error_codes() -> None:
    from app.platform.persistence import FenceViolation, IdempotencyConflict, LeaseUnavailable

    assert map_exception(IdempotencyConflict("key")) == PlatformError(
        "idempotency_conflict",
        "The idempotency key conflicts with a previous request",
        {},
        409,
    )
    assert map_exception(FenceViolation("fence")).code == "fence_conflict"
    assert map_exception(LeaseUnavailable("lease")).code == "lease_unavailable"


def test_platform_app_applies_configured_log_level() -> None:
    root = logging.getLogger()
    previous_level = root.level
    previous_handlers = list(root.handlers)
    try:
        app = create_platform_app(
            load_platform_settings(
                {
                    "RAG_PLATFORM_PROFILE": "development",
                    "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                    "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                    "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                    "RAG_PROVIDER_NAME": "fake",
                    "RAG_LOG_LEVEL": "DEBUG",
                }
            )
        )
        app.state.platform_runtime.close()
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(previous_level)
        root.handlers[:] = previous_handlers
