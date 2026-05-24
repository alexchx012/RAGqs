"""请求数据模型"""

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """对话请求"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="会话 ID", alias="Id")
    question: str = Field(..., description="用户问题", alias="Question")
    space_id: str = Field("default", description="知识库空间 ID", alias="spaceId")


class ClearRequest(BaseModel):
    """清空会话请求"""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., description="会话 ID", alias="sessionId")
