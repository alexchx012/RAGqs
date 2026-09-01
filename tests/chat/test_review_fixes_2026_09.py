"""2026-09 review findings acceptance tests for the chat contract.

Covers A3 (auto-conversation idempotency), A4 (citations from actual model
references and the three answer_mode states), A13 (SSE 401/404 before the
response starts), A19 (delete relies on FK cascades), A26 (content limit),
A27 (search wildcard escaping and query length) and A36 (SSE poll backoff).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine

from app.chat.models import (
    AskRequest,
    ChatProviderResponse,
    ConversationScope,
    RetrievalHitOutcome,
    RetrievalOutcome,
    StoredEvent,
)
from app.chat.ports import ChatProviderRequest, RecordingChatRetrievalPort
from app.chat.schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_ab_vote_table,
    chat_conversation_table,
    chat_generation_event_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_message_feedback_table,
    chat_message_table,
    chat_subscription_lease_table,
)
from app.platform.config import load_platform_settings

from .conftest import FakeCalibration, build_test_env, open_window, provision_and_login

# --------------------------------------------------------------------- helpers


def _auth(env: dict, username: str) -> dict[str, str]:
    token, _ = provision_and_login(env["identity"], username)
    return {"Authorization": f"Bearer {token}"}


def _hit(document_id: str = "doc_1", chunk_id: str = "chunk_1") -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id=document_id,
        document_version_id=f"ver_{document_id}",
        publication_id=f"pub_{document_id}",
        chunk_id=chunk_id,
        space_id="space_1",
        locator={"page": 1},
        snippet=f"the answer from {document_id}",
    )


def _ask_via_api(env: dict, headers: dict, *, content: str, key: str) -> Any:
    return env["client"].post(
        "/v1/chat",
        json={"content": content, "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": key},
    )


class MarkerlessProvider:
    """Provider whose answers never reference any source identifier."""

    def __init__(self) -> None:
        self.calls: list[ChatProviderRequest] = []

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls.append(request)
        return ChatProviderResponse(
            content=f"general knowledge answer for {request.content[:20]}",
            input_tokens=5,
            output_tokens=5,
        )


class AllFilteredRetrievalPort(RecordingChatRetrievalPort):
    """Every hit fails the final ACL/visibility check at citation resolution."""

    def resolve_citations(
        self, hits: tuple[dict[str, Any], ...], *, principal: Any
    ) -> tuple[dict[str, Any], ...]:
        del hits, principal
        return ()


def _assistant_message(env: dict, headers: dict, conversation_id: str) -> dict[str, Any]:
    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    return detail["messages"][1]


def _ask_and_complete(env: dict, headers: dict, *, content: str, key: str) -> tuple[str, str]:
    """POST /v1/chat (auto conversation), run the worker once, return ids."""
    accepted = _ask_via_api(env, headers, content=content, key=key)
    assert accepted.status_code == 202
    env["runtime"].resolve("chat_generation_worker").run_once()
    data = accepted.json()["data"]
    return data["generation_id"], data["conversation_id"]


# ------------------------------------------------------------------------ A3


def test_post_chat_without_conversation_same_key_replays_without_duplicates() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")

    first = _ask_via_api(env, headers, content="hello", key="auto-1")
    assert first.status_code == 202
    assert first.json()["data"]["replay"] is False

    second = _ask_via_api(env, headers, content="hello", key="auto-1")
    assert second.status_code == 202
    data = second.json()["data"]
    assert data["replay"] is True
    for field in ("conversation_id", "generation_id", "message_id", "user_message_id"):
        assert data[field] == first.json()["data"][field], field

    with env["engine"].connect() as connection:
        conversations = connection.execute(
            select(func.count()).select_from(chat_conversation_table)
        ).scalar_one()
        generations = connection.execute(
            select(func.count()).select_from(chat_generation_table)
        ).scalar_one()
        messages = connection.execute(
            select(func.count()).select_from(chat_message_table)
        ).scalar_one()
    assert conversations == 1
    assert generations == 1
    assert messages == 2  # one user message + one assistant message


def test_post_chat_without_conversation_same_key_other_request_conflicts() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")

    first = _ask_via_api(env, headers, content="hello", key="auto-conflict")
    assert first.status_code == 202
    second = _ask_via_api(env, headers, content="different", key="auto-conflict")
    assert second.status_code == 409
    assert second.json()["data"]["error"]["code"] == "idempotency_key_conflict"


# ------------------------------------------------------------------------ A4


def test_answer_mode_grounded_publishes_only_referenced_citation_subset() -> None:
    env = build_test_env(
        outcomes={
            "hello": RetrievalOutcome(hits=(_hit("doc_1", "chunk_1"), _hit("doc_2", "chunk_2")))
        }
    )
    headers = _auth(env, "alice")
    generation_id, conversation_id = _ask_and_complete(
        env, headers, content="hello", key="a4-grounded"
    )

    # The fake provider's content only carries doc_1's source marker.
    assistant = _assistant_message(env, headers, conversation_id)
    assert assistant["answer_mode"] == "grounded"
    assert [citation["document_id"] for citation in assistant["citations"]] == ["doc_1"]

    frames = env["client"].get(
        f"/v1/generations/{generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    answer_frames = [
        line
        for line in frames.text.splitlines()
        if line.startswith("data:") and "citations" in line
    ]
    assert answer_frames, "expected an answer event"
    payload = json.loads(answer_frames[0][len("data:") :].strip())
    assert payload["answer_mode"] == "grounded"
    assert [citation["document_id"] for citation in payload["citations"]] == ["doc_1"]


def test_answer_mode_direct_when_model_references_no_hit() -> None:
    env = build_test_env(
        provider=MarkerlessProvider(),
        outcomes={
            "hello": RetrievalOutcome(hits=(_hit("doc_1", "chunk_1"), _hit("doc_2", "chunk_2")))
        },
    )
    headers = _auth(env, "alice")
    generation_id, conversation_id = _ask_and_complete(
        env, headers, content="hello", key="a4-direct"
    )

    assistant = _assistant_message(env, headers, conversation_id)
    assert assistant["answer_mode"] == "direct"
    assert assistant["citations"] == []
    assert assistant["status"] == "completed"

    frames = env["client"].get(
        f"/v1/generations/{generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    assert '"answer_mode":"direct"' in frames.text.replace(" ", "")


def test_answer_mode_no_context_when_all_hits_are_acl_filtered() -> None:
    env = build_test_env(
        retrieval=AllFilteredRetrievalPort(),
        outcomes={
            "hello": RetrievalOutcome(hits=(_hit("doc_1", "chunk_1"), _hit("doc_2", "chunk_2")))
        },
    )
    headers = _auth(env, "alice")
    generation_id, conversation_id = _ask_and_complete(
        env, headers, content="hello", key="a4-no-context"
    )

    assistant = _assistant_message(env, headers, conversation_id)
    assert assistant["answer_mode"] == "no_context"
    assert assistant["citations"] == []

    frames = env["client"].get(
        f"/v1/generations/{generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    assert '"answer_mode":"no_context"' in frames.text.replace(" ", "")


# ----------------------------------------------------------------------- A59


def test_think_tier_gets_boosted_ab_sampling_over_quick() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window(sample_rate=0.3)),
        candidate_config_versions=("default", "candidate_b"),
        sampler=lambda: 0.5,
    )
    headers = _auth(env, "alice")
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(headers["Authorization"].split(" ")[1])
    conversation = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    personal = ConversationScope(space_ids=("personal:user_1",), document_ids=())

    quick = service.ask(
        principal=principal,
        conversation_id=conversation,
        request=AskRequest(content="quick question", effort_level="quick", scope=personal),
        idempotency_key="a59-quick",
    )
    think = service.ask(
        principal=principal,
        conversation_id=conversation,
        request=AskRequest(content="think question", effort_level="think", scope=personal),
        idempotency_key="a59-think",
    )
    with env["engine"].connect() as connection:
        pairs = connection.execute(select(chat_ab_pair_table.c.generation_id)).scalars().all()
    # 0.5 >= 0.3 skips the quick ask; the think boost lifts the rate to 0.6.
    assert pairs == [think.generation_id]
    assert quick.generation_id not in pairs


def test_ab_sampling_stratifies_by_user_only_in_shared_spaces() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window(sample_rate=0.3)),
        candidate_config_versions=("default", "candidate_b"),
        sampler=lambda: 0.0,
    )
    headers = _auth(env, "alice")
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(headers["Authorization"].split(" ")[1])
    conversation = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    department = ConversationScope(space_ids=("department:dept_1",), document_ids=())
    personal = ConversationScope(space_ids=("personal:user_1",), document_ids=())

    def _ask(content: str, key: str, scope: ConversationScope) -> Any:
        return service.ask(
            principal=principal,
            conversation_id=conversation,
            request=AskRequest(content=content, effort_level="quick", scope=scope),
            idempotency_key=key,
        )

    # First shared-space ask: full rate 0.3, draw 0.0 -> pair.
    _ask("first", "a59-s1", department)
    with env["engine"].connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(chat_ab_pair_table)).scalar_one()
            == 1
        )
    # One contributed pair halves the shared-space rate to 0.15; draw 0.2 skips.
    service._sampler = lambda: 0.2
    _ask("second", "a59-s2", department)
    with env["engine"].connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(chat_ab_pair_table)).scalar_one()
            == 1
        )
    # Personal libraries never stratify: rate stays 0.3 and draw 0.2 samples.
    _ask("third", "a59-s3", personal)
    with env["engine"].connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(chat_ab_pair_table)).scalar_one()
            == 2
        )


# ----------------------------------------------------------------------- A13


def test_sse_events_for_unknown_generation_returns_404_before_stream() -> None:
    env = build_test_env()
    headers = {**_auth(env, "alice"), "Accept": "text/event-stream"}

    response = env["client"].get("/v1/generations/gen_missing/events", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "generation_not_found"


def test_sse_events_for_foreign_generation_returns_404() -> None:
    env = build_test_env()
    alice = {**_auth(env, "alice"), "Accept": "text/event-stream"}
    bob = _auth(env, "bob")
    created = _ask_via_api(env, bob, content="secret", key="a13-bob")

    response = env["client"].get(
        f"/v1/generations/{created.json()['data']['generation_id']}/events", headers=alice
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "generation_not_found"


# ----------------------------------------------------------------------- A19


def _fk_enforced_sqlite_engine() -> Engine:
    handle, path = tempfile.mkstemp(prefix="chat_fk_", suffix=".sqlite3")
    os.close(handle)
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    from app.chat.schema import chat_metadata

    chat_metadata.create_all(engine)
    return engine


def test_delete_conversation_cascades_child_rows_through_foreign_keys() -> None:
    from app.chat.conversations import ConversationService

    engine = _fk_enforced_sqlite_engine()
    now = datetime(2026, 9, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            chat_conversation_table.insert().values(
                id="conv_1",
                owner_user_id="user_1",
                title="to delete",
                pinned=False,
                group_id=None,
                effort_level="quick",
                scope_json={},
                last_active_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id="msg_1",
                conversation_id="conv_1",
                owner_user_id="user_1",
                role="assistant",
                content="answer",
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_generation_table.insert().values(
                id="gen_1",
                conversation_id="conv_1",
                owner_user_id="user_1",
                user_message_id="msg_user_1",
                message_id="msg_1",
                root_generation_id="gen_1",
                attempt_number=1,
                status="completed",
                requested_effort_level="quick",
                effective_effort_level="quick",
                retrieval_profile_id="default",
                retrieval_profile_version="1",
                rag_budget_policy_version="budget-1",
                absolute_deadline_at_utc=now + timedelta(hours=1),
                auth_session_id="session_1",
                control_version=1,
                request_content="question",
                request_scope_json={},
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_generation_event_table.insert().values(
                generation_id="gen_1",
                event_seq=1,
                event_type="start",
                data_json={},
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_generation_execution_table.insert().values(
                execution_id="exec_1",
                generation_id="gen_1",
                execution_attempt_number=1,
                status="completed",
                fencing_token=1,
                checkpoint_version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_subscription_lease_table.insert().values(
                id="lease_1",
                generation_id="gen_1",
                auth_session_id="session_1",
                lease_token="token_1",
                expires_at_utc=now + timedelta(minutes=5),
                created_at_utc=now,
                last_renewed_at_utc=now,
            )
        )
        connection.execute(
            chat_message_feedback_table.insert().values(
                message_id="msg_1", voter_user_id="user_1", vote="up", created_at_utc=now
            )
        )
        connection.execute(
            chat_ab_pair_table.insert().values(
                pair_id="pair_1",
                generation_id="gen_1",
                message_id="msg_1",
                owner_user_id="user_1",
                space_id="public",
                status="voted",
                voted=True,
                choice="0",
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_ab_candidate_table.insert().values(
                pair_id="pair_1",
                candidate=0,
                status="published",
                content="answer",
                citations_json=[],
                answer_mode="grounded",
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_ab_vote_table.insert().values(
                pair_id="pair_1", voter_user_id="user_1", choice="0", created_at_utc=now
            )
        )

    ConversationService(engine).delete_conversation(user_id="user_1", conversation_id="conv_1")

    child_tables = (
        chat_generation_table,
        chat_message_table,
        chat_generation_event_table,
        chat_generation_execution_table,
        chat_subscription_lease_table,
        chat_message_feedback_table,
        chat_ab_pair_table,
        chat_ab_candidate_table,
        chat_ab_vote_table,
        chat_conversation_table,
    )
    with engine.connect() as connection:
        for table in child_tables:
            remaining = connection.execute(select(func.count()).select_from(table)).scalar_one()
            assert remaining == 0, f"{table.name} rows survived the delete"


# ----------------------------------------------------------------------- A26


def test_chat_content_length_limit_returns_422_with_details() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")
    max_chars = env["runtime"].settings.chat.max_content_chars

    oversized = _ask_via_api(env, headers, content="x" * (max_chars + 1), key="a26-over")
    assert oversized.status_code == 422
    error = oversized.json()["data"]["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "content"
    assert error["details"]["max_length"] == max_chars

    boundary = _ask_via_api(env, headers, content="x" * max_chars, key="a26-boundary")
    assert boundary.status_code == 202

    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    messages = env["client"].post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"content": "y" * (max_chars + 1), "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "a26-msg", "Accept": "text/event-stream"},
    )
    assert messages.status_code == 422


def test_chat_max_content_chars_is_configurable() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
            "RAG_CHAT_MAX_CONTENT_CHARS": "10",
        }
    )
    assert settings.chat.max_content_chars == 10


# ----------------------------------------------------------------------- A27


def test_conversation_search_escapes_like_wildcards() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")
    client = env["client"]
    for title in ("doc_1 report", "docX1 report", "save 50% now", "unrelated notes"):
        created = client.post("/v1/conversations", json={}, headers=headers).json()
        patched = client.patch(
            f"/v1/conversations/{created['id']}", json={"title": title}, headers=headers
        )
        assert patched.status_code == 200

    underscore = {
        item["title"]
        for item in client.get("/v1/conversations", params={"q": "doc_1"}, headers=headers).json()[
            "items"
        ]
    }
    assert underscore == {"doc_1 report"}
    percent = {
        item["title"]
        for item in client.get("/v1/conversations", params={"q": "50% n"}, headers=headers).json()[
            "items"
        ]
    }
    assert percent == {"save 50% now"}

    too_long = client.get("/v1/conversations", params={"q": "x" * 300}, headers=headers)
    assert too_long.status_code == 422


# ----------------------------------------------------------------------- A36


def test_sse_polling_backs_off_after_consecutive_empty_polls_and_recovers() -> None:
    from app.chat.streaming import GenerationStreamService

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = GenerationStreamService(
        engine=None,  # type: ignore[arg-type]
        clock=None,
        authorization=None,  # type: ignore[arg-type]
        poll_seconds=0.2,
        idle_backoff_seconds=0.5,
        empty_polls_before_backoff=2,
        heartbeat_seconds=30,
        sleep=fake_sleep,
    )
    reads: list[list[StoredEvent]] = [
        [],
        [],
        [],  # third empty poll happens while already backed off
        [StoredEvent(seq=1, event_type="stage", data={})],  # resets the counter
        [],
        [StoredEvent(seq=2, event_type="done", data={})],
    ]
    service._read_events = lambda generation_id, after_seq: reads.pop(0)  # type: ignore[method-assign]
    service._open = lambda principal, generation_id, last_seq: ([], "lease_1")  # type: ignore[method-assign]
    released: list[str] = []
    service._release_lease = lambda lease_token: released.append(lease_token)  # type: ignore[method-assign]

    async def collect() -> list[str]:
        frames: list[str] = []
        async for frame in service.stream(
            principal=object(), generation_id="gen_1", last_event_id=0
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(collect())
    # Two fast polls reach the empty-poll threshold, backoff applies while the
    # stream stays quiet, the stage event restores the fast cadence, and the
    # second quiet stretch backs off again before the done event terminates.
    assert sleeps == [0.2, 0.2, 0.5, 0.5, 0.2, 0.2]
    assert len(frames) == 2
    assert released == ["lease_1"]
