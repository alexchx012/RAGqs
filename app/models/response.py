"""响应数据模型"""

from typing import Any

from pydantic import BaseModel, Field


class SessionInfoResponse(BaseModel):
    session_id: str = Field(..., description="会话 ID")
    message_count: int = Field(..., description="消息数量")
    history: list[dict[str, Any]] = Field(..., description="历史消息列表")


class ApiResponse(BaseModel):
    status: str = Field(..., description="状态")
    message: str = Field(..., description="消息")
    data: Any | None = Field(None, description="数据")
