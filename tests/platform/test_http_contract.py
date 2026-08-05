from __future__ import annotations

from typing import Annotated

from fastapi import Query
from fastapi.testclient import TestClient

from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.errors import PlatformError, map_exception
from app.platform.http_contract import (
    batch_item_error,
    paginated_response,
    parse_idempotency_key,
    parse_if_match,
    request_error_payload,
    sse_error_event,
)


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
    assert sse_error_event(error, "req_123") == (
        'event: error\ndata: {"code":"validation_error","message":"Invalid input",'
        '"details":{"field":"name"},"request_id":"req_123"}\n\n'
    )


def test_idempotency_key_parser_does_not_accept_empty_values() -> None:
    assert parse_idempotency_key({"Idempotency-Key": "abc"}) == "abc"
    assert parse_idempotency_key({"idempotency-key": "  "}) is None
    assert parse_idempotency_key({}) is None


def test_conditional_and_pagination_helpers_keep_v1_contract_shape() -> None:
    assert parse_if_match({"If-Match": '"version-3"'}) == "version-3"
    assert parse_if_match({"if-match": "  "}) is None
    assert paginated_response([{"id": "doc-1"}], total=3, page=2, page_size=1) == {
        "items": [{"id": "doc-1"}],
        "total": 3,
        "page": 2,
        "page_size": 1,
    }


def test_fastapi_validation_http_and_internal_errors_use_contract() -> None:
    app = create_platform_app(settings())

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
