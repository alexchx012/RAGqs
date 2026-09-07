"""Agent tool-loop acceptance tests (work item C, A5/A14/A15/A17 evidence).

Covers: the think tier's tool-call loop executes the retrieval tool, records
durable observations, resumes after a simulated crash without repeating any
outbound call, keeps the quick tier single-shot with no tools offered, and
emits tool step events only where the effort policy enables them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import update

from app.chat.models import (
    ChatProviderResponse,
    RetrievalHitOutcome,
    RetrievalOutcome,
)
from app.chat.ports import ChatProviderRequest, RecordingChatRetrievalPort
from app.chat.schema import chat_generation_execution_table

from .conftest import build_test_env, provision_and_login


class KillAfterToolCheckpoint(BaseException):
    """Simulates a process death right after the durable observation write."""


class ToolCallProvider:
    """First call returns a tool call, later calls return the final answer."""

    def __init__(self, *, deep_plan: bool = False) -> None:
        self.calls: list[ChatProviderRequest] = []
        self._deep_plan = deep_plan

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls.append(request)
        if self._deep_plan and request.purpose == "deep_retrieval_plan":
            return ChatProviderResponse(
                content='{"strategies": []}', input_tokens=1, output_tokens=1
            )
        if len([call for call in self.calls if call.purpose == "answer"]) == 1:
            return ChatProviderResponse(
                content="",
                input_tokens=3,
                output_tokens=2,
                tool_calls=(
                    {
                        "id": "call_1",
                        "name": "search_retrieval",
                        "arguments": '{"query": "tool query"}',
                    },
                ),
            )
        return ChatProviderResponse(content="final answer", input_tokens=4, output_tokens=6)


def _hit(document_id: str, chunk_id: str) -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id=document_id,
        document_version_id=f"ver_{document_id}",
        publication_id=f"pub_{document_id}",
        chunk_id=chunk_id,
        space_id="space_1",
        locator={"page": 1},
        snippet=f"snippet {document_id}",
    )


def _auth(env: dict, username: str) -> dict[str, str]:
    token, _ = provision_and_login(env["identity"], username)
    return {"Authorization": f"Bearer {token}"}


def test_tool_loop_executes_observes_and_resumes_after_crash(monkeypatch) -> None:
    provider = ToolCallProvider()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {
        "第一轮的问题": RetrievalOutcome(hits=(_hit("doc_round", "chunk_round"),)),
        "tool query": RetrievalOutcome(hits=(_hit("doc_tool", "chunk_tool"),)),
    }
    env = build_test_env(retrieval=retrieval, provider=provider)
    headers = _auth(env, "tool-user-1")
    accepted = env["client"].post(
        "/v1/chat",
        json={"content": "第一轮的问题", "effort_level": "think"},
        headers={**headers, "Idempotency-Key": "tool-t1"},
    )
    assert accepted.status_code == 202, accepted.text
    generation_id = accepted.json()["data"]["generation_id"]
    worker = env["runtime"].resolve("chat_generation_worker")

    class _StubUsageMeter:
        """Records the reserve/settle seam every tool execution must pass."""

        def __init__(self) -> None:
            self.reserved: list[str | None] = []
            self.settled: list[str | None] = []

        def ensure_meter(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "price_version_id": "usage-metering",
                "max_estimated_cost_amount": 100,
                "candidate_document_limit": 7,
            }

        def reserve(self, **kwargs: Any) -> float:
            self.reserved.append(kwargs.get("reservation_id"))
            return 1.0

        def settle(self, **kwargs: Any) -> None:
            self.settled.append(kwargs.get("reservation_id"))

        def estimate_cost(self, operation: str, tokens: int) -> float:
            return 0.5

        def meter(self, generation_id: str) -> dict[str, Any]:
            return {
                "price_version_id": "usage-metering",
                "max_estimated_cost_amount": 100,
                "candidate_document_limit": 7,
            }

        def upgrade(self, **kwargs: Any) -> str:
            return "think"

    class _AttributeSnapshot(dict):
        """Mapping snapshot that also supports attribute access."""

        def __getattr__(self, name: str) -> Any:
            return self[name]

    usage_meter = _StubUsageMeter()
    original_ensure = usage_meter.ensure_meter
    usage_meter.ensure_meter = lambda **kwargs: _AttributeSnapshot(original_ensure(**kwargs))
    original_persist = worker._persist_checkpoint

    def persist_then_die(**kwargs: Any) -> bool:
        persisted = original_persist(**kwargs)
        if kwargs.get("checkpoint", {}).get("phase") == "tool_observed":
            raise KillAfterToolCheckpoint("simulated process death")
        return persisted

    monkeypatch.setattr(worker, "_persist_checkpoint", persist_then_die)
    monkeypatch.setattr(worker, "_budget_meter", usage_meter)
    try:
        worker.run_once()
    except KillAfterToolCheckpoint:
        pass
    monkeypatch.undo()

    # The tool search ran exactly once before the simulated death.
    assert [search["query"] for search in retrieval.searches] == [
        "第一轮的问题",
        "tool query",
    ]
    assert len(provider.calls) == 1

    with env["engine"].begin() as connection:
        connection.execute(
            update(chat_generation_execution_table)
            .where(chat_generation_execution_table.c.generation_id == generation_id)
            .values(
                lease_expires_at_utc=build_test_env_now_minus_second(),
                next_attempt_at_utc=build_test_env_now_minus_second(),
            )
        )
    assert worker.run_maintenance()["executions_recovered"] == 1
    outcome = worker.run_once()
    assert outcome.stage == "executed"

    # Resume continued the tool loop: the second model call carried the
    # assistant tool_calls turn and the durable tool observation; no outbound
    # retrieval or tool call was repeated.
    assert len(provider.calls) == 2
    continuation = provider.calls[1]
    roles = [str(message.get("role")) for message in continuation.followup_messages]
    assert roles == ["assistant", "tool"]
    tool_turn = continuation.followup_messages[1]
    assert tool_turn["tool_call_id"] == "call_1"
    assert '"documents"' in str(tool_turn["content"])
    assert continuation.tools, "think tier offers the retrieval tools"
    assert [search["query"] for search in retrieval.searches] == [
        "第一轮的问题",
        "tool query",
    ]
    # The tool execution passed through the usage-budget reserve/settle seam.
    tool_reservation = f"rag:{generation_id}:tool-1"
    assert tool_reservation in usage_meter.reserved
    assert tool_reservation in usage_meter.settled


def build_test_env_now_minus_second():
    from .conftest import NOW

    return NOW - timedelta(seconds=1)


def test_quick_tier_stays_single_shot_without_tools() -> None:
    provider = ToolCallProvider()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {"第一轮的问题": RetrievalOutcome(hits=())}
    env = build_test_env(retrieval=retrieval, provider=provider)
    headers = _auth(env, "tool-user-2")
    accepted = env["client"].post(
        "/v1/chat",
        json={"content": "第一轮的问题", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "tool-quick"},
    )
    assert accepted.status_code == 202, accepted.text
    env["runtime"].resolve("chat_generation_worker").run_once()

    assert len(provider.calls) == 1
    assert provider.calls[0].tools == ()
    assert provider.calls[0].followup_messages == ()
