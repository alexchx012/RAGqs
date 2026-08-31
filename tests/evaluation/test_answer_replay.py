"""Production answer replay adapter contracts (A7/A18)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat.models import ChatProviderResponse
from app.evaluation.ports import OnlineAnswerReplayAdapter
from app.platform.errors import PlatformError


class _RecordingChatProvider:
    def __init__(self, *, response: object | None = None, error: Exception | None = None):
        self.requests = []
        self.response = response or ChatProviderResponse(
            content="online answer",
            input_tokens=8,
            output_tokens=5,
        )
        self.error = error

    def generate(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def test_online_answer_replay_uses_the_chat_provider_pipeline_without_config_leak() -> None:
    provider = _RecordingChatProvider()
    adapter = OnlineAnswerReplayAdapter(provider)
    context = (
        {
            "document_id": "doc_1",
            "document_version_id": "version_1",
            "space_id": "space_1",
            "library": "handbook",
            "snippet": "supporting text",
        },
    )

    answer = adapter.replay(
        question="What is the policy?",
        source_ref="message_1",
        principal=SimpleNamespace(user_id="user_1"),
        space_id="space_1",
        candidate_config_version="cfg_a",
        session_id="shadow:run_1:item_1",
        context_items=context,
    )

    assert answer == "online answer"
    request = provider.requests[0]
    assert request.generation_id == "shadow:run_1:item_1"
    assert request.owner_user_id == "user_1"
    assert request.content == "What is the policy?"
    assert request.effort_level == "think"
    assert request.candidate is None
    assert request.context_items == context
    assert "cfg_a" not in str(request)


def test_online_answer_replay_maps_provider_failures_to_retryable_evaluation_error() -> None:
    provider = _RecordingChatProvider(
        error=PlatformError("provider_unavailable", "provider failed", {}, 503, True)
    )
    adapter = OnlineAnswerReplayAdapter(provider)

    with pytest.raises(PlatformError) as raised:
        adapter.replay(
            question="q",
            source_ref="message_1",
            principal=SimpleNamespace(user_id="user_1"),
            space_id="space_1",
            candidate_config_version="cfg_a",
            session_id="shadow:run:item:cfg_a",
        )

    assert raised.value.code == "evaluation_generation_unavailable"
    assert raised.value.status_code == 503
    assert raised.value.retryable is True
