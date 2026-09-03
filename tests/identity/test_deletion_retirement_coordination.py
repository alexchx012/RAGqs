"""Blocker 2: identity account deletion coordinates with the outbox retirement.

The default deletion flow must verify the identity-owned archive proof and
obtain a completed outbox retirement receipt before an account becomes
`deleted`. A failed or unconfirmed retirement keeps the account pending_delete.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata
from app.identity.schema import (
    identity_deletion_workflow_table,
    identity_metadata,
    identity_user_table,
)
from app.identity.worker import IdentityDeletionWorker
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_table,
    outbox_metadata,
)
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.platform.storage import MemoryObjectStore
from app.platform.worker import create_worker_runtime
from tests._support import build_engine, fixed_now, make_publisher, make_settings


def settings():
    return make_settings()


def build_runtime_with_outbox(**adapter_overrides):
    configured = settings()
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    adapters = {"database_engine": engine, "object_store": MemoryObjectStore()}
    adapters.update(adapter_overrides)
    runtime = build_runtime(configured, adapters=adapters)
    return runtime, engine


def deliver_notification(engine, *, user_id: str) -> None:
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id="evt_del_1",
                caller_principal="ingestion",
                event_type="ingestion_completed",
                schema_version=1,
                aggregate_type="ingestion_job",
                aggregate_id="job_del_1",
                transition_version=1,
                occurred_at=fixed_now(),
                payload={
                    "job_id": "job_del_1",
                    "document_id": "doc_del_1",
                    "document_version_id": "docv_del_1",
                    "publication_id": "pub_del_1",
                },
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=user_id),),
            ),
            connection=connection,
        )
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-delivery")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-delivery")


def setup_deletion(runtime, engine, *, user_id: str) -> None:
    service = runtime.resolve("identity_access")
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    service.delete_managed_user(
        actor=admin,
        user_id=user_id,
        expected_version=1,
        idempotency_key=f"delete-{user_id}",
    )
    with engine.begin() as connection:
        connection.execute(
            update(identity_deletion_workflow_table)
            .where(identity_deletion_workflow_table.c.user_id == user_id)
            .values(purge_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    # §9.2.1: the physical archive package must exist before finalization.
    runtime.resolve("identity_access").build_deletion_archive(user_id=user_id)


def test_deletion_worker_obtains_completed_retirement_before_deleting_account() -> None:
    runtime, engine = build_runtime_with_outbox()
    service = runtime.resolve("identity_access")
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    deliver_notification(engine, user_id=user["id"])
    setup_deletion(runtime, engine, user_id=user["id"])

    worker_runtime = create_worker_runtime(settings(), runtime=runtime)
    stats = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert stats.completed == 1
    assert stats.deferred == 0
    with engine.connect() as connection:
        user_row = connection.execute(
            select(identity_user_table.c.lifecycle_status).where(
                identity_user_table.c.id == user["id"]
            )
        ).scalar_one()
        assert user_row == "deleted"
        # The notification was retired into a permanent receipt.
        assert connection.execute(select(notification_table)).all() == []
        receipts = connection.execute(
            select(notification_delivery_receipt_table).where(
                notification_delivery_receipt_table.c.recipient_user_id == user["id"]
            )
        ).all()
        assert len(receipts) == 1
        workflow = (
            connection.execute(
                select(identity_deletion_workflow_table).where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()
        )
        assert workflow["status"] == "completed"
        assert workflow["archive_ref"] is not None
        assert workflow["archive_checksum"] is not None
    worker_runtime.close()
    runtime.close()


def test_deletion_is_deferred_when_the_retirement_receipt_is_not_completed() -> None:
    class RejectingVerifier:
        def verify_archive(self, *, archive_ref: str, checksum: str) -> bool:
            del archive_ref, checksum
            return False

    runtime, engine = build_runtime_with_outbox(archive_verifier=RejectingVerifier())
    service = runtime.resolve("identity_access")
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    deliver_notification(engine, user_id=user["id"])
    setup_deletion(runtime, engine, user_id=user["id"])

    worker_runtime = create_worker_runtime(settings(), runtime=runtime)
    stats = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert stats.completed == 0
    assert stats.deferred == 1
    with engine.connect() as connection:
        user_row = connection.execute(
            select(identity_user_table.c.lifecycle_status).where(
                identity_user_table.c.id == user["id"]
            )
        ).scalar_one()
        # The account must NOT be deleted while retirement is unconfirmed.
        assert user_row == "pending_delete"
        workflow = (
            connection.execute(
                select(identity_deletion_workflow_table).where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()
        )
        assert workflow["status"] == "pending"
    worker_runtime.close()
    runtime.close()


def test_deletion_retries_after_transient_retirement_failure() -> None:
    class FlakyGateway:
        def __init__(self, delegate) -> None:
            self._delegate = delegate
            self._failed = False

        def retire(self, request, *, connection):
            if not self._failed:
                self._failed = True
                raise RuntimeError("transient retirement failure")
            return self._delegate.retire(request, connection=connection)

    runtime, engine = build_runtime_with_outbox()
    gateway = FlakyGateway(runtime.resolve("account_retirement_gateway"))
    runtime.adapters["account_retirement_gateway"] = gateway
    service = runtime.resolve("identity_access")
    service._account_retirement_gateway = gateway
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    setup_deletion(runtime, engine, user_id=user["id"])

    worker_runtime = create_worker_runtime(settings(), runtime=runtime)
    first = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert first.completed == 0
    assert first.deferred == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(identity_user_table.c.lifecycle_status).where(
                    identity_user_table.c.id == user["id"]
                )
            ).scalar_one()
            == "pending_delete"
        )
    # The same worker loop retries and completes (the first attempt held the
    # task lease; expire it so the retry can re-acquire).
    from app.platform.database import platform_lease_table

    with engine.begin() as connection:
        connection.execute(
            update(platform_lease_table)
            .where(platform_lease_table.c.resource == f"identity-deletion:{user['id']}")
            .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    second = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")
    assert second.completed == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(identity_user_table.c.lifecycle_status).where(
                    identity_user_table.c.id == user["id"]
                )
            ).scalar_one()
            == "deleted"
        )
    worker_runtime.close()
    runtime.close()
