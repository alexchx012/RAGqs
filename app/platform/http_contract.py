from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .context import current_context
from .errors import PlatformError, map_exception

IDEMPOTENCY_KEY_MAX_LENGTH = 256


def _request_id() -> str:
    context = current_context()
    return context.request_id if context is not None else "req_system"


def request_error_payload(error: PlatformError, request_id: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": dict(error.details),
            "request_id": (
                request_id if request_id and request_id.startswith("req_") else _request_id()
            ),
        }
    }


def compatibility_error_payload(
    error: PlatformError, request_id: str | None = None
) -> dict[str, Any]:
    """Return the legacy ``/chat`` error envelope without changing SSE errors."""

    payload = request_error_payload(error, request_id)
    return {
        "code": error.status_code,
        "message": error.message,
        "data": payload,
        "errorMessage": error.message,
    }


def batch_item_error(error: PlatformError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": dict(error.details),
        }
    }


def validate_idempotency_key(value: str | None) -> str:
    if not value or not value.strip():
        raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
    normalized = value.strip()
    if len(normalized) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise PlatformError(
            "validation_error",
            "Idempotency-Key is too long",
            {"max_length": IDEMPOTENCY_KEY_MAX_LENGTH},
            422,
        )
    return normalized


def paginated_response(
    items: list[Any], *, total: int, page: int, page_size: int
) -> dict[str, Any]:
    if total < 0 or page < 1 or page_size < 1:
        raise ValueError(
            "pagination values must be non-negative with page and page_size starting at one"
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _validation_error(exc: RequestValidationError) -> PlatformError:
    fields = []
    for item in exc.errors():
        location = [str(part) for part in item.get("loc", ()) if part != "body"]
        fields.append({"loc": location, "code": str(item.get("type", "invalid"))})
    return PlatformError(
        code="validation_error",
        message="Request validation failed",
        details={"fields": fields},
        status_code=422,
    )


def _http_error(exc: StarletteHTTPException) -> PlatformError:
    code_by_status = {
        400: "bad_request",
        401: "authentication_required",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
    }
    message_by_status = {
        400: "The request is invalid",
        401: "Authentication is required",
        403: "The operation is not allowed",
        404: "The requested resource was not found",
        409: "The request conflicts with current state",
        422: "Request validation failed",
        429: "Too many requests",
    }
    return PlatformError(
        code=code_by_status.get(exc.status_code, "http_error"),
        message=message_by_status.get(exc.status_code, "The request could not be completed"),
        details={},
        status_code=exc.status_code,
        retryable=exc.status_code == 429,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlatformError)
    async def handle_platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        payload = (
            compatibility_error_payload(exc)
            if request.url.path == "/v1/chat"
            else request_error_payload(exc)
        )
        return JSONResponse(payload, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        error = _validation_error(exc)
        payload = (
            compatibility_error_payload(error)
            if request.url.path == "/v1/chat"
            else request_error_payload(error)
        )
        return JSONResponse(payload, status_code=error.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = _http_error(exc)
        payload = (
            compatibility_error_payload(error)
            if request.url.path == "/v1/chat"
            else request_error_payload(error)
        )
        return JSONResponse(payload, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        error = map_exception(exc)
        payload = (
            compatibility_error_payload(error)
            if request.url.path == "/v1/chat"
            else request_error_payload(error)
        )
        return JSONResponse(payload, status_code=error.status_code)
