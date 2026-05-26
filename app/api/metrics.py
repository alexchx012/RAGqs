"""Runtime metrics API."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.models.response import success_envelope
from app.observability.metrics import (
    RuntimeMetrics,
    render_prometheus_metrics,
    runtime_metrics,
)


def create_metrics_router(metrics_collector: RuntimeMetrics | None = None) -> APIRouter:
    """Create a router exposing the current in-process metrics snapshot."""

    router = APIRouter()
    active_metrics = metrics_collector or runtime_metrics

    @router.get("/metrics")
    async def get_metrics():
        return success_envelope(active_metrics.snapshot())

    @router.get("/metrics/prometheus", response_class=PlainTextResponse)
    async def get_prometheus_metrics():
        return render_prometheus_metrics(active_metrics.snapshot())

    return router


router = create_metrics_router()
