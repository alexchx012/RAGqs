"""响应数据模型"""

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional


class SessionInfoResponse(BaseModel):
    session_id: str = Field(..., description="会话 ID")
    message_count: int = Field(..., description="消息数量")
    history: List[Dict[str, Any]] = Field(..., description="历史消息列表")


class ApiResponse(BaseModel):
    status: str = Field(..., description="状态")
    message: str = Field(..., description="消息")
    data: Optional[Any] = Field(None, description="数据")
