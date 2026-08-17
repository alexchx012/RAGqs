"""`GET /v1/quota/me`、`POST /v1/quota-requests` 路由（Task 9）。

使用项目当前 AuthPrincipal/request-state service DI（request.app.state.
platform_runtime.resolve）与 PlatformError 映射（register_exception_handlers）；
Header `Idempotency-Key` 在路由层统一校验非空和限长（服务层再校验角色/范围）；
Pydantic 严格校验（extra="forbid" + requested_pages strict int ge=1 le=500——
JSON true/false、字符串"1"、1.0、null 均标准 422 validation_error，1 与 500
成功）。服务解析失败即 RuntimeError（fail-closed，由平台 500 处理器兜底），
不在模块 import 时创建 engine 或隐式数据库。
路由仅依赖 app 状态注入的 quota_request_service，可被生产 app 后续 wiring 复用。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.identity.service import AuthPrincipal
from app.platform.http_contract import validate_idempotency_key
from app.usage.requests import QuotaRequestService

from .dependencies import current_principal

router = APIRouter(tags=["quota"])


class QuotaRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_pages: int = Field(strict=True, ge=1, le=500)


def _quota_request_service(request: Request) -> QuotaRequestService:
    service = request.app.state.platform_runtime.resolve("quota_request_service")
    if not isinstance(service, QuotaRequestService):
        raise RuntimeError("quota request service is not configured")
    return service


@router.get("/quota/me")
def quota_me(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
) -> dict:
    return _quota_request_service(request).me(actor=principal)


@router.post("/quota-requests", status_code=201)
def create_quota_request(
    body: QuotaRequestCreate,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    key = validate_idempotency_key(request.headers.get("Idempotency-Key"))
    result = _quota_request_service(request).create(
        actor=principal, requested_pages=body.requested_pages, idempotency_key=key
    )
    return JSONResponse(result, status_code=201)
