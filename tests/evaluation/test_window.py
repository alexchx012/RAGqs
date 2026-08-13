"""Calibration window state machine, idempotency and read model (A29–A38)."""

from __future__ import annotations

from sqlalchemy import select

from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import calibration_window_table

from .conftest import NOW, build_test_env, provision_and_login


def _ops(env):
    return provision_and_login(env["identity"], "ops1", role="ops")


def _admin(env):
    return provision_and_login(env["identity"], "admin1", role="admin")


def _user(env):
    return provision_and_login(env["identity"], "user1", role="user")


def _post(env, token: str, *, body: dict, key: str):
    return env["client"].post(
        "/v1/calibration/window",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


def _get(env, token: str):
    return env["client"].get(
        "/v1/calibration/window",
        headers={"Authorization": f"Bearer {token}"},
    )


def _seed_policy(env) -> None:
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=default_policy_snapshot(now=NOW))


def test_no_window_returns_synthetic_closed() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    response = _get(env, token)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "window_id",
        "status",
        "window_kind",
        "policy_version",
        "sample_rate",
        "pairs_collected",
        "opened_at",
        "closed_at",
        "close_deadline_at",
        "opened_by",
        "closed_by",
    }
    assert body == {
        "window_id": None,
        "status": "closed",
        "window_kind": None,
        "policy_version": None,
        "sample_rate": 0,
        "pairs_collected": 0,
        "opened_at": None,
        "closed_at": None,
        "close_deadline_at": None,
        "opened_by": None,
        "closed_by": None,
    }


def test_manual_open_and_close_lifecycle() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    opened = _post(env, token, body={"action": "open", "window_kind": "manual"}, key="k1")
    assert opened.status_code == 201
    body = opened.json()
    assert body["status"] == "open"
    assert body["window_kind"] == "manual"
    assert body["pairs_collected"] == 0
    # Second open while open → 409.
    second = _post(env, token, body={"action": "open", "window_kind": "manual"}, key="k2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "calibration_window_already_open"
    # Close.
    closed = _post(env, token, body={"action": "close"}, key="k3")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closing"
    assert closed.json()["close_deadline_at"] is not None
    # No open window after close.
    with env["engine"].connect() as connection:
        rows = connection.execute(
            select(calibration_window_table).where(calibration_window_table.c.status == "open")
        ).all()
    assert rows == []
    assert _get(env, token).json()["status"] == "closing"


def test_close_without_open_returns_409() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    response = _post(env, token, body={"action": "close"}, key="k1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "calibration_window_not_open"


def test_window_idempotent_replay_and_conflict() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    first = _post(env, token, body={"action": "open", "window_kind": "manual"}, key="k1")
    assert first.status_code == 201
    replay = _post(env, token, body={"action": "open", "window_kind": "manual"}, key="k1")
    assert replay.status_code == 201
    assert replay.json() == first.json()
    conflict = _post(env, token, body={"action": "open", "window_kind": "cold_start"}, key="k1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


def test_window_extra_field_returns_422() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    response = _post(
        env,
        token,
        body={"action": "open", "window_kind": "manual", "policy_version": "evil"},
        key="k1",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_admin_cannot_write_window() -> None:
    env = build_test_env()
    _seed_policy(env)
    admin_token, _, _ = _admin(env)
    response = _post(
        env,
        admin_token,
        body={"action": "open", "window_kind": "manual"},
        key="k1",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "calibration_window_forbidden"


def test_user_cannot_write_or_read_window() -> None:
    env = build_test_env()
    _seed_policy(env)
    user_token, _, _ = _user(env)
    write = _post(env, user_token, body={"action": "open", "window_kind": "manual"}, key="k1")
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "calibration_window_forbidden"
    read = _get(env, user_token)
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "calibration_window_forbidden"


def test_open_without_window_kind_returns_422() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    response = _post(env, token, body={"action": "open"}, key="k1")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_cold_start_without_actionable_suggestion_returns_409() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, _ = _ops(env)
    response = _post(env, token, body={"action": "open", "window_kind": "cold_start"}, key="k1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "calibration_window_not_eligible"


def test_opened_and_closed_by_snapshot() -> None:
    env = build_test_env()
    _seed_policy(env)
    token, _, user_id = _ops(env)
    opened = _post(env, token, body={"action": "open", "window_kind": "manual"}, key="k1").json()
    assert opened["opened_by"] == user_id
    closed = _post(env, token, body={"action": "close"}, key="k2").json()
    assert closed["closed_by"] == user_id
