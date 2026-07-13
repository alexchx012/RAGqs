"""Local credential auth endpoints: POST /auth/login, POST /auth/logout, GET /auth/me."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import config
from app.models.response import envelope_json_response
from app.security.auth import LOCAL_SESSION_COOKIE_NAME, AuthContext, get_current_auth_context
from app.security.local_auth_service import LocalAuthError, get_local_auth_service

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(payload: LoginRequest) -> Any:
    try:
        result = get_local_auth_service(config).login(payload.username, payload.password)
    except LocalAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    response = envelope_json_response(
        {
            "user_id": result.user_id,
            "roles": sorted(result.roles),
            "spaces": sorted(result.spaces),
        }
    )
    # Cookie must be set on the response object that is actually returned: FastAPI does not
    # merge headers from an injected `Response` parameter when the endpoint returns a Response
    # instance directly (verified against fastapi==0.136.1's routing.py), so setting the cookie
    # on an injected `response: Response` param here would silently drop it.
    response.set_cookie(
        key=LOCAL_SESSION_COOKIE_NAME,
        value=result.token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=_session_ttl_seconds(),
        secure=_cookie_secure(),
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> Any:
    token = request.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    if token:
        get_local_auth_service(config).logout(token)
    response = envelope_json_response({"logged_out": True})
    response.delete_cookie(LOCAL_SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/auth/me")
async def me(auth_context: AuthContext = Depends(get_current_auth_context)) -> Any:
    return envelope_json_response(
        {
            "user_id": auth_context.user_id,
            "roles": sorted(auth_context.roles),
            "spaces": sorted(auth_context.spaces),
        }
    )


def _session_ttl_seconds() -> int:
    return int(config.auth.session_ttl_seconds)


def _cookie_secure() -> bool:
    return config.deployment.environment.strip().lower() != "local"
