from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.identity.service import (
    AuthPrincipal,
    AuthResult,
    IdentityAccessService,
    SessionActionPrincipal,
)
from app.platform.config import PlatformSettings

from .dependencies import current_principal, identity_access_service, session_action_principal

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=256)


class PreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: str
    chat_font_size: str
    ab_opt_out: bool


class PasswordUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


def _settings(request: Request) -> PlatformSettings:
    return request.app.state.platform_runtime.settings


def _set_auth_cookies(response: Response, result: AuthResult, settings: PlatformSettings) -> None:
    secure = settings.profile == "production"
    response.set_cookie(
        "refresh_token",
        result.refresh_token,
        max_age=settings.auth.refresh_ttl_seconds,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        "csrf_token",
        result.csrf_token,
        max_age=settings.auth.refresh_ttl_seconds,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def _clear_auth_cookies(response: Response, settings: PlatformSettings) -> None:
    secure = settings.profile == "production"
    response.delete_cookie("refresh_token", path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie("csrf_token", path="/", secure=secure, httponly=False, samesite="lax")


@router.post("/auth/login")
def login(
    body: LoginRequest,
    request: Request,
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> JSONResponse:
    result = service.login(
        username=body.username,
        password=body.password,
        device=request.headers.get("User-Agent"),
    )
    response = JSONResponse({"token": result.access_token, "user": result.user})
    _set_auth_cookies(response, result, _settings(request))
    return response


@router.post("/auth/refresh")
def refresh(
    request: Request,
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> JSONResponse:
    result = service.refresh(
        refresh_token=request.cookies.get("refresh_token"),
        csrf_cookie=request.cookies.get("csrf_token"),
        csrf_header=request.headers.get("X-CSRF-Token"),
        origin=request.headers.get("Origin"),
    )
    response = JSONResponse({"token": result.access_token})
    _set_auth_cookies(response, result, _settings(request))
    return response


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    principal: Annotated[SessionActionPrincipal, Depends(session_action_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> Response:
    service.revoke_session_for_action(
        principal=principal,
        session_id=principal.auth_session_id,
        reason="user_logout",
    )
    response = Response(status_code=204)
    _clear_auth_cookies(response, _settings(request))
    return response


@router.get("/auth/me")
def me(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.user_response(principal.user_id)


@router.get("/auth/sessions")
def sessions(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, list[dict[str, object]]]:
    return {
        "items": service.list_sessions(
            user_id=principal.user_id,
            current_session_id=principal.auth_session_id,
        )
    }


@router.delete("/auth/sessions/{session_id}", status_code=204)
def revoke_session(
    session_id: str,
    request: Request,
    principal: Annotated[SessionActionPrincipal, Depends(session_action_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> Response:
    service.revoke_session_for_action(
        principal=principal,
        session_id=session_id,
        reason="device_revoked",
    )
    response = Response(status_code=204)
    if session_id == principal.auth_session_id:
        _clear_auth_cookies(response, _settings(request))
    return response


@router.delete("/auth/sessions", status_code=204)
def revoke_all_sessions(
    request: Request,
    principal: Annotated[SessionActionPrincipal, Depends(session_action_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> Response:
    service.revoke_all_sessions_for_action(principal=principal)
    response = Response(status_code=204)
    _clear_auth_cookies(response, _settings(request))
    return response


@router.patch("/users/me/profile")
def update_profile(
    body: ProfileUpdateRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.update_profile(user_id=principal.user_id, display_name=body.display_name)


@router.post("/users/me/avatar")
async def replace_avatar(
    file: Annotated[UploadFile, File()],
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, str]:
    return service.replace_avatar(
        user_id=principal.user_id,
        content=await file.read(),
        content_type=file.content_type or "application/octet-stream",
    )


@router.get("/users/me/preferences")
def preferences(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.get_preferences(user_id=principal.user_id)


@router.put("/users/me/preferences")
def replace_preferences(
    body: PreferencesRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.replace_preferences(user_id=principal.user_id, preferences=body.model_dump())


@router.put("/users/me/password", status_code=204)
def update_password(
    body: PasswordUpdateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> Response:
    service.change_password(
        user_id=principal.user_id,
        old_password=body.old_password,
        new_password=body.new_password,
    )
    response = Response(status_code=204)
    _clear_auth_cookies(response, _settings(request))
    return response
