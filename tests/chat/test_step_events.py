"""Deep-tier `step` SSE event contract tests (frontend contract §3.7)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.chat.models import AskRequest, RetrievalHitOutcome, RetrievalOutcome
from app.chat.ports import RecordingChatRetrievalPort
from app.chat.schema import chat_generation_event_table

from .conftest import build_test_env, provision_and_login, sse_frames


def _two_hit_outcome() -> RetrievalOutcome:
    return RetrievalOutcome(
        hits=(
            RetrievalHitOutcome(
                document_id="doc_1",
                document_version_id="ver_1",
                publication_id="pub_1",
                chunk_id="chunk_1",
                space_id="space_1",
                locator={"page": 1},
                snippet="first",
            ),
            RetrievalHitOutcome(
                document_id="doc_2",
                document_version_id="ver_2",
                publication_id="pub_2",
                chunk_id="chunk_2",
                space_id="space_2",
                locator={"page": 2},
                snippet="second",
            ),
        )
    )


class _PartiallyVisibleRetrieval(RecordingChatRetrievalPort):
    """Hides one hit from the first citation resolution to force a second round."""

    def __init__(self) -> None:
        super().__init__()
        self._resolution_count = 0

    def resolve_citations(self, hits, *, principal):  # type: ignore[no-untyped-def]
        self._resolution_count += 1
        if self._resolution_count == 1:
            return super().resolve_citations(hits[:1], principal=principal)
        return super().resolve_citations(hits, principal=principal)


def _run_generation(env: dict, *, effort_level: str, content: str = "hello"):
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = (
        env["runtime"]
        .resolve("chat_generation_service")
        .ask(
            principal=principal,
            conversation_id=conversation_id,
            request=AskRequest(content=content, effort_level=effort_level, scope=None),
            idempotency_key=f"ask-step-{effort_level}-{content}",
        )
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(
                    chat_generation_event_table.c.event_seq,
                    chat_generation_event_table.c.event_type,
                    chat_generation_event_table.c.data_json,
                )
                .where(chat_generation_event_table.c.generation_id == result.generation_id)
                .order_by(chat_generation_event_table.c.event_seq)
            )
            .mappings()
            .all()
        )
    events = [dict(row) for row in rows]
    return result, events, headers


def _step_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] == "step"]


def test_deep_multi_round_retrieval_emits_paired_step_events() -> None:
    retrieval = _PartiallyVisibleRetrieval()
    retrieval.outcomes["hello"] = _two_hit_outcome()
    env = build_test_env(retrieval=retrieval)
    _, events, _ = _run_generation(env, effort_level="deep")

    steps = _step_rows(events)
    assert [
        (event["data_json"]["index"], event["data_json"]["label"], event["data_json"]["state"])
        for event in steps
    ] == [
        (1, "retrieve_round_1", "active"),
        (1, "retrieve_round_1", "done"),
        (2, "retrieve_round_2", "active"),
        (2, "retrieve_round_2", "done"),
    ]
    # The step payload carries only the contract fields; no tool parameters,
    # raw results, model reasoning or candidate content.
    assert all(set(event["data_json"]) == {"index", "label", "state"} for event in steps)
    # Steps sit after `start` and before the terminal event, without
    # disturbing the surrounding stage/answer ordering.
    types = [event["event_type"] for event in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    first_step = types.index("step")
    last_step = len(types) - 1 - types[::-1].index("step")
    assert 0 < first_step < last_step < len(types) - 1
    seqs = [event["event_seq"] for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_deep_single_round_emits_one_paired_step() -> None:
    retrieval = RecordingChatRetrievalPort()
    retrieval.outcomes["hello"] = _two_hit_outcome()
    env = build_test_env(retrieval=retrieval)
    _, events, _ = _run_generation(env, effort_level="deep")

    assert [
        (event["data_json"]["index"], event["data_json"]["label"], event["data_json"]["state"])
        for event in _step_rows(events)
    ] == [
        (1, "retrieve_round_1", "active"),
        (1, "retrieve_round_1", "done"),
    ]


def test_quick_and_think_generations_do_not_emit_step_events() -> None:
    for effort_level in ("quick", "think"):
        retrieval = _PartiallyVisibleRetrieval()
        retrieval.outcomes["hello"] = _two_hit_outcome()
        env = build_test_env(retrieval=retrieval)
        _, events, _ = _run_generation(env, effort_level=effort_level)
        assert _step_rows(events) == [], effort_level


def test_step_events_replay_by_last_event_id() -> None:
    retrieval = _PartiallyVisibleRetrieval()
    retrieval.outcomes["hello"] = _two_hit_outcome()
    env = build_test_env(retrieval=retrieval)
    result, _, headers = _run_generation(env, effort_level="deep")

    response = env["client"].get(
        f"/v1/generations/{result.generation_id}/events",
        headers={**headers, "Accept": "text/event-stream", "Last-Event-ID": "1"},
    )
    assert response.status_code == 200
    steps = [
        (event_id, json.loads(data))
        for event, event_id, data in sse_frames(response.text)
        if event == "step"
    ]
    assert [payload["index"] for _, payload in steps] == [1, 1, 2, 2]
    assert [payload["state"] for _, payload in steps] == ["active", "done", "active", "done"]
    ids = [event_id for event_id, _ in steps]
    assert all(event_id is not None and event_id > 1 for event_id in ids)
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
