"""Request trace id propagation and structured access logging."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from loguru import logger

from app.observability.metrics import RuntimeMetrics, runtime_metrics

TRACE_ID_HEADER = "X-Trace-Id"

_current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)

AccessLogSink = Callable[[dict[str, Any]], None]
Clock = Callable[[], float]


def get_current_trace_id() -> str | None:
    """Return the trace id bound to the current request context."""

    return _current_trace_id.get()


def emit_access_log(record: dict[str, Any]) -> None:
    """Emit one structured access log record."""

    logger.info(json.dumps(record, ensure_ascii=True, sort_keys=True))


def install_request_context_middleware(
    app: FastAPI,
    *,
    access_log_sink: AccessLogSink = emit_access_log,
    clock: Clock = perf_counter,
    metrics_collector: RuntimeMetrics | None = runtime_metrics,
) -> None:
    """Install trace id propagation and request access logging middleware."""

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        trace_id = request.headers.get(TRACE_ID_HEADER) or str(uuid4())
        token = _current_trace_id.set(trace_id)
        request.state.trace_id = trace_id
        start_time = clock()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_ID_HEADER] = trace_id
            return response
        finally:
            latency_ms = round((clock() - start_time) * 1000, 3)
            if metrics_collector is not None:
                metrics_collector.record_http_request(
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    latency_ms=latency_ms,
                )
            access_log_sink(
                {
                    "event": "http_request",
                    "traceId": trace_id,
                    "method": request.method,
                    "path": request.url.path,
                    "statusCode": status_code,
                    "latencyMs": latency_ms,
                }
            )
            _current_trace_id.reset(token)
