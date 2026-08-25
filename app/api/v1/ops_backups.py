"""V1 ops API for instance-level backup/restore operations (Q1-Q9).

All nine endpoints are strictly ops-only. The four write commands (create
backup, start restore, repair-target retry, policy PATCH) require an
`Idempotency-Key`; lookup and replay happen in `BackupOpsService` scoped by
(operator, endpoint, hashed key). Handlers never wait for provider work: the
accepted commands are persisted facts that the backup worker executes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.backup.ops_service import (
    ENDPOINT_CREATE_BACKUP,
    ENDPOINT_RETRY_REPAIR_TARGET,
    ENDPOINT_START_RESTORE,
    ENDPOINT_UPDATE_BACKUP_POLICY,
    BackupOpsService,
)
from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key

from .dependencies import backup_ops_service, current_principal

router = APIRouter(prefix="/ops", tags=["ops"])


class RestoreCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_id: str = Field(min_length=1, max_length=128)


class BackupPolicyPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    enabled: bool | None = None
    frequency: Literal["daily", "weekly"] | None = None
    local_time: str | None = Field(default=None, min_length=1, max_length=5)
    weekdays: list[int] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    keep_last: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)


def _command_request_hash(
    *,
    endpoint: str,
    idempotency_key: str,
    targets: dict[str, str],
    body: dict[str, object],
) -> str:
    encoded = json.dumps(
        {
            "endpoint": endpoint,
            "idempotency_key": idempotency_key,
            "targets": targets,
            "body": body,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(b"ops-backup-command-v1\0" + encoded.encode("utf-8")).hexdigest()


def _require_ops(
    principal: AuthPrincipal,
    service: BackupOpsService,
    *,
    action: str,
    resource_id: str,
) -> None:
    if principal.role == "ops":
        return
    service.record_authorization_denial(
        actor_id=principal.user_id, action=action, resource_id=resource_id
    )
    raise PlatformError("forbidden", "Ops access is required", {}, 403)


@router.post("/backups", status_code=202)
def create_backup(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    service = backup_ops_service(request)
    _require_ops(principal, service, action=ENDPOINT_CREATE_BACKUP, resource_id="backup_sets")
    key = validate_idempotency_key(idempotency_key)
    status, payload = service.create_backup(
        operator_user_id=principal.user_id,
        idempotency_key=key,
        request_hash=_command_request_hash(
            endpoint=ENDPOINT_CREATE_BACKUP, idempotency_key=key, targets={}, body={}
        ),
    )
    return JSONResponse(payload, status_code=status)


@router.get("/backups")
def list_backups(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    service = backup_ops_service(request)
    _require_ops(principal, service, action="list_backups", resource_id="backup_sets")
    return service.list_backups(page=page, page_size=page_size)


@router.get("/backups/{backup_id}")
def get_backup(
    backup_id: Annotated[str, Path(min_length=1, max_length=128)],
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
) -> dict[str, object]:
    service = backup_ops_service(request)
    _require_ops(principal, service, action="get_backup", resource_id=backup_id)
    return service.get_backup(backup_id)


@router.post("/restores", status_code=202)
def start_restore(
    body: RestoreCreateRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    service = backup_ops_service(request)
    _require_ops(principal, service, action=ENDPOINT_START_RESTORE, resource_id=body.backup_id)
    key = validate_idempotency_key(idempotency_key)
    status, payload = service.start_restore(
        operator_user_id=principal.user_id,
        backup_id=body.backup_id,
        idempotency_key=key,
        request_hash=_command_request_hash(
            endpoint=ENDPOINT_START_RESTORE,
            idempotency_key=key,
            targets={"backup_id": body.backup_id},
            body={"backup_id": body.backup_id},
        ),
    )
    return JSONResponse(payload, status_code=status)


@router.get("/restores")
def list_restores(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    service = backup_ops_service(request)
    _require_ops(principal, service, action="list_restores", resource_id="restore_sessions")
    return service.list_restores(page=page, page_size=page_size)


@router.get("/restores/{restore_id}")
def get_restore(
    restore_id: Annotated[str, Path(min_length=1, max_length=128)],
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
) -> dict[str, object]:
    service = backup_ops_service(request)
    _require_ops(principal, service, action="get_restore", resource_id=restore_id)
    return service.get_restore(restore_id)


@router.post("/restores/{restore_id}/repair-targets/{target_id}/retry", status_code=202)
def retry_repair_target(
    restore_id: Annotated[str, Path(min_length=1, max_length=128)],
    target_id: Annotated[str, Path(min_length=1, max_length=128)],
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    service = backup_ops_service(request)
    _require_ops(principal, service, action=ENDPOINT_RETRY_REPAIR_TARGET, resource_id=target_id)
    key = validate_idempotency_key(idempotency_key)
    status, payload = service.retry_repair_target(
        operator_user_id=principal.user_id,
        restore_id=restore_id,
        target_id=target_id,
        idempotency_key=key,
        request_hash=_command_request_hash(
            endpoint=ENDPOINT_RETRY_REPAIR_TARGET,
            idempotency_key=key,
            targets={"restore_id": restore_id, "target_id": target_id},
            body={},
        ),
    )
    return JSONResponse(payload, status_code=status)


@router.get("/backup-policy")
def get_backup_policy(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
) -> dict[str, object]:
    service = backup_ops_service(request)
    _require_ops(principal, service, action="get_policy", resource_id="backup_policy")
    return service.get_policy()


@router.patch("/backup-policy")
def patch_backup_policy(
    body: BackupPolicyPatchRequest,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    service = backup_ops_service(request)
    _require_ops(
        principal, service, action=ENDPOINT_UPDATE_BACKUP_POLICY, resource_id="backup_policy"
    )
    key = validate_idempotency_key(idempotency_key)
    changes = body.model_dump(exclude_none=True, exclude={"expected_version"})
    status, payload = service.patch_policy(
        operator_user_id=principal.user_id,
        expected_version=body.expected_version,
        changes=changes,
        idempotency_key=key,
        request_hash=_command_request_hash(
            endpoint=ENDPOINT_UPDATE_BACKUP_POLICY,
            idempotency_key=key,
            targets={},
            body=dict(body.model_dump(exclude_none=True)),
        ),
    )
    return JSONResponse(payload, status_code=status)
