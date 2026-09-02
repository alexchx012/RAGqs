"""V1 evaluation & calibration HTTP API."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key

from .dependencies import current_principal, require_ops_role

router = APIRouter(tags=["evaluation"])


def evaluation_service(request: Request):
    service = request.app.state.platform_runtime.resolve("evaluation_service")
    if service is None:
        raise PlatformError(
            "evaluation_unavailable",
            "The evaluation service is not configured",
            {"retryable": True},
            503,
            True,
        )
    return service


def require_ops(principal: AuthPrincipal) -> None:
    require_ops_role(
        principal,
        error_code="evaluation_run_forbidden",
        message="The evaluation:run capability is required",
    )


class ShadowRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str


class CalibrationWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["open", "close"]
    window_kind: Literal["cold_start", "sentinel", "manual"] | None = None


# ------------------------------------------------------------------- shadow runs


@router.post("/admin/evaluations/shadow-runs", status_code=202)
def create_shadow_run(
    body: ShadowRunCreateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    require_ops(principal)
    key = validate_idempotency_key(idempotency_key)
    response = evaluation_service(request).create_shadow_run(
        actor=principal,
        space_id=body.space_id,
        idempotency_key=key,
    )
    return JSONResponse(response, status_code=202)


@router.get("/admin/evaluations/shadow-runs/{run_id}")
def get_shadow_run(
    run_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    if principal.role not in {"ops", "admin"}:
        raise PlatformError(
            "evaluation_run_forbidden",
            "The evaluation:run read capability is required",
            {},
            403,
        )
    return evaluation_service(request).get_shadow_run(run_id, actor=principal).to_json()


# --------------------------------------------------------------------- leaderboard


@router.get("/evaluation/leaderboard")
def leaderboard(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    return evaluation_service(request).leaderboard(actor=principal)


# ----------------------------------------------------------------------- window


@router.get("/calibration/window")
def read_window(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    return evaluation_service(request).read_window(actor=principal)


@router.post("/calibration/window")
def write_window(
    body: CalibrationWindowRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    if principal.role != "ops":
        raise PlatformError(
            "calibration_window_forbidden",
            "Calibration window writes require ops",
            {},
            403,
        )
    key = validate_idempotency_key(idempotency_key)
    service = evaluation_service(request)
    if body.action == "open":
        response, status = service.open_window(
            actor=principal,
            action=body.action,
            window_kind=body.window_kind,
            idempotency_key=key,
        )
    else:
        response, status = service.close_window(actor=principal, idempotency_key=key)
    return JSONResponse(response, status_code=status)
