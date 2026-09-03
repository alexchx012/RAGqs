"""PostgreSQL concurrency acceptance for the chat worker.

Fixes verified here:
- maintenance (reap/expire) and execution (claim/stop/fence) paths take row
  locks in the same execution-then-generation order, so mixed instances never
  deadlock;
- concurrent claim uses FOR UPDATE SKIP LOCKED so workers split the queue
  instead of blocking or double-claiming.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url

from app.chat.models import RetrievalOutcome
from app.chat.schema import (
    chat_conversation_table,
    chat_generation_event_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_message_table,
)
from app.chat.worker import ChatGenerationWorker
from app.identity.schema import identity_metadata
from app.outbox.schema import outbox_metadata
from app.platform.database import core_metadata
from app.usage.schema import usage_metadata

from .conftest import (
    NOW,
    FakeCalibration,
    FakeChatProvider,
    FixedClock,
    RecordingUsageSubmission,
)

pytestmark = pytest.mark.integration

_PG_URL_ENV = "RAGQS_TEST_POSTGRES_URL"


def _postgres_test_url() -> URL:
    url = os.environ.get(_PG_URL_ENV)
    if not url:
        pytest.skip(
            "PostgreSQL concurrency acceptance requires RAGQS_TEST_POSTGRES_URL "
            "(NOT RUN/BLOCKED)"
        )
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - an invalid external test URL must skip
        pytest.skip("RAGQS_TEST_POSTGRES_URL is malformed (NOT RUN/BLOCKED)")
    if parsed.get_backend_name() != "postgresql":
        pytest.skip("RAGQS_TEST_POSTGRES_URL must use a postgresql backend (NOT RUN/BLOCKED)")
    if parsed.database is None or "test" not in parsed.database.lower():
        pytest.skip("RAGQS_TEST_POSTGRES_URL database name must contain 'test' (NOT RUN/BLOCKED)")
    return parsed


def _schema_url(url: URL, schema: str) -> URL:
    query = dict(url.query)
    existing_options = str(query.get("options", "")).strip()
    query["options"] = f"{existing_options} -csearch_path={schema}".strip()
    return url.set(query=query)


@pytest.fixture()
def pg_chat_engine():
    base_url = _postgres_test_url()
    schema = f"chat_lock_test_{uuid4().hex[:12]}"
    base_engine = create_engine(base_url)
    scoped_engine = None
    try:
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_engine(_schema_url(base_url, schema))
        core_metadata.create_all(scoped_engine)
        identity_metadata.create_all(scoped_engine)
        outbox_metadata.create_all(scoped_engine)
        usage_metadata.create_all(scoped_engine)
        from app.chat.schema import chat_metadata

        chat_metadata.create_all(scoped_engine)
        yield scoped_engine
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


class _WallClock:
    def now_utc(self, connection: object = None) -> datetime:
        del connection
        return datetime.now(UTC)


class _RecordingRetrieval:
    """Minimal retrieval port: no hits, so claimed generations complete fast."""

    def search(self, query: str, **kwargs: object) -> RetrievalOutcome:
        del query, kwargs
        return RetrievalOutcome(hits=())

    def resolve_citations(self, hits: tuple[object, ...], *, principal: object) -> tuple[()]:
        del hits, principal
        return ()


def _worker(engine, *, clock: object | None = None) -> ChatGenerationWorker:
    return ChatGenerationWorker(
        engine,
        clock=clock if clock is not None else FixedClock(NOW),  # type: ignore[arg-type]
        retrieval=_RecordingRetrieval(),
        provider=FakeChatProvider(),
        usage=RecordingUsageSubmission(),
        calibration=FakeCalibration(),
    )


def _seed_generation(
    engine,
    *,
    generation_id: str,
    execution_id: str,
    generation_status: str = "running",
    execution_status: str = "queued",
    now: datetime = NOW,
    next_attempt_at_utc: datetime | None = None,
    disconnect_deadline_at_utc: datetime | None = None,
    absolute_deadline_at_utc: datetime | None = None,
    lease_expires_at_utc: datetime | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            chat_conversation_table.insert().values(
                id=f"conv_{generation_id}",
                owner_user_id="user_1",
                title="lock ordering",
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
                id=f"msg_user_{generation_id}",
                conversation_id=f"conv_{generation_id}",
                owner_user_id="user_1",
                role="user",
                content="question",
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id=f"msg_{generation_id}",
                conversation_id=f"conv_{generation_id}",
                owner_user_id="user_1",
                role="assistant",
                content="",
                status="generating",
                generation_id=generation_id,
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_generation_table.insert().values(
                id=generation_id,
                conversation_id=f"conv_{generation_id}",
                owner_user_id="user_1",
                user_message_id=f"msg_user_{generation_id}",
                message_id=f"msg_{generation_id}",
                root_generation_id=generation_id,
                attempt_number=1,
                status=generation_status,
                requested_effort_level="quick",
                effective_effort_level="quick",
                retrieval_profile_id="default",
                retrieval_profile_version="1",
                rag_budget_policy_version="budget-1",
                absolute_deadline_at_utc=absolute_deadline_at_utc or now + timedelta(hours=1),
                auth_session_id="session_1",
                control_version=0,
                request_content="question",
                request_scope_json={},
                disconnect_deadline_at_utc=disconnect_deadline_at_utc,
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_generation_execution_table.insert().values(
                execution_id=execution_id,
                generation_id=generation_id,
                execution_attempt_number=1,
                status=execution_status,
                fencing_token=1,
                checkpoint_version=0,
                next_attempt_at_utc=(
                    next_attempt_at_utc if next_attempt_at_utc is not None else now
                ),
                lease_expires_at_utc=lease_expires_at_utc,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )


def _execution_status(engine, execution_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(chat_generation_execution_table.c.status).where(
                    chat_generation_execution_table.c.execution_id == execution_id
                )
            ).scalar_one()
        )


def _generation_row(engine, generation_id: str) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(
                    chat_generation_table.c.status,
                    chat_generation_table.c.stop_reason,
                ).where(chat_generation_table.c.id == generation_id)
            )
            .mappings()
            .one()
        )


# ------------------------------------------------------------------------- A1


def test_reconciling_expiry_locks_execution_before_generation(monkeypatch, pg_chat_engine) -> None:
    """The expire path must take the execution row lock before the generation row.

    With a worker transaction parked on the execution row (the execution-path
    lock order), a correctly ordered expirer touches the execution row first
    and therefore cannot make progress into the generation row, so the mixed
    interleaving can never form a lock cycle.
    """

    _seed_generation(
        pg_chat_engine,
        generation_id="gen_exp",
        execution_id="exec_exp",
        generation_status="running",
        execution_status="provider_reconciling",
        absolute_deadline_at_utc=NOW - timedelta(seconds=1),
    )
    probe = _worker(pg_chat_engine)
    maintenance = _worker(pg_chat_engine)
    probe_locked = threading.Event()
    maint_at_execution_lock = threading.Event()
    expiry_done = threading.Event()
    release_probe = threading.Event()
    probe_error: list[BaseException] = []
    expiry_error: list[BaseException] = []
    first_pass: list[int] = []

    def hold_execution_row() -> None:
        try:
            with pg_chat_engine.begin() as connection:
                row = probe._lock_execution(
                    connection, generation_id="gen_exp", execution_id="exec_exp"
                )
                assert row is not None
                probe_locked.set()
                assert release_probe.wait(timeout=30)
                # Execution-path order: the execution row first, then the
                # generation row, exactly like _stop_terminal/_fence_current.
                assert probe._lock_generation(connection, generation_id="gen_exp") is not None
        except BaseException as error:  # noqa: BLE001 - reported via probe_error
            probe_error.append(error)
            probe_locked.set()
            release_probe.set()

    original_lock = maintenance._lock_execution

    def signalling_lock(connection, **kwargs):  # type: ignore[no-untyped-def]
        maint_at_execution_lock.set()
        return original_lock(connection, **kwargs)

    monkeypatch.setattr(maintenance, "_lock_execution", signalling_lock)

    def run_first_expiry() -> None:
        try:
            with pg_chat_engine.begin() as connection:
                first_pass.append(
                    maintenance._expire_provider_reconciling_executions(connection, now=NOW)
                )
        except BaseException as error:  # noqa: BLE001 - reported via expiry_error
            expiry_error.append(error)
        finally:
            expiry_done.set()

    probe_thread = threading.Thread(target=hold_execution_row, daemon=True)
    probe_thread.start()
    assert probe_locked.wait(timeout=10)

    expiry_thread = threading.Thread(target=run_first_expiry, daemon=True)
    expiry_thread.start()
    try:
        assert expiry_done.wait(timeout=10), (
            "the expirer blocked behind the execution row: it must take the "
            "execution row lock before the generation row lock"
        )
        assert not expiry_error, expiry_error
        assert (
            maint_at_execution_lock.is_set()
        ), "the expirer must lock the execution row before the generation row"
        # The locked execution row is skipped this round instead of deadlocking.
        assert first_pass == [0]
    finally:
        release_probe.set()
        probe_thread.join(timeout=30)
        expiry_thread.join(timeout=30)
    assert not probe_error, probe_error

    # Once the execution path released its locks, the next maintenance round
    # terminalizes the generation; the skipped row was not lost.
    with pg_chat_engine.begin() as connection:
        second_pass = maintenance._expire_provider_reconciling_executions(connection, now=NOW)
    assert second_pass == 1
    assert _generation_row(pg_chat_engine, "gen_exp") == {
        "status": "failed",
        "stop_reason": None,
    }
    assert _execution_status(pg_chat_engine, "exec_exp") == "failed"
    with pg_chat_engine.connect() as connection:
        message_status = connection.execute(
            select(chat_message_table.c.status).where(
                chat_message_table.c.generation_id == "gen_exp"
            )
        ).scalar_one()
    assert message_status == "failed"


def test_disconnect_reaper_locks_executions_before_generation(monkeypatch, pg_chat_engine) -> None:
    """The reap path and the stop path must not deadlock on PostgreSQL."""

    _seed_generation(
        pg_chat_engine,
        generation_id="gen_reap",
        execution_id="exec_reap",
        generation_status="running",
        execution_status="running",
        disconnect_deadline_at_utc=NOW - timedelta(seconds=1),
        lease_expires_at_utc=NOW - timedelta(seconds=30),
    )
    probe = _worker(pg_chat_engine)
    maintenance = _worker(pg_chat_engine)
    probe_locked = threading.Event()
    maint_at_execution_lock = threading.Event()
    release_probe = threading.Event()
    probe_error: list[BaseException] = []
    reap_error: list[BaseException] = []
    reap_result: list[int] = []

    def hold_execution_row() -> None:
        try:
            with pg_chat_engine.begin() as connection:
                row = probe._lock_execution(
                    connection, generation_id="gen_reap", execution_id="exec_reap"
                )
                assert row is not None
                probe_locked.set()
                assert release_probe.wait(timeout=30)
                assert probe._lock_generation(connection, generation_id="gen_reap") is not None
        except BaseException as error:  # noqa: BLE001 - reported via probe_error
            probe_error.append(error)
            probe_locked.set()
            release_probe.set()

    original_lock = maintenance._lock_generation_executions

    def signalling_lock(connection, **kwargs):  # type: ignore[no-untyped-def]
        maint_at_execution_lock.set()
        return original_lock(connection, **kwargs)

    monkeypatch.setattr(maintenance, "_lock_generation_executions", signalling_lock)

    probe_thread = threading.Thread(target=hold_execution_row, daemon=True)
    probe_thread.start()
    assert probe_locked.wait(timeout=10)

    def run_reap() -> None:
        try:
            with pg_chat_engine.begin() as connection:
                reap_result.append(maintenance._reap_disconnect_grace(connection, now=NOW))
        except BaseException as error:  # noqa: BLE001 - reported via reap_error
            reap_error.append(error)

    reap_thread = threading.Thread(target=run_reap, daemon=True)
    reap_thread.start()
    try:
        assert maint_at_execution_lock.wait(
            timeout=10
        ), "the reaper must lock the execution rows before the generation row"
        assert not reap_error, reap_error
    finally:
        release_probe.set()
        probe_thread.join(timeout=30)
        reap_thread.join(timeout=30)
    assert not probe_error, probe_error
    assert not reap_error, reap_error
    assert reap_result == [1]
    assert _generation_row(pg_chat_engine, "gen_reap") == {
        "status": "stopped",
        "stop_reason": "client_disconnected",
    }
    assert _execution_status(pg_chat_engine, "exec_reap") == "cancelled"


def test_mixed_maintenance_and_claim_load_converges_without_deadlock(pg_chat_engine) -> None:
    """Concurrent maintenance and execution workers converge; nobody deadlocks."""

    contested = 10
    seed_now = datetime.now(UTC)
    for index in range(contested):
        _seed_generation(
            pg_chat_engine,
            generation_id=f"gen_mix_{index}",
            execution_id=f"exec_mix_{index}",
            generation_status="running",
            execution_status="queued",
            now=seed_now,
            disconnect_deadline_at_utc=seed_now - timedelta(seconds=1),
        )
    errors: list[BaseException] = []

    def maintenance_loop(rounds: int) -> None:
        worker = _worker(pg_chat_engine, clock=_WallClock())
        try:
            for _ in range(rounds):
                worker.run_maintenance()
        except BaseException as error:  # noqa: BLE001 - collected for the assert
            errors.append(error)

    def claim_loop() -> None:
        worker = _worker(pg_chat_engine, clock=_WallClock())
        try:
            idle_rounds = 0
            while idle_rounds < 5:
                outcome = worker.run_once()
                if outcome.executed is None:
                    idle_rounds += 1
                    time.sleep(0.01)
                else:
                    idle_rounds = 0
        except BaseException as error:  # noqa: BLE001 - collected for the assert
            errors.append(error)

    threads = [
        threading.Thread(target=maintenance_loop, args=(25,), daemon=True) for _ in range(2)
    ] + [threading.Thread(target=claim_loop, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors, errors

    # Drain whatever the race left queued, then require full convergence.
    drainer = _worker(pg_chat_engine, clock=_WallClock())
    for _ in range(2 * contested):
        if drainer.run_once().executed is None:
            drainer.run_maintenance()
    drainer.run_maintenance()

    with pg_chat_engine.connect() as connection:
        generation_statuses = dict(
            connection.execute(
                select(chat_generation_table.c.id, chat_generation_table.c.status)
            ).all()
        )
        execution_statuses = dict(
            connection.execute(
                select(
                    chat_generation_execution_table.c.execution_id,
                    chat_generation_execution_table.c.status,
                )
            ).all()
        )
    assert set(generation_statuses.values()) <= {"completed", "stopped", "failed"}
    assert set(execution_statuses.values()) <= {"completed", "cancelled", "failed"}
    assert len(generation_statuses) == contested
    assert len(execution_statuses) == contested
    # Every generation that completed kept a single coherent terminal event.
    with pg_chat_engine.connect() as connection:
        terminal_rows = connection.execute(
            select(
                chat_generation_event_table.c.generation_id,
                chat_generation_event_table.c.event_type,
            ).where(chat_generation_event_table.c.event_type.in_(["done", "error", "stopped"]))
        ).all()
    terminal_by_generation: dict[str, list[str]] = {}
    for generation_id, event_type in terminal_rows:
        terminal_by_generation.setdefault(str(generation_id), []).append(str(event_type))
    for generation_id, event_types in terminal_by_generation.items():
        assert len(event_types) == 1, (generation_id, event_types)


# ------------------------------------------------------------------------- A3


def test_concurrent_claims_skip_locked_rows_without_double_claim(
    monkeypatch, pg_chat_engine
) -> None:
    _seed_generation(
        pg_chat_engine,
        generation_id="gen_claim_a",
        execution_id="exec_claim_a",
        next_attempt_at_utc=NOW - timedelta(seconds=10),
    )
    _seed_generation(
        pg_chat_engine,
        generation_id="gen_claim_b",
        execution_id="exec_claim_b",
    )
    first = _worker(pg_chat_engine)
    second = _worker(pg_chat_engine)
    claimed = threading.Event()
    release = threading.Event()
    first_error: list[BaseException] = []
    first_claim: list[tuple[str, str] | None] = []
    original_lock_generation = first._lock_generation

    def gated_lock_generation(connection, *, generation_id):  # type: ignore[no-untyped-def]
        claimed.set()
        assert release.wait(timeout=30)
        return original_lock_generation(connection, generation_id=generation_id)

    monkeypatch.setattr(first, "_lock_generation", gated_lock_generation)

    def claim_first() -> None:
        try:
            first_claim.append(first._claim_execution())
        except BaseException as error:  # noqa: BLE001 - collected for the assert
            first_error.append(error)
            release.set()

    claim_thread = threading.Thread(target=claim_first, daemon=True)
    claim_thread.start()
    assert claimed.wait(timeout=10), "the first claim must reach its generation lock"

    # The first worker holds the exec_claim_a row lock and is parked before its
    # generation lock. The second worker must skip that locked candidate and
    # claim exec_claim_b instead of blocking or idling.
    second_claim: list[tuple[str, str] | None] = []
    second_error: list[BaseException] = []

    def claim_second() -> None:
        try:
            second_claim.append(second._claim_execution())
        except BaseException as error:  # noqa: BLE001 - collected for the assert
            second_error.append(error)

    second_thread = threading.Thread(target=claim_second, daemon=True)
    second_thread.start()
    try:
        second_thread.join(timeout=10)
        assert not second_thread.is_alive(), "the second claim must not queue behind the first"
        assert not second_error, second_error
        assert second_claim == [("exec_claim_b", "gen_claim_b")]
    finally:
        release.set()
        claim_thread.join(timeout=30)
    assert not first_error, first_error
    assert first_claim == [("exec_claim_a", "gen_claim_a")]

    with pg_chat_engine.connect() as connection:
        owners = dict(
            connection.execute(
                select(
                    chat_generation_execution_table.c.execution_id,
                    chat_generation_execution_table.c.lease_owner,
                )
            ).all()
        )
    assert set(owners) == {"exec_claim_a", "exec_claim_b"}
    assert all(owners.values()), owners
    assert owners["exec_claim_a"] != owners["exec_claim_b"]
