"""对话接口"""

import inspect
import json

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.models.request import ChatRequest, ClearRequest
from app.models.response import ApiResponse, SessionInfoResponse, error_envelope, success_envelope
from app.security.auth import (
    AuthContext,
    active_auth_context,
    is_all_space_context,
    require_permission,
    require_space_access,
)
from app.services.rag_agent_service import rag_agent_service

router = APIRouter()


def format_stream_chunk(chunk: dict) -> dict | None:
    chunk_type = chunk.get("type", "unknown")
    chunk_data = chunk.get("data", None)
    output_type_by_chunk_type = {
        "retrieval": "retrieval",
        "retrieval_decision": "retrieval_decision",
        "handoff": "handoff",
        "error_policy": "error_policy",
        "source": "source",
        "tool_call": "tool_call",
        "tool_result": "tool_result",
        "token": "content",
        "content": "content",
        "complete": "done",
        "done": "done",
        "error": "error",
    }
    output_type = output_type_by_chunk_type.get(chunk_type)
    if output_type is None:
        return None

    payload = {"type": output_type, "data": chunk_data}
    if "node" in chunk:
        payload["node"] = chunk["node"]
    return payload


@router.post("/chat")
async def chat(
    request: ChatRequest,
    auth_context: AuthContext = Depends(require_permission("chat:write")),
):
    """快速对话接口（非流式）"""
    active_context = active_auth_context(auth_context)
    require_space_access(active_context, request.space_id)
    try:
        result = await _call_query_with_trace(
            request.question,
            session_id=request.id,
            space_id=request.space_id,
        )
        retrieval = result.get("retrieval") or {}
        errors = [str(error) for error in result.get("errors", [])]
        success = bool(result.get("success", True)) and not errors
        return success_envelope(
            {
                "success": success,
                "answer": result["answer"],
                "sources": result.get("sources", []),
                "retrievalDebug": retrieval.get("debug", {}),
                "retrieval": retrieval,
                "errorMessage": None if success else "; ".join(errors),
            }
        ).model_dump(mode="json")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对话接口错误: {e}")
        return error_envelope(
            "error",
            data={"success": False, "answer": None, "errorMessage": str(e)},
        ).model_dump(mode="json")


async def _call_query_with_trace(question: str, *, session_id: str, space_id: str):
    method = rag_agent_service.query_with_trace
    if _accepts_keyword(method, "space_id"):
        return await method(question, session_id=session_id, space_id=space_id)
    return await method(question, session_id=session_id)


def _accepts_keyword(method, keyword: str) -> bool:
    parameters = inspect.signature(method).parameters.values()
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


@router.post("/chat_stream")
async def chat_stream(
    request: ChatRequest,
    auth_context: AuthContext = Depends(require_permission("chat:write")),
):
    """流式对话接口（SSE）"""
    active_context = active_auth_context(auth_context)
    require_space_access(active_context, request.space_id)

    async def event_generator():
        try:
            async for chunk in rag_agent_service.query_stream_with_trace(
                request.question,
                session_id=request.id,
                space_id=request.space_id,
            ):
                payload = format_stream_chunk(chunk)
                if payload is not None:
                    yield {"event": "message", "data": json.dumps(payload, ensure_ascii=False)}
        except Exception as e:
            yield {"event": "message", "data": json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False)}

    return EventSourceResponse(event_generator())


@router.post("/chat/clear", response_model=ApiResponse)
async def clear_session(
    request: ClearRequest,
    auth_context: AuthContext = Depends(require_permission("chat:write")),
):
    """清空会话历史"""
    active_context = active_auth_context(auth_context)
    _require_session_space_access(active_context, request.session_id)
    try:
        success = rag_agent_service.clear_session(request.session_id)
        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/chat/sessions")
async def list_sessions(
    query: str | None = None,
    auth_context: AuthContext = Depends(require_permission("session:read")),
):
    """查询会话摘要列表"""
    active_context = active_auth_context(auth_context)
    try:
        allowed_space_ids = None if is_all_space_context(active_context) else active_context.spaces
        summaries = _call_list_sessions(query=query, allowed_space_ids=allowed_space_ids)
        sessions = [_serialize_session_summary(summary) for summary in summaries]
        return success_envelope({"count": len(sessions), "sessions": sessions}).model_dump(
            mode="json"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/chat/audits")
async def list_retrieval_audits(
    session_id: str | None = None,
    space_id: str | None = None,
    trace_id: str | None = None,
    limit: int = 50,
    auth_context: AuthContext = Depends(require_permission("audit:read")),
):
    """查询最近的检索审计记录"""
    active_context = active_auth_context(auth_context)
    if space_id:
        require_space_access(active_context, space_id)
    try:
        normalized_limit = max(0, min(limit, 500))
        audits = rag_agent_service.list_retrieval_audits(
            session_id=session_id,
            space_id=space_id,
            trace_id=trace_id,
            limit=normalized_limit,
        )
        if not space_id and not is_all_space_context(active_context):
            audits = [
                audit
                for audit in audits
                if active_context.can_access_space(getattr(audit, "space_id", "default"))
            ]
        serialized = [_serialize_retrieval_audit(audit) for audit in audits]
        return success_envelope({"count": len(serialized), "audits": serialized}).model_dump(
            mode="json"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/chat/session/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(
    session_id: str,
    auth_context: AuthContext = Depends(require_permission("session:read")),
) -> SessionInfoResponse:
    """查询会话历史"""
    active_context = active_auth_context(auth_context)
    _require_session_space_access(active_context, session_id)
    try:
        history = rag_agent_service.get_session_history(session_id)
        return SessionInfoResponse(session_id=session_id, message_count=len(history), history=history)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _serialize_session_summary(summary) -> dict:
    return {
        "id": summary.session_id,
        "title": summary.title,
        "messageCount": summary.message_count,
        "updatedAt": summary.updated_at,
        "lastMessage": summary.last_message,
    }


def _call_list_sessions(query: str | None, allowed_space_ids: set[str] | None):
    method = rag_agent_service.list_sessions
    if _accepts_keyword(method, "allowed_space_ids"):
        return method(query=query, allowed_space_ids=allowed_space_ids)
    summaries = method(query=query)
    if allowed_space_ids is None or "*" in allowed_space_ids:
        return summaries
    return [
        summary
        for summary in summaries
        if _session_space_ids_allowed(
            allowed_space_ids,
            getattr(summary, "session_id", ""),
        )
    ]


def _require_session_space_access(auth_context: AuthContext, session_id: str) -> None:
    for space_id in _session_space_ids(session_id):
        require_space_access(auth_context, space_id)


def _session_space_ids_allowed(allowed_space_ids: set[str], session_id: str) -> bool:
    session_spaces = _session_space_ids(session_id)
    return session_spaces.issubset(allowed_space_ids)


def _session_space_ids(session_id: str) -> set[str]:
    method = getattr(rag_agent_service, "session_space_ids", None)
    if method is None:
        return {"default"}
    spaces = method(session_id)
    return {str(space or "default").strip() or "default" for space in spaces} or {"default"}


def _serialize_retrieval_audit(audit) -> dict:
    return {
        "id": getattr(audit, "audit_id", ""),
        "traceId": getattr(audit, "trace_id", ""),
        "sessionId": getattr(audit, "session_id", ""),
        "spaceId": getattr(audit, "space_id", ""),
        "question": getattr(audit, "question", ""),
        "answer": getattr(audit, "answer", ""),
        "sources": list(getattr(audit, "sources", [])),
        "retrieval": dict(getattr(audit, "retrieval", {})),
        "createdAt": getattr(audit, "created_at", ""),
    }
