from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.platform.config import load_platform_settings
from app.platform.context import (
    current_context,
    new_request_context,
    task_context,
)
from app.platform.errors import PlatformError, error_response, map_exception
from app.platform.runtime import PlatformRuntime, build_runtime


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


def test_request_context_always_uses_server_generated_request_id() -> None:
    context = new_request_context(request_id="req_client_supplied", trace_id="invalid trace")

    assert context.request_id.startswith("req_")
    assert context.request_id != "req_client_supplied"
    assert context.trace_id != "invalid trace"
    assert context.started_at_utc.tzinfo == UTC


def test_task_context_is_visible_inside_nested_scope() -> None:
    request = new_request_context()
    task = task_context(
        request,
        task_id="job-1",
        lease_owner="worker-1",
        fence_token=4,
        deadline_utc=datetime.now(UTC),
    )

    assert current_context() is None
    with task:
        assert current_context() == task
    assert current_context() is None


def test_platform_error_has_stable_request_error_shape() -> None:
    error = PlatformError(
        code="validation_error",
        message="Invalid input",
        details={"field": "name"},
        status_code=422,
    )

    assert error_response(error, request_id="req_123") == {
        "error": {
            "code": "validation_error",
            "message": "Invalid input",
            "details": {"field": "name"},
            "request_id": "req_123",
        }
    }


def test_platform_error_rejects_non_snake_case_codes() -> None:
    with pytest.raises(ValueError, match="snake_case"):
        PlatformError(code="CamelCase", message="invalid")


def test_platform_error_propagates_out_of_a_sqlalchemy_transaction() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with pytest.raises(PlatformError) as exc_info:
        with engine.begin():
            raise PlatformError("validation_error", "Invalid input", {}, 422)

    assert exc_info.value.code == "validation_error"


def test_unknown_exception_is_mapped_without_internal_text() -> None:
    mapped = map_exception(RuntimeError("database password=secret"))

    assert mapped.code == "internal_error"
    assert mapped.status_code == 500
    assert "password" not in mapped.message
    assert "secret" not in repr(error_response(mapped, request_id="req_123"))


def test_runtime_is_app_scoped_and_closes_owned_resources() -> None:
    first = build_runtime(settings(), adapters={"marker": object()})
    second = build_runtime(settings())

    assert isinstance(first, PlatformRuntime)
    assert first is not second
    assert first.resolve("marker") is not None
    first.close()
    assert first.closed is True
