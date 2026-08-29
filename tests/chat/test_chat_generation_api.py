"""API contract tests: conversations, SSE creation/replay, stop/retry, feedback, A/B."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.chat.models import CalibrationWindowSnapshot, RetrievalHitOutcome, RetrievalOutcome
from app.chat.schema import chat_ab_pair_table

from .conftest import (
    NOW,
    FakeCalibration,
    build_runtime_authorization,
    build_test_env,
    open_window,
    provision_and_login,
    sse_frames,
)


def _hit(snippet: str = "the answer is 42") -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id="doc_1",
        document_version_id="ver_1",
        publication_id="pub_1",
        chunk_id="chunk_1",
        space_id="space_1",
        locator={"page": 1},
        snippet=snippet,
    )


def _complete_generation(
    env: dict, token: str, conversation_id: str, content: str = "hello"
) -> dict:
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    result = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content=content, effort_level="quick", scope=None),
        idempotency_key="key-1",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    return {
        "generation_id": result.generation_id,
        "message_id": result.message_id,
        "user_message_id": result.user_message_id,
    }


def test_conversation_and_group_crud_with_ownership() -> None:
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    other_token, _ = provision_and_login(env["identity"], "bob")
    headers = {"Authorization": f"Bearer {token}"}
    client = env["client"]

    created = client.post("/v1/conversations", json={}, headers=headers)
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    group = client.post("/v1/conversation-groups", json={"name": "work"}, headers=headers)
    assert group.status_code == 201
    group_id = group.json()["id"]

    patched = client.patch(
        f"/v1/conversations/{conversation_id}",
        json={"title": "planning", "pinned": True, "group_id": group_id},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "planning"
    assert patched.json()["pinned"] is True

    listing = client.get("/v1/conversations", headers=headers).json()
    assert any(item["id"] == conversation_id for item in listing["items"])
    assert any(item["id"] == group_id for item in listing["groups"])

    forbidden = client.get(
        f"/v1/conversations/{conversation_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["error"]["code"] == "conversation_not_found"

    deleted = client.delete(f"/v1/conversation-groups/{group_id}", headers=headers)
    assert deleted.status_code == 204
    detail = client.get(f"/v1/conversations/{conversation_id}", headers=headers)
    assert detail.json()["messages"] == []
    assert detail.json()["title"] == "planning"
    # Deleting the group nulls the conversation group_id, not the conversation.
    listing_after = client.get("/v1/conversations", headers=headers).json()
    item = next(item for item in listing_after["items"] if item["id"] == conversation_id)
    assert item["group_id"] is None

    removed = client.delete(f"/v1/conversations/{conversation_id}", headers=headers)
    assert removed.status_code == 204


def test_patch_conversation_group_id_null_moves_out_of_group() -> None:
    """显式 group_id=null 移出分组；未提交 group_id 保持原分组；patch 不动 last_active_at。"""
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    client = env["client"]

    created = client.post("/v1/conversations", json={}, headers=headers)
    assert created.status_code == 201
    conversation_id = created.json()["id"]
    last_active_at = created.json()["last_active_at"]

    group = client.post("/v1/conversation-groups", json={"name": "work"}, headers=headers)
    group_id = group.json()["id"]
    moved = client.patch(
        f"/v1/conversations/{conversation_id}", json={"group_id": group_id}, headers=headers
    )
    assert moved.status_code == 200
    assert moved.json()["group_id"] == group_id

    # 未提交 group_id 的 patch（如重命名）不影响分组归属
    renamed = client.patch(
        f"/v1/conversations/{conversation_id}", json={"title": "planning"}, headers=headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["group_id"] == group_id

    cleared = client.patch(
        f"/v1/conversations/{conversation_id}", json={"group_id": None}, headers=headers
    )
    assert cleared.status_code == 200
    assert cleared.json()["group_id"] is None
    # 移出后按原最后对话时间排序（patch 不刷新 last_active_at）
    assert cleared.json()["last_active_at"] == last_active_at

    listing = client.get("/v1/conversations", headers=headers).json()
    item = next(item for item in listing["items"] if item["id"] == conversation_id)
    assert item["group_id"] is None

    # 空字符串不是合法分组 id（也不作移出语义）
    blank = client.patch(
        f"/v1/conversations/{conversation_id}", json={"group_id": ""}, headers=headers
    )
    assert blank.status_code == 422


def test_empty_group_is_deleted_after_last_conversation_moved_out() -> None:
    """分组内最后一个会话被移出/转移后，空分组自动删除；分组仍有会话时保留。"""
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    client = env["client"]

    c1 = client.post("/v1/conversations", json={}, headers=headers).json()["id"]
    c2 = client.post("/v1/conversations", json={}, headers=headers).json()["id"]
    group_a = client.post("/v1/conversation-groups", json={"name": "a"}, headers=headers).json()["id"]
    group_b = client.post("/v1/conversation-groups", json={"name": "b"}, headers=headers).json()["id"]

    def group_ids() -> set[str]:
        listing = client.get("/v1/conversations", headers=headers).json()
        return {item["id"] for item in listing["groups"]}

    client.patch(f"/v1/conversations/{c1}", json={"group_id": group_a}, headers=headers)
    client.patch(f"/v1/conversations/{c2}", json={"group_id": group_a}, headers=headers)

    # 移出一个但分组仍有会话：分组保留
    client.patch(f"/v1/conversations/{c1}", json={"group_id": None}, headers=headers)
    assert group_a in group_ids()

    # 最后一个会话转移到其他分组：原分组自动删除
    client.patch(f"/v1/conversations/{c2}", json={"group_id": group_b}, headers=headers)
    assert group_a not in group_ids()
    assert group_b in group_ids()

    # 移出最后一个会话（null）：分组自动删除
    client.patch(f"/v1/conversations/{c2}", json={"group_id": None}, headers=headers)
    client.patch(f"/v1/conversations/{c1}", json={"group_id": group_b}, headers=headers)
    client.patch(f"/v1/conversations/{c1}", json={"group_id": None}, headers=headers)
    assert group_b not in group_ids()


def test_message_creation_requires_streaming_accept_and_idempotency_key() -> None:
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]

    no_key = env["client"].post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"content": "hi", "effort_level": "quick"},
        headers={**headers, "Accept": "text/event-stream"},
    )
    assert no_key.status_code == 422
    assert no_key.json()["error"]["code"] == "validation_error"

    not_streaming = env["client"].post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"content": "hi", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "key-1", "Accept": "application/json"},
    )
    assert not_streaming.status_code == 406
    assert not_streaming.json()["error"]["code"] == "streaming_response_required"

    missing = env["client"].post(
        "/v1/conversations/not-a-conversation/messages",
        json={"content": "hi", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "key-1", "Accept": "text/event-stream"},
    )
    assert missing.status_code == 404


def test_ask_creates_identity_rows_and_start_event_then_replays_same_stream() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=(_hit(),))})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]

    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    first = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="key-1",
    )
    replay = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="key-1",
    )
    assert replay.generation_id == first.generation_id
    assert replay.replay is True

    conflict = False
    try:
        service.ask(
            principal=principal,
            conversation_id=conversation_id,
            request=AskRequest(content="different", effort_level="quick", scope=None),
            idempotency_key="key-1",
        )
    except Exception as error:  # noqa: BLE001
        conflict = "idempotency_key_conflict" == getattr(error, "code", None)
    assert conflict

    with env["engine"].connect() as connection:
        from sqlalchemy import func, select

        from app.chat.schema import (
            chat_generation_event_table,
            chat_generation_execution_table,
            chat_generation_table,
            chat_message_table,
        )

        messages = (
            connection.execute(
                select(chat_message_table).order_by(chat_message_table.c.created_at_utc)
            )
            .mappings()
            .all()
        )
        assert [row["role"] for row in messages] == ["user", "assistant"]
        assert messages[1]["status"] == "generating"
        generation = connection.execute(select(chat_generation_table)).mappings().one()
        assert generation["status"] == "running"
        assert generation["auth_session_id"] == principal.auth_session_id
        execution = connection.execute(select(chat_generation_execution_table)).mappings().one()
        assert execution["status"] == "queued"
        start = (
            connection.execute(
                select(chat_generation_event_table).where(
                    chat_generation_event_table.c.event_seq == 1
                )
            )
            .mappings()
            .one()
        )
        assert start["event_type"] == "start"
        assert func is not None

    env["runtime"].resolve("chat_generation_worker").run_once()
    response = env["client"].post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"content": "hello", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "key-1", "Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = sse_frames(response.text)
    event_types = [frame[0] for frame in frames]
    assert event_types[0] == "start"
    assert event_types[-1] == "done"
    assert [frame[1] for frame in frames] == list(range(1, len(frames) + 1))
    done = json.loads(frames[-1][2])
    assert done["status"] == "completed"


def test_event_replay_respects_last_event_id_and_read_model_shape() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=(_hit(),))})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    completed = _complete_generation(env, token, conversation_id)

    response = env["client"].get(
        f"/v1/generations/{completed['generation_id']}/events",
        headers={**headers, "Last-Event-ID": "1", "Accept": "text/event-stream"},
    )
    frames = sse_frames(response.text)
    assert frames[0][1] == 2
    assert frames[-1][0] == "done"

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assert detail["effort_level"] == "quick"
    assert detail["title"] == "hello"
    user, assistant = detail["messages"]
    assert user["role"] == "user"
    assert assistant["status"] == "completed"
    assert assistant["answer_mode"] == "grounded"
    assert assistant["citations"][0]["document_id"] == "doc_1"
    assert assistant["feedback"] is None
    assert assistant["ab"] is None


def test_live_stream_observes_worker_events_and_terminates() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=(_hit(),))})
    token, _ = provision_and_login(env["identity"], "alice")
    service = env["runtime"].resolve("chat_generation_service")
    from app.chat.streaming import GenerationStreamService

    stream_service = GenerationStreamService(
        env["engine"],
        clock=env["clock"],
        authorization=build_runtime_authorization(env["identity"]),
        poll_seconds=0.05,
        heartbeat_seconds=0,
    )
    principal = env["identity"].authenticate_access_token(token)
    conversation_id = (
        env["client"]
        .post(
            "/v1/conversations",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        .json()["id"]
    )
    from app.chat.models import AskRequest

    result = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="key-1",
    )

    def finish_later() -> None:
        time.sleep(0.3)
        env["runtime"].resolve("chat_generation_worker").run_once()

    thread = threading.Thread(target=finish_later)
    thread.start()

    async def collect() -> list[str]:
        frames: list[str] = []
        async for frame in stream_service.stream(
            principal=principal, generation_id=result.generation_id, last_event_id=0
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(collect())
    thread.join()
    event_types = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("event: "):
                event_types.append(line.split(":", 1)[1].strip())
                break
    assert event_types[0] == "start"
    assert event_types[-1] == "done"
    assert any(frame.startswith(": keep-alive") for frame in frames)


def test_stop_returns_202_then_worker_converges_to_stopped() -> None:
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    result = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="key-1",
    )
    stopped = env["client"].post(f"/v1/generations/{result.generation_id}/stop", headers=headers)
    assert stopped.status_code == 202
    assert stopped.json()["status"] == "stop_requested"
    second = env["client"].post(f"/v1/generations/{result.generation_id}/stop", headers=headers)
    assert second.status_code == 202

    env["runtime"].resolve("chat_generation_worker").run_once()
    events = env["client"].get(
        f"/v1/generations/{result.generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    frames = sse_frames(events.text)
    assert frames[-1][0] == "stopped"
    assert json.loads(frames[-1][2])["stop_reason"] == "manual_request"
    third = env["client"].post(f"/v1/generations/{result.generation_id}/stop", headers=headers)
    assert third.status_code == 200
    assert third.json()["status"] == "stopped"


def test_failed_generation_retry_chain_and_feedback_immutability() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    env["provider"].fail_next = True
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    failed = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="ask-1",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    failed_events = env["client"].get(
        f"/v1/generations/{failed.generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    assert sse_frames(failed_events.text)[-1][0] == "error"

    retry_result = service.retry(
        principal=principal,
        failed_generation_id=failed.generation_id,
        idempotency_key="retry-1",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    assert retry_result.replay is False

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    user_messages = [message for message in detail["messages"] if message["role"] == "user"]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] == "hello"
    assistant_messages = [
        message for message in detail["messages"] if message["role"] == "assistant"
    ]
    assert len(assistant_messages) == 2
    retry_message = assistant_messages[1]
    assert retry_message["root_generation_id"] == failed.generation_id
    assert retry_message["retry_of_generation_id"] == failed.generation_id
    assert retry_message["attempt_number"] == 2
    assert retry_message["status"] == "completed"

    feedback = env["client"].post(
        f"/v1/messages/{retry_message['id']}/feedback",
        json={"vote": "up"},
        headers={**headers, "Idempotency-Key": "fb-1"},
    )
    assert feedback.status_code == 204
    replay = env["client"].post(
        f"/v1/messages/{retry_message['id']}/feedback",
        json={"vote": "up"},
        headers={**headers, "Idempotency-Key": "fb-1"},
    )
    assert replay.status_code == 204
    conflict = env["client"].post(
        f"/v1/messages/{retry_message['id']}/feedback",
        json={"vote": "down", "reason": "no_grounding"},
        headers={**headers, "Idempotency-Key": "fb-2"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "feedback_already_submitted"
    after = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    last = next(
        message
        for message in after["messages"]
        if message["role"] == "assistant" and message["attempt_number"] == 2
    )
    assert last["feedback"] == {"vote": "up"}


def test_ab_pair_open_vote_and_expiry() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="ask-1",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = detail["messages"][1]
    assert assistant["ab"]["status"] == "open"
    assert [candidate["candidate"] for candidate in assistant["ab"]["candidates"]] == [0, 1]
    assert all(
        {"candidate", "content", "citations", "answer_mode"}.issubset(candidate)
        for candidate in assistant["ab"]["candidates"]
    )
    pair_id = assistant["ab"]["pair_id"]

    vote = env["client"].post(
        f"/v1/messages/{assistant['id']}/ab-vote",
        json={"pair_id": pair_id, "choice": "0"},
        headers={**headers, "Idempotency-Key": "vote-1"},
    )
    assert vote.status_code == 200
    assert vote.json() == {"pair_id": pair_id, "voted": True, "choice": "0"}
    assert env["calibration"].collected == ["window_1"]
    replay = env["client"].post(
        f"/v1/messages/{assistant['id']}/ab-vote",
        json={"pair_id": pair_id, "choice": "0"},
        headers={**headers, "Idempotency-Key": "vote-1"},
    )
    assert replay.status_code == 200
    assert env["calibration"].collected == ["window_1"]
    again = env["client"].post(
        f"/v1/messages/{assistant['id']}/ab-vote",
        json={"pair_id": pair_id, "choice": "1"},
        headers={**headers, "Idempotency-Key": "vote-2"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ab_vote_already_submitted"

    after = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    voted = after["messages"][1]
    assert voted["ab"]["status"] == "voted"
    assert voted["ab"]["choice"] == "0"
    assert "answer for hello" in voted["content"]

    feedback_after_vote = env["client"].post(
        f"/v1/messages/{assistant['id']}/feedback",
        json={"vote": "up"},
        headers={**headers, "Idempotency-Key": "fb-after-vote"},
    )
    assert feedback_after_vote.status_code == 204


def test_ab_pair_expires_at_uses_policy_ttl_and_is_not_overwritten_on_open() -> None:
    """A31: pair expiry = earlier of policy TTL and window deadline, frozen at
    creation and preserved when the pair opens for voting."""
    ttl = 900
    snapshot = CalibrationWindowSnapshot(
        window_id="window_1",
        status="open",
        policy_version="cal-v1",
        sample_rate=1.0,
        window_kind="manual",
        expires_at_utc=NOW + timedelta(hours=1),
        close_deadline_at_utc=NOW + timedelta(hours=1),
        pair_vote_ttl_seconds=ttl,
    )
    env = build_test_env(
        calibration=FakeCalibration(window=snapshot),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="ask-policy-ttl",
    )
    with env["engine"].begin() as connection:
        created = connection.execute(select(chat_ab_pair_table)).mappings().one()
    # The earlier of the policy TTL (900s) and the window deadline (1h).
    from datetime import UTC

    assert created["expires_at_utc"].replace(tzinfo=UTC) == NOW + timedelta(seconds=ttl)
    env["runtime"].resolve("chat_generation_worker").run_once()
    with env["engine"].begin() as connection:
        opened = connection.execute(select(chat_ab_pair_table)).mappings().one()
    assert opened["status"] == "open"
    # Opening for voting must not overwrite the frozen expiry (A31).
    from datetime import UTC

    assert opened["expires_at_utc"].replace(tzinfo=UTC) == NOW + timedelta(seconds=ttl)


def test_expired_ab_pair_displays_candidate_zero_after_two_candidates_published() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest

    service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="ask-expired-pair",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    with env["engine"].begin() as connection:
        connection.execute(update(chat_ab_pair_table).values(status="expired"))

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = detail["messages"][1]
    assert assistant["ab"] is None
    assert assistant["content"] == "answer for hello using the answer is 42"
    assert assistant["answer_mode"] == "grounded"


# ------------------------------------------- A/B vote contract fixes (A1-A3)


def _open_ab_pair_for(
    env: dict,
    *,
    token: str,
    content: str = "hello",
    scope: dict | None = None,
) -> tuple[str, str]:
    """Create one completed generation with an open A/B pair; return (headers, message_id)."""
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    from app.chat.models import AskRequest, ConversationScope

    service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(
            content=content,
            effort_level="quick",
            scope=ConversationScope.from_value(scope),
        ),
        idempotency_key=f"ask-{content}-{scope}",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = detail["messages"][1]
    assert assistant["ab"]["status"] == "open"
    return headers, assistant["id"], assistant["ab"]["pair_id"]


def test_ab_vote_by_non_owner_returns_403_and_missing_message_keeps_404() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    alice_token, alice_id = provision_and_login(env["identity"], "alice")
    bob_token, _ = provision_and_login(env["identity"], "bob")
    headers, message_id, _pair_id = _open_ab_pair_for(env, token=alice_token)
    bob_headers = {"Authorization": f"Bearer {bob_token}"}
    forbidden = env["client"].post(
        f"/v1/messages/{message_id}/ab-vote",
        json={"pair_id": "any", "choice": "0"},
        headers={**bob_headers, "Idempotency-Key": "bob-vote"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ab_vote_forbidden"
    # An authorized voter referencing a nonexistent message keeps the
    # existing resource semantics (A1).
    missing = env["client"].post(
        "/v1/messages/msg_does_not_exist/ab-vote",
        json={"pair_id": "pair_x", "choice": "0"},
        headers={**headers, "Idempotency-Key": "missing-vote"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "message_not_found"


def test_ab_vote_replay_keeps_single_vote_row_with_idempotency_columns() -> None:
    from app.chat.schema import chat_ab_vote_table

    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    alice_token, _ = provision_and_login(env["identity"], "alice")
    headers, message_id, pair_id = _open_ab_pair_for(env, token=alice_token)
    for _ in range(2):
        vote = env["client"].post(
            f"/v1/messages/{message_id}/ab-vote",
            json={"pair_id": pair_id, "choice": "0"},
            headers={**headers, "Idempotency-Key": "vote-once"},
        )
        assert vote.status_code == 200
    with env["engine"].connect() as connection:
        votes = connection.execute(select(chat_ab_vote_table)).mappings().all()
    # One valid vote regardless of resubmissions (A2).
    assert len(votes) == 1
    assert str(votes[0]["operation_kind"]) == "ab_vote"
    assert str(votes[0]["idempotency_key"]) == "vote-once"


def test_ab_pair_is_space_isolated_and_cross_space_vote_is_forbidden() -> None:
    from app.chat.generation import GenerationService
    from app.chat.models import AbVoteRequest
    from app.chat.schema import chat_ab_vote_table
    from app.platform.errors import PlatformError

    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    alice_token, alice_id = provision_and_login(env["identity"], "alice")
    alice_space = f"personal:{alice_id}"
    headers, message_id, pair_id = _open_ab_pair_for(
        env, token=alice_token, scope={"space_ids": [alice_space]}
    )
    with env["engine"].connect() as connection:
        pair = connection.execute(select(chat_ab_pair_table)).mappings().one()
    # The pair records its owning space (A3).
    assert str(pair["space_id"]) == alice_space
    principal = env["identity"].authenticate_access_token(alice_token)

    class _NoSpaceAuthorization:
        """Delegates session checks but reports an empty retrieval scope."""

        def __init__(self, inner: object) -> None:
            self._inner = inner

        def verify_active(self, connection, principal):
            return self._inner.verify_active(connection, principal)

        def allowed_retrieval_scope(self, principal):
            return {"space_ids": frozenset()}

    restricted = GenerationService(
        env["engine"],
        clock=env["clock"],
        authorization=_NoSpaceAuthorization(build_runtime_authorization(env["identity"])),
        calibration=env["calibration"],
        sampler=lambda: 0.0,
    )
    with pytest.raises(PlatformError) as raised:
        restricted.submit_ab_vote(
            principal=principal,
            message_id=message_id,
            request=AbVoteRequest(pair_id=pair_id, choice="0"),
            idempotency_key="cross-space-vote",
        )
    assert raised.value.code == "ab_vote_forbidden"
    assert raised.value.status_code == 403
    # No vote landed for the inaccessible space (A3).
    with env["engine"].connect() as connection:
        assert connection.execute(select(chat_ab_vote_table)).all() == []
