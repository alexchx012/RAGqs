"""`POST /v1/prompt-enhancements` 路由（「优化输入」）。

非流式单次「prompt → 优化文本」：无任何持久化与聊天副作用（不创建
conversation/message/generation，不落审计）。请求体就近定义（extra="forbid"）；
认证 Depends(current_principal)；错误一律 PlatformError，经标准 envelope 返回。
Provider 经 runtime.resolve("prompt_enhance_provider_port") 获取：未配置时
build_runtime 装配 fail-closed 占位（503 prompt_enhance_unavailable）；解析失败即
RuntimeError（fail-closed，由平台 500 处理器兜底），不在模块 import 时建连。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.chat.ports import PromptEnhancePort
from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError

from .dependencies import current_principal

router = APIRouter(tags=["prompt-enhancements"])


class PromptEnhancementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str


class PromptEnhancementResult(BaseModel):
    enhanced_prompt: str


def _provider(request: Request) -> PromptEnhancePort:
    provider = request.app.state.platform_runtime.resolve("prompt_enhance_provider_port")
    if provider is None:
        raise RuntimeError("prompt enhance provider is not configured")
    return provider


@router.post("/prompt-enhancements", response_model=PromptEnhancementResult)
def create_prompt_enhancement(
    body: PromptEnhancementCreate,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> PromptEnhancementResult:
    del principal  # 认证即授权：端点只要求已登录，无角色/范围收窄。
    prompt = body.prompt.strip()
    if not prompt:
        raise PlatformError(
            "validation_error",
            "prompt must not be empty",
            {"field": "prompt"},
            422,
        )
    max_chars = request.app.state.platform_runtime.settings.chat.enhance_max_prompt_chars
    if len(body.prompt) > max_chars:
        raise PlatformError(
            "validation_error",
            "prompt is too long",
            {"field": "prompt", "max_length": max_chars},
            422,
        )
    return PromptEnhancementResult(enhanced_prompt=_provider(request).enhance(prompt))
