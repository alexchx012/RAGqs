from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.identity.service import AuthPrincipal, IdentityAccessService

from .dependencies import current_principal, identity_access_service

router = APIRouter(tags=["spaces"])


@router.get("/spaces")
def list_spaces(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
    usage: Annotated[Literal["retrieval", "upload", "manage"], Query()] = "manage",
) -> dict[str, list[dict[str, object]]]:
    return {"items": service.list_spaces(principal=principal, usage=usage)}
