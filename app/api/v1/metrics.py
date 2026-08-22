"""V1 retention read-model API: dashboard, operations metrics and ops jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.retention.service import RetentionOpsService

from .dependencies import current_principal

router = APIRouter(tags=["metrics"])

_WINDOWS = ("today", "7d", "30d")
_VIEWS = ("all", "active", "replayable", "stale")


def retention_service(request: Request) -> RetentionOpsService:
    service = request.app.state.platform_runtime.resolve("retention_ops")
    if not isinstance(service, RetentionOpsService):
        raise RuntimeError("retention ops service is not configured")
    return service


def _require_metrics_reader(principal: AuthPrincipal) -> None:
    if principal.role not in {"ops", "admin"}:
        raise PlatformError(
            "metrics_forbidden",
            "Metrics access requires an ops or admin principal",
            {},
            403,
        )


def _require_ops_jobs_reader(principal: AuthPrincipal) -> None:
    if principal.role not in {"ops", "admin"}:
        raise PlatformError(
            "ops_jobs_forbidden",
            "Ops jobs access requires an ops or admin principal",
            {},
            403,
        )


def _window(value: str | None) -> str:
    window = value or "7d"
    if window not in _WINDOWS:
        raise PlatformError(
            "validation_error",
            "window must be today, 7d or 30d",
            {"field": "window"},
            422,
        )
    return window


def _view(value: str | None) -> str:
    view = value or "all"
    if view not in _VIEWS:
        raise PlatformError(
            "validation_error",
            "view must be all, active, replayable or stale",
            {"field": "view"},
            422,
        )
    return view


def _expand(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if value != "user_rank":
        raise PlatformError(
            "validation_error",
            "expand must be user_rank",
            {"field": "expand"},
            422,
        )
    return value


@router.get("/metrics/dashboard")
def metrics_dashboard(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    window: Annotated[str | None, Query()] = None,
    expand: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    _require_metrics_reader(principal)
    return retention_service(request).dashboard(
        role=principal.role,
        window=_window(window),
        expand=_expand(expand),
    )


@router.get("/metrics/operations")
def metrics_operations(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    window: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    _require_metrics_reader(principal)
    return retention_service(request).operations(window=_window(window))


@router.get("/ops/jobs")
def ops_jobs(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    view: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    _require_ops_jobs_reader(principal)
    return retention_service(request).ops_jobs(principal=principal, view=_view(view))
