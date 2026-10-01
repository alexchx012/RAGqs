"""生成侧的父子切块消费（parent-child-chunking）：装配后命中、完整正文、预算口径与收手。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from app.agents import SelfEvaluationResult
from app.chat import budget as budget_module
from app.chat.budget import (
    EFFORT_TOKEN_LIMITS,
    TOOL_OBSERVATION_TOKEN_ALLOWANCE,
    BudgetMeter,
    BudgetPolicy,
    conservative_chat_token_estimate,
)
from app.chat.models import ChatProviderResponse, RetrievalHitOutcome, RetrievalOutcome
from app.chat.ports import (
    ChatProviderRequest,
    IndexingAbSourceFilterPort,
    IndexingChatRetrievalPort,
    RecordingChatRetrievalPort,
)
from app.chat.prompt import _context_block
from app.chat.schema import chat_generation_event_table, chat_generation_table, chat_message_table
from app.chat.worker import ChatGenerationWorker
from app.indexing.models import IndexChunk, RetrievalHit, RetrievalResult
from app.usage.budget import BudgetMeterPolicy
from tests.chat.conftest import build_test_env, provision_and_login

LONG_BODY = "父块正文，包含命中子块之外的同父内容。" * 60


# ---------------------------------------------------------------------------
# 检索端口：装配后命中，而不是精排前候选池
# ---------------------------------------------------------------------------


def _index_chunk(chunk_id: str, document_id: str = "doc_1") -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="gen_index_1",
        publication_id=f"pub_{document_id}",
        document_id=document_id,
        document_version_id="ver_1",
        space_id="space_1",
        text=f"child {chunk_id}",
        embedding_text=f"child {chunk_id}",
        locator={},
        snippet=f"child {chunk_id}",
        media_kind="text/markdown",
        manifest_hash="hash_1",
    )


class _Request:
    def __init__(self, result_for: Any) -> None:
        self._result_for = result_for

    def search(self, query: str, *, profile: Any, **kwargs: Any) -> RetrievalResult:
        del query, kwargs
        return self._result_for(profile)

    def __exit__(self, *args: Any) -> None:
        del args


class _Indexing:
    def __init__(self, result_for: Any) -> None:
        self._result_for = result_for

    def open_retrieval_request(self) -> _Request:
        return _Request(self._result_for)


def _result(profile: Any, hits: tuple[RetrievalHit, ...], pool: tuple[RetrievalHit, ...]):
    return RetrievalResult(
        hits=hits, generation_id="gen_index_1", profile=profile, candidate_hits=pool
    )


def test_chat_port_hands_generation_only_the_assembled_hits() -> None:
    assembled = RetrievalHit(_index_chunk("chunk_1"), 1.0, "sparse", context_text=LONG_BODY)
    pool = (
        assembled,
        RetrievalHit(_index_chunk("chunk_2"), 0.5, "sparse"),
        RetrievalHit(_index_chunk("chunk_3"), 0.1, "sparse"),
    )
    port = IndexingChatRetrievalPort(
        _Indexing(lambda profile: _result(profile, (assembled,), pool))
    )

    outcome = port.search(
        "问题",
        principal=None,
        narrowing_scope=None,
        profile_id="default",
        profile_version="1",
        effort="quick",
    )

    assert [hit.chunk_id for hit in outcome.hits] == ["chunk_1"]
    assert outcome.hits[0].context_text == LONG_BODY
    assert outcome.hits[0].snippet == "child chunk_1"


def test_ab_source_filter_compares_assembled_hits_not_candidate_pools() -> None:
    shared = RetrievalHit(_index_chunk("chunk_1"), 1.0, "sparse")

    def result_for(profile: Any) -> RetrievalResult:
        # 两套配置的精排前候选池不同，最终进入上下文的命中相同。
        extra = RetrievalHit(_index_chunk(f"pool_{profile.version}"), 0.2, "sparse")
        return _result(profile, (shared,), (shared, extra))

    port = IndexingAbSourceFilterPort(_Indexing(result_for))

    assert port.candidate_sources_identical(
        "问题",
        principal=None,
        narrowing_scope=None,
        candidate_profiles=(("default", "a"), ("default", "b")),
        effort="quick",
    )


# ---------------------------------------------------------------------------
# prompt 与预算估算
# ---------------------------------------------------------------------------


def test_context_block_sends_the_full_body_once() -> None:
    block = _context_block({"text": LONG_BODY, "snippet": "高亮片段"})

    # 块头之后只有完整正文一份：不截断，也不再追加 snippet。
    assert block.split("\n", 1)[1] == LONG_BODY
    assert "高亮片段" not in block


def test_context_block_falls_back_to_snippet_for_unassembled_items() -> None:
    assert _context_block({"snippet": "legacy snippet"}).endswith("\nlegacy snippet")


def _worker() -> ChatGenerationWorker:
    return build_test_env()["runtime"].resolve("chat_generation_worker")


def test_answer_estimate_counts_context_body_history_and_tool_turns() -> None:
    request = ChatProviderRequest(
        generation_id="gen_1",
        owner_user_id="user_1",
        content="问题",
        effort_level="think",
        candidate=None,
        context_items=({"text": LONG_BODY, "snippet": LONG_BODY[:500]},),
        history_messages=({"role": "user", "content": "上一轮"},),
        followup_messages=({"role": "tool", "tool_call_id": "c1", "content": "观测" * 300},),
    )

    estimate = _worker()._estimated_provider_tokens(request)

    assert estimate == conservative_chat_token_estimate("问题", [LONG_BODY, "上一轮", "观测" * 300])


def test_quick_budget_admits_one_fully_loaded_answer_call() -> None:
    from datetime import UTC, datetime

    policy = BudgetPolicy.for_effort(
        "quick",
        price_version="test",
        max_estimated_cost_amount=100.0,
        pricer=lambda operation, tokens: 0.0,
    )
    meter = BudgetMeter(policy=policy)
    # 问题上限 4,000 + 上下文总量硬闸 48,000 + 3 轮历史上限 13,500。
    estimate = conservative_chat_token_estimate("问" * 4000, ["文" * 48000, "史" * 13500])

    assert estimate == 74050
    assert meter.gate("chat_generation", estimated_tokens=estimate, now=datetime.now(UTC)) is None


def test_persistent_budget_defaults_match_the_generation_budget_table() -> None:
    policy = BudgetMeterPolicy.configured(
        price_version_id="p1",
        currency_code="CNY",
        max_estimated_cost_amounts={effort: 1 for effort in ("quick", "think", "deep")},
        cost_estimator=lambda operation, tokens: 0,
    )

    assert (
        {effort: item.max_total_tokens for effort, item in policy.efforts.items()}
        == EFFORT_TOKEN_LIMITS
        == {"quick": 80_000, "think": 160_000, "deep": 320_000}
    )
    assert policy.version == budget_module.RAG_BUDGET_POLICY_VERSION == "chat-rag-budget-v3"


# ---------------------------------------------------------------------------
# worker：工具观测正文与预算收手
# ---------------------------------------------------------------------------


def _hit(document_id: str, chunk_id: str, *, context_text: str | None = None):
    return RetrievalHitOutcome(
        document_id=document_id,
        document_version_id=f"ver_{document_id}",
        publication_id=f"pub_{document_id}",
        chunk_id=chunk_id,
        space_id="space_1",
        locator={"page": 1},
        snippet=f"snippet {document_id}",
        context_text=context_text,
    )


class _ToolsThenAnswer:
    """Asks for one retrieval tool call whenever tools are offered."""

    def __init__(self, *, input_tokens: int = 3) -> None:
        self.calls: list[ChatProviderRequest] = []
        self._input_tokens = input_tokens

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls.append(request)
        if request.tools and not request.followup_messages:
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
        return ChatProviderResponse(
            content="final answer", input_tokens=self._input_tokens, output_tokens=6
        )


def _start(env: dict, username: str, *, content: str, effort: str) -> str:
    token, _ = provision_and_login(env["identity"], username)
    accepted = env["client"].post(
        "/v1/chat",
        json={"content": content, "effort_level": effort},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"{username}-1"},
    )
    assert accepted.status_code == 202, accepted.text
    return str(accepted.json()["data"]["generation_id"])


def _notices(env: dict, generation_id: str) -> list[dict[str, Any]]:
    with env["engine"].connect() as connection:
        return [
            dict(row[0])
            for row in connection.execute(
                select(chat_generation_event_table.c.data_json).where(
                    chat_generation_event_table.c.generation_id == generation_id,
                    chat_generation_event_table.c.event_type == "notice",
                )
            ).all()
        ]


def _status(env: dict, generation_id: str) -> str:
    with env["engine"].connect() as connection:
        return str(
            connection.execute(
                select(chat_generation_table.c.status).where(
                    chat_generation_table.c.id == generation_id
                )
            ).scalar_one()
        )


def test_tool_observation_carries_full_context_body_unescaped(monkeypatch) -> None:
    provider = _ToolsThenAnswer()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {"第一轮的问题": RetrievalOutcome(hits=(_hit("doc_round", "c_round"),))}
    env = build_test_env(retrieval=retrieval, provider=provider)
    generation_id = _start(env, "pc-tool-1", content="第一轮的问题", effort="think")
    worker = env["runtime"].resolve("chat_generation_worker")
    tool_retrieval = RecordingChatRetrievalPort()
    tool_retrieval.outcomes = {
        "tool query": RetrievalOutcome(hits=(_hit("doc_tool", "c_tool", context_text=LONG_BODY),))
    }
    monkeypatch.setattr(worker, "_tool_retrieval", tool_retrieval)

    worker.run_once()

    assert _status(env, generation_id) == "completed"
    tool_turn = provider.calls[1].followup_messages[1]
    assert tool_turn["role"] == "tool"
    observation = json.loads(str(tool_turn["content"]))
    assert observation["documents"][0]["text"] == LONG_BODY
    assert "\\u" not in str(tool_turn["content"])


def test_tool_loop_answers_without_tools_when_budget_cannot_cover_another_step(
    monkeypatch,
) -> None:
    provider = _ToolsThenAnswer()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {"问题": RetrievalOutcome(hits=(_hit("doc_1", "c_1", context_text="短"),))}
    env = build_test_env(retrieval=retrieval, provider=provider)
    generation_id = _start(env, "pc-tool-2", content="问题", effort="think")
    first_call = conservative_chat_token_estimate("问题", ["短"])
    # 容得下一次回答，容不下「本次 + 一次工具观测 + 最终回答」。
    limit = first_call + TOOL_OBSERVATION_TOKEN_ALLOWANCE
    assert limit < 2 * first_call + TOOL_OBSERVATION_TOKEN_ALLOWANCE
    monkeypatch.setitem(budget_module.EFFORT_TOKEN_LIMITS, "think", limit)
    worker = env["runtime"].resolve("chat_generation_worker")
    tool_retrieval = RecordingChatRetrievalPort()
    tool_retrieval.outcomes = {"tool query": RetrievalOutcome(hits=())}
    monkeypatch.setattr(worker, "_tool_retrieval", tool_retrieval)

    worker.run_once()

    assert _status(env, generation_id) == "completed"
    assert tool_retrieval.searches == []
    assert len(provider.calls) == 1
    assert provider.calls[0].tools == ()
    assert {"kind": "retrieval_degraded", "detail": {"reason": "budget_exhausted"}} in _notices(
        env, generation_id
    )


class _AnswerOnly:
    """Answers directly and reports ~1,000 consumed tokens per call."""

    def __init__(self) -> None:
        self.calls: list[ChatProviderRequest] = []

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls.append(request)
        return ChatProviderResponse(content="final answer", input_tokens=1000, output_tokens=6)


class _AlwaysRewrite:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, *, query, candidate_content, citations, context_items):
        del candidate_content, citations, context_items
        self.calls += 1
        return SelfEvaluationResult(
            acceptable=False,
            rewritten_query=f"{query} again",
            diagnosis={"reason": "low_relevance"},
        )


def test_self_evaluation_publishes_current_draft_when_budget_cannot_cover_a_round(
    monkeypatch,
) -> None:
    provider = _AnswerOnly()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {
        "问题": RetrievalOutcome(hits=(_hit("doc_1", "c_1", context_text=LONG_BODY),)),
        "问题 again": RetrievalOutcome(hits=(_hit("doc_2", "c_2", context_text=LONG_BODY),)),
    }
    env = build_test_env(retrieval=retrieval, provider=provider)
    generation_id = _start(env, "pc-eval-1", content="问题", effort="think")
    answer_call = conservative_chat_token_estimate("问题", [LONG_BODY])
    # 首个回答调用装得下；消耗掉约 1,000 token 后，剩余不够再来一轮。
    monkeypatch.setitem(budget_module.EFFORT_TOKEN_LIMITS, "think", answer_call + 100)
    evaluator = _AlwaysRewrite()
    worker = env["runtime"].resolve("chat_generation_worker")
    monkeypatch.setattr(worker, "_self_evaluator", evaluator)
    # 隔离工具循环的收手判定：本用例只验证自评估轮次的预算收手。
    monkeypatch.setattr(worker, "_tool_step_affordable", lambda request, budget: True)

    worker.run_once()

    # 没有收手时 AlwaysRewrite 会触发第二轮检索，且第二轮回答预留失败使生成失败。
    assert evaluator.calls == 1
    assert [search["query"] for search in retrieval.searches] == ["问题"]
    assert len(provider.calls) == 1
    assert _status(env, generation_id) == "completed"
    assert (
        _notices(env, generation_id).count(
            {"kind": "retrieval_degraded", "detail": {"reason": "budget_exhausted"}}
        )
        == 1
    )


class _FullContextProvider:
    def __init__(self, *, tool_calls: int = 0) -> None:
        self.calls: list[ChatProviderRequest] = []
        self.tool_calls = tool_calls

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls.append(request)
        input_tokens = int(
            1.1
            * (len(request.content) + sum(len(str(item["text"])) for item in request.context_items))
        )
        if request.tools and not request.followup_messages and self.tool_calls:
            return ChatProviderResponse(
                content="",
                input_tokens=input_tokens,
                output_tokens=100,
                tool_calls=tuple(
                    {
                        "id": f"call_{index}",
                        "name": "search_retrieval",
                        "arguments": '{"query": "tool query"}',
                    }
                    for index in range(self.tool_calls)
                ),
            )
        first = request.context_items[0]
        return ChatProviderResponse(
            content=f"draft {len(self.calls)} [{first['document_id']}@{first['document_version_id']}]",
            input_tokens=input_tokens,
            output_tokens=100,
        )


class _RewriteIndexingRequest:
    def __init__(self, indexing):
        self.indexing = indexing
        self.closed = False

    def search(self, query, *, profile, **kwargs):
        index = query.count(" again")
        size = (40_000, 40_000, 20_000, 48_000)[index]
        chunk = replace(_index_chunk(f"c_{index}", f"doc_{index}"), text="文" * size)
        hit = RetrievalHit(chunk, 1.0, "sparse")
        return _result(profile, (hit,), (hit,))

    def resolve_citation(self, hit, *, principal):
        assert not self.closed
        chunk = hit.chunk
        if (
            self.indexing.revoke
            and len(self.indexing.requests) == 4
            and chunk.document_id == "doc_2"
        ):
            return {"state": "unavailable"}
        return {
            "state": "available",
            "document_id": chunk.document_id,
            "document_version_id": chunk.document_version_id,
            "publication_id": chunk.publication_id,
            "chunk_id": chunk.chunk_id,
            "space_id": chunk.space_id,
            "locator": chunk.locator,
            "snippet": chunk.snippet,
        }

    def __exit__(self, *args):
        self.closed = True


class _RewriteIndexing:
    def __init__(self, *, revoke):
        self.revoke = revoke
        self.requests = []

    def open_retrieval_request(self):
        request = _RewriteIndexingRequest(self)
        self.requests.append(request)
        return request


@pytest.mark.parametrize("revoke", [False, True])
def test_budget_fallback_revalidates_original_draft_through_indexing_port(monkeypatch, revoke):
    indexing = _RewriteIndexing(revoke=revoke)
    provider = _FullContextProvider()
    env = build_test_env(retrieval=IndexingChatRetrievalPort(indexing), provider=provider)
    generation_id = _start(env, "pc-real-port", content="问题", effort="think")
    worker = env["runtime"].resolve("chat_generation_worker")
    monkeypatch.setattr(worker, "_self_evaluator", _AlwaysRewrite())

    worker.run_once()

    with env["engine"].connect() as connection:
        answer = connection.execute(
            select(chat_message_table.c.content, chat_message_table.c.citations_json).where(
                chat_message_table.c.generation_id == generation_id,
                chat_message_table.c.role == "assistant",
            )
        ).one_or_none()
    if revoke:
        assert answer is None or (answer.content == "" and not answer.citations_json)
        assert _status(env, generation_id) != "completed"
    else:
        assert _status(env, generation_id) == "completed"
        assert answer.content.startswith("draft 3 ")
        assert [item["document_id"] for item in answer.citations_json] == ["doc_2"]
    assert all(request.closed for request in indexing.requests)


def test_first_answer_that_does_not_fit_still_fails_structurally(monkeypatch):
    provider = _AnswerOnly()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {"问题": RetrievalOutcome(hits=(_hit("doc_1", "c_1"),))}
    env = build_test_env(retrieval=retrieval, provider=provider)
    generation_id = _start(env, "pc-first-fail", content="问题", effort="quick")
    monkeypatch.setitem(budget_module.EFFORT_TOKEN_LIMITS, "quick", 1000)

    env["runtime"].resolve("chat_generation_worker").run_once()

    assert _status(env, generation_id) == "failed"
    assert provider.calls == []
    with env["engine"].connect() as connection:
        error = connection.execute(
            select(chat_generation_event_table.c.data_json).where(
                chat_generation_event_table.c.generation_id == generation_id,
                chat_generation_event_table.c.event_type == "error",
            )
        ).scalar_one()
    assert error["code"] == "budget_exhausted"


@pytest.mark.parametrize("token_limit,expected_status", [(1000, "failed"), (80_000, "completed")])
def test_indexing_requests_close_when_answer_fails_or_uses_no_citations(
    monkeypatch, token_limit, expected_status
):
    indexing = _RewriteIndexing(revoke=False)
    env = build_test_env(retrieval=IndexingChatRetrievalPort(indexing), provider=_AnswerOnly())
    generation_id = _start(env, "pc-close-port", content="问题", effort="quick")
    monkeypatch.setitem(budget_module.EFFORT_TOKEN_LIMITS, "quick", token_limit)

    env["runtime"].resolve("chat_generation_worker").run_once()

    assert _status(env, generation_id) == expected_status
    assert all(request.closed for request in indexing.requests)


@pytest.mark.parametrize("last_size,expected_calls", [(48_000, 3), (20_000, 4)])
def test_rewrite_checks_new_context_and_retains_previous_draft_and_citations(
    monkeypatch, last_size, expected_calls
) -> None:
    provider = _FullContextProvider()
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {
        "问题"
        + " again"
        * index: RetrievalOutcome(
            hits=(_hit(f"doc_{index}", f"c_{index}", context_text="文" * size),)
        )
        for index, size in enumerate((40_000, 40_000, 20_000, last_size))
    }
    env = build_test_env(retrieval=retrieval, provider=provider)
    generation_id = _start(env, f"pc-grow-{last_size}", content="问题", effort="think")
    worker = env["runtime"].resolve("chat_generation_worker")

    class RewriteThreeTimes(_AlwaysRewrite):
        def evaluate(self, **kwargs):
            if self.calls == 3:
                return SelfEvaluationResult(acceptable=True)
            return super().evaluate(**kwargs)

    monkeypatch.setattr(worker, "_self_evaluator", RewriteThreeTimes())
    worker.run_once()

    assert _status(env, generation_id) == "completed"
    assert len(provider.calls) == expected_calls
    assert len(retrieval.searches) == 4
    with env["engine"].connect() as connection:
        answer = connection.execute(
            select(chat_message_table.c.content, chat_message_table.c.citations_json).where(
                chat_message_table.c.generation_id == generation_id,
                chat_message_table.c.role == "assistant",
            )
        ).one()
    assert answer.content.startswith(f"draft {expected_calls} ")
    assert [item["document_id"] for item in answer.citations_json] == [f"doc_{expected_calls - 1}"]
    if last_size == 48_000:
        assert {"kind": "retrieval_degraded", "detail": {"reason": "budget_exhausted"}} in _notices(
            env, generation_id
        )


@pytest.mark.parametrize(
    "question_size,context_size,calls,observation_character,search_count",
    [(4000, 48_000, 1, "观", 0), (1000, 30_000, 2, "观", 1), (1000, 30_000, 1, '"', 1)],
)
def test_large_tool_observations_leave_budget_for_final_answer(
    monkeypatch, question_size, context_size, calls, observation_character, search_count
) -> None:
    question = "问" * question_size
    provider = _FullContextProvider(tool_calls=calls)
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes = {
        question: RetrievalOutcome(hits=(_hit("doc_1", "c_1", context_text="文" * context_size),))
    }
    env = build_test_env(retrieval=retrieval, provider=provider)
    generation_id = _start(env, f"pc-large-tool-{calls}", content=question, effort="think")
    worker = env["runtime"].resolve("chat_generation_worker")
    tool_retrieval = RecordingChatRetrievalPort()
    tool_retrieval.outcomes = {
        "tool query": RetrievalOutcome(
            hits=(_hit("doc_tool", "c_tool", context_text=observation_character * 48_000),)
        )
    }
    monkeypatch.setattr(worker, "_tool_retrieval", tool_retrieval)

    worker.run_once()

    assert _status(env, generation_id) == "completed"
    assert {"kind": "retrieval_degraded", "detail": {"reason": "budget_exhausted"}} in _notices(
        env, generation_id
    )
    assert provider.calls[-1].tools == ()
    assert len(tool_retrieval.searches) == search_count
    if calls == 2:
        observations = [
            json.loads(str(turn["content"]))
            for turn in provider.calls[-1].followup_messages
            if turn["role"] == "tool"
        ]
        assert observations[0]["documents"][0]["text"] == "观" * 48_000
        assert observations[1]["degradations"] == [{"code": "budget_exhausted"}]
