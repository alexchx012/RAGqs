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
    """把内部流式 chunk 转换成前端 SSE payload。

    这是 API 格式适配层：接收 RAG/Agent service 或 graph 层产生的 chunk，
    将内部 type 映射为前端稳定消费的 type，例如 token -> content、
    complete -> done。未知类型返回 None，表示不发送给前端；如果原始 chunk
    带有 node 字段，则保留下来用于定位事件来自哪个 graph 节点。
    """

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
        "answer_mode": "answer_mode",
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
    """处理非流式聊天接口 POST /api/chat。

    这是 API 编排层：FastAPI 将请求体解析为 ChatRequest，并通过 Depends
    注入具备 chat:write 权限的 AuthContext。函数先规范化认证上下文并检查
    knowledge space 访问权限，再调用 RAG/Agent service 获取带 trace 的结果，
    最后统一包装为 success_envelope 或 error_envelope。这里不直接做 Milvus
    检索、prompt 拼接或模型调用。
    """
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
    """兼容性调用 rag_agent_service.query_with_trace。

    question 是用户问题；session_id 和 space_id 必须用关键字传入，避免同为
    字符串时被按位置传反。函数先取出 service 方法对象，再用 _accepts_keyword
    判断它是否支持 space_id：支持则传入知识空间，不支持则只传 session_id，
    以兼容旧签名或测试替身。
    """

    method = rag_agent_service.query_with_trace
    if _accepts_keyword(method, "space_id"):
        return await method(question, session_id=session_id, space_id=space_id)
    return await method(question, session_id=session_id)


def _accepts_keyword(method, keyword: str) -> bool:
    """判断方法是否能接收指定关键字参数。

    这是 API 兼容适配层的辅助函数。它用 inspect.signature 读取 method 的
    函数签名；只要参数列表里明确存在 keyword，或方法声明了 **kwargs
    (VAR_KEYWORD)，就认为可以安全传入该关键字参数。
    """

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
    """处理流式聊天接口 POST /api/chat_stream。

    这是 API 编排层的 SSE 接口：FastAPI 先解析 ChatRequest，并通过 Depends
    注入具备 chat:write 权限的 AuthContext；函数再检查 knowledge space
    访问权限。内部 event_generator 是异步生成器，负责消费
    rag_agent_service.query_stream_with_trace(...) 产生的 chunk，调用
    format_stream_chunk(...) 转成前端可识别的 payload，再用 SSE message
    一段一段推给客户端。流式过程中出现普通异常时，函数用同样的 SSE 事件
    格式推送 error payload，而不是返回普通 JSON envelope。
    """
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
    """处理清空会话历史接口 POST /api/chat/clear。

    这是 API 编排层：FastAPI 将请求体解析为 ClearRequest，并通过 Depends
    注入具备 chat:write 权限的 AuthContext。函数先取得有效认证上下文，
    再用 _require_session_space_access(...) 确认当前用户有权操作该 session，
    避免通过猜测 session_id 清空其他知识空间的会话。真正的清空动作交给
    rag_agent_service.clear_session(...)；返回值按 ApiResponse 模型序列化。
    """
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
    """处理会话摘要列表查询接口 GET /api/chat/sessions。

    这是 API 编排层的只读接口：query 来自 URL query string，可选用于过滤
    会话摘要；AuthContext 通过 session:read 权限注入。函数会根据当前用户
    是否拥有全空间权限决定 allowed_space_ids：None 表示不做空间过滤，
    普通用户则只允许查询 active_context.spaces 内的会话。真正的会话查询
    交给 _call_list_sessions(...)，返回前再把 service summary 转成 API
    字典结构并包进 success_envelope。
    """
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
    """把 service 层会话摘要对象转换成前端 API 字典。

    这是 API 序列化适配层：输入通常是 rag_agent_service 返回的 session
    summary 对象，输出是前端更容易消费的 JSON 字段。这里把 Python 后端的
    snake_case 属性转换成前端常用的 camelCase 字段，例如 message_count
    -> messageCount、updated_at -> updatedAt。
    """

    return {
        "id": summary.session_id,
        "title": summary.title,
        "messageCount": summary.message_count,
        "updatedAt": summary.updated_at,
        "lastMessage": summary.last_message,
    }


def _call_list_sessions(query: str | None, allowed_space_ids: set[str] | None):
    """兼容性调用会话摘要查询，并按空间权限做兜底过滤。

    这是 API 兼容适配层：新版 rag_agent_service.list_sessions 如果支持
    allowed_space_ids，就把空间过滤交给 service 层；旧版方法不支持时，
    先按 query 取回 summaries，再由 API 层用 _session_space_ids_allowed(...)
    过滤掉当前用户无权访问的会话。allowed_space_ids 为 None 或包含 "*"
    表示不限制空间，通常对应全空间权限。
    """

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
    """校验当前用户是否有权操作指定 session 关联的全部知识库空间（security 层辅助）。

    职责：作为 API 层的"会话级权限闸门"，在清空会话、追加提问等操作前调用，
    确保用户对该 session 牵涉的每一个 space 都有访问权。

    输入：
    - auth_context：当前请求的认证上下文，携带用户可访问的 space 集合。
    - session_id：目标会话 ID。
    返回：None。这是一个纯副作用的"检查型函数"——成功则隐式返回 None 放行，
    失败则由下游 require_space_access(...) 直接 raise HTTPException(403)。

    关键控制流：遍历 _session_space_ids(session_id) 得到的 space 集合，逐个调用
    require_space_access(...)。一旦遇到第一个无权限的 space 立即抛 403 并短路，
    剩余 space 不再检查——安全结果与"全检查"等价，但实现更简单、信息暴露更少。

    所属层：API 层内的 security 辅助函数；依赖下游 _session_space_ids(...) 取空间集合、
    require_space_access(...) 做单空间鉴权。

    改错影响：若误把 raise 改成 return 或吞掉异常，会导致越权用户绕过空间隔离，
    操作到无权访问的会话；若循环写错只检查首个 space，则多空间会话出现鉴权漏洞。
    """

    for space_id in _session_space_ids(session_id):
        require_space_access(auth_context, space_id)


def _session_space_ids_allowed(allowed_space_ids: set[str], session_id: str) -> bool:
    """判断某个 session 是否完全落在用户的允许空间集合内（API 层过滤辅助）。

    职责：为会话列表的"静默过滤"提供布尔判据。与抛异常的
    _require_session_space_access(...) 形成对比——本函数用于"系统批量筛选、
    把无权会话剔除"的正常流程，权限不足不是错误，因此返回 bool 而非 raise。

    输入：
    - allowed_space_ids：当前用户被允许访问的空间集合。
    - session_id：待判定的会话 ID。
    返回：bool。仅当 session 关联的每一个 space 都在 allowed_space_ids 内时返回 True，
    缺任意一个空间权限即返回 False（该会话会被列表过滤掉）。

    关键控制流：取 _session_space_ids(session_id) 得到 session 牵涉的空间集合，用
    issubset 判断它是否为 allowed_space_ids 的子集。注意空集 issubset 恒为 True，
    这正是 _session_space_ids(...) 必须兜底成 {"default"} 而非空集的原因。

    所属层：API 层 security 辅助；依赖下游 _session_space_ids(...) 取空间集合。
    被 _call_list_sessions(...) 在旧版 service 不支持空间过滤时作为兜底过滤条件调用。

    改错影响：若把 issubset 写反成 superset 或逻辑取反，会让用户看到无权访问的会话，
    造成会话列表层面的越权信息泄露。
    """

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
