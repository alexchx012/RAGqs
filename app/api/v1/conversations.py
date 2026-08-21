"""Conversation, group, message creation and conversation read-model routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.chat.conversations import ConversationService
from app.chat.generation import GenerationService
from app.chat.models import AskRequest, ConversationScope
from app.chat.streaming import GenerationStreamService
from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key

from .dependencies import current_principal, require_streaming

router = APIRouter(tags=["conversations"])


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space_ids: list[str] | None = None
    document_ids: list[str] | None = None


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1)
    effort_level: str
    scope: ScopeBody | None = None
    # 专家模式预留契约字段：先接受并忽略，不改变现有执行默认值。
    overrides: dict[str, Any] | None = None


class ApiEnvelope(BaseModel):
    """成功 JSON 响应的兼容信封。"""

    model_config = ConfigDict(extra="forbid")
    code: int
    message: str
    data: dict[str, Any]


class ChatBody(AskBody):
    """POST /chat 非流式兼容请求；省略 conversation_id 时自动新建会话。"""

    conversation_id: str | None = None


def _ask_request(body: AskBody) -> AskRequest:
    if body.effort_level not in {"quick", "think", "deep"}:
        raise PlatformError(
            "validation_error",
            "effort_level must be quick, think or deep",
            {"field": "effort_level"},
            422,
        )
    scope = None
    if body.scope is not None:
        scope = ConversationScope.from_value(body.scope.model_dump())
    return AskRequest(content=body.content, effort_level=body.effort_level, scope=scope)


class GroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)


class PatchConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    pinned: bool | None = None
    group_id: str | None = None


def _service(request: Request) -> GenerationService:
    service = request.app.state.platform_runtime.resolve("chat_generation_service")
    if not isinstance(service, GenerationService):
        raise RuntimeError("chat generation service is not configured")
    return service


def _conversation_service(request: Request) -> ConversationService:
    service = request.app.state.platform_runtime.resolve("chat_conversation_service")
    if not isinstance(service, ConversationService):
        raise RuntimeError("chat conversation service is not configured")
    return service


def _stream_service(request: Request) -> GenerationStreamService:
    service = request.app.state.platform_runtime.resolve("chat_stream_service")
    if not isinstance(service, GenerationStreamService):
        raise RuntimeError("chat stream service is not configured")
    return service


def _idempotency_key(request: Request) -> str:
    return validate_idempotency_key(request.headers.get("Idempotency-Key"))


@router.get("/conversations")
def list_conversations(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    q: str | None = None,
) -> dict[str, Any]:
    return _conversation_service(request).list_conversations(
        user_id=str(principal.user_id), query=q
    )


@router.post("/conversations", status_code=201)
def create_conversation(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    return JSONResponse(
        _conversation_service(request).create_conversation(user_id=str(principal.user_id)),
        status_code=201,
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, Any]:
    return _conversation_service(request).get_conversation_detail(
        user_id=str(principal.user_id), conversation_id=conversation_id
    )


@router.patch("/conversations/{conversation_id}")
def patch_conversation(
    conversation_id: str,
    body: PatchConversationBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    fields = body.model_dump(exclude_unset=True)
    return JSONResponse(
        _conversation_service(request).patch_conversation(
            user_id=str(principal.user_id),
            conversation_id=conversation_id,
            **fields,
        )
    )


@router.delete("/conversations/{conversation_id}", status_code=204, response_class=Response)
def delete_conversation(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> Response:
    _conversation_service(request).delete_conversation(
        user_id=str(principal.user_id), conversation_id=conversation_id
    )
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/messages")
def create_message(
    conversation_id: str,
    body: AskBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    accept: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    require_streaming(accept)
    key = _idempotency_key(request)
    ask = _ask_request(body)
    result = _service(request).ask(
        principal=principal,
        conversation_id=conversation_id,
        request=ask,
        idempotency_key=key,
    )
    return StreamingResponse(
        _stream_service(request).stream(
            principal=principal,
            generation_id=result.generation_id,
            last_event_id=0,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat", status_code=202, response_model=ApiEnvelope)
def post_chat(
    body: ChatBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> ApiEnvelope:
    """非流式兼容出口：复用会话 + 生成服务，返回创建信封（答案经既有 SSE/读模型获取）。"""
    key = _idempotency_key(request)
    conversation_id = body.conversation_id
    if conversation_id is None:
        conversation_id = str(
            _conversation_service(request).create_conversation(user_id=str(principal.user_id))["id"]
        )
    result = _service(request).ask(
        principal=principal,
        conversation_id=conversation_id,
        request=_ask_request(body),
        idempotency_key=key,
    )
    return ApiEnvelope(
        code=202,
        message="accepted",
        data={
            "conversation_id": conversation_id,
            "generation_id": result.generation_id,
            "message_id": result.message_id,
            "user_message_id": result.user_message_id,
            "replay": result.replay,
        },
    )


@router.post("/conversation-groups", status_code=201)
def create_group(
    body: GroupBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    return JSONResponse(
        _conversation_service(request).create_group(user_id=str(principal.user_id), name=body.name),
        status_code=201,
    )


@router.patch("/conversation-groups/{group_id}")
def patch_group(
    group_id: str,
    body: GroupBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    return JSONResponse(
        _conversation_service(request).patch_group(
            user_id=str(principal.user_id), group_id=group_id, name=body.name
        )
    )


@router.delete("/conversation-groups/{group_id}", status_code=204, response_class=Response)
def delete_group(
    group_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> Response:
    _conversation_service(request).delete_group(user_id=str(principal.user_id), group_id=group_id)
    return Response(status_code=204)
