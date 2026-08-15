"""EvaluationCalibrationWindowPort against chat generation (A32)."""

from __future__ import annotations

from sqlalchemy import select, update

from app.evaluation.calibration_port import EvaluationCalibrationWindowPort
from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import calibration_window_table
from app.identity.schema import identity_user_table

from .conftest import NOW, build_test_env, provision_and_login


def _seed_window(env, *, status: str = "open") -> str:
    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=NOW)
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        window = repo.create_window(
            connection,
            window_id="window_1",
            status=status,
            window_kind="manual",
            policy_version=policy.policy_version,
            sample_rate=0.5,
            opened_by="ops_1",
            now=NOW,
        )
    assert window.window_id is not None
    return window.window_id


def test_get_open_window_returns_snapshot() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        snapshot = port.get_open_window(connection, now=NOW, user_id="u1")
    assert snapshot is not None
    assert snapshot.window_id == "window_1"
    assert snapshot.status == "open"
    assert snapshot.sample_rate == 0.5
    assert snapshot.window_kind == "manual"


def test_get_open_window_none_when_closed() -> None:
    env = build_test_env()
    _seed_window(env, status="closed")
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        snapshot = port.get_open_window(connection, now=NOW, user_id="u1")
    assert snapshot is None


def test_user_ab_opt_out_reads_preferences() -> None:
    env = build_test_env()
    token, _, user_id = provision_and_login(env["identity"], "u1", role="user")
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(preferences_json={"ab_opt_out": True})
        )
    with env["engine"].connect() as connection:
        assert port.user_ab_opt_out(connection, user_id=user_id) is True


def test_increment_pairs_collected_same_transaction_and_once() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.increment_pairs_collected(connection, "window_1")
        port.increment_pairs_collected(connection, "window_1")
    with env["engine"].connect() as connection:
        value = connection.execute(
            select(calibration_window_table.c.pairs_collected).where(
                calibration_window_table.c.window_id == "window_1"
            )
        ).scalar_one()
    assert value == 2


def test_increment_pairs_collected_updates_the_window_timestamp() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        before = connection.execute(
            select(calibration_window_table.c.updated_at_utc).where(
                calibration_window_table.c.window_id == "window_1"
            )
        ).scalar_one()
    with env["engine"].begin() as connection:
        port.increment_pairs_collected(connection, "window_1")
    with env["engine"].connect() as connection:
        after = connection.execute(
            select(calibration_window_table.c.updated_at_utc).where(
                calibration_window_table.c.window_id == "window_1"
            )
        ).scalar_one()
    assert after != before


def test_increment_unknown_window_is_silent() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.increment_pairs_collected(connection, "missing_window")


def test_chat_generation_calls_port_compatible() -> None:
    # The chat generation service calls get_open_window/user_ab_opt_out/
    # increment_pairs_collected exactly through this protocol shape.
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    assert callable(port.get_open_window)
    assert callable(port.user_ab_opt_out)
    assert callable(port.increment_pairs_collected)
    with env["engine"].connect() as connection:
        snapshot = port.get_open_window(connection, now=NOW, user_id="u1")
    assert snapshot is not None and snapshot.status == "open"
