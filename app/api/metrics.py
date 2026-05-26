"""Runtime metrics API."""

from __future__ import annotations

from fastapi import APIRouter

from app.observability.metrics import RuntimeMetrics, runtime_metrics


def create_metrics_router(metrics_collector: RuntimeMetrics | None = None) -> APIRouter:
    """Create a router exposing the current in-process metrics snapshot."""

    router = APIRouter()
    active_metrics = metrics_collector or runtime_metrics

    @router.get("/metrics")
    async def get_metrics():
        return {
            "code": 200,
            "message": "success",
            "data": active_metrics.snapshot(),
        }

    return router


router = create_metrics_router()
