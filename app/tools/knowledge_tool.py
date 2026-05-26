"""知识检索工具"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.documents import Document
from langchain_core.tools import tool
from loguru import logger

from app.config import config
from app.providers.contracts import RetrievalRequest, RetrieverProvider
from app.providers.factory import get_default_provider_container

_active_knowledge_space_id: ContextVar[str | None] = ContextVar(
    "active_knowledge_space_id",
    default=None,
)


@tool(response_format="content_and_artifact")
def retrieve_knowledge(query: str, space_id: str = "default") -> tuple[str, list[Document]]:
    """从知识库中检索相关信息来回答问题

    当用户的问题涉及专业知识、文档内容或需要参考资料时，使用此工具。

    Args:
        query: 用户的问题或查询
        space_id: 知识库空间，默认为 default

    Returns:
        Tuple[str, List[Document]]: (格式化的上下文文本, 原始文档列表)
    """
    try:
        container = get_default_provider_container()
        return retrieve_knowledge_with_settings(
            query=query,
            retriever_provider=container.retriever_provider,
            settings=config,
            space_id=space_id,
        )
    except Exception as e:
        logger.error(f"知识检索失败: {e}")
        return f"检索知识时发生错误: {str(e)}", []


def retrieve_knowledge_with_provider(
    query: str,
    retriever_provider: RetrieverProvider,
    top_k: int,
    space_id: str = "default",
) -> tuple[str, list[Document]]:
    effective_space_id = resolve_knowledge_space_id(space_id)
    filters = (
        {"space_id": effective_space_id}
        if effective_space_id and effective_space_id != "default"
        else {}
    )
    result = retriever_provider.retrieve(
        RetrievalRequest(query=query, top_k=top_k, filters=filters)
    )
    docs = result.documents
    if not docs:
        return "没有找到相关信息。", []
    context = _format_docs(docs)
    logger.info(f"检索到 {len(docs)} 个相关文档")
    return context, docs


def retrieve_knowledge_with_settings(
    query: str,
    retriever_provider: RetrieverProvider,
    settings: Any,
    space_id: str = "default",
) -> tuple[str, list[Document]]:
    return retrieve_knowledge_with_provider(
        query=query,
        retriever_provider=retriever_provider,
        top_k=_settings_value(settings, "rag", "top_k", "rag_top_k", 3),
        space_id=space_id,
    )


@contextmanager
def enforce_knowledge_space(space_id: str):
    """Force knowledge retrieval tools to use the request-selected space."""

    token = _active_knowledge_space_id.set(_normalize_space_id(space_id))
    try:
        yield
    finally:
        _active_knowledge_space_id.reset(token)


def get_current_knowledge_space_id(default: str = "default") -> str:
    return _active_knowledge_space_id.get() or _normalize_space_id(default)


def resolve_knowledge_space_id(space_id: str = "default") -> str:
    return _active_knowledge_space_id.get() or _normalize_space_id(space_id)


def _normalize_space_id(space_id: str) -> str:
    normalized = (space_id or "default").strip()
    return normalized or "default"


def _settings_value(
    settings: Any,
    group_name: str,
    group_field_name: str,
    flat_field_name: str,
    default: Any,
) -> Any:
    group = getattr(settings, group_name, None)
    if group is not None and hasattr(group, group_field_name):
        return getattr(group, group_field_name)
    return getattr(settings, flat_field_name, default)


def _format_docs(docs: list[Document]) -> str:
    parts = []
    for i, doc in enumerate(docs, 1):
        metadata = doc.metadata
        source = metadata.get("_file_name", "未知来源")
        headers = []
        for key in ["h1", "h2", "h3"]:
            if key in metadata and metadata[key]:
                headers.append(metadata[key])
        header_str = " > ".join(headers) if headers else ""
        formatted = f"【参考资料 {i}】"
        if header_str:
            formatted += f"\n标题: {header_str}"
        formatted += f"\n来源: {source}\n内容:\n{doc.page_content}\n"
        parts.append(formatted)
    return "\n".join(parts)
