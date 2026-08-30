"""Worker integration tests for the agents self-evaluation rewrite loop.

Exercises the think/deep bounded rewrite -> re-retrieve loop, quick's skip of
self-evaluation and internal isolation of rejected drafts through the real
ChatGenerationWorker against the chat fixtures. Budget-meter gating, tier
limits and candidate selection are covered by tests/chat/test_budget_*.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agents import SelfEvaluationResult
from app.chat.models import ChatProviderResponse, RetrievalHitOutcome, RetrievalOutcome
from app.chat.ports import RecordingChatRetrievalPort
from app.chat.schema import chat_generation_event_table, chat_generation_table
from app.chat.worker import ChatGenerationWorker
from tests.chat.conftest import build_test_env, provision_and_login


def _hit(document_id: str = "doc-1", chunk_id: str = "chunk-1") -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id=document_id,
        document_version_id="ver-1",
        publication_id="pub-1",
        chunk_id=chunk_id,
        space_id="public",
        locator={"anchor": "a"},
        snippet="some grounding snippet",
    )


def _outcome(*hits: RetrievalHitOutcome) -> RetrievalOutcome:
    return RetrievalOutcome(hits=tuple(hits))


def make_worker(env: dict, **kwargs: Any) -> ChatGenerationWorker:
    return ChatGenerationWorker(
        env["engine"],
        clock=env["clock"],
        retrieval=env["retrieval"],
        provider=env["provider"],
        usage=env["usage"],
        calibration=env["calibration"],
        **kwargs,
    )


def _ask(env: dict, principal: Any, conversation_id: str, *, content: str, effort: str):
    from app.chat.models import AskRequest

    return (
        env["runtime"]
        .resolve("chat_generation_service")
        .ask(
            principal=principal,
            conversation_id=conversation_id,
            request=AskRequest(content=content, effort_level=effort, scope=None),
            idempotency_key=f"ask-{content}-{effort}",
        )
    )


def _events(env: dict, generation_id: str) -> list[dict]:
    with env["engine"].connect() as connection:
        return [
            {"event_type": row[0], "data": row[1]}
            for row in connection.execute(
                select(
                    chat_generation_event_table.c.event_type,
                    chat_generation_event_table.c.data_json,
                )
                .where(chat_generation_event_table.c.generation_id == generation_id)
                .order_by(chat_generation_event_table.c.event_seq)
            ).all()
        ]


def _generation_status(env: dict, generation_id: str) -> str:
    with env["engine"].connect() as connection:
        return str(
            connection.execute(
                select(chat_generation_table.c.status).where(
                    chat_generation_table.c.id == generation_id
                )
            ).scalar_one()
        )


class ScriptedSelfEvaluator:
    """Rejects the first candidate with a rewrite, then accepts."""

    def __init__(self, rewritten_query: str) -> None:
        self.rewritten_query = rewritten_query
        self.calls = 0

    def evaluate(self, *, query, candidate_content, citations, context_items):
        self.calls += 1
        if self.calls == 1:
            return SelfEvaluationResult(
                acceptable=False,
                rewritten_query=self.rewritten_query,
                diagnosis={"reason": "low_relevance"},
            )
        return SelfEvaluationResult(acceptable=True)


class AlwaysRewriteEvaluator:
    def evaluate(self, *, query, candidate_content, citations, context_items):
        return SelfEvaluationResult(
            acceptable=False,
            rewritten_query=f"{query} again",
            diagnosis={"reason": "low_relevance"},
        )


class StrategyPlanningProvider:
    """Main chat transport fake whose first deep call emits a strategy plan."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def generate(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        if request.purpose == "deep_retrieval_plan":
            return ChatProviderResponse(
                content=json.dumps({"strategies": ["rewrite", "hyde", "tree", "document_summary"]}),
                input_tokens=3,
                output_tokens=5,
            )
        return ChatProviderResponse(content="grounded answer", input_tokens=10, output_tokens=20)


