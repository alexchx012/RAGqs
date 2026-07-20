"""Tests for RagAgentService lazy knowledge_catalog and graph_registry loading."""

from unittest.mock import Mock

from app.knowledge.catalog import InMemoryKnowledgeCatalog
from app.services.rag_agent_service import RagAgentService


class _FakeChatModelProvider:
    def create_chat_model(self, streaming: bool = True):
        return Mock()


class _FakeCompiledGraph:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def invoke(self, payload, config):
        self.calls.append((payload, config))
        return dict(self._result)


def test_constructing_service_does_not_resolve_knowledge_catalog():
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
    )

    assert service.knowledge_catalog_provider is None
    assert service.graph_registry is None


def test_get_knowledge_catalog_lazily_builds_and_caches():
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
    )
    fake_catalog = Mock()
    service.knowledge_catalog_provider = fake_catalog

    first = service._get_knowledge_catalog()
    second = service._get_knowledge_catalog()

    assert first is fake_catalog
    assert second is fake_catalog


def test_get_graph_registry_builds_once_and_caches():
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
        retriever_provider=Mock(),
    )

    first = service._get_graph_registry()
    second = service._get_graph_registry()

    assert first is second
    assert set(first.keys()) == {"baseline", "agentic"}


def test_invoke_explicit_graph_uses_baseline_when_space_has_no_rag_path():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    baseline_graph = _FakeCompiledGraph({"answer": "baseline answer", "sources": [], "events": []})
    agentic_graph = _FakeCompiledGraph({"answer": "agentic answer", "sources": [], "events": []})
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
        retriever_provider=Mock(),
    )
    service.knowledge_catalog_provider = catalog
    service.graph_registry = {"baseline": baseline_graph, "agentic": agentic_graph}

    service._invoke_explicit_graph("q", "s1", space_id="finance")

    assert len(baseline_graph.calls) == 1
    assert len(agentic_graph.calls) == 0


def test_invoke_explicit_graph_uses_configured_agentic_path():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", rag_path="agentic")
    baseline_graph = _FakeCompiledGraph({"answer": "baseline answer", "sources": [], "events": []})
    agentic_graph = _FakeCompiledGraph({"answer": "agentic answer", "sources": [], "events": []})
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
        retriever_provider=Mock(),
    )
    service.knowledge_catalog_provider = catalog
    service.graph_registry = {"baseline": baseline_graph, "agentic": agentic_graph}

    service._invoke_explicit_graph("q", "s1", space_id="finance")

    assert len(agentic_graph.calls) == 1
    assert len(baseline_graph.calls) == 0


def test_invoke_explicit_graph_falls_back_to_baseline_for_unknown_path(caplog):
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", rag_path="unknown_future_path")
    baseline_graph = _FakeCompiledGraph({"answer": "baseline answer", "sources": [], "events": []})
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
        retriever_provider=Mock(),
    )
    service.knowledge_catalog_provider = catalog
    service.graph_registry = {"baseline": baseline_graph}

    service._invoke_explicit_graph("q", "s1", space_id="finance")

    assert len(baseline_graph.calls) == 1


def test_invoke_explicit_graph_prefers_explicitly_injected_graph_for_backward_compat():
    injected_graph = _FakeCompiledGraph({"answer": "injected answer", "sources": [], "events": []})
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
        use_explicit_graph=True,
        explicit_graph=injected_graph,
    )

    result = service._invoke_explicit_graph("q", "s1", space_id="default")

    assert result["answer"] == "injected answer"
    assert len(injected_graph.calls) == 1


class _FakeStreamingCompiledGraph:
    def __init__(self, chunks):
        self._chunks = chunks
        self.calls = []

    def stream(self, payload, config, stream_mode=None):
        self.calls.append((payload, config, stream_mode))
        yield from self._chunks

    def invoke(self, payload, config):
        raise AssertionError("stream() should be used, not invoke()")


def test_stream_explicit_graph_uses_configured_agentic_path():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", rag_path="agentic")
    baseline_graph = _FakeStreamingCompiledGraph([])
    agentic_graph = _FakeStreamingCompiledGraph(
        [("updates", {"final_response": {"answer": "agentic streamed"}})]
    )
    service = RagAgentService(
        streaming=False,
        chat_model_provider=_FakeChatModelProvider(),
        enabled_tool_names=["get_current_time"],
        retriever_provider=Mock(),
    )
    service.knowledge_catalog_provider = catalog
    service.graph_registry = {"baseline": baseline_graph, "agentic": agentic_graph}

    chunks = list(service._stream_explicit_graph("q", "s1", space_id="finance"))

    assert len(agentic_graph.calls) == 1
    assert len(baseline_graph.calls) == 0
    assert any(chunk.get("type") == "done" for chunk in chunks)
