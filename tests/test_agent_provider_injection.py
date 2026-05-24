import pytest

from app.providers import FakeChatModelProvider
from app.services.rag_agent_service import RagAgentService


@pytest.mark.asyncio
async def test_rag_agent_service_uses_injected_chat_model_provider_lazily():
    created = []

    def agent_factory(model, tools, checkpointer):
        created.append((model, tools, checkpointer))
        return object()

    provider = FakeChatModelProvider(response="ok")
    service = RagAgentService(
        streaming=False,
        chat_model_provider=provider,
        agent_factory=agent_factory,
        tools=[],
        agent_runtime="legacy",
    )

    assert service.model is None

    await service._initialize_agent()

    assert service._agent_initialized is True
    assert service.model.response == "ok"
    assert created == [(service.model, [], service.checkpointer)]


@pytest.mark.asyncio
async def test_rag_agent_service_uses_injected_checkpoint_provider():
    created = []
    checkpointer = object()

    class RecordingCheckpointProvider:
        def __init__(self):
            self.create_calls = 0

        def create_checkpointer(self):
            self.create_calls += 1
            return checkpointer

    def agent_factory(model, tools, checkpointer):
        created.append(checkpointer)
        return object()

    provider = RecordingCheckpointProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=FakeChatModelProvider(response="ok"),
        checkpoint_provider=provider,
        agent_factory=agent_factory,
        tools=[],
        agent_runtime="legacy",
    )

    await service._initialize_agent()

    assert provider.create_calls == 1
    assert service.checkpointer is checkpointer
    assert created == [checkpointer]
