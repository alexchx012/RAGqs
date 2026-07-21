"""结构化知识库检索工具，供 agentic 编排路径的工具调用循环使用。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import tool
from loguru import logger

from app.config import config
from app.providers.contracts import RetrievalRequest, RetrieverProvider
from app.providers.factory import get_default_provider_container
from app.tools.knowledge_tool import resolve_knowledge_space_id

_active_retriever_provider: ContextVar[RetrieverProvider | None] = ContextVar(
    "active_retriever_provider",
    default=None,
)


@contextmanager
def enforce_retriever_provider(provider: RetrieverProvider):
    """Force search_knowledge_base to use the graph-injected retriever provider."""

    token = _active_retriever_provider.set(provider)
    try:
        yield
    finally:
        _active_retriever_provider.reset(token)


@tool
def search_knowledge_base(query: str, space_id: str = "default") -> dict[str, Any]:
    """在知识库中检索与问题相关的文档，返回结构化命中结果。

    当问题依赖本组织的私有/内部信息时调用此工具。

    Args:
        query: 用户的问题或查询
        space_id: 知识库空间，默认为 default

    Returns:
        dict: {"hit": bool, "documents": [{"content": str, "metadata": dict}, ...]}
    """
    try:
        injected_provider = _active_retriever_provider.get()
        if injected_provider is not None:
            provider = injected_provider
        else:
            provider = get_default_provider_container().retriever_provider
        effective_space_id = resolve_knowledge_space_id(space_id)
        filters = (
            {"space_id": effective_space_id}
            if effective_space_id and effective_space_id != "default"
            else {}
        )
        top_k = getattr(getattr(config, "rag", None), "top_k", None) or getattr(
            config, "rag_top_k", 3
        )
        result = provider.retrieve(
            RetrievalRequest(query=query, top_k=top_k, filters=filters)
        )
        documents = [
            {"content": document.page_content, "metadata": dict(document.metadata)}
            for document in result.documents
        ]
        return {"hit": bool(documents), "documents": documents}
    except Exception as e:
        logger.error(f"知识检索失败: {e}")
        return {"hit": False, "documents": []}
