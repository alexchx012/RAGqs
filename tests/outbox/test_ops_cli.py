"""Protected outbox-delivery ops CLI tests (service-layer equivalence).

list/replay 与 /ops/outbox-deliveries HTTP 端点同语义、幂等键命名空间与 HTTP
公式一致、维护密钥 fail-closed、main 的退出码与 JSON 输出。
"""

from __future__ import annotations

import json

import pytest

import app.outbox.ops_cli as ops_cli_module
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.ops_cli import (
    _replay_request_hash,
    main,
    run_outbox_delivery_replay,
    run_outbox_delivery_view,
)
from app.platform.config import load_platform_settings
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)

_SETTINGS = load_platform_settings(
    {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_MAINTENANCE_KEY": "ops-key",
    }
)


def _settings_without_key():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )


def _fixture_runtime() -> tuple[PlatformRuntime, OutboxDispatcher]:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    command = OutboxPublishCommand(
        event_id="evt_cli_1",
        event_type="ingestion_completed",
        caller_principal="ingestion",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_1",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_1",
            "document_id": "doc_1",
            "document_version_id": "docv_1",
            "publication_id": "pub_1",
        },
        trace_id="trace_x",
        recipients=(RecipientSelection(recipient_user_id=alice),),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)
    materializer = NotificationMaterializer(engine, notification_retention_days=90)
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    outcome = dispatcher.fail_and_schedule(
        claim,
        owner="worker-1",
        error_category="permanent",
        error_code="unsupported_schema",
    )
    assert outcome.status == "dead_letter"
    runtime = PlatformRuntime(_SETTINGS, adapters={"outbox_dispatcher": dispatcher})
    return runtime, dispatcher


def test_view_reports_dead_letter_without_payload_or_recipients() -> None:
    runtime, _dispatcher = _fixture_runtime()
    try:
        view = run_outbox_delivery_view(_SETTINGS, runtime=runtime, event_id="evt_cli_1")
        assert view is not None
        assert view["status"] == "dead_letter"
        assert view["replayable"] is True
        assert view["error"] == {"category": "permanent", "code": "unsupported_schema"}
        assert "payload" not in view
        assert "recipients" not in view

        missing = run_outbox_delivery_view(_SETTINGS, runtime=runtime, event_id="evt_missing")
        assert missing is None
    finally:
        runtime.close()


def test_replay_resets_the_cycle_and_is_idempotent_per_key() -> None:
    runtime, dispatcher = _fixture_runtime()
    try:
        receipt = run_outbox_delivery_replay(
            _SETTINGS,
            runtime=runtime,
            event_id="evt_cli_1",
            expected_version=2,
            idempotency_key="cli-replay-1",
        )
        assert receipt == {
            "event_id": "evt_cli_1",
            "consumer_name": "in_app_notification",
            "status": "pending",
            "replay_generation": 2,
            "version": 3,
        }
        # The same key replays the original receipt, never a second cycle.
        replayed = run_outbox_delivery_replay(
            _SETTINGS,
            runtime=runtime,
            event_id="evt_cli_1",
            expected_version=2,
            idempotency_key="cli-replay-1",
        )
        assert replayed == receipt
        # A different key against the now-pending delivery conflicts.
        with pytest.raises(PlatformError) as error:
            run_outbox_delivery_replay(
                _SETTINGS,
                runtime=runtime,
                event_id="evt_cli_1",
                expected_version=2,
                idempotency_key="cli-replay-2",
            )
        assert error.value.code == "outbox_delivery_not_replayable"
    finally:
        runtime.close()


def test_cli_request_hash_matches_the_http_endpoint_formula() -> None:
    from app.api.v1.ops import DeliveryReplayRequest
    from app.api.v1.ops import _replay_request_hash as http_hash

    body = DeliveryReplayRequest(consumer_name="in_app_notification", expected_version=2)
    key = "shared-key"
    assert _replay_request_hash(
        consumer_name="in_app_notification", expected_version=2, idempotency_key=key
    ) == http_hash(body, key)


def test_operations_fail_closed_without_maintenance_key() -> None:
    runtime, _dispatcher = _fixture_runtime()
    try:
        settings = _settings_without_key()
        with pytest.raises(ValueError, match="RAG_MAINTENANCE_KEY"):
            run_outbox_delivery_view(settings, runtime=runtime, event_id="evt_cli_1")
        with pytest.raises(ValueError, match="RAG_MAINTENANCE_KEY"):
            run_outbox_delivery_replay(
                settings,
                runtime=runtime,
                event_id="evt_cli_1",
                expected_version=2,
                idempotency_key="k",
            )
    finally:
        runtime.close()


def test_main_list_prints_json_and_exit_zero(capsys, monkeypatch) -> None:
    runtime, _dispatcher = _fixture_runtime()
    monkeypatch.setattr(ops_cli_module, "load_platform_settings", lambda: _SETTINGS)
    monkeypatch.setattr(ops_cli_module, "build_runtime", lambda _settings: runtime)
    main(["list", "--event-id", "evt_cli_1"])
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "dead_letter"
    assert output["version"] == 2


def test_main_list_not_found_exits_one(capsys, monkeypatch) -> None:
    runtime, _dispatcher = _fixture_runtime()
    monkeypatch.setattr(ops_cli_module, "load_platform_settings", lambda: _SETTINGS)
    monkeypatch.setattr(ops_cli_module, "build_runtime", lambda _settings: runtime)
    with pytest.raises(SystemExit) as exit_info:
        main(["list", "--event-id", "evt_missing"])
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


def test_main_replay_conflict_exits_one_with_error_json(capsys, monkeypatch) -> None:
    runtime, _dispatcher = _fixture_runtime()
    monkeypatch.setattr(ops_cli_module, "load_platform_settings", lambda: _SETTINGS)
    monkeypatch.setattr(ops_cli_module, "build_runtime", lambda _settings: runtime)
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "replay",
                "--event-id",
                "evt_cli_1",
                "--expected-version",
                "99",
                "--idempotency-key",
                "stale-key",
            ]
        )
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"] == "version_conflict"


def test_main_without_maintenance_key_exits_two(capsys, monkeypatch) -> None:
    monkeypatch.setattr(ops_cli_module, "load_platform_settings", lambda: _settings_without_key())
    with pytest.raises(SystemExit) as exit_info:
        main(["list", "--event-id", "evt_cli_1"])
    assert exit_info.value.code == 2
    assert "RAG_MAINTENANCE_KEY" in capsys.readouterr().err


def test_main_rejects_invalid_arguments() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["list"])
    assert exit_info.value.code == 2
