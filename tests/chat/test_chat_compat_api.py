"""POST /chat compat outlet, AskBody.overrides and provider_result_unknown SSE contract."""

from __future__ import annotations

from sqlalchemy import select

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest
from app.chat.schema import chat_generation_execution_table, chat_generation_table

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
        generation = (
            connection.execute(
                select(chat_generation_table).where(
                    chat_generation_table.c.id == result.generation_id
                )
            )
            .mappings()
            .one()
        )
        execution = (
            connection.execute(
                select(chat_generation_execution_table).where(
                    chat_generation_execution_table.c.generation_id == result.generation_id
                )
            )
            .mappings()
            .one()
        )
    assert generation["status"] == "running"
    assert execution["status"] == "provider_reconciling"
