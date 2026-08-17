"""V1 ops API for outbox delivery inspection and manual replay."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key

from .dependencies import current_principal

router = APIRouter(prefix="/ops", tags=["ops"])

def dispatcher(request: Request):
    from app.outbox.dispatcher import OutboxDispatcher

    dispatcher = request.app.state.platform_runtime.resolve("outbox_dispatcher")
    if not isinstance(dispatcher, OutboxDispatcher):
        raise RuntimeError("outbox dispatcher is not configured")
    return dispatcher


def require_ops(principal: AuthPrincipal) -> None:
    if principal.role != "ops":
        raise PlatformError("forbidden", "Ops access is required", {}, 403)


class DeliveryReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_name: Literal["in_app_notification"] = "in_app_notification"
    expected_version: int = Field(ge=1)


def _replay_request_hash(
    body: DeliveryReplayRequest,
    idempotency_key: str,
) -> str:
    encoded = json.dumps(
        {
            "consumer_name": body.consumer_name,
            "expected_version": body.expected_version,
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(b"outbox-replay-v1\0" + encoded.encode("utf-8")).hexdigest()


@router.get("/outbox-deliveries/{event_id}")
def outbox_delivery_view(
    event_id: str,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    consumer_name: Annotated[Literal["in_app_notification"], Query()] = "in_app_notification",
) -> dict[str, object]:
    require_ops(principal)
    view = dispatcher(request).ops_view(event_id, consumer_name=consumer_name)
    if view is None:
        raise PlatformError("not_found", "Delivery was not found", {}, 404)
    return {
        "event_id": view.event_id,
        "consumer_name": view.consumer_name,
        "status": view.status,
        "version": view.version,
        "replay_generation": view.replay_generation,
        "attempt_number": view.attempt_number,
        "error": (
            {"category": view.error_category, "code": view.error_code}
            if view.error_category is not None
            else None
        ),
        "replayable": view.replayable,
        "next_attempt_at": view.next_attempt_at,
        "lease_expires_at": view.lease_expires_at,
    }


@router.post("/outbox-deliveries/{event_id}/replay", status_code=202)
def outbox_delivery_replay(
    event_id: str,
    body: DeliveryReplayRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    require_ops(principal)
    key = validate_idempotency_key(idempotency_key)
    receipt = dispatcher(request).replay(
        event_id,
        consumer_name=body.consumer_name,
        expected_version=body.expected_version,
        idempotency_key=key,
        request_hash=_replay_request_hash(body, key),
    )
    return JSONResponse(
        {
            "event_id": receipt.event_id,
            "consumer_name": receipt.consumer_name,
            "status": receipt.status,
            "replay_generation": receipt.replay_generation,
            "version": receipt.version,
        },
        status_code=202,
    )