class InvalidStrategyPlanningProvider(StrategyPlanningProvider):
    def generate(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        if request.purpose == "deep_retrieval_plan":
            return ChatProviderResponse(content="not JSON", input_tokens=3, output_tokens=5)
        return ChatProviderResponse(content="grounded answer", input_tokens=10, output_tokens=20)


class BudgetAwarePartialRetrieval(RecordingChatRetrievalPort):
    """Exercises the worker's logical meter on an effort-upgraded round."""

    def __init__(self) -> None:
        super().__init__()
        self.rag_limits: list[int] = []
        self._resolution_count = 0

    def search(self, query, *, budget=None, **kwargs):  # type: ignore[no-untyped-def]
        if budget is not None:
            self.rag_limits.append(budget.policy.max_rag_calls)
            assert budget.deadline is not None
            now = budget.deadline - timedelta(seconds=1)
            notice = budget.gate("retrieval", estimated_tokens=len(query), now=now)
            if notice is not None:
                return RetrievalOutcome(hits=(), degradations=(notice,))
            reserved = budget.reserve("retrieval", estimated_tokens=len(query), now=now)
            budget.reconcile(reserved, actual_tokens=0)
        return super().search(query, budget=budget, **kwargs)

    def resolve_citations(self, hits, *, principal):  # type: ignore[no-untyped-def]
        self._resolution_count += 1
        if self._resolution_count == 1:
            return super().resolve_citations(hits[:1], principal=principal)
        return super().resolve_citations(hits, principal=principal)


def _start_generation(env: dict, username: str, *, content: str, effort: str):
    token, _ = provision_and_login(env["identity"], username)
    principal = env["identity"].authenticate_access_token(token)
    conversation_id = (
        env["client"]
        .post("/v1/conversations", json={}, headers={"Authorization": f"Bearer {token}"})
        .json()["id"]
    )
    return _ask(env, principal, conversation_id, content=content, effort=effort)


def test_think_rewrite_loop_re_retrieves_within_same_generation() -> None:
    env = build_test_env(
        outcomes={
            "hello": _outcome(_hit()),
            "hello refined": _outcome(_hit("doc-2", "chunk-2")),
        }
    )
    result = _start_generation(env, "bob", content="hello", effort="think")

    evaluator = ScriptedSelfEvaluator("hello refined")
    worker = make_worker(env, self_evaluator=evaluator)
    worker.run_once()

    # The rewrite loop re-ran retrieval with the rewritten query inside the
    # same generation: one generation, two provider calls, second candidate
    # published, rejected draft never exposed as a public answer.
    assert evaluator.calls == 2
    assert [s["query"] for s in env["retrieval"].searches] == ["hello", "hello refined"]
    assert len(env["provider"].calls) == 2
    assert env["provider"].calls[-1].content == "hello refined"
    assert _generation_status(env, result.generation_id) == "completed"
    events = _events(env, result.generation_id)
    assert any(e["event_type"] == "stage" and e["data"]["phase"] == "rewriting" for e in events)
    answer_events = [e for e in events if e["event_type"] == "answer"]
    assert len(answer_events) == 1


def test_quick_skips_self_evaluation_loop() -> None:
    env = build_test_env(
        outcomes={
            "hello": _outcome(_hit()),
            "hello again": _outcome(_hit()),
        }
    )
    result = _start_generation(env, "carol", content="hello", effort="quick")

    worker = make_worker(env, self_evaluator=AlwaysRewriteEvaluator())
    worker.run_once()

    # Quick runs exactly one retrieval + one generation pass: the evaluator
    # is never consulted and no rewrite happens even though it always offers
    # one.
    assert [s["query"] for s in env["retrieval"].searches] == ["hello"]
    assert len(env["provider"].calls) == 1
    assert _generation_status(env, result.generation_id) == "completed"
    events = _events(env, result.generation_id)
    assert not any(e["event_type"] == "stage" and e["data"]["phase"] == "rewriting" for e in events)


def test_heuristic_default_rejects_ungrounded_candidate_without_rewrite() -> None:
    # Hits exist but every citation is filtered: the default heuristic
    # evaluator rejects but offers no rewrite, so the bounded loop terminates
    # instead of burning a round on the same query.
    env = build_test_env(outcomes={"hello": _outcome(_hit())})
    env["retrieval"].citations.clear()
    result = _start_generation(env, "dave", content="hello", effort="think")

    worker = make_worker(env)  # default HeuristicSelfEvaluationPort
    worker.run_once()

    assert [s["query"] for s in env["retrieval"].searches] == ["hello"]
    assert _generation_status(env, result.generation_id) == "completed"


def test_deep_main_provider_plan_drives_retrieval_and_sse_hides_model_details() -> None:
    provider = StrategyPlanningProvider()
    env = build_test_env(
        provider=provider,
        outcomes={
            "hello": RetrievalOutcome(
                hits=(_hit(),),
                route_output={
                    "kind": "no_rewrite",
                    "strategy_operations": ["rewrite", "hyde", "tree", "document_summary"],
                    "original_query": "hello",
                    "hyde_text": "secret model text",
                    "metadata_prefilter": {"published_from": "2024-01-01"},
                    "return_granularity": "document_summary",
                },
            )
        },
    )
    result = _start_generation(env, "deep-user", content="hello", effort="deep")

    env["runtime"].resolve("chat_generation_worker").run_once()

    assert [request.purpose for request in provider.calls] == ["deep_retrieval_plan", "answer"]
    assert env["retrieval"].searches[0]["strategy_operations"] == (
        "rewrite",
        "hyde",
        "tree",
        "document_summary",
    )
    routed = next(
        event["data"]
        for event in _events(env, result.generation_id)
        if event["event_type"] == "stage" and event["data"]["phase"] == "retrieval_routed"
    )
    assert routed["route"] == {
        "kind": "no_rewrite",
        "strategies": ["rewrite", "hyde", "tree", "document_summary"],
        "return_granularity": "document_summary",
    }


def test_invalid_deep_plan_falls_back_to_default_hybrid_with_a_stable_notice() -> None:
    provider = InvalidStrategyPlanningProvider()
    env = build_test_env(provider=provider, outcomes={"hello": _outcome(_hit())})
    result = _start_generation(env, "deep-fallback", content="hello", effort="deep")

    env["runtime"].resolve("chat_generation_worker").run_once()

    assert env["retrieval"].searches[0]["strategy_operations"] == ()
    notices = [
        event["data"]
        for event in _events(env, result.generation_id)
        if event["event_type"] == "notice"
    ]
    assert {"kind": "retrieval_degraded", "detail": {"reason": "strategy_plan_invalid"}} in notices


def test_effort_upgrade_also_upgrades_the_logical_retrieval_meter() -> None:
    retrieval = BudgetAwarePartialRetrieval()
    retrieval.outcomes["hello"] = _outcome(_hit(), _hit("doc-2", "chunk-2"))
    env = build_test_env(retrieval=retrieval)
    result = _start_generation(env, "meter-upgrade", content="hello", effort="quick")

    make_worker(env).run_once()

    assert retrieval.rag_limits == [1, 8]
    assert _generation_status(env, result.generation_id) == "completed"


def test_rewrite_loop_effort_upgrade_also_upgrades_the_logical_retrieval_meter() -> None:
    retrieval = BudgetAwarePartialRetrieval()
    retrieval.outcomes["hello"] = _outcome(_hit())
    env = build_test_env(retrieval=retrieval)
    result = _start_generation(env, "meter-upgrade-deep", content="hello", effort="think")

    make_worker(env, self_evaluator=AlwaysRewriteEvaluator()).run_once()

    assert retrieval.rag_limits == [8, 8, 8, 8, 10, 10, 10, 10, 10, 10]
    assert _generation_status(env, result.generation_id) == "completed"
