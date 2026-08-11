"""`GET /v1/approvals/summary`、`GET /v1/approvals/quota-requests`、
`POST /v1/approvals/quota-requests/{id}/approve|reject` 路由（Task 10）。

使用项目当前 AuthPrincipal/request-state service DI（request.app.state.
platform_runtime.resolve）与 PlatformError 映射（register_exception_handlers）；
Header `Idempotency-Key` 在路由层校验非空（服务层再校验限长）；Pydantic 严格校验
（extra="forbid" + strict int——JSON true/false、字符串"1"、1.0、null 均标准 422
validation_error；expected_version ge=1；approved_pages 可选 ge=1 le=500，缺省由
服务层取申请量；reject 不接受自由文本 reason，extra="forbid" 拒绝任何多余字段）。
status 查询参数固定 Literal pending/approved/rejected/cancelled。
服务解析失败即 RuntimeError（fail-closed，由平台 500 处理器兜底），不在模块
import 时创建 engine 或隐式数据库。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.usage.requests import QuotaRequestService

from .dependencies import current_principal

router = APIRouter(tags=["approvals"])

_REQUEST_STATUS = Literal["pending", "approved", "rejected", "cancelled"]


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=1)
    approved_pages: int | None = Field(default=None, strict=True, ge=1, le=500)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=1)


def _quota_request_service(request: Request) -> QuotaRequestService:
    service = request.app.state.platform_runtime.resolve("quota_request_service")
    if not isinstance(service, QuotaRequestService):
        raise RuntimeError("quota request service is not configured")
    return service


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key or not key.strip():
        raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
    return key


@router.get("/approvals/summary")
def approvals_summary(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
) -> dict:
    return _quota_request_service(request).summary(actor=principal)


@router.get("/approvals/quota-requests")
def list_quota_requests(
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    request: Request,
    status: _REQUEST_STATUS = Query(default="pending"),
) -> dict:
    items = _quota_request_service(request).list_quota_requests(actor=principal, status=status)
    return {"items": items}


@router.post("/approvals/quota-requests/{request_id}/approve")
def approve_quota_request(
    request_id: Annotated[str, Path(min_length=1, max_length=64)],
    body: ApproveRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict:
    return _quota_request_service(request).approve(
        actor=principal,
        request_id=request_id,
        expected_version=body.expected_version,
        approved_pages=body.approved_pages,
        idempotency_key=_idempotency_key(request),
    )


@router.post("/approvals/quota-requests/{request_id}/reject")
def reject_quota_request(
    request_id: Annotated[str, Path(min_length=1, max_length=64)],
    body: RejectRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict:
    return _quota_request_service(request).reject(
        actor=principal,
        request_id=request_id,
        expected_version=body.expected_version,
        idempotency_key=_idempotency_key(request),
    )
