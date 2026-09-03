"""V1 ops outbox delivery API contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.platform.app_factory import create_platform_app
from app.platform.runtime import build_runtime
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    make_settings,
    provision_user,
)


def make_app(*, role: str):
    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    actor = provision_user(identity, username="caller", role=role)
    materializer = NotificationMaterializer(engine, notification_retention_days=90)
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
    )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": identity},
    )
    runtime.adapters["outbox_dispatcher"] = dispatcher
    app = create_platform_app(configured, runtime=runtime)
    login = identity.login(username="caller", password="Password1")
    return app, login.access_token, engine, actor, dispatcher


def publish_and_dead_letter(engine, dispatcher, *, user_id):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
        event_id="evt_1",
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
        recipients=(RecipientSelection(recipient_user_id=user_id),),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.fail_and_schedule(
        claim, owner="worker-1", error_category="permanent", error_code="unsupported_schema"
    )


def test_ops_delivery_view_requires_ops_role() -> None:
    app, token, _, _, _ = make_app(role="user")

    with TestClient(app) as client:
        response = client.get(
            "/v1/ops/outbox-deliveries/evt_1",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403


def test_ops_delivery_view_reports_status_without_payload() -> None:
    app, token, engine, actor, dispatcher = make_app(role="ops")
    publish_and_dead_letter(engine, dispatcher, user_id=actor)

    with TestClient(app) as client:
        response = client.get(
            "/v1/ops/outbox-deliveries/evt_1",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dead_letter"
    assert body["replayable"] is True
    assert body["error"] == {"category": "permanent", "code": "unsupported_schema"}
    assert "payload" not in body
    assert "recipients" not in body


def test_ops_delivery_view_404_for_missing_event() -> None:
    app, token, _, _, _ = make_app(role="ops")

    with TestClient(app) as client:
        response = client.get(
            "/v1/ops/outbox-deliveries/evt_missing",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404


def test_ops_replay_returns_202_pending_cycle() -> None:
    app, token, engine, actor, dispatcher = make_app(role="ops")
    publish_and_dead_letter(engine, dispatcher, user_id=actor)

    with TestClient(app) as client:
        response = client.post(
            "/v1/ops/outbox-deliveries/evt_1/replay",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "replay-key-1",
            },
            json={"consumer_name": "in_app_notification", "expected_version": 2},
        )

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "event_id": "evt_1",
        "consumer_name": "in_app_notification",
        "status": "pending",
        "replay_generation": 2,
        "version": 3,
    }


def test_ops_replay_rejects_plain_users_and_missing_keys() -> None:
    app, token, _, _, _ = make_app(role="user")

    with TestClient(app) as client:
        forbidden = client.post(
            "/v1/ops/outbox-deliveries/evt_1/replay",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k"},
            json={"consumer_name": "in_app_notification", "expected_version": 1},
        )

    assert forbidden.status_code == 403

    app, token, engine, actor, dispatcher = make_app(role="ops")
    publish_and_dead_letter(engine, dispatcher, user_id=actor)
    with TestClient(app) as client:
        missing_key = client.post(
            "/v1/ops/outbox-deliveries/evt_1/replay",
            headers={"Authorization": f"Bearer {token}"},
            json={"consumer_name": "in_app_notification", "expected_version": 2},
        )

    assert missing_key.status_code == 422


def test_ops_replay_conflict_and_version_errors() -> None:
    app, token, engine, actor, dispatcher = make_app(role="ops")
    publish_and_dead_letter(engine, dispatcher, user_id=actor)

    with TestClient(app) as client:
        version_conflict = client.post(
            "/v1/ops/outbox-deliveries/evt_1/replay",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k2"},
            json={"consumer_name": "in_app_notification", "expected_version": 99},
        )
        not_replayable = client.post(
            "/v1/ops/outbox-deliveries/evt_missing/replay",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k3"},
            json={"consumer_name": "in_app_notification", "expected_version": 1},
        )

    assert version_conflict.status_code == 409
    assert version_conflict.json()["error"]["code"] == "version_conflict"
    assert not_replayable.status_code == 404
