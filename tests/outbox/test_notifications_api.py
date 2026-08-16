"""V1 notifications API contract."""

from __future__ import annotations

from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_settings,
    provision_user,
)
from fastapi.testclient import TestClient

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.platform.app_factory import create_platform_app
from app.platform.runtime import build_runtime


def make_app(*, alice_ops: bool = False):
    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice", role="ops" if alice_ops else "user")
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
    login = identity.login(username="alice", password="Password1")
    return app, login.access_token, engine, alice, dispatcher


def deliver(engine, *, user_ids, event_id="evt_1", event_type="ingestion_completed"):
    from _helpers import make_publisher

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())

    payload = (
        {
            "job_id": f"job_{event_id}",
            "document_id": f"doc_{event_id}",
            "document_version_id": f"docv_{event_id}",
            "publication_id": f"pub_{event_id}",
        }
        if event_type in {"ingestion_completed", "ocr_low_confidence"}
        else {"submission_id": f"sub_{event_id}"}
    )
    command = OutboxPublishCommand(
        event_id=event_id,
        event_type=event_type,
        caller_principal="ingestion" if "ingestion" in event_type else "submissions",
        schema_version=1,
        aggregate_type="ingestion_job" if "ingestion" in event_type else "knowledge_submission",
        aggregate_id=f"job_{event_id}",
        transition_version=1,
        occurred_at=fixed_now(),
        payload=payload,
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def notification_id(engine, *, event_id, user_id) -> str:
    from sqlalchemy import select

    from app.outbox.schema import notification_table

    with engine.connect() as connection:
        return str(
            connection.execute(
                select(notification_table.c.id).where(
                    notification_table.c.event_id == event_id,
                    notification_table.c.recipient_user_id == user_id,
                )
            ).scalar_one()
        )


def test_notifications_list_returns_limited_unread_items() -> None:
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")

    with TestClient(app) as client:
        response = client.get(
            "/v1/notifications?limit=1",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items"}
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert set(item) == {"id", "type", "title", "payload", "read", "event_occurred_at"}
    assert item["payload"]["document_id"] == "doc_evt_2"
    assert item["read"] is False


def test_notifications_list_rejects_invalid_limit() -> None:
    app, token, _, _, _ = make_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/notifications?limit=0",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_notifications_unread_count_returns_a_count() -> None:
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_1")

    with TestClient(app) as client:
        response = client.get(
            "/v1/notifications/unread-count",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"count": 1}


def test_notifications_read_returns_204_and_is_repeatable() -> None:
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    nid = notification_id(engine, event_id="evt_1", user_id=alice)

    with TestClient(app) as client:
        first = client.post(
            f"/v1/notifications/{nid}/read",
            headers={"Authorization": f"Bearer {token}"},
        )
        second = client.post(
            f"/v1/notifications/{nid}/read",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first.status_code == 204
    assert second.status_code == 204


def test_notifications_read_rejects_foreign_and_missing_notifications() -> None:
    app, token, engine, alice, _ = make_app()
    identity = build_identity_service(engine)
    bob = provision_user(identity, username="bob")
    deliver(engine, user_ids=(bob,), event_id="evt_1")
    nid = notification_id(engine, event_id="evt_1", user_id=bob)

    with TestClient(app) as client:
        foreign = client.post(
            f"/v1/notifications/{nid}/read",
            headers={"Authorization": f"Bearer {token}"},
        )
        missing = client.post(
            "/v1/notifications/notification_missing/read",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert foreign.status_code == 404
    assert missing.status_code == 404


def test_notifications_read_all_returns_204_and_marks_watermark() -> None:
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_1")

    with TestClient(app) as client:
        response = client.post(
            "/v1/notifications/read-all",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204


def test_notifications_ack_returns_204_and_reads_the_notification() -> None:
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_1")

    with TestClient(app) as client:
        response = client.post(
            "/v1/notifications/events/evt_1/ack",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204


def test_notifications_ack_rejects_non_acknowledgeable_events() -> None:
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_sub", event_type="submission_approved")

    with TestClient(app) as client:
        response = client.post(
            "/v1/notifications/events/evt_sub/ack",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "notification_event_not_acknowledgeable"


def test_notifications_require_authentication() -> None:
    app, _, _, _, _ = make_app()

    with TestClient(app) as client:
        response = client.get("/v1/notifications")

    assert response.status_code == 401
