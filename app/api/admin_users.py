"""Administrator user-management HTTP endpoints."""

from __future__ import annotations

from typing import Any, NoReturn, Self

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.models.response import envelope_json_response
from app.security.auth import AuthContext, require_permission
from app.services.admin_user_service import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    AdminUserScopeError,
    AdminUserService,
    AdminUserServiceError,
    AdminUserValidationError,
    AdminUserVersionConflictError,
    LastAdminError,
)

router = APIRouter()

_SAFE_USER_FIELDS = ("id", "username", "roles", "spaces", "department_id", "version", "created_at")


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    roles: list[str]
    spaces: list[str]
    department_id: str | None = None


class UpdateUserRequest(BaseModel):
    expected_version: int = Field(ge=1)
    roles: list[str] | None = None
    spaces: list[str] | None = None
    department_id: str | None = None
    clear_department: bool = False

    @model_validator(mode="after")
    def require_update_fields(self) -> Self:
        if (
            self.roles is None
            and self.spaces is None
            and self.department_id is None
            and not self.clear_department
        ):
            raise ValueError(
                "roles, spaces, department_id, or clear_department must be provided"
            )
        return self


class DeleteUserRequest(BaseModel):
    expected_version: int = Field(ge=1)


def admin_user_service_dependency() -> AdminUserService:
    return AdminUserService()


def _safe_user(user: dict[str, Any]) -> dict[str, Any]:
    return {field: user[field] for field in _SAFE_USER_FIELDS}


def _http_error(error: AdminUserServiceError) -> NoReturn:
    if isinstance(error, AdminUserScopeError):
        status_code = 403
        detail = "administrator scope violation"
    elif isinstance(error, AdminUserValidationError):
        status_code = 422
        detail = "invalid administrator user input"
    elif isinstance(error, AdminUserNotFoundError):
        status_code = 404
        detail = "administrator user not found"
    elif isinstance(error, AdminUserAlreadyExistsError):
        status_code = 409
        detail = "administrator user already exists"
    elif isinstance(error, AdminUserVersionConflictError):
        status_code = 409
        detail = "administrator user version conflict"
    elif isinstance(error, LastAdminError):
        status_code = 409
        detail = "cannot remove last administrator"
    else:
        status_code = 500
        detail = "administrator user operation failed"
    raise HTTPException(status_code=status_code, detail=detail) from error


@router.get("/admin/users")
def list_users(
    auth_context: AuthContext = Depends(require_permission("user:manage")),
    service: AdminUserService = Depends(admin_user_service_dependency),
) -> JSONResponse:
    try:
        users = service.list_users(actor=auth_context)
    except AdminUserServiceError as error:
        _http_error(error)
    return envelope_json_response({"users": [_safe_user(user) for user in users]})


@router.get("/admin/users/{user_id}")
def get_user(
    user_id: str,
    auth_context: AuthContext = Depends(require_permission("user:manage")),
    service: AdminUserService = Depends(admin_user_service_dependency),
) -> JSONResponse:
    try:
        user = service.get_user(user_id, actor=auth_context)
    except AdminUserServiceError as error:
        _http_error(error)
    return envelope_json_response({"user": _safe_user(user)})


@router.post("/admin/users")
def create_user(
    payload: CreateUserRequest,
    auth_context: AuthContext = Depends(require_permission("user:manage")),
    service: AdminUserService = Depends(admin_user_service_dependency),
) -> JSONResponse:
    try:
        user = service.create_user(
            actor=auth_context,
            username=payload.username,
            password=payload.password,
            roles=payload.roles,
            spaces=payload.spaces,
            department_id=payload.department_id,
        )
    except AdminUserServiceError as error:
        _http_error(error)
    return envelope_json_response({"user": _safe_user(user)})


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    auth_context: AuthContext = Depends(require_permission("user:manage")),
    service: AdminUserService = Depends(admin_user_service_dependency),
) -> JSONResponse:
    try:
        user = service.update_user(
            actor=auth_context,
            user_id=user_id,
            expected_version=payload.expected_version,
            roles=payload.roles,
            spaces=payload.spaces,
            department_id=payload.department_id,
            clear_department=payload.clear_department,
        )
    except AdminUserServiceError as error:
        _http_error(error)
    return envelope_json_response({"user": _safe_user(user)})


@router.delete("/admin/users/{user_id}")
def delete_user(
    user_id: str,
    payload: DeleteUserRequest,
    auth_context: AuthContext = Depends(require_permission("user:manage")),
    service: AdminUserService = Depends(admin_user_service_dependency),
) -> JSONResponse:
    try:
        service.delete_user(
            actor=auth_context, user_id=user_id, expected_version=payload.expected_version
        )
    except AdminUserServiceError as error:
        _http_error(error)
    return envelope_json_response({"deleted": True, "user_id": user_id})
