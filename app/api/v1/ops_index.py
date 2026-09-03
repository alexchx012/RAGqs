"""V1 ops API for index generation rollback (design §4.9 代际回滚 / §7.4.1).

Rollback itself stays owned by ``IndexGenerationRepository.rollback``: the
endpoint only forwards the candidate identity and validates the ops-only
contract (current_principal + require_ops, ``extra="forbid"`` request body,
required ``Idempotency-Key``). Window, catch-up and source-receipt checks keep
their repository error codes (409 ``rollback_not_eligible`` /
``release_gate_failed``); the graph-coordination rollback path is untouched.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.identity.service import AuthPrincipal
from app.platform.http_contract import validate_idempotency_key

from .dependencies import current_principal, index_generation_manager, require_ops_role

router = APIRouter(prefix="/ops", tags=["ops"])


class IndexGenerationRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_generation_id: str = Field(min_length=1, max_length=128)
    current_revision: int | None = Field(default=None, ge=0)
    source_receipt: dict[str, Any] | None = None


@router.post("/index-generations/rollback")
def rollback_index_generation(
    body: IndexGenerationRollbackRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    require_ops_role(principal, error_code="forbidden", message="Ops access is required")
    validate_idempotency_key(idempotency_key)
    generation = index_generation_manager(request).rollback(
        body.candidate_generation_id,
        current_revision=body.current_revision,
        source_receipt=body.source_receipt,
    )
    return JSONResponse(
        {
            "generation_id": generation.generation_id,
            "status": str(generation.status),
            "applied_revision": generation.applied_revision,
            "rollback_applied_revision": generation.rollback_applied_revision,
        }
    )
