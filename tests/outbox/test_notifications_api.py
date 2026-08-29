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
from sqlalchemy import func, select

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_table,
)
from app.outbox.service import NotificationService
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

    if event_type in {"ingestion_completed", "ocr_low_confidence"}:
        payload = {
            "job_id": f"job_{event_id}",
            "document_id": f"doc_{event_id}",
            "document_version_id": f"docv_{event_id}",
            "publication_id": f"pub_{event_id}",
        }
    elif event_type == "submission_approved":
        payload = {
            "submission_id": f"sub_{event_id}",
            "document_id": f"doc_{event_id}",
            "job_id": f"job_{event_id}",
        }
    else:
        payload = {"submission_id": f"sub_{event_id}", "reason": "machine_reason"}
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


def _inbox_row(connection, user_id):
    return (
        connection.execute(
            select(notification_inbox_table).where(
                notification_inbox_table.c.recipient_user_id == user_id
            )
        )
        .mappings()
        .one()
    )


def test_provisioned_account_owns_an_inbox_row() -> None:
    """C5-5/#80：账号创建事务提交后 inbox 行存在（next_seq=1、read_through=0）。"""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")

    with engine.connect() as connection:
        inbox = _inbox_row(connection, alice)
    assert inbox["next_notification_seq"] == 1
    assert inbox["read_through_seq"] == 0
    assert inbox["version"] == 1


def test_materialization_enforces_the_50_cap_inside_the_transaction() -> None:
    """C5-2/#99：物化第 51 条后，排名 >50 的通知在物化事务内被退休并写收据，
    未读计数受 50 约束；多接收者各自独立处理。"""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    bob = provision_user(identity, username="bob")
    for index in range(1, 52):
        deliver(engine, user_ids=(alice, bob), event_id=f"evt_{index}")
    service = NotificationService(engine, now=lambda: fixed_now())

    assert service.unread_count(alice) == 50
    with engine.connect() as connection:
        online = dict(
            connection.execute(
                select(
                    notification_table.c.recipient_user_id,
                    func.count(),
                )
                .where(notification_table.c.recipient_user_id.in_([alice, bob]))
                .group_by(notification_table.c.recipient_user_id)
            ).all()
        )
        retired_seqs = connection.execute(
            select(
                notification_delivery_receipt_table.c.recipient_user_id,
                notification_delivery_receipt_table.c.original_notification_seq,
            ).where(
                notification_delivery_receipt_table.c.outcome == "materialized",
                notification_delivery_receipt_table.c.recipient_user_id.in_([alice, bob]),
            )
        ).all()
    assert online == {alice: 50, bob: 50}
    assert sorted(retired_seqs) == sorted([(alice, 1), (bob, 1)])


def test_read_all_watermark_uses_next_seq_basis_and_repeat_is_a_zero_write() -> None:
    """C5-3/#88：水位取 next_notification_seq - 1；无新通知的重复 read-all
    逐字段零写入且仍 204。"""
    app, token, engine, alice, _ = make_app()
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        assert client.post("/v1/notifications/read-all", headers=headers).status_code == 204
        with engine.connect() as connection:
            after_first = _inbox_row(connection, alice)
        assert client.post("/v1/notifications/read-all", headers=headers).status_code == 204
        with engine.connect() as connection:
            after_second = _inbox_row(connection, alice)

    assert after_first["read_through_seq"] == 2
    assert after_second["read_through_seq"] == after_first["read_through_seq"]
    assert after_second["read_all_at_utc"] == after_first["read_all_at_utc"]
    assert after_second["version"] == after_first["version"]
