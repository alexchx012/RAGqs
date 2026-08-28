from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from app.chat.schema import chat_conversation_table, chat_metadata
from app.documents.schema import document_versions_table, documents_metadata, documents_table
from app.identity.archive_package import _hash_file
from app.identity.ports import NoopAccountRetirementGateway
from app.identity.schema import (
    identity_account_cleanup_target_table,
    identity_deletion_workflow_table,
    identity_metadata,
    identity_user_table,
)
from app.outbox.schema import outbox_metadata
from app.platform.config import load_platform_settings, resolve_user_deletion_archive_dir
from app.platform.database import core_metadata, platform_audit_table
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime
from app.platform.storage import MemoryObjectStore

_BASE_ENV = {
    "RAG_PLATFORM_PROFILE": "development",
    "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
    "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
    "RAG_PROVIDER_NAME": "fake",
}


def _settings(extra: dict[str, str] | None = None):
    env = dict(_BASE_ENV)
    env.update(extra or {})
    return load_platform_settings(env)


def _build(tmp_path, extra_env: dict[str, str] | None = None):
    object_store = MemoryObjectStore()
    runtime = build_runtime(
        _settings(extra_env),
        adapters={
            "object_store": object_store,
            "account_retirement_gateway": NoopAccountRetirementGateway(),
        },
    )
    engine = runtime.resolve("database_engine")
    for metadata in (
        core_metadata,
        identity_metadata,
        chat_metadata,
        documents_metadata,
        outbox_metadata,
    ):
        metadata.create_all(engine)
    return runtime, engine, object_store


def _admin_and_target(service):
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
    target = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    return admin, target


def _expire_retention(engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_deletion_workflow_table)
            .where(identity_deletion_workflow_table.c.user_id == user_id)
            .values(purge_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )


