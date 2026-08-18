"""V1 notification read-model API for the current user."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.identity.service import AuthPrincipal
from app.outbox.service import NotificationService

from .dependencies import current_principal

router = APIRouter(prefix="/notifications", tags=["notifications"])


def notification_service(request: Request) -> NotificationService:
    service = request.app.state.platform_runtime.resolve("notification_service")
    if not isinstance(service, NotificationService):
        raise RuntimeError("notification service is not configured")
    return service


@router.get("")
def list_notifications(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[NotificationService, Depends(notification_service)],
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> dict[str, list[dict[str, object]]]:
    return {"items": service.list_notifications(principal.user_id, limit=limit)}


@router.get("/unread-count")
def unread_count(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[NotificationService, Depends(notification_service)],
) -> dict[str, int]:
    return {"count": service.unread_count(principal.user_id)}


@router.post("/{notification_id}/read", status_code=204)
def mark_read(
    notification_id: str,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[NotificationService, Depends(notification_service)],
):
    service.mark_read(principal.user_id, notification_id)
    return None


@router.post("/read-all", status_code=204)
def read_all(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[NotificationService, Depends(notification_service)],
):
    service.read_all(principal.user_id)
    return None


@router.post("/events/{event_id}/ack", status_code=204)
def ack_event(
    event_id: str,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[NotificationService, Depends(notification_service)],
):
    service.ack_event(principal.user_id, event_id)
    return None
