from __future__ import annotations

from typing import Annotated

from fastapi import Header, Request

from app.identity.service import AuthPrincipal, IdentityAccessService, SessionActionPrincipal
from app.platform.errors import PlatformError


def identity_access_service(request: Request) -> IdentityAccessService:
    service = request.app.state.platform_runtime.resolve("identity_access")
    if not isinstance(service, IdentityAccessService):
        raise RuntimeError("identity access service is not configured")
    return service


def outbox_dispatcher(request: Request):
    from app.outbox.dispatcher import OutboxDispatcher

    dispatcher = request.app.state.platform_runtime.resolve("outbox_dispatcher")
    if not isinstance(dispatcher, OutboxDispatcher):
        raise RuntimeError("outbox dispatcher is not configured")
    return dispatcher


def quota_request_service(request: Request):
    from app.usage.requests import QuotaRequestService

    service = request.app.state.platform_runtime.resolve("quota_request_service")
    if not isinstance(service, QuotaRequestService):
        raise RuntimeError("quota request service is not configured")
    return service


def require_streaming(accept: str | None) -> None:
    if accept is None:
        return
    accepted = [part.strip() for part in accept.split(",")]
    if not any(part == "*/*" or part.startswith("text/event-stream") for part in accepted):
        raise PlatformError(
            "streaming_response_required",
            "This endpoint only returns text/event-stream",
            {},
            406,
        )


def require_ops_role(principal: AuthPrincipal, *, error_code: str, message: str) -> None:
    if principal.role != "ops":
        raise PlatformError(error_code, message, {}, 403)


def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthPrincipal:
    if authorization is None or not authorization.startswith("Bearer "):
        raise PlatformError("authentication_required", "Access token is required", {}, 401)
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise PlatformError("authentication_required", "Access token is required", {}, 401)
    return identity_access_service(request).authenticate_access_token(token)


def session_action_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> SessionActionPrincipal:
    if authorization is None or not authorization.startswith("Bearer "):
        raise PlatformError("authentication_required", "Access token is required", {}, 401)
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise PlatformError("authentication_required", "Access token is required", {}, 401)
    return identity_access_service(request).authenticate_session_action_token(token)
