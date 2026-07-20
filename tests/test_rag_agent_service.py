"""Tests for RagAgentService lazy knowledge_catalog and graph_registry loading."""

from unittest.mock import Mock

from app.services.rag_agent_service import RagAgentService


class _FakeChatModelProvider:
    def create_chat_model(self, streaming: bool = True):
        return Mock()


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
