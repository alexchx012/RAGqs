from __future__ import annotations

from contextlib import AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any

_CURRENT_CONTEXT: ContextVar[RequestContext | TaskContext | None] = ContextVar(
    "platform_current_context", default=None
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _request_id() -> str:
    return f"req_{token_hex(12)}"


def _trace_id() -> str:
    return f"trace_{token_hex(16)}"


@dataclass(frozen=True, slots=True)
class RequestContext(AbstractContextManager["RequestContext"]):
    request_id: str
    trace_id: str
    started_at_utc: datetime
    deadline_utc: datetime | None = None
    _token: Token[RequestContext | TaskContext | None] | None = None

    def __enter__(self) -> RequestContext:
        object.__setattr__(self, "_token", _CURRENT_CONTEXT.set(self))
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._token is not None:
            _CURRENT_CONTEXT.reset(self._token)


@dataclass(frozen=True, slots=True)
class TaskContext(AbstractContextManager["TaskContext"]):
    request_id: str
    trace_id: str
    started_at_utc: datetime
    deadline_utc: datetime
    task_id: str
    lease_owner: str
    fence_token: int
    _token: Token[RequestContext | TaskContext | None] | None = None

    def __enter__(self) -> TaskContext:
        object.__setattr__(self, "_token", _CURRENT_CONTEXT.set(self))
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._token is not None:
            _CURRENT_CONTEXT.reset(self._token)


def new_request_context(
    *,
    started_at_utc: datetime | None = None,
    deadline_utc: datetime | None = None,
) -> RequestContext:
    """Create server-owned identifiers; client-provided values are never trusted."""

    started = started_at_utc or _utc_now()
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return RequestContext(
        request_id=_request_id(),
        trace_id=_trace_id(),
        started_at_utc=started.astimezone(UTC),
        deadline_utc=deadline_utc,
    )


def task_context(
    request: RequestContext,
    *,
    task_id: str,
    lease_owner: str,
    fence_token: int,
    deadline_utc: datetime,
) -> TaskContext:
    if deadline_utc.tzinfo is None:
        deadline_utc = deadline_utc.replace(tzinfo=UTC)
    return TaskContext(
        request_id=request.request_id,
        trace_id=request.trace_id,
        started_at_utc=request.started_at_utc,
        deadline_utc=deadline_utc.astimezone(UTC),
        task_id=task_id,
        lease_owner=lease_owner,
        fence_token=fence_token,
    )


def current_context() -> RequestContext | TaskContext | None:
    return _CURRENT_CONTEXT.get()
