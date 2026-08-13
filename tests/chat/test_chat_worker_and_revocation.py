"""Worker state machine, revocation convergence and maintenance reaper tests."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select, update

from app.chat.models import AskRequest, RetrievalOutcome
from app.chat.schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_generation_event_table,
    chat_generation_execution_table,
    chat_generation_table,
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
