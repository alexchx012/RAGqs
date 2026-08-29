"""POST /chat compat outlet, AskBody.overrides and provider_result_unknown SSE contract."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest
from app.chat.schema import (
    chat_generation_event_table,
    chat_generation_execution_table,
    chat_generation_table,
)

from .conftest import build_test_env, provision_and_login


class TransportDroppingProvider:
    """Provider transport that dies after dispatch with a non-platform error."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("connection reset after dispatch")
        return ChatProviderResponse(content="recovered", input_tokens=1, output_tokens=1)


def _auth(env: dict, username: str) -> dict[str, str]:
    token, _ = provision_and_login(env["identity"], username)
    return {"Authorization": f"Bearer {token}"}


def test_post_chat_returns_envelope_and_enters_execution_path() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")

    response = env["client"].post(
        "/v1/chat",
        json={"content": "hello", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "compat-1"},
    )

    assert response.status_code == 202
    envelope = response.json()
    assert envelope["code"] == 202
    assert envelope["message"] == "accepted"
    assert set(envelope["data"]) == {
        "conversation_id",
        "generation_id",
        "message_id",
        "user_message_id",
        "replay",
    }
    assert envelope["data"]["replay"] is False

    # 兼容出口必须进入现有会话执行路径：worker 执行后可在既有会话读模型中看到消息。
    env["runtime"].resolve("chat_generation_worker").run_once()
    detail = env["client"].get(
        f"/v1/conversations/{envelope['data']['conversation_id']}", headers=headers
    )
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert any(item["id"] == envelope["data"]["message_id"] for item in messages)


def test_post_chat_with_existing_conversation_and_idempotent_replay() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]

    first = env["client"].post(
        "/v1/chat",
        json={"content": "hi", "effort_level": "quick", "conversation_id": conversation_id},
        headers={**headers, "Idempotency-Key": "compat-2"},
    )
    assert first.status_code == 202
    assert first.json()["data"]["conversation_id"] == conversation_id

    replay = env["client"].post(
        "/v1/chat",
        json={"content": "hi", "effort_level": "quick", "conversation_id": conversation_id},
        headers={**headers, "Idempotency-Key": "compat-2"},
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["replay"] is True
    assert replay.json()["data"]["generation_id"] == first.json()["data"]["generation_id"]


def test_post_chat_validation_errors_use_compatibility_envelope() -> None:
    env = build_test_env()
    headers = _auth(env, "alice")

    response = env["client"].post(
        "/v1/chat",
        json={"content": "", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "compat-error-1"},
    )

    assert response.status_code == 422
    envelope = response.json()
    assert envelope["code"] == 422
    assert envelope["message"]
    assert envelope["errorMessage"] == envelope["message"]
    assert envelope["data"]["error"]["code"] == "validation_error"
    assert set(envelope) == {"code", "message", "data", "errorMessage"}


def test_post_chat_applies_per_user_sliding_rate_limit() -> None:
    env = build_test_env()
    service = env["runtime"].resolve("chat_generation_service")
    service._ask_rate_limit_per_minute = 1
    headers = _auth(env, "alice")

    first = env["client"].post(
        "/v1/chat",
        json={"content": "first", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "rate-1"},
    )
    second = env["client"].post(
        "/v1/chat",
        json={"content": "second", "effort_level": "quick"},
        headers={**headers, "Idempotency-Key": "rate-2"},
    )

    assert first.status_code == 202
    assert second.status_code == 429
    envelope = second.json()
    assert envelope["data"]["error"]["code"] == "rate_limit_exceeded"
    assert envelope["data"]["error"]["details"]["retry_after_seconds"] >= 1
    assert envelope["errorMessage"] == envelope["message"]


def test_ask_body_accepts_optional_overrides_without_changing_execution_defaults() -> None:
    env = build_test_env()
    headers = {**_auth(env, "alice"), "Accept": "text/event-stream"}
    base = {"content": "hello", "effort_level": "quick"}

    without = env["client"].post(
        "/v1/chat",
        json=base,
        headers={**headers, "Idempotency-Key": "ovr-1"},
    )
    with_overrides = env["client"].post(
        "/v1/chat",
        json={**base, "overrides": {"model": "reserved"}},
        headers={**headers, "Idempotency-Key": "ovr-2"},
    )
    assert with_overrides.status_code == 202
    assert without.status_code == 202
    assert (
        with_overrides.json()["data"]["conversation_id"]
        != without.json()["data"]["conversation_id"]
    )

    for envelope in (without.json(), with_overrides.json()):
        assert envelope["data"]["replay"] is False

    worker = env["runtime"].resolve("chat_generation_worker")
    worker.run_once()
    worker.run_once()
    assert len(env["provider"].calls) == 2
    assert {request.effort_level for request in env["provider"].calls} == {"quick"}


def test_provider_result_unknown_emits_parseable_error_event() -> None:
    provider = TransportDroppingProvider()
    env = build_test_env(provider=provider)
    token, _ = provision_and_login(env["identity"], "alice")
    from app.chat.models import AskRequest

    principal = env["identity"].authenticate_access_token(token)
    service = env["runtime"].resolve("chat_generation_service")
    conversation_id = (
        env["runtime"]
        .resolve("chat_conversation_service")
        .create_conversation(user_id=str(principal.user_id))["id"]
    )
    result = service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="unknown-1",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    assert provider.calls == 1

    with env["engine"].connect() as connection:
        generation = connection.execute(
            select(chat_generation_table).where(chat_generation_table.c.id == result.generation_id)
        ).mappings().one()
        execution = connection.execute(
            select(chat_generation_execution_table).where(
                chat_generation_execution_table.c.generation_id == result.generation_id
            )
        ).mappings().one()
    assert generation["status"] == "running"
    assert str(generation["request_id"]).startswith("req_")
    assert execution["status"] == "provider_reconciling"

    with env["engine"].begin() as connection:
        connection.execute(
            chat_generation_table.update()
            .where(chat_generation_table.c.id == result.generation_id)
            .values(absolute_deadline_at_utc=env["clock"].now - timedelta(seconds=1))
        )
    env["runtime"].resolve("chat_generation_worker").run_maintenance()

    with env["engine"].connect() as connection:
        error_event = connection.execute(
            select(chat_generation_event_table)
            .where(
                chat_generation_event_table.c.generation_id == result.generation_id,
                chat_generation_event_table.c.event_type == "error",
            )
        ).mappings().one()
    assert error_event["data_json"]["code"] == "provider_result_unknown"
    assert error_event["data_json"]["request_id"] == generation["request_id"]
