"""Runtime wiring for evaluation adapters (default chat_calibration_port)."""

from __future__ import annotations

from app.evaluation.calibration_port import EvaluationCalibrationWindowPort

from .conftest import build_test_env


def test_runtime_wires_evaluation_adapters() -> None:
    env = build_test_env()
    runtime = env["runtime"]
    assert runtime.resolve("evaluation_repository") is not None
    assert runtime.resolve("evaluation_service") is not None
    assert runtime.resolve("judge_provider") is not None
    assert runtime.resolve("calibration_outbox_port") is not None
    assert runtime.resolve("evaluation_worker") is not None
    assert runtime.resolve("calibration_close_worker") is not None
    assert runtime.resolve("evaluation_usage_submission") is not None


def test_default_chat_calibration_port_is_evaluation_port() -> None:
    env = build_test_env()
    runtime = env["runtime"]
    port = runtime.resolve("chat_calibration_port")
    assert isinstance(port, EvaluationCalibrationWindowPort)


def test_table_missing_get_open_window_returns_none() -> None:
    env = build_test_env()
    # Build a separate engine with NO evaluation tables.
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.chat.schema import chat_metadata
    from app.identity.schema import identity_metadata
    from app.platform.database import core_metadata

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    port = EvaluationCalibrationWindowPort(engine)
    with engine.connect() as connection:
        assert port.get_open_window(connection, now=env["clock"].now, user_id="u1") is None
    engine.dispose()
