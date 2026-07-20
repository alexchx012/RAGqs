"""Tests for the structured search_knowledge_base tool."""

import importlib

from langchain_core.documents import Document

from app.providers.contracts import RetrievalRequest, RetrievalResult
from app.tools.search_knowledge_base import search_knowledge_base

# importlib avoids package re-export shadowing the module name for monkeypatch
_skb_module = importlib.import_module("app.tools.search_knowledge_base")


class _FakeRetrieverProvider:
    def __init__(self, documents):
        self._documents = documents
        self.last_request: RetrievalRequest | None = None

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.last_request = request
        return RetrievalResult(query=request.query, documents=self._documents)


def test_search_knowledge_base_returns_hit_with_structured_documents(monkeypatch):
    documents = [Document(page_content="报销比例是 80%", metadata={"_file_name": "policy.md"})]
    provider = _FakeRetrieverProvider(documents)
    monkeypatch.setattr(
        _skb_module,
        "get_default_provider_container",
        lambda: type("Container", (), {"retriever_provider": provider})(),
    )

    result = search_knowledge_base.invoke({"query": "报销比例是多少", "space_id": "finance"})

    assert result["hit"] is True
    assert len(result["documents"]) == 1
    assert result["documents"][0]["content"] == "报销比例是 80%"
    assert result["documents"][0]["metadata"]["_file_name"] == "policy.md"


def test_search_knowledge_base_returns_no_hit_for_empty_results(monkeypatch):
    provider = _FakeRetrieverProvider([])
    monkeypatch.setattr(
        _skb_module,
        "get_default_provider_container",
        lambda: type("Container", (), {"retriever_provider": provider})(),
    )

    result = search_knowledge_base.invoke({"query": "不存在的问题", "space_id": "finance"})

    assert result["hit"] is False
    assert result["documents"] == []


def test_search_knowledge_base_result_is_json_serializable(monkeypatch):
    documents = [Document(page_content="内容", metadata={"_file_name": "a.md"})]
    provider = _FakeRetrieverProvider(documents)
    monkeypatch.setattr(
        _skb_module,
        "get_default_provider_container",
        lambda: type("Container", (), {"retriever_provider": provider})(),
    )

    import json

    result = search_knowledge_base.invoke({"query": "问题", "space_id": "finance"})
    json.dumps(result)  # must not raise
