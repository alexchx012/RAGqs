"""Conversation-history pipeline acceptance tests (work item B).

Covers A2/A3/A10/A11 evidence: history turns enter the provider request,
assistant answers are truncated to digests, the deep strategy plan sees the
same history, and the retrieval search receives the recent user queries.
"""

from __future__ import annotations

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest, RecordingChatRetrievalPort
from app.chat.prompt import assemble_generation_messages, assemble_generation_prompt

from .conftest import FakeChatProvider, build_test_env, provision_and_login


def _auth(env: dict, username: str) -> dict[str, str]:
    token, _ = provision_and_login(env["identity"], username)
    return {"Authorization": f"Bearer {token}"}


class DigestEvidenceProvider(FakeChatProvider):
    """Long turn-1 answers so the assistant digest truncation is observable."""

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        response = super().generate(request)
        if request.purpose == "answer":
            return ChatProviderResponse(
                content="长回答。" * 200,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        return response


def _ask_first_turn(env: dict, headers: dict[str, str], key: str) -> str:
    accepted = env["client"].post(
        "/v1/chat",
        json={"content": "RAGqs 的检索架构是什么？", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": key},
    )
    assert accepted.status_code == 202, accepted.text
    conversation_id = accepted.json()["data"]["conversation_id"]
    env["runtime"].resolve("chat_generation_worker").run_once()
    return conversation_id


def test_conversation_history_enters_provider_and_retrieval() -> None:
    provider = DigestEvidenceProvider()
    retrieval = RecordingChatRetrievalPort()
    env = build_test_env(retrieval=retrieval, provider=provider)
    headers = _auth(env, "history-user-1")

    conversation_id = _ask_first_turn(env, headers, "history-t1")
    # Second turn via the non-streaming compat exit (the conversation messages
    # POST is the SSE-coupled endpoint and would hold the stream open).
    follow_up = env["client"].post(
        "/v1/chat",
        json={
            "conversation_id": conversation_id,
            "content": "它呢？",
            "effort_level": "quick",
        },
        headers={**headers, "Idempotency-Key": "history-t2"},
    )
    assert follow_up.status_code == 202, follow_up.text
    env["runtime"].resolve("chat_generation_worker").run_once()

    answer_calls = [call for call in provider.calls if call.purpose == "answer"]
    last = answer_calls[-1]
    assert last.content == "它呢？"
    roles = [str(message["role"]) for message in last.history_messages]
    contents = [str(message["content"]) for message in last.history_messages]
    assert roles == ["user", "assistant"]
    assert contents[0] == "RAGqs 的检索架构是什么？"
    assert len(contents[1]) == 500

    assert retrieval.searches[-1]["recent_queries"] == ["RAGqs 的检索架构是什么？"]


def test_deep_strategy_plan_sees_history() -> None:
    provider = DigestEvidenceProvider()
    env = build_test_env(provider=provider)
    headers = _auth(env, "history-user-2")

    conversation_id = _ask_first_turn(env, headers, "history-deep-t1")
    follow_up = env["client"].post(
        "/v1/chat",
        json={
            "conversation_id": conversation_id,
            "content": "它呢？",
            "effort_level": "deep",
        },
        headers={**headers, "Idempotency-Key": "history-deep-t2"},
    )
    assert follow_up.status_code == 202, follow_up.text
    env["runtime"].resolve("chat_generation_worker").run_once()

    plan_calls = [call for call in provider.calls if call.purpose == "deep_retrieval_plan"]
    assert plan_calls, "deep tier must issue a strategy plan request"
    roles = [str(message["role"]) for message in plan_calls[-1].history_messages]
    assert roles == ["user", "assistant"]
    assert str(plan_calls[-1].history_messages[0]["content"]) == "RAGqs 的检索架构是什么？"


def test_assemble_generation_messages_orders_history_then_prompt() -> None:
    request = ChatProviderRequest(
        generation_id="gen_history_1",
        owner_user_id="user_1",
        content="它呢？",
        effort_level="quick",
        candidate=None,
        context_items=(),
        history_messages=(
            {"role": "user", "content": "RAGqs 的检索架构是什么？"},
            {"role": "assistant", "content": "摘要"},
        ),
    )
    messages = assemble_generation_messages(request)
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    final_content = messages[-1]["content"]
    assert "RAGqs 的检索架构是什么？" not in final_content
    assert "它呢？" in final_content


def test_plan_prompt_embeds_history_inside_single_message() -> None:
    request = ChatProviderRequest(
        generation_id="gen_history_2",
        owner_user_id="user_1",
        content="它呢？",
        effort_level="deep",
        candidate=None,
        context_items=(),
        history_messages=(
            {"role": "user", "content": "RAGqs 的检索架构是什么？"},
            {"role": "assistant", "content": "摘要"},
        ),
        purpose="deep_retrieval_plan",
    )
    prompt = assemble_generation_prompt(request)
    assert "最近对话" in prompt
    assert "用户: RAGqs 的检索架构是什么？" in prompt
    messages = assemble_generation_messages(request)
    assert [message["role"] for message in messages] == ["user"]
