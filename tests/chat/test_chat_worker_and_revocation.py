"""Worker state machine, revocation convergence and maintenance reaper tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select, update

from app.chat.models import AskRequest, RetrievalHitOutcome, RetrievalOutcome
from app.chat.ports import RecordingChatRetrievalPort
from app.chat.schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_generation_event_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_message_table,
    chat_subscription_lease_table,
)
from app.identity.revocation import GenerationRevocationCommand
from app.identity.schema import identity_revocation_command_table

from .conftest import (
    NOW,
    FakeCalibration,
    build_test_env,
    open_window,
    provision_and_login,
    sse_frames,
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


def _events(env: dict, headers: dict, generation_id: str):
    response = env["client"].get(
        f"/v1/generations/{generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    return sse_frames(response.text)


def test_session_revocation_converges_running_generation_to_stopped() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, session_id = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    service = env["runtime"].resolve("chat_generation_service")

    # A live recovery stream creates a subscription lease.
    with env["engine"].begin() as connection:
        from app.chat.leases import create_lease

        create_lease(
            connection,
            generation_id=result.generation_id,
            auth_session_id=principal.auth_session_id,
            now=NOW,
        )
    env["identity"].revoke_session(
        user_id=principal.user_id, session_id=session_id, reason="session_revoked"
    )

    with env["engine"].connect() as connection:
        generation = connection.execute(select(chat_generation_table)).mappings().one()
        assert generation["status"] == "stop_requested"
        assert generation["stop_reason"] == "authorization_revoked"
        assert connection.execute(select(chat_subscription_lease_table)).mappings().first() is None

    env["runtime"].resolve("chat_generation_worker").run_once()
    with env["engine"].connect() as connection:
        events = (
            connection.execute(
                select(chat_generation_event_table)
                .where(chat_generation_event_table.c.generation_id == result.generation_id)
                .order_by(chat_generation_event_table.c.event_seq)
            )
            .mappings()
            .all()
        )
    assert events[-1]["event_type"] == "stopped"
    assert events[-1]["data_json"]["stop_reason"] == "authorization_revoked"

    revoked_token = env["client"].get(
        f"/v1/generations/{result.generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    assert revoked_token.status_code == 401
    assert revoked_token.json()["error"]["code"] == "session_revoked"
    del service


def test_account_revocation_and_receipt_replay_idempotency() -> None:
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    _ask(env, principal, conversation_id)

    env["identity"].revoke_all_sessions(user_id=principal.user_id, reason="account_revoked")
    with env["engine"].connect() as connection:
        generation = connection.execute(select(chat_generation_table)).mappings().one()
        assert generation["status"] == "stop_requested"
        assert generation["stop_reason"] == "authorization_revoked"

    port = env["runtime"].resolve("generation_revocation_port")
    command = GenerationRevocationCommand(
        operation_id="operation_1",
        user_id=principal.user_id,
        auth_session_id=None,
        reason="account_revoked",
        revoked_at=NOW,
        identity_transition_version=2,
    )
    with env["engine"].begin() as connection:
        first = port.revoke(command, connection=connection)
    with env["engine"].begin() as connection:
        second = port.revoke(command, connection=connection)
    assert first.reference == second.reference
    assert second.state == "completed"


def test_durable_revocation_command_is_consumed_idempotently() -> None:
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)

    with env["engine"].begin() as connection:
        connection.execute(
            identity_revocation_command_table.insert().values(
                operation_id="operation_durable",
                user_id=principal.user_id,
                auth_session_id=principal.auth_session_id,
                reason="session_revoked",
                identity_transition_version=1,
                receipt_reference="generation-outbox:operation_durable",
                receipt_state="accepted",
                created_at_utc=NOW,
            )
        )
    worker = env["runtime"].resolve("chat_generation_worker")
    worker.run_maintenance()
    with env["engine"].connect() as connection:
        generation = connection.execute(select(chat_generation_table)).mappings().one()
        receipt_state = connection.execute(
            select(identity_revocation_command_table.c.receipt_state).where(
                identity_revocation_command_table.c.operation_id == "operation_durable"
            )
        ).scalar_one()
    assert generation["status"] == "stop_requested"
    assert receipt_state == "completed"
    worker.run_once()
    frames = _events(env, headers, result.generation_id)
    assert [frame[0] for frame in frames].count("stopped") == 1


def test_expired_execution_lease_recovers_and_respects_physical_cap() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)

    with env["engine"].begin() as connection:
        connection.execute(
            update(chat_generation_execution_table)
            .where(chat_generation_execution_table.c.generation_id == result.generation_id)
            .values(status="running", lease_expires_at_utc=NOW - timedelta(seconds=1))
        )
    worker = env["runtime"].resolve("chat_generation_worker")
    worker.run_maintenance()
    with env["engine"].connect() as connection:
        executions = (
            connection.execute(
                select(chat_generation_execution_table).order_by(
                    chat_generation_execution_table.c.execution_attempt_number
                )
            )
            .mappings()
            .all()
        )
        assert [row["status"] for row in executions] == ["expired", "queued"]
    worker.run_once()
    frames = _events(env, headers, result.generation_id)
    assert frames[-1][0] == "done"


def test_recovery_skips_execution_lost_to_another_maintenance_worker() -> None:
    env = build_test_env()
    worker = env["runtime"].resolve("chat_generation_worker")
    statements: list[object] = []

    class _Result:
        def __init__(
            self,
            *,
            rows: list[dict[str, object]] | None = None,
            row: dict[str, object] | None = None,
            rowcount: int = 0,
        ) -> None:
            self._rows = [] if rows is None else rows
            self._row = row
            self.rowcount = rowcount

        def mappings(self):  # type: ignore[no-untyped-def]
            return self

        def all(self):  # type: ignore[no-untyped-def]
            return self._rows

        def one_or_none(self):  # type: ignore[no-untyped-def]
            return self._row

    class _Connection:
        def execute(self, statement):  # type: ignore[no-untyped-def]
            statements.append(statement)
            if len(statements) == 1:
                return _Result(
                    rows=[
                        {
                            "execution_id": "exec_1",
                            "generation_id": "gen_1",
                            "execution_attempt_number": 0,
                            "fencing_token": 1,
                            "lease_expires_at_utc": NOW - timedelta(seconds=1),
                        }
                    ]
                )
            if len(statements) == 2:
                # Another maintenance transaction already changed the row.
                return _Result(rowcount=0)
            if len(statements) == 3:
                return _Result(
                    row={
                        "status": "running",
                        "absolute_deadline_at_utc": NOW + timedelta(minutes=1),
                    }
                )
            if len(statements) == 4:
                return _Result(rows=[])
            return _Result()

    recovered = worker._recover_expired_executions(_Connection(), now=NOW)

    assert recovered == 0
    assert len(statements) == 2


def test_disconnect_grace_reaps_unleased_generation() -> None:
    env = build_test_env()
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    with env["engine"].begin() as connection:
        connection.execute(
            update(chat_generation_table)
            .where(chat_generation_table.c.id == result.generation_id)
            .values(disconnect_deadline_at_utc=NOW - timedelta(seconds=1))
        )
    env["runtime"].resolve("chat_generation_worker").run_maintenance()
    frames = _events(env, headers, result.generation_id)
    assert frames[-1][0] == "stopped"
    assert json.loads(frames[-1][2])["stop_reason"] == "client_disconnected"


def test_rouge_l_near_duplicate_collapses_ab_pair() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=())},
    )
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    env["runtime"].resolve("chat_generation_worker").run_once()

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = detail["messages"][1]
    assert assistant["ab"] is None
    assert "answer for hello" in assistant["content"]
    frames = _events(env, headers, result.generation_id)
    answer_frames = [frame for frame in frames if frame[0] == "answer"]
    assert len(answer_frames) == 1
    assert answer_frames[0][1] is not None


def test_no_context_answer_mode_when_retrieval_returns_nothing() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    env["runtime"].resolve("chat_generation_worker").run_once()
    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assert detail["messages"][1]["answer_mode"] == "no_context"
    assert detail["messages"][1]["citations"] == []
    frames = _events(env, headers, result.generation_id)
    answer = json.loads(frames[-2][2])
    assert answer["answer_mode"] == "no_context"


def test_provider_failure_marks_generation_failed_and_not_retryable_when_completed() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    env["provider"].fail_next = True
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    env["runtime"].resolve("chat_generation_worker").run_once()
    frames = _events(env, headers, result.generation_id)
    assert frames[-1][0] == "error"
    error = json.loads(frames[-1][2])
    assert error["code"] == "provider_failed"

    service = env["runtime"].resolve("chat_generation_service")
    completed_conflict = env["client"].post(
        f"/v1/generations/{result.generation_id}/stop", headers=headers
    )
    assert completed_conflict.status_code == 409
    assert completed_conflict.json()["error"]["code"] == "generation_already_terminal"
    del service


def test_ab_pair_discarded_and_expired_when_generation_fails() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=())},
    )
    env["provider"].fail_next = True
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    env["runtime"].resolve("chat_generation_worker").run_once()

    with env["engine"].connect() as connection:
        pair = connection.execute(select(chat_ab_pair_table)).mappings().one()
        candidates = (
            connection.execute(
                select(chat_ab_candidate_table.c.status).order_by(
                    chat_ab_candidate_table.c.candidate
                )
            )
            .scalars()
            .all()
        )
    assert pair["status"] == "expired"
    assert list(candidates) == ["discarded", "discarded"]
    frames = _events(env, headers, result.generation_id)
    assert frames[-1][0] == "error"


def test_stop_rechecks_session_authorization_in_transaction() -> None:
    from app.platform.errors import PlatformError

    env = build_test_env()
    token, session_id = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    env["identity"].revoke_session(
        user_id=principal.user_id, session_id=session_id, reason="session_revoked"
    )

    service = env["runtime"].resolve("chat_generation_service")
    try:
        service.stop(principal=principal, generation_id=result.generation_id)
    except PlatformError as error:
        assert error.code == "session_revoked"
        assert error.status_code == 401
    else:
        raise AssertionError("stop must reject a revoked session in-transaction")


class _PartiallyVisibleRetrieval(RecordingChatRetrievalPort):
    def __init__(
        self, *, before_first_partial_resolution: Callable[[], None] | None = None
    ) -> None:
        super().__init__()
        self._resolution_count = 0
        self._before_first_partial_resolution = before_first_partial_resolution

    def resolve_citations(self, hits, *, principal):  # type: ignore[no-untyped-def]
        self._resolution_count += 1
        if self._resolution_count == 1:
            if self._before_first_partial_resolution is not None:
                self._before_first_partial_resolution()
            return super().resolve_citations(hits[:1], principal=principal)
        return super().resolve_citations(hits, principal=principal)


def test_effort_upgrade_persists_the_effort_used_by_retrieval_provider_and_events() -> None:
    retrieval = _PartiallyVisibleRetrieval()
    retrieval.outcomes["hello"] = RetrievalOutcome(
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
                space_id="space_1",
                locator={"page": 2},
                snippet="second",
            ),
        )
    )
    env = build_test_env(retrieval=retrieval)
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)

    env["runtime"].resolve("chat_generation_worker").run_once()

    with env["engine"].connect() as connection:
        generation = connection.execute(select(chat_generation_table)).mappings().one()
        answer = (
            connection.execute(
                select(chat_generation_event_table)
                .where(
                    chat_generation_event_table.c.generation_id == result.generation_id,
                    chat_generation_event_table.c.event_type == "answer",
                )
                .order_by(chat_generation_event_table.c.event_seq)
            )
            .mappings()
            .one()
        )
        notice = (
            connection.execute(
                select(chat_generation_event_table)
                .where(
                    chat_generation_event_table.c.generation_id == result.generation_id,
                    chat_generation_event_table.c.event_type == "notice",
                )
                .order_by(chat_generation_event_table.c.event_seq)
            )
            .mappings()
            .one()
        )
    assert generation["effective_effort_level"] == "think"
    assert generation["upgraded_from"] == "quick"
    assert [search["effort"] for search in retrieval.searches] == ["quick", "think"]
    assert [request.effort_level for request in env["provider"].calls] == ["think"]
    assert notice["data_json"] == {"kind": "effort_upgraded", "detail": {"effort_level": "think"}}
    assert answer["data_json"]["effort_level"] == "think"
    assert answer["data_json"]["upgraded_from"] == "quick"


def test_stale_execution_cannot_persist_effort_upgrade_after_fence_then_lease_recovery(
    monkeypatch,
) -> None:
    retrieval = _PartiallyVisibleRetrieval()
    retrieval.outcomes["hello"] = RetrievalOutcome(
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
                space_id="space_1",
                locator={"page": 2},
                snippet="second",
            ),
        )
    )
    env = build_test_env(retrieval=retrieval)
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    worker = env["runtime"].resolve("chat_generation_worker")
    original_fence_current = worker._fence_current
    recovery_triggered = False

    def recover_claimed_execution() -> None:
        with env["engine"].begin() as connection:
            connection.execute(
                update(chat_generation_execution_table)
                .where(chat_generation_execution_table.c.generation_id == result.generation_id)
                .values(lease_expires_at_utc=NOW - timedelta(seconds=1))
            )
        assert worker.run_maintenance()["executions_recovered"] == 1

    def recover_after_current_fence(
        connection,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
    ) -> bool:
        nonlocal recovery_triggered
        current = original_fence_current(
            connection,
            generation_id=generation_id,
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
        )
        if current and not recovery_triggered:
            recovery_triggered = True
            recover_claimed_execution()
        return current

    def arm_post_fence_recovery() -> None:
        monkeypatch.setattr(worker, "_fence_current", recover_after_current_fence)

    retrieval._before_first_partial_resolution = arm_post_fence_recovery
    worker.run_once()

    with env["engine"].connect() as connection:
        generation = connection.execute(select(chat_generation_table)).mappings().one()
        executions = (
            connection.execute(
                select(chat_generation_execution_table).order_by(
                    chat_generation_execution_table.c.execution_attempt_number
                )
            )
            .mappings()
            .all()
        )
        notices = (
            connection.execute(
                select(chat_generation_event_table).where(
                    chat_generation_event_table.c.generation_id == result.generation_id,
                    chat_generation_event_table.c.event_type == "notice",
                )
            )
            .mappings()
            .all()
        )
    assert recovery_triggered
    assert generation["status"] == "running"
    assert generation["effective_effort_level"] == "quick"
    assert generation["upgraded_from"] is None
    assert [execution["status"] for execution in executions] == ["expired", "queued"]
    assert not notices
    assert not env["provider"].calls

    worker.run_once()
    assert [request.effort_level for request in env["provider"].calls] == ["quick"]


def test_recovered_execution_uses_persisted_effective_effort() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)

    with env["engine"].begin() as connection:
        connection.execute(
            update(chat_generation_table)
            .where(chat_generation_table.c.id == result.generation_id)
            .values(effective_effort_level="think", upgraded_from="quick")
        )
        connection.execute(
            update(chat_generation_execution_table)
            .where(chat_generation_execution_table.c.generation_id == result.generation_id)
            .values(
                status="running",
                lease_owner="interrupted_worker",
                lease_expires_at_utc=NOW - timedelta(seconds=1),
            )
        )

    worker = env["runtime"].resolve("chat_generation_worker")
    worker.run_maintenance()
    worker.run_once()

    assert [request.effort_level for request in env["provider"].calls] == ["think"]


def test_stale_publish_converges_a_stop_requested_generation_without_publishing_an_answer() -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask(env, principal, conversation_id)
    worker = env["runtime"].resolve("chat_generation_worker")
    claimed = worker._claim_execution()
    assert claimed is not None
    execution_id, generation_id = claimed
    generation = worker._read_generation(generation_id)
    fencing_token = worker._execution_fence(generation_id, execution_id)

    env["runtime"].resolve("chat_generation_service").stop(
        principal=principal,
        generation_id=result.generation_id,
    )

    worker._publish(
        generation=generation,
        execution_id=execution_id,
        fencing_token=fencing_token,
        control_version=int(generation["control_version"]),
        candidates=[
            {
                "candidate": 0,
                "content": "stale answer",
                "citations": [],
                "answer_mode": "no_context",
            }
        ],
    )

    with env["engine"].connect() as connection:
        execution = connection.execute(select(chat_generation_execution_table)).mappings().one()
        generation_after = connection.execute(select(chat_generation_table)).mappings().one()
        message = (
            connection.execute(
                select(chat_message_table).where(
                    chat_message_table.c.generation_id == result.generation_id
                )
            )
            .mappings()
            .one()
        )
        events = connection.execute(select(chat_generation_event_table)).mappings().all()
    assert execution["status"] == "cancelled"
    assert execution["lease_owner"] is None
    assert execution["lease_expires_at_utc"] is None
    assert generation_after["status"] == "stopped"
    assert generation_after["stop_reason"] == "manual_request"
    assert message["status"] == "stopped"
    assert message["content"] == ""
    assert events[-1]["event_type"] == "stopped"
    assert all(event["event_type"] != "done" for event in events)


def test_stale_publish_cannot_outlive_a_fence_then_lease_recovery(monkeypatch) -> None:
    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    _ask(env, principal, conversation_id)
    worker = env["runtime"].resolve("chat_generation_worker")
    claimed = worker._claim_execution()
    assert claimed is not None
    execution_id, generation_id = claimed
    generation = worker._read_generation(generation_id)
    fencing_token = worker._execution_fence(generation_id, execution_id)
    original_fence_current = worker._fence_current
    recovery_triggered = False

    def recover_claimed_execution() -> None:
        with env["engine"].begin() as connection:
            connection.execute(
                update(chat_generation_execution_table)
                .where(chat_generation_execution_table.c.execution_id == execution_id)
                .values(lease_expires_at_utc=NOW - timedelta(seconds=1))
            )
        assert worker.run_maintenance()["executions_recovered"] == 1

    def recover_after_current_fence(
        connection,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
    ) -> bool:
        nonlocal recovery_triggered
        current = original_fence_current(
            connection,
            generation_id=generation_id,
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
        )
        if current and not recovery_triggered:
            recovery_triggered = True
            recover_claimed_execution()
        return current

    monkeypatch.setattr(worker, "_fence_current", recover_after_current_fence)
    worker._publish(
        generation=generation,
        execution_id=execution_id,
        fencing_token=fencing_token,
        control_version=int(generation["control_version"]),
        candidates=[
            {
                "candidate": 0,
                "content": "stale answer",
                "citations": [],
                "answer_mode": "no_context",
            }
        ],
    )

    with env["engine"].connect() as connection:
        generation_after = connection.execute(select(chat_generation_table)).mappings().one()
        executions = (
            connection.execute(
                select(chat_generation_execution_table).order_by(
                    chat_generation_execution_table.c.execution_attempt_number
                )
            )
            .mappings()
            .all()
        )
        events = connection.execute(select(chat_generation_event_table)).mappings().all()
    assert recovery_triggered
    assert generation_after["status"] == "running"
    assert [execution["status"] for execution in executions] == ["expired", "queued"]
    assert all(event["event_type"] not in {"answer", "done"} for event in events)


def test_fence_locks_execution_before_generation(monkeypatch) -> None:
    env = build_test_env()
    worker = env["runtime"].resolve("chat_generation_worker")
    lock_order: list[str] = []

    class _Result:
        def mappings(self):  # type: ignore[no-untyped-def]
            return self

        def one_or_none(self):  # type: ignore[no-untyped-def]
            return {"fencing_token": 7, "status": "running"}

    class _Connection:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def execute(self, statement):  # type: ignore[no-untyped-def]
            del statement
            lock_order.append("execution")
            return _Result()

    def lock_generation(connection, *, generation_id: str):  # type: ignore[no-untyped-def]
        del connection, generation_id
        lock_order.append("generation")
        return {"control_version": 3, "status": "running"}

    monkeypatch.setattr(worker, "_lock_generation", lock_generation)

    assert worker._fence_current(
        _Connection(),
        generation_id="gen_1",
        execution_id="exec_1",
        fencing_token=7,
        control_version=3,
    )
    assert lock_order == ["execution", "generation"]


# --------------------------------------------- query error classification fixes


def _acl_hit(chunk: str) -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id=f"doc_{chunk}",
        document_version_id=f"docver_{chunk}",
        publication_id=f"pub_{chunk}",
        chunk_id=chunk,
        space_id="space_1",
        locator={"page": 1},
        snippet=f"snippet {chunk}",
    )


class _AllFilteredRetrieval(RecordingChatRetrievalPort):
    """Every citation resolution is ACL-filtered during generation."""

    def resolve_citations(self, hits, *, principal):  # type: ignore[no-untyped-def]
        del principal
        return ()


def _ask_scoped(env: dict, principal, conversation_id: str, content: str, scope=None):
    return (
        env["runtime"]
        .resolve("chat_generation_service")
        .ask(
            principal=principal,
            conversation_id=conversation_id,
            request=AskRequest(content=content, effort_level="quick", scope=scope),
            idempotency_key=f"ask-{content}",
        )
    )


def _failed_generation(env: dict, principal, conversation_id: str, scope=None):
    env["provider"].fail_next = True
    result = _ask_scoped(env, principal, conversation_id, "hello", scope=scope)
    env["runtime"].resolve("chat_generation_worker").run_once()
    return result.generation_id


def test_all_hits_acl_filtered_answers_no_context_instead_of_failing() -> None:
    env = build_test_env(
        retrieval=_AllFilteredRetrieval(),
        outcomes={"hello": RetrievalOutcome(hits=(_acl_hit("c1"), _acl_hit("c2")))},
    )
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask_scoped(env, principal, conversation_id, "hello")
    env["runtime"].resolve("chat_generation_worker").run_once()

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = detail["messages"][1]
    assert assistant["status"] == "completed"
    assert assistant["answer_mode"] == "no_context"
    assert assistant["citations"] == []
    frames = _events(env, headers, result.generation_id)
    assert frames[-1][0] == "done"
    answer = json.loads(frames[-2][2])
    assert answer["answer_mode"] == "no_context"


def test_rerank_degradation_uses_its_own_notice_kind() -> None:
    env = build_test_env(
        outcomes={
            "hello": RetrievalOutcome(
                hits=(),
                degradations=(
                    {"code": "rerank_degraded", "provider": "none"},
                    {"code": "tree_degraded", "failed": "tree"},
                ),
            )
        },
    )
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    _ask_scoped(env, principal, conversation_id, "hello")
    env["runtime"].resolve("chat_generation_worker").run_once()

    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    kinds = {notice["kind"]: notice for notice in detail["messages"][1]["notices"]}
    assert "rerank_degraded" in kinds
    assert kinds["rerank_degraded"]["detail"]["code"] == "rerank_degraded"
    assert "retrieval_degraded" in kinds
    assert kinds["retrieval_degraded"]["detail"]["code"] == "tree_degraded"


def test_retry_rejected_when_scope_no_longer_accessible() -> None:
    from app.chat.models import ConversationScope
    from app.platform.errors import PlatformError

    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    scope = ConversationScope(space_ids=("space_gone",), document_ids=())
    failed_id = _failed_generation(env, principal, conversation_id, scope=scope)

    service = env["runtime"].resolve("chat_generation_service")
    try:
        service.retry(
            principal=principal,
            failed_generation_id=failed_id,
            idempotency_key="retry-1",
        )
    except PlatformError as error:
        assert error.code == "retry_scope_changed"
        assert error.status_code == 409
    else:
        raise AssertionError("retry must reject a scope the user can no longer see")
    # No retry message appeared: only the failed assistant message exists.
    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant_messages = [item for item in detail["messages"] if item["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["status"] == "failed"


def test_retry_rejected_when_concurrency_quota_exhausted() -> None:
    from app.chat.generation import GenerationService
    from app.platform.errors import PlatformError

    from .conftest import FakeCalibration, build_runtime_authorization

    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    env["runtime"].adapters["chat_generation_service"] = GenerationService(
        env["engine"],
        clock=env["clock"],
        authorization=build_runtime_authorization(env["identity"]),
        calibration=FakeCalibration(),
        max_running_per_user=1,
        sampler=lambda: 0.0,
    )
    service = env["runtime"].resolve("chat_generation_service")

    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    failed_id = _failed_generation(env, principal, conversation_id)
    # A second, still-running generation exhausts the per-user quota.
    service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="still running", effort_level="quick", scope=None),
        idempotency_key="ask-2",
    )
    try:
        service.retry(
            principal=principal,
            failed_generation_id=failed_id,
            idempotency_key="retry-1",
        )
    except PlatformError as error:
        assert error.code == "concurrency_limit_exceeded"
        assert error.status_code == 429
    else:
        raise AssertionError("retry must respect the running-generation quota")


def test_retry_rejected_when_retrieval_profile_superseded(monkeypatch) -> None:
    from app.platform.errors import PlatformError

    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    failed_id = _failed_generation(env, principal, conversation_id)
    # The pinned profile generation of the failed attempt is now superseded.
    monkeypatch.setattr("app.chat.generation.DEFAULT_RETRIEVAL_PROFILE_VERSION", "2")

    service = env["runtime"].resolve("chat_generation_service")
    try:
        service.retry(
            principal=principal,
            failed_generation_id=failed_id,
            idempotency_key="retry-1",
        )
    except PlatformError as error:
        assert error.code == "retrieval_profile_superseded"
        assert error.status_code == 409
    else:
        raise AssertionError("retry must reject a superseded retrieval profile")


def test_deadline_expiry_commits_failed_state_atomically() -> None:
    from app.chat.generation import GenerationService

    from .conftest import FakeCalibration, build_runtime_authorization

    env = build_test_env(outcomes={"hello": RetrievalOutcome(hits=())})
    # Every created generation is already past its absolute deadline.
    env["runtime"].adapters["chat_generation_service"] = GenerationService(
        env["engine"],
        clock=env["clock"],
        authorization=build_runtime_authorization(env["identity"]),
        calibration=FakeCalibration(),
        absolute_deadline_seconds=-1,
    )
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = _ask_scoped(env, principal, conversation_id, "hello")

    outcome = env["runtime"].resolve("chat_generation_worker").run_once()
    assert outcome.stage == "failed"
    assert outcome.details["code"] == "generation_deadline_exceeded"

    frames = _events(env, headers, result.generation_id)
    assert frames[-1][0] == "error"
    assert json.loads(frames[-1][2])["code"] == "generation_deadline_exceeded"
    assert sum(1 for frame in frames if frame[0] == "error") == 1
    detail = env["client"].get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = detail["messages"][1]
    assert assistant["status"] == "failed"
    assert assistant["content"] == ""
