"""Worker integration tests for the agents self-evaluation rewrite loop.

Exercises the think/deep bounded rewrite -> re-retrieve loop, quick's skip of
self-evaluation and internal isolation of rejected drafts through the real
ChatGenerationWorker against the chat fixtures. Budget-meter gating, tier
limits and candidate selection are covered by tests/chat/test_budget_*.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.agents import SelfEvaluationResult
from app.chat.models import RetrievalHitOutcome, RetrievalOutcome
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


def _start_generation(env: dict, username: str, *, content: str, effort: str):
    token, _ = provision_and_login(env["identity"], username)
    principal = env["identity"].authenticate_access_token(token)
    conversation_id = env["client"].post(
        "/v1/conversations", json={}, headers={"Authorization": f"Bearer {token}"}
    ).json()["id"]
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
    assert any(
        e["event_type"] == "stage" and e["data"]["phase"] == "rewriting" for e in events
    )
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
    assert not any(
        e["event_type"] == "stage" and e["data"]["phase"] == "rewriting" for e in events
    )


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
