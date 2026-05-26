"""响应数据模型"""

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class SessionInfoResponse(BaseModel):
    session_id: str = Field(..., description="会话 ID")
    message_count: int = Field(..., description="消息数量")
    history: list[dict[str, Any]] = Field(..., description="历史消息列表")


class ApiResponse(BaseModel):
    status: str = Field(..., description="状态")
    message: str = Field(..., description="消息")
    data: Any | None = Field(None, description="数据")


class ApiEnvelope(BaseModel):
    """Canonical JSON API response envelope."""

    code: int = Field(..., description="业务状态码")
    message: str = Field(..., description="响应消息")
    data: Any | None = Field(None, description="响应数据")


def success_envelope(data: Any | None = None, *, message: str = "success") -> ApiEnvelope:
    return ApiEnvelope(code=200, message=message, data=data)


def error_envelope(
    message: str,
    *,
    code: int = 500,
    data: Any | None = None,
) -> ApiEnvelope:
    return ApiEnvelope(code=code, message=message, data=data)


def envelope_json_response(
    data: Any | None = None,
    *,
    code: int = 200,
    message: str = "success",
    status_code: int | None = None,
    include_message: bool = True,
) -> JSONResponse:
    envelope = ApiEnvelope(code=code, message=message, data=data)
    content = envelope.model_dump(mode="json")
    if not include_message:
        content.pop("message", None)
    return JSONResponse(
        status_code=status_code or code,
        content=content,
    )