def _workflow(engine, user_id: str):
    with engine.connect() as connection:
        return (
            connection.execute(
                identity_deletion_workflow_table.select().where(
                    identity_deletion_workflow_table.c.user_id == user_id
                )
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------- A1 config


def test_archive_dir_config_rules(tmp_path) -> None:
    dev = _settings()
    # development fallback: local data directory
    assert resolve_user_deletion_archive_dir(dev).endswith("user-deletion-archives")
    configured = _settings({"USER_DELETION_ARCHIVE_DIR": str(tmp_path)})
    assert resolve_user_deletion_archive_dir(configured) == str(tmp_path)
    forbidden = _settings({"USER_DELETION_ARCHIVE_DIR": str(tmp_path / "static" / "archives")})
    with pytest.raises(ValueError):
        resolve_user_deletion_archive_dir(forbidden)
    from app.platform.config import AuthSettings, _resolve_user_deletion_archive_dir

    auth = AuthSettings()
    with pytest.raises(ValueError):
        _resolve_user_deletion_archive_dir(auth, "production")
    forbidden_auth = AuthSettings(user_deletion_archive_dir="uploads/archives")
    with pytest.raises(ValueError):
        _resolve_user_deletion_archive_dir(forbidden_auth, "production")


def test_archive_dir_relative_paths_are_rejected(tmp_path) -> None:
    # A3/A16: 相对路径（含 ~ 展开形态）在解析层直接拒绝，生产预检因此拒绝启动。
    from app.platform.config import AuthSettings, _resolve_user_deletion_archive_dir

    for bad in ("archives", "./archives", "../archives", "~/ragqs-archives"):
        auth = AuthSettings(user_deletion_archive_dir=bad)
        with pytest.raises(ValueError, match="absolute path"):
            _resolve_user_deletion_archive_dir(auth, "production")
        with pytest.raises(ValueError, match="absolute path"):
            _resolve_user_deletion_archive_dir(auth, "development")
    # 绝对路径不受影响。
    ok = AuthSettings(user_deletion_archive_dir=str(tmp_path / "archives"))
    assert _resolve_user_deletion_archive_dir(ok, "production") == str(tmp_path / "archives")


def test_deletion_transaction_snapshots_archive_dir(tmp_path) -> None:
    archive_dir = tmp_path / "archives"
    runtime, engine, _ = _build(tmp_path, {"USER_DELETION_ARCHIVE_DIR": str(archive_dir)})
    service = runtime.resolve("identity_access")
    admin, target = _admin_and_target(service)
    response = service.delete_managed_user(
        actor=admin, user_id=target["id"], expected_version=1, idempotency_key="k"
    )
    workflow = _workflow(engine, target["id"])
    assert workflow["archive_dir_snapshot"] == str(archive_dir)
    # the 202 response never leaks the archive location
    assert "archive" not in json.dumps(response)
    runtime.close()


# ------------------------------------------------------------- A2/A3/A9/A10


def test_full_deletion_archive_and_tombstone_flow(tmp_path) -> None:
    archive_dir = tmp_path / "archives"
    runtime, engine, object_store = _build(
        tmp_path, {"USER_DELETION_ARCHIVE_DIR": str(archive_dir)}
    )
    service = runtime.resolve("identity_access")
    admin, target = _admin_and_target(service)
    user_id = target["id"]

    # personal space + one document + chat data owned by the target
    # provision_user already created the personal space
    with engine.begin() as connection:
        connection.execute(
            insert(documents_table).values(
                id="doc-1",
                space_id=f"personal:{user_id}",
                lifecycle_status="deleted",  # already a tombstone: sub-workflow count is 0
                version=1,
                name="doc",
                uploaded_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                created_by_user_id=user_id,
            )
        )
        connection.execute(
            insert(chat_conversation_table).values(
                id="conv-1",
                owner_user_id=user_id,
                title="hello",
                pinned=False,
                effort_level="quick",
                scope_json={},
                last_active_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    service.replace_avatar(user_id=user_id, content=b"avatar", content_type="image/png")
    service.delete_managed_user(
        actor=admin, user_id=user_id, expected_version=1, idempotency_key="k"
    )

    result = service.build_deletion_archive(user_id=user_id)
    assert result["archive_status"] == "archived"
    workflow = _workflow(engine, user_id)
    deletion_id = workflow["cleanup_operation_id"]
    package = archive_dir / f"{user_id}-{deletion_id}.zip"
    assert package.exists()
    assert workflow["archive_sha256"] is not None
    assert workflow["archive_size_bytes"] == package.stat().st_size

    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        profile = json.loads(archive.read("profile.json"))[0]
    assert manifest["format_version"] == "account-archive/v1"
    assert manifest["user_id"] == user_id
    assert manifest["entity_counts"]["conversations"] == 1
    assert all("sha256" in entry and "size" in entry for entry in manifest["files"])
    assert "objects/avatar" in names
    assert "conversations.json" in names
    # excluded content: no credentials ever enter the package
    assert "password_hash" not in profile
    assert "token" not in " ".join(names)

    _expire_retention(engine, user_id)
    avatar_key = None
    with engine.connect() as connection:
        avatar_key = (
            connection.execute(
                select(identity_user_table.c.avatar_url).where(identity_user_table.c.id == user_id)
            ).scalar_one()
        ).removeprefix("object://")
    finalized = service.finalize_pending_deletion(user_id=user_id)
    assert finalized["lifecycle_status"] == "deleted"
    # object_store.avatar target removed the current avatar object (A29)
    assert not object_store.exists(avatar_key)

    with engine.connect() as connection:
        user = (
            connection.execute(
                identity_user_table.select().where(identity_user_table.c.id == user_id)
            )
            .mappings()
            .one()
        )
        assert user["lifecycle_status"] == "deleted"
        targets = (
            connection.execute(
                identity_account_cleanup_target_table.select().where(
                    identity_account_cleanup_target_table.c.deletion_id == deletion_id
                )
            )
            .mappings()
            .all()
        )
        backend_kinds = {row["backend_kind"] for row in targets}
        assert "postgres.chat_conversations" in backend_kinds
        assert "postgres.identity_spaces" in backend_kinds
        assert "object_store.avatar" in backend_kinds
        assert all(row["status"] == "completed" for row in targets)
        assert True
        audit_results = set(
            connection.execute(
                select(platform_audit_table.c.result).where(
                    platform_audit_table.c.resource_id == user_id
                )
            ).scalars()
        )
    # A10 audit events (accept, session revoke, archive, cleanup, deleted)
    assert {
        "user_pending_delete",
        "user_sessions_revoked",
        "user_archive_completed",
        "user_cleanup_completed",
        "user_deleted",
    } <= audit_results
    # A6: re-running finalize is idempotent
    assert service.finalize_pending_deletion(user_id=user_id)["lifecycle_status"] == "deleted"
    runtime.close()


# ------------------------------------------------------------------- A4/A5


def test_archive_missing_before_cleanup_is_rebuilt(tmp_path) -> None:
    archive_dir = tmp_path / "archives"
    runtime, engine, _ = _build(tmp_path, {"USER_DELETION_ARCHIVE_DIR": str(archive_dir)})
    service = runtime.resolve("identity_access")
    admin, target = _admin_and_target(service)
    user_id = target["id"]
    service.delete_managed_user(
        actor=admin, user_id=user_id, expected_version=1, idempotency_key="k"
    )
    service.build_deletion_archive(user_id=user_id)
    workflow = _workflow(engine, user_id)
    package = archive_dir / str(workflow["archive_file_name"])
    # corrupt the package before any destructive cleanup: rebuild from frozen data
    package.write_bytes(b"corrupted")
    _expire_retention(engine, user_id)
    result = service.finalize_pending_deletion(user_id=user_id)
    assert result["lifecycle_status"] == "deleted"
    rebuilt = _workflow(engine, user_id)
    # DB record must describe the package that is actually on disk after the rebuild
    assert rebuilt["archive_sha256"] == _hash_file(str(package))[1]
    assert rebuilt["archive_size_bytes"] == package.stat().st_size
    runtime.close()


def test_archive_rebuild_with_shifted_clock_still_finalizes(tmp_path) -> None:
    """A10 回归：重建与初始构建的 manifest 时间戳不同（非同秒巧合）仍能 finalize。

    生产 PostgreSQL 的时钟带微秒精度；修复前 finalize 的最终校验沿用重建前的
    旧 SHA，与磁盘上重建出的新包必然失配，造成删除 finalize 死循环。
    """

    archive_dir = tmp_path / "archives"
    runtime, engine, _ = _build(tmp_path, {"USER_DELETION_ARCHIVE_DIR": str(archive_dir)})
    service = runtime.resolve("identity_access")
    admin, target = _admin_and_target(service)
    user_id = target["id"]
    service.delete_managed_user(
        actor=admin, user_id=user_id, expected_version=1, idempotency_key="k"
    )
    service.build_deletion_archive(user_id=user_id)
    workflow = _workflow(engine, user_id)
    original_sha = workflow["archive_sha256"]
    package = archive_dir / str(workflow["archive_file_name"])
    # force the rebuild to stamp a strictly later manifest timestamp
    shifted = datetime.now(UTC) + timedelta(hours=1)
    service._now = lambda: shifted
    package.write_bytes(b"corrupted")
    _expire_retention(engine, user_id)
    result = service.finalize_pending_deletion(user_id=user_id)
    assert result["lifecycle_status"] == "deleted"
    rebuilt = _workflow(engine, user_id)
    disk_sha = _hash_file(str(package))[1]
    assert disk_sha != original_sha  # manifest timestamp changed the package bytes
    assert rebuilt["archive_sha256"] == disk_sha
    runtime.close()


def test_archive_missing_after_cleanup_stops_and_alerts(tmp_path) -> None:
    archive_dir = tmp_path / "archives"
    runtime, engine, _ = _build(tmp_path, {"USER_DELETION_ARCHIVE_DIR": str(archive_dir)})
    service = runtime.resolve("identity_access")
    admin, target = _admin_and_target(service)
    user_id = target["id"]
    service.delete_managed_user(
        actor=admin, user_id=user_id, expected_version=1, idempotency_key="k"
    )
    service.build_deletion_archive(user_id=user_id)
    workflow = _workflow(engine, user_id)
    deletion_id = workflow["cleanup_operation_id"]
    # one cleanup target already completed
    with engine.begin() as connection:
        connection.execute(
            insert(identity_account_cleanup_target_table).values(
                deletion_id=deletion_id,
                backend_kind="postgres.chat_conversations",
                resource_id=user_id,
                status="completed",
                attempts=1,
                created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    (archive_dir / str(workflow["archive_file_name"])).unlink()
    _expire_retention(engine, user_id)
    with pytest.raises(PlatformError) as exc_info:
        service.finalize_pending_deletion(user_id=user_id)
    assert exc_info.value.code == "account_archive_restore_required"
    alerted = _workflow(engine, user_id)
    assert alerted["archive_alert"] == "archive_restore_required"
    runtime.close()


# --------------------------------------------------------------------- A7


def test_personal_document_subworkflow_delegation(tmp_path) -> None:
    runtime, engine, _ = _build(tmp_path)
    service = runtime.resolve("identity_access")
    documents_service = runtime.resolve("documents_service")
    admin, target = _admin_and_target(service)
    user_id = target["id"]
    # provision_user already created the personal space
    with engine.begin() as connection:
        connection.execute(
            insert(document_versions_table).values(
                id="dv-1",
                document_id="doc-active",
                version_number=1,
                status="active",
                object_manifest_json={},
                size_bytes=0,
                created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        connection.execute(
            insert(documents_table).values(
                id="doc-active",
                active_version_id="dv-1",
                space_id=f"personal:{user_id}",
                lifecycle_status="active",
                version=1,
                name="doc",
                uploaded_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                created_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                created_by_user_id=user_id,
            )
        )
    service.delete_managed_user(
        actor=admin, user_id=user_id, expected_version=1, idempotency_key="k"
    )
    service.build_deletion_archive(user_id=user_id)
    deletion_id = _workflow(engine, user_id)["cleanup_operation_id"]
    with engine.begin() as connection:
        pending = documents_service.delete_personal_documents_for_account(
            connection, user_id=user_id, user_deletion_id=deletion_id
        )
    assert pending == 1  # document entered pending_delete, not yet a tombstone
    with engine.connect() as connection:
        doc = (
            connection.execute(documents_table.select().where(documents_table.c.id == "doc-active"))
            .mappings()
            .one()
        )
    assert doc["lifecycle_status"] == "pending_delete"
    # idempotent reuse: second delegation does not fail or duplicate
    with engine.begin() as connection:
        assert (
            documents_service.delete_personal_documents_for_account(
                connection, user_id=user_id, user_deletion_id=deletion_id
            )
            == 1
        )
    # finalize must refuse the tombstone while the sub-workflow is unfinished
    _expire_retention(engine, user_id)
    with pytest.raises(PlatformError) as exc_info:
        service.finalize_pending_deletion(user_id=user_id)
    assert exc_info.value.code == "deletion_documents_pending"
    runtime.close()
