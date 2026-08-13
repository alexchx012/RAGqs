"""Calibration close worker: immediate and deadline close paths (A31)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.chat.ports import SqlAlchemyChatPairExpiry
from app.chat.schema import (
    chat_ab_pair_table,
    chat_conversation_table,
    chat_generation_table,
    chat_message_table,
)
from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import calibration_window_table
from app.evaluation.worker import CalibrationCloseWorker

from .conftest import NOW, build_test_env, provision_and_login


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def _ops(env):
    return provision_and_login(env["identity"], "ops1", role="ops")


def _post(env, token: str, *, body: dict, key: str):
    return env["client"].post(
        "/v1/calibration/window",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


def _seed_policy(env) -> None:
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=default_policy_snapshot(now=NOW))


def _insert_chat_pair(env, *, window_id: str, status: str) -> str:
    now = NOW
    suffix = f"{window_id}_{status}"
    conversation_id = f"conv_{suffix}"
    user_msg_id = f"umsg_{suffix}"
    msg_id = f"msg_{suffix}"
    gen_id = f"gen_{suffix}"
    pair_id = f"pair_{suffix}"
    with env["engine"].begin() as connection:
        connection.execute(
            chat_conversation_table.insert().values(
                id=conversation_id,
                owner_user_id="u1",
                title="t",
                pinned=False,
                effort_level="quick",
                scope_json={},
                last_active_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id=user_msg_id,
                conversation_id=conversation_id,
                owner_user_id="u1",
                role="user",
                content="q",
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id=msg_id,
                conversation_id=conversation_id,
                owner_user_id="u1",
                role="assistant",
                content="",
                created_at_utc=now,
            )
        )
        connection.execute(
            chat_generation_table.insert().values(
                id=gen_id,
                conversation_id=conversation_id,
                owner_user_id="u1",
                user_message_id=user_msg_id,
                message_id=msg_id,
                root_generation_id=gen_id,
                attempt_number=1,
                status="completed",
                requested_effort_level="quick",
                effective_effort_level="quick",
                retrieval_profile_id="p",
                retrieval_profile_version="v1",
                rag_budget_policy_version="b1",
                absolute_deadline_at_utc=now + timedelta(hours=1),
                auth_session_id="sess",
                control_version=1,
                request_content="q",
                request_scope_json={},
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            chat_ab_pair_table.insert().values(
                pair_id=pair_id,
                generation_id=gen_id,
                message_id=msg_id,
                window_id=window_id,
                owner_user_id="u1",
                status=status,
                voted=False,
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
    return pair_id


def _worker(env, clock: MutableClock) -> CalibrationCloseWorker:
    repository = env["runtime"].resolve("evaluation_repository")
    return CalibrationCloseWorker(
        env["engine"],
        repository,
        pair_expiry=SqlAlchemyChatPairExpiry(env["engine"]),
        now=clock,
    )


def _window_status(env) -> str:
    with env["engine"].connect() as connection:
        row = connection.execute(
            select(calibration_window_table.c.status).where(
                calibration_window_table.c.status.in_(("open", "closing"))
            )
        ).scalar_one_or_none()
    return "closed" if row is None else str(row)


def _pair_statuses(env) -> list[str]:
    with env["engine"].connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                select(chat_ab_pair_table.c.status).order_by(chat_ab_pair_table.c.pair_id)
            ).all()
        ]


def _open_and_close(env) -> tuple[str, str]:
    token, _, _ = _ops(env)
    opened = _post(env, token, body={"action": "open", "window_kind": "manual"}, key="k1")
    assert opened.status_code == 201
    window_id = opened.json()["window_id"]
    closed = _post(env, token, body={"action": "close"}, key="k2")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closing"
    return token, window_id


def test_close_worker_closes_immediately_when_no_pairs() -> None:
    env = build_test_env()
    _seed_policy(env)
    _open_and_close(env)
    clock = MutableClock()
    worker = _worker(env, clock)
    assert worker.run_once() == 1
    assert _window_status(env) == "closed"


def test_close_worker_waits_for_deadline_then_expires_pairs_and_closes() -> None:
    env = build_test_env()
    _seed_policy(env)
    _, window_id = _open_and_close(env)
    open_pair = _insert_chat_pair(env, window_id=window_id, status="open")
    pending_pair = _insert_chat_pair(env, window_id=window_id, status="pending")
    assert open_pair and pending_pair
    clock = MutableClock()
    worker = _worker(env, clock)
    # Before the deadline: pairs stay votable, window stays closing.
    assert worker.run_once() == 0
    assert _window_status(env) == "closing"
    assert _pair_statuses(env) == ["open", "pending"]
    # Past the deadline: pairs expire first, then the window closes.
    clock.now = NOW + timedelta(seconds=3601)
    assert worker.run_once() == 1
    assert _window_status(env) == "closed"
    assert _pair_statuses(env) == ["expired", "expired"]
