from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.identity.cleanup import ObjectStoreAccountDeletionCleanupPort
from app.identity.ports import NoopAccountRetirementGateway
from app.identity.schema import identity_deletion_workflow_table, identity_metadata
from app.identity.worker import IdentityDeletionWorker
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata, platform_lease_table
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime
from app.platform.storage import MemoryObjectStore, StorageKeyError
from app.platform.worker import create_worker_runtime


def settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )


class ExpireDeletionFenceOnce:
    def __init__(self, object_store: MemoryObjectStore) -> None:
        self._delegate = ObjectStoreAccountDeletionCleanupPort(object_store)
        self._should_expire_fence = True

    def confirm_cleanup(self, command, *, connection):
        receipt = self._delegate.confirm_cleanup(command, connection=connection)
        if self._should_expire_fence:
            self._should_expire_fence = False
            connection.execute(
                update(platform_lease_table)
                .where(platform_lease_table.c.resource == f"identity-deletion:{command.user_id}")
                .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
            )
        return receipt


def test_default_runtime_worker_finalizes_due_deletions_and_cleans_avatar() -> None:
    configured = settings()
    object_store = MemoryObjectStore()
    runtime = build_runtime(
        configured,
        adapters={
            "object_store": object_store,
            "account_retirement_gateway": NoopAccountRetirementGateway(),
        },
    )
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = runtime.resolve("identity_access")
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    avatar_url = service.replace_avatar(
        user_id=user["id"], content=b"image", content_type="image/png"
    )["avatar_url"]
    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="delete-alice",
    )
    with engine.begin() as connection:
        connection.execute(
            update(identity_deletion_workflow_table)
            .where(identity_deletion_workflow_table.c.user_id == user["id"])
            .values(purge_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    stats = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert stats.completed == 1
    assert stats.deferred == 0
    with pytest.raises(StorageKeyError):
        object_store.get(avatar_url.removeprefix("object://"))
    with engine.connect() as connection:
        workflow = (
            connection.execute(
                identity_deletion_workflow_table.select().where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()
        )
    assert workflow["status"] == "completed"
    assert workflow["cleanup_reference"] is not None
    worker_runtime.close()
    runtime.close()


def test_deletion_worker_processes_pending_avatar_cleanup() -> None:
    configured = settings()
    object_store = MemoryObjectStore()
    runtime = build_runtime(
        configured,
        adapters={
            "object_store": object_store,
            "account_retirement_gateway": NoopAccountRetirementGateway(),
        },
    )
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = runtime.resolve("identity_access")
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    first_avatar = service.replace_avatar(
        user_id=user["id"], content=b"first-image", content_type="image/png"
    )["avatar_url"]
    second_avatar = service.replace_avatar(
        user_id=user["id"], content=b"second-image", content_type="image/png"
    )["avatar_url"]

    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    stats = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert stats.completed == 1
    assert stats.deferred == 0
    with pytest.raises(StorageKeyError):
        object_store.get(first_avatar.removeprefix("object://"))
    assert object_store.get(second_avatar.removeprefix("object://"))[0] == b"second-image"
    worker_runtime.close()
    runtime.close()


def test_account_finalization_waits_for_replaced_avatar_cleanup() -> None:
    class FailOldAvatarDeleteStore(MemoryObjectStore):
        blocked_key: str | None = None

        def delete(self, key: str) -> None:
            if key == self.blocked_key:
                raise StorageKeyError(key)
            super().delete(key)

    configured = settings()
    object_store = FailOldAvatarDeleteStore()
    runtime = build_runtime(
        configured,
        adapters={
            "object_store": object_store,
            "account_retirement_gateway": NoopAccountRetirementGateway(),
        },
    )
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = runtime.resolve("identity_access")
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    first_avatar = service.replace_avatar(
        user_id=user["id"], content=b"first-image", content_type="image/png"
    )["avatar_url"]
    object_store.blocked_key = first_avatar.removeprefix("object://")
    service.replace_avatar(user_id=user["id"], content=b"second-image", content_type="image/png")
    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="delete-alice-with-old-avatar",
    )
    with engine.begin() as connection:
        connection.execute(
            update(identity_deletion_workflow_table)
            .where(identity_deletion_workflow_table.c.user_id == user["id"])
            .values(purge_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    with pytest.raises(PlatformError) as exc_info:
        service.finalize_pending_deletion(user_id=user["id"])

    assert exc_info.value.code == "avatar_cleanup_unavailable"
    with engine.connect() as connection:
        workflow = (
            connection.execute(
                identity_deletion_workflow_table.select().where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()
        )
    assert workflow["status"] == "pending"
    runtime.close()


def test_deletion_worker_retries_cleanup_after_a_fenced_transaction_rolls_back() -> None:
    configured = settings()
    object_store = MemoryObjectStore()
    cleanup_port = ExpireDeletionFenceOnce(object_store)
    runtime = build_runtime(
        configured,
        adapters={
            "object_store": object_store,
            "account_deletion_cleanup_port": cleanup_port,
            "account_retirement_gateway": NoopAccountRetirementGateway(),
        },
    )
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = runtime.resolve("identity_access")
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    avatar_url = service.replace_avatar(
        user_id=user["id"], content=b"image", content_type="image/png"
    )["avatar_url"]
    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="delete-alice",
    )
    with engine.begin() as connection:
        connection.execute(
            update(identity_deletion_workflow_table)
            .where(identity_deletion_workflow_table.c.user_id == user["id"])
            .values(purge_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    first = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert first.completed == 0
    assert first.deferred == 1
    with pytest.raises(StorageKeyError):
        object_store.get(avatar_url.removeprefix("object://"))
    with engine.connect() as connection:
        assert (
            connection.execute(
                identity_deletion_workflow_table.select().where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()["status"]
            == "pending"
        )
    with engine.begin() as connection:
        connection.execute(
            update(platform_lease_table)
            .where(platform_lease_table.c.resource == f"identity-deletion:{user['id']}")
            .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    second = IdentityDeletionWorker(worker_runtime).run_once(owner="worker-1")

    assert second.completed == 1
    assert second.deferred == 0
    with engine.connect() as connection:
        assert (
            connection.execute(
                identity_deletion_workflow_table.select().where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()["status"]
            == "completed"
        )
    worker_runtime.close()
    runtime.close()
