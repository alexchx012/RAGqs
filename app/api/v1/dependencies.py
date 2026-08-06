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
