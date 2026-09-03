from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.platform.context import current_context
from app.platform.errors import PlatformError

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    context = current_context()
    settings = request.app.state.platform_runtime.settings
    return {
        "status": "ok",
        "service": "core-platform",
        "version": request.app.version,
        "release_id": settings.release_id,
        "request_id": context.request_id if context is not None else "req_system",
    }


@router.get("/ready")
def ready(request: Request) -> dict[str, str]:
    """Readiness probe: verify the load-bearing dependencies answer.

    Liveness (``/v1/health``) stays dependency-free. ``/v1/ready`` runs a
    ``SELECT 1`` against the platform database and an object-storage probe;
    either failure keeps the workload out of service with a retryable 503.
    """

    runtime = request.app.state.platform_runtime
    engine = runtime.resolve("database_engine")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise PlatformError(
            "dependency_unavailable",
            "The database readiness check failed",
            {},
            503,
            True,
        ) from exc
    object_store = runtime.resolve("object_store")
    exists = getattr(object_store, "exists", None)
    if not callable(exists):
        raise RuntimeError("object storage adapter is not configured")
    try:
        exists("readiness/probe")
    except Exception as exc:
        raise PlatformError(
            "dependency_unavailable",
            "The object storage readiness check failed",
            {},
            503,
            True,
        ) from exc
    return {
        "status": "ready",
        "service": "core-platform",
        "version": request.app.version,
        "release_id": request.app.state.platform_runtime.settings.release_id,
        "database": "ok",
        "object_storage": "ok",
    }
