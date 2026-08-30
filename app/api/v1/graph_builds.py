"""V1 ops API for the public graph build control plane.

The only V1 contract: GET /ops/graph-builds/current,
POST /ops/graph-builds and POST /ops/graph-builds/{graph_build_id}/cancel.
There is no build-history list, no admin bypass, no synchronous build and no
legacy graph API.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key

from .dependencies import current_principal
from .ops import require_ops_role

router = APIRouter(prefix="/ops/graph-builds", tags=["ops"])


def require_graph_builds_ops(principal: AuthPrincipal) -> None:
    """Graph-build control plane keeps a dedicated 403 code (后端设计 §2.6.1)."""
    require_ops_role(
        principal,
        error_code="graph_build_forbidden",
        message="Graph build ops access is required",
    )


# The operation-id contract is deliberately stricter than the platform-wide
# idempotency limit: gb_create_<key> and gb_cancel_<key> remain within the
# graph domain's public 64-character operation identifier contract.
GRAPH_IDEMPOTENCY_KEY_MAX_LENGTH = 54


def graph_service(request: Request):
    service = request.app.state.platform_runtime.resolve("graph_build_service")
    if service is None:
        raise PlatformError(
            "graph_build_unavailable", "The graph build service is not configured", {}, 503
        )
    return service


class GraphBuildCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_source_revision: int = Field(ge=0)


class GraphBuildCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


def _request_hash(payload: dict[str, object], idempotency_key: str) -> str:
    encoded = json.dumps(
        {**payload, "idempotency_key": idempotency_key},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(b"graph-build-v1\0" + encoded.encode("utf-8")).hexdigest()


def _graph_idempotency_key(value: str | None) -> str:
    key = validate_idempotency_key(value)
    if len(key) > GRAPH_IDEMPOTENCY_KEY_MAX_LENGTH:
        raise PlatformError(
            "validation_error",
            "Idempotency-Key is too long",
            {"max_length": GRAPH_IDEMPOTENCY_KEY_MAX_LENGTH},
            422,
        )
    return key


@router.get("/current")
def graph_build_current(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
) -> dict[str, object]:
    require_graph_builds_ops(principal)
    return graph_service(request).current()


@router.post("", status_code=202)
def graph_build_create(
    body: GraphBuildCreateRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    require_graph_builds_ops(principal)
    key = _graph_idempotency_key(idempotency_key)
    view = graph_service(request).create(
        initiator_identity_id=principal.user_id,
        expected_source_revision=body.expected_source_revision,
        idempotency_key=key,
        request_hash=_request_hash(
            {"expected_source_revision": body.expected_source_revision}, key
        ),
    )
    return JSONResponse(view.to_dict(), status_code=202)


@router.post("/{graph_build_id}/cancel", status_code=202)
def graph_build_cancel(
    graph_build_id: str,
    body: GraphBuildCancelRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    require_graph_builds_ops(principal)
    key = _graph_idempotency_key(idempotency_key)
    view = graph_service(request).cancel(
        actor_identity_id=principal.user_id,
        graph_build_id=graph_build_id,
        expected_version=body.expected_version,
        idempotency_key=key,
        request_hash=_request_hash(
            {"graph_build_id": graph_build_id, "expected_version": body.expected_version},
            key,
        ),
    )
    return JSONResponse(view.to_dict(), status_code=202)


__all__ = ["GRAPH_IDEMPOTENCY_KEY_MAX_LENGTH", "router"]
