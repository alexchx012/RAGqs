"""Administrator department-management HTTP endpoints."""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.models.response import envelope_json_response
from app.security.auth import AuthContext, require_permission
from app.services.department_service import (
    AdminDepartmentAlreadyExistsError,
    AdminDepartmentNotEmptyError,
    AdminDepartmentNotFoundError,
    AdminDepartmentScopeError,
    AdminDepartmentServiceError,
    AdminDepartmentValidationError,
    DepartmentService,
)

router = APIRouter()

_SAFE_DEPARTMENT_FIELDS = ("id", "name", "description", "created_at")


class CreateDepartmentRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None


class UpdateDepartmentRequest(BaseModel):
    name: str | None = None
    description: str | None = None


def admin_department_service_dependency() -> DepartmentService:
    return DepartmentService()


def _safe_department(department: dict[str, Any]) -> dict[str, Any]:
    return {field: department[field] for field in _SAFE_DEPARTMENT_FIELDS}


def _http_error(error: AdminDepartmentServiceError) -> NoReturn:
    if isinstance(error, AdminDepartmentScopeError):
        status_code = 403
        detail = "only super_admin can manage departments"
    elif isinstance(error, AdminDepartmentValidationError):
        status_code = 422
        detail = "invalid department input"
    elif isinstance(error, AdminDepartmentNotFoundError):
        status_code = 404
        detail = "department not found"
    elif isinstance(error, AdminDepartmentAlreadyExistsError):
        status_code = 409
        detail = "department name already exists"
    elif isinstance(error, AdminDepartmentNotEmptyError):
        status_code = 409
        detail = "department still has member users"
    else:
        status_code = 500
        detail = "administrator department operation failed"
    raise HTTPException(status_code=status_code, detail=detail) from error


@router.get("/admin/departments")
def list_departments(
    auth_context: AuthContext = Depends(require_permission("department:manage")),
    service: DepartmentService = Depends(admin_department_service_dependency),
) -> JSONResponse:
    del auth_context
    try:
        departments = service.list_departments()
    except AdminDepartmentServiceError as error:
        _http_error(error)
    return envelope_json_response(
        {"departments": [_safe_department(d) for d in departments]}
    )


@router.get("/admin/departments/{department_id}")
def get_department(
    department_id: str,
    auth_context: AuthContext = Depends(require_permission("department:manage")),
    service: DepartmentService = Depends(admin_department_service_dependency),
) -> JSONResponse:
    del auth_context
    try:
        department = service.get_department(department_id)
    except AdminDepartmentServiceError as error:
        _http_error(error)
    return envelope_json_response({"department": _safe_department(department)})


@router.post("/admin/departments")
def create_department(
    payload: CreateDepartmentRequest,
    auth_context: AuthContext = Depends(require_permission("department:manage")),
    service: DepartmentService = Depends(admin_department_service_dependency),
) -> JSONResponse:
    try:
        department = service.create_department(
            actor=auth_context, name=payload.name, description=payload.description
        )
    except AdminDepartmentServiceError as error:
        _http_error(error)
    return envelope_json_response({"department": _safe_department(department)})


@router.patch("/admin/departments/{department_id}")
def update_department(
    department_id: str,
    payload: UpdateDepartmentRequest,
    auth_context: AuthContext = Depends(require_permission("department:manage")),
    service: DepartmentService = Depends(admin_department_service_dependency),
) -> JSONResponse:
    try:
        department = service.update_department(
            actor=auth_context,
            department_id=department_id,
            name=payload.name,
            description=payload.description,
        )
    except AdminDepartmentServiceError as error:
        _http_error(error)
    return envelope_json_response({"department": _safe_department(department)})


@router.delete("/admin/departments/{department_id}")
def delete_department(
    department_id: str,
    auth_context: AuthContext = Depends(require_permission("department:manage")),
    service: DepartmentService = Depends(admin_department_service_dependency),
) -> JSONResponse:
    try:
        result = service.delete_department(actor=auth_context, department_id=department_id)
    except AdminDepartmentServiceError as error:
        _http_error(error)
    return envelope_json_response(result)
