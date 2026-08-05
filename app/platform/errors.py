from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .context import current_context
from .persistence import FenceViolation, IdempotencyConflict, LeaseUnavailable


@dataclass(frozen=True, slots=True)
class PlatformError(Exception):
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    status_code: int = 500
    retryable: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", self.code):
            raise ValueError("error code must be a stable snake_case identifier")
        if not isinstance(self.details, Mapping):
            raise TypeError("error details must be an object")
        Exception.__init__(self, self.message)


def _request_id_or_default(request_id: str | None) -> str:
    if request_id and request_id.startswith("req_"):
        return request_id
    context = current_context()
    return context.request_id if context is not None else "req_system"


def error_response(error: PlatformError, *, request_id: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": dict(error.details),
            "request_id": _request_id_or_default(request_id),
        }
    }


def map_exception(exc: BaseException) -> PlatformError:
    if isinstance(exc, PlatformError):
        return exc
    if isinstance(exc, IdempotencyConflict):
        return PlatformError(
            code="idempotency_conflict",
            message="The idempotency key conflicts with a previous request",
            details={},
            status_code=409,
        )
    if isinstance(exc, FenceViolation):
        return PlatformError(
            code="fence_conflict",
            message="The task lease is no longer current",
            details={},
            status_code=409,
        )
    if isinstance(exc, LeaseUnavailable):
        return PlatformError(
            code="lease_unavailable",
            message="The task lease is temporarily unavailable",
            details={},
            status_code=503,
            retryable=True,
        )
    return PlatformError(
        code="internal_error",
        message="An internal error occurred",
        details={},
        status_code=500,
        retryable=False,
    )
