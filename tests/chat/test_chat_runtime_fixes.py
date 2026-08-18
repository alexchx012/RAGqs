"""Regression tests for chat runtime correctness fixes (lease heartbeat, deletion,
SSE event-loop blocking, A/B pair expiry accounting, naive-clock UTC)."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.chat.generation import GenerationService
from app.chat.models import AskRequest, RetrievalOutcome
from app.chat.schema import chat_generation_execution_table, chat_generation_table
from app.chat.worker import ChatGenerationWorker
from app.platform.errors import PlatformError

from .conftest import (
    NOW,
    FakeCalibration,
    build_runtime_authorization,
    build_test_env,
    provision_and_login,
)


def _ask(env: dict, principal, conversation_id: str, key: str = "ask-1"):
    return (
        env["runtime"]
        .resolve("chat_generation_service")
        .ask(
            principal=principal,
            conversation_id=conversation_id,
            request=AskRequest(content="hello", effort_level="quick", scope=None),
            idempotency_key=key,
        )
    )


class NaiveClock:
    """Clock that returns a timezone-naive value, as some DB backends do."""

    def now_utc(self, connection: Any = None) -> datetime:
        del connection
        return datetime(2026, 8, 13, 12, 0)


# --------------------------------------------------------------------- A1


def test_execution_heartbeat_renews_lease_and_prevents_recovery(monkeypatch) -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    _ask(env, principal, conversation_id)

    with env["engine"].begin() as connection:
        # Single execution row in this environment.
        execution_id = str(
            connection.execute(select(chat_generation_execution_table.c.execution_id)).scalar_one()
        )
        connection.execute(
            update(chat_generation_execution_table).values(
                status="running",
                fencing_token=7,
                lease_owner="worker_a",
                lease_expires_at_utc=NOW - timedelta(seconds=1),
            )
        )

    worker = env["runtime"].resolve("chat_generation_worker")
    monkeypatch.setattr("app.chat.worker.HEARTBEAT_SECONDS", 0.05)
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=worker._heartbeat_loop,
        kwargs={"execution_id": execution_id, "fencing_token": 7, "stop": stop},
        daemon=True,
    )
    heartbeat.start()
    time.sleep(0.3)
    stop.set()
    heartbeat.join(timeout=2)

    with env["engine"].connect() as connection:
        row = (
            connection.execute(
                select(
                    chat_generation_execution_table.c.status,
                    chat_generation_execution_table.c.lease_expires_at_utc,
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "running"
        assert row["lease_expires_at_utc"] is not None

    maintenance = worker.run_maintenance()
    assert maintenance["executions_recovered"] == 0
    with env["engine"].connect() as connection:
        statuses = (
            connection.execute(select(chat_generation_execution_table.c.status)).scalars().all()
        )
    assert statuses == ["running"]


# --------------------------------------------------------------------- A2


def test_deleting_running_conversation_does_not_break_worker(monkeypatch) -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    first = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    second = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    _ask(env, principal, first, key="ask-1")
    _ask(env, principal, second, key="ask-2")

    class DeletingFailingProvider:
        def __init__(self) -> None:
            self.deleted = False

        def generate(self, request: Any) -> Any:
            if not self.deleted:
                self.deleted = True
                response = env["client"].delete(f"/v1/conversations/{first}", headers=headers)
                assert response.status_code == 204
                raise PlatformError("provider_failed", "provider failed", {}, 502)
            return env["provider"].generate(request)

    worker = env["runtime"].resolve("chat_generation_worker")
    deleting_provider = DeletingFailingProvider()
    monkeypatch.setattr(worker, "_provider", deleting_provider)

    first_outcome = worker.run_once()
    assert first_outcome.stage == "failed"

    second_outcome = worker.run_once()
    assert second_outcome.stage == "executed"
    with env["engine"].connect() as connection:
        # Only the second (undeleted) generation remains, completed.
        remaining = connection.execute(select(chat_generation_table.c.status)).scalars().all()
    assert remaining == ["completed"]


# --------------------------------------------------------------------- A3


def test_sse_polling_runs_db_reads_off_the_event_loop() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)

    service = env["runtime"].resolve("chat_stream_service")
    service._poll_seconds = 0.02
    threads: list[int] = []
    original_read = service._read_events

    def recording_read(generation_id: str, after_seq: int) -> list[Any]:
        threads.append(threading.get_ident())
        return original_read(generation_id, after_seq)

    service._read_events = recording_read

    async def scenario() -> None:
        generator = service.stream(
            principal=principal, generation_id=result.generation_id, last_event_id=0
        )
        try:
            async with asyncio.timeout(0.5):
                async for _ in generator:
                    pass
        except TimeoutError:
            pass

    asyncio.run(scenario())
    assert threads, "expected at least one polling DB read"
    main_thread = threading.get_ident()
    assert all(ident != main_thread for ident in threads)


# --------------------------------------------------------------------- A4


def test_expire_past_deadline_pairs_counts_actual_updates() -> None:
    env = build_test_env()
    worker = env["runtime"].resolve("chat_generation_worker")
    statements: list[object] = []

    class _Result:
        def __init__(self, *, rows=None, rowcount: int = 0) -> None:
            self._rows = rows if rows is not None else []
            self.rowcount = rowcount

        def mappings(self):  # type: ignore[no-untyped-def]
            return self

        def all(self):  # type: ignore[no-untyped-def]
            return self._rows

    class _Connection:
        def execute(self, statement):  # type: ignore[no-untyped-def]
            statements.append(statement)
            if len(statements) == 1:
                return _Result(
                    rows=[
                        {
                            "pair_id": "pair_past",
                            "expires_at_utc": NOW - timedelta(seconds=1),
                            "close_deadline_at_utc": None,
                        },
                        {
                            "pair_id": "pair_future",
                            "expires_at_utc": NOW + timedelta(minutes=5),
                            "close_deadline_at_utc": None,
                        },
                    ]
                )
            return _Result(rowcount=1)

    expired = worker._expire_past_deadline_pairs(_Connection(), now=NOW)
    assert expired == 1


def test_naive_clock_values_are_normalized_to_utc() -> None:
    env = build_test_env()
    worker = ChatGenerationWorker(
        env["engine"],
        clock=NaiveClock(),
        retrieval=env["retrieval"],
        provider=env["provider"],
        usage=env["runtime"].resolve("chat_usage_submission"),
        calibration=FakeCalibration(),
    )
    assert worker._now().tzinfo is UTC

    generation_service = GenerationService(
        env["engine"],
        clock=NaiveClock(),
        authorization=build_runtime_authorization(env["identity"]),
        calibration=FakeCalibration(),
    )
    assert generation_service._now(None).tzinfo is UTC

    from app.chat.conversations import ConversationService

    conversations = ConversationService(env["engine"], now=NaiveClock())
    assert conversations._current_time(None).tzinfo is UTC
