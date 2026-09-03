from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.documents.service import DocumentsService
from app.identity.service import AuthPrincipal, IdentityAccessService
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key

from .dependencies import current_principal, identity_access_service

router = APIRouter(prefix="/admin", tags=["admin"])


class DepartmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)


class ManagedUserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    real_name: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)
    department_id: str | None
    role: Literal["user", "minister", "ops", "admin"]
    initial_password: str = Field(min_length=1, max_length=1024)


class ManagedUserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    role: Literal["user", "minister", "ops", "admin"] | None = None
    department_id: str | None = None


class DepartmentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=256)


class ExpectedVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


@router.get("/users")
def list_users(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
    q: Annotated[str | None, Query(max_length=256)] = None,
    department_id: str | None = None,
    role: Literal["user", "minister", "ops", "admin"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    return service.list_managed_users(
        actor=principal,
        q=q,
        department_id=department_id,
        role=role,
        page=page,
        page_size=page_size,
    )


def document_service(request: Request) -> DocumentsService:
    service = request.app.state.platform_runtime.resolve("documents_service")
    if not isinstance(service, DocumentsService):
        raise RuntimeError("documents service is not configured")
    return service


def _require_management_reader(principal: AuthPrincipal) -> None:
    if principal.role not in {"ops", "admin"}:
        raise PlatformError(
            "forbidden_target",
            "Management document access requires ops or admin",
            {},
            403,
        )


@router.get("/users/{user_id}/documents")
def list_user_documents(
    user_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    q: Annotated[str | None, Query(max_length=256)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    _require_management_reader(principal)
    return document_service(request).list_documents(
        principal=principal,
        space_id=f"personal:{user_id}",
        q=q,
        page=page,
        page_size=page_size,
    )


@router.get("/departments/{department_id}/documents")
def list_department_documents(
    department_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    q: Annotated[str | None, Query(max_length=256)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    _require_management_reader(principal)
    return document_service(request).list_documents(
        principal=principal,
        space_id=f"department:{department_id}",
        q=q,
        page=page,
        page_size=page_size,
    )


@router.post("/users", status_code=201)
def create_user(
    body: ManagedUserCreateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> JSONResponse:
    response = service.create_managed_user(
        actor=principal,
        username=body.username,
        password=body.initial_password,
        real_name=body.real_name,
        display_name=body.display_name or body.real_name,
        role=body.role,
        department_id=body.department_id,
        idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )
    return JSONResponse(response, status_code=201)


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    body: ManagedUserUpdateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.update_managed_user(
        actor=principal,
        user_id=user_id,
        expected_version=body.expected_version,
        role=body.role,
        department_id=body.department_id,
        department_provided="department_id" in body.model_fields_set,
        idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )


@router.delete("/users/{user_id}", status_code=202)
def delete_user(
    user_id: str,
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> JSONResponse:
    response = service.delete_managed_user(
        actor=principal,
        user_id=user_id,
        expected_version=body.expected_version,
        idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )
    return JSONResponse(response, status_code=202)


@router.get("/departments")
def list_departments(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
    status: Literal["active", "inactive", "all"] = "active",
) -> dict[str, list[dict[str, object]]]:
    return {"items": service.list_departments(actor=principal, status=status)}


@router.post("/departments", status_code=201)
def create_department(
    body: DepartmentCreateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> JSONResponse:
    response = service.create_department(
        actor=principal,
        name=body.name,
        idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )
    return JSONResponse(response, status_code=201)


@router.patch("/departments/{department_id}")
def update_department(
    department_id: str,
    body: DepartmentUpdateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.rename_department(
        actor=principal,
        department_id=department_id,
        expected_version=body.expected_version,
        name=body.name,
        idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )


@router.post("/departments/{department_id}/deactivate")
def deactivate_department(
    department_id: str,
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.deactivate_department(
        actor=principal,
        department_id=department_id,
        expected_version=body.expected_version,
        idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )


@router.get("/permission-matrix")
def permission_matrix(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    service: Annotated[IdentityAccessService, Depends(identity_access_service)],
) -> dict[str, object]:
    return service.permission_matrix(actor=principal)
