from __future__ import annotations

import hashlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.documents.schema import DOCUMENTS_TABLE_NAMES, documents_metadata
from app.outbox.schema import outbox_metadata


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_documents_metadata_owns_expected_tables() -> None:
    assert set(documents_metadata.tables) == set(DOCUMENTS_TABLE_NAMES)


def test_head_upgrade_creates_documents_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'documents.sqlite3'}"
    command.upgrade(_config(database_url), "head")

    engine = create_engine(database_url)
    try:
        assert DOCUMENTS_TABLE_NAMES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_replay_config_snapshot_migration_round_trips(tmp_path: Path) -> None:
    # documents_metadata.create_all 建的新库在 0012 就带全量列；0045 的补列
    # 只对旧版本升级库生效。这里用 downgrade 先摘除该列，再验证 0045 能为
    # 既有库补回可空列。
    database_url = f"sqlite:///{tmp_path / 'replay-config-snapshot.sqlite3'}"
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0044_ab_candidate_config_mapping")

    engine = create_engine(database_url)
    try:
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("ingestion_jobs")
        }
        assert "replay_config_snapshot_json" not in columns
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("ingestion_jobs")
        }
        assert columns["replay_config_snapshot_json"]["nullable"] is True
    finally:
        engine.dispose()


def test_submission_contract_migration_adds_nullable_columns_to_legacy_rows(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'submission-contract.sqlite3'}"
    config = _config(database_url)
    command.upgrade(config, "0041_merge_chat_sse_compact")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                    INSERT INTO knowledge_submissions (
                        id, space_id, submitter_user_id, version, status, file_name, media_kind,
                        content_hash_sha256, private_object_key, object_manifest_json,
                        created_at_utc, updated_at_utc
                    ) VALUES (
                        'submission_legacy', 'public', 'user_legacy', 1, 'pending', 'legacy.txt',
                        'text/plain', 'legacy-hash', 'submissions/legacy/original', '{}',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                    )
                    """))

        command.upgrade(config, "head")

        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("knowledge_submissions")
        }
        expected_columns = {
            "submitter_role_snapshot",
            "submitter_department_snapshot",
            "submitter_display_name_snapshot",
            "submitter_department_name_snapshot",
            "invalidated_reason",
            "invalidated_at",
        }
        assert expected_columns <= set(columns)
        assert all(columns[column_name]["nullable"] for column_name in expected_columns)
        with engine.connect() as connection:
            legacy = connection.execute(text("""
                    SELECT submitter_role_snapshot, submitter_department_snapshot,
                           submitter_display_name_snapshot, submitter_department_name_snapshot,
                           invalidated_reason, invalidated_at
                    FROM knowledge_submissions
                    WHERE id = 'submission_legacy'
                    """)).mappings().one()
        assert legacy == {
            "submitter_role_snapshot": None,
            "submitter_department_snapshot": None,
            "submitter_display_name_snapshot": None,
            "submitter_department_name_snapshot": None,
            "invalidated_reason": None,
            "invalidated_at": None,
        }
    finally:
        engine.dispose()


def test_documents_write_migration_hashes_legacy_keys_and_is_rerunnable() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    legacy = MetaData()
    documents = Table(
        "documents",
        legacy,
        Column("id", String(128), primary_key=True),
        Column("pending_version_id", String(128)),
        Column("active_version_id", String(128)),
    )
    versions = Table("document_versions", legacy, Column("id", String(128), primary_key=True))
    claims = Table(
        "upload_dedup_claims",
        legacy,
        Column("space_id", String(128), primary_key=True),
        Column("normalized_filename", String(512), primary_key=True),
        Column("content_hash_sha256", String(64), primary_key=True),
        Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
    )
    idempotency = Table(
        "documents_idempotency",
        legacy,
        Column("actor_id", String(64), primary_key=True),
        Column("endpoint", String(256), primary_key=True),
        Column("target_id", String(128), primary_key=True),
        Column("idempotency_key", String(256), primary_key=True),
        Column("request_fingerprint", String(128), nullable=False),
        Column("status", String(32), nullable=False),
        Column("response_json", JSON),
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
        Column("completed_at_utc", DateTime(timezone=True)),
    )
    legacy.create_all(engine)
    with engine.begin() as connection:
        connection.execute(versions.insert().values(id="version_1"))
        connection.execute(
            documents.insert().values(
                id="document_1",
                pending_version_id=None,
                active_version_id="version_1",
            )
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        connection.execute(
            claims.insert().values(
                space_id="space_1",
                normalized_filename="guide.txt",
                content_hash_sha256="hash",
                document_id="document_1",
                created_at_utc=now,
            )
        )
        connection.execute(
            idempotency.insert().values(
                actor_id="user_1",
                endpoint="documents.initial_upload",
                target_id="space_1",
                idempotency_key="legacy-secret",
                request_fingerprint="fingerprint",
                status="completed",
                response_json={"ok": True},
                created_at_utc=now,
                completed_at_utc=now,
            )
        )
        connection.execute(
            idempotency.insert().values(
                actor_id="user_1",
                endpoint="documents.initial_upload",
                target_id="space_1",
                idempotency_key="legacy-second-secret",
                request_fingerprint="fingerprint-2",
                status="completed",
                response_json={"ok": True, "second": True},
                created_at_utc=now,
                completed_at_utc=now,
            )
        )
        migration_path = Path("alembic/versions/0035_documents_write_idempotency.py")
        spec = importlib.util.spec_from_file_location("documents_write_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()

        idempotency_columns = {
            column["name"] for column in inspect(connection).get_columns("documents_idempotency")
        }
        claim_columns = {
            column["name"] for column in inspect(connection).get_columns("upload_dedup_claims")
        }
        idem_rows = connection.execute(text("SELECT * FROM documents_idempotency")).mappings().all()
        claim_row = connection.execute(text("SELECT * FROM upload_dedup_claims")).mappings().one()
        primary_key = inspect(connection).get_pk_constraint("documents_idempotency")[
            "constrained_columns"
        ]

    assert "idempotency_key" not in idempotency_columns
    assert "idempotency_key_hash" in idempotency_columns
    assert {row["idempotency_key_hash"] for row in idem_rows} == {
        hashlib.sha256(b"legacy-secret").hexdigest(),
        hashlib.sha256(b"legacy-second-secret").hexdigest(),
    }
    assert primary_key == ["actor_id", "endpoint", "target_id", "idempotency_key_hash"]
    assert "document_version_id" in claim_columns
    assert claim_row["document_version_id"] == "version_1"


_SECONDARY_INDEXES = (
    ("upload_batch_items", "ix_upload_batch_items_batch"),
    ("ingestion_jobs", "ix_ingestion_jobs_document"),
)


def _index_names(engine, table: str) -> set[str]:
    return {index["name"] for index in inspect(engine).get_indexes(table)}


def test_secondary_indexes_migration_round_trips_for_fresh_and_legacy_databases(
    tmp_path: Path,
) -> None:
    """0048：新装库的表由 create_all 随当前 schema.py 建成（索引已存在），迁移的
    inspect 守卫必须跳过；索引缺失的存量库升级时由迁移补建，downgrade 再对称摘除。"""
    database_url = f"sqlite:///{tmp_path / 'secondary-indexes.sqlite3'}"
    config = _config(database_url)

    # 新装库路径：守卫跳过（无守卫时 create_index 会因重复创建而失败）。
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    for table, index_name in _SECONDARY_INDEXES:
        assert index_name in _index_names(engine, table)
    engine.dispose()

    # downgrade 对称摘除。
    command.downgrade(config, "0047_drop_notification_seq_idx")
    engine = create_engine(database_url)
    for table, index_name in _SECONDARY_INDEXES:
        assert index_name not in _index_names(engine, table)
    engine.dispose()

    # 存量库路径：索引缺失时由迁移补建。
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    for table, index_name in _SECONDARY_INDEXES:
        assert index_name in _index_names(engine, table)
    engine.dispose()


def test_secondary_indexes_migration_is_rerunnable_in_both_directions() -> None:
    """直接以 Operations 上下文重跑 0048，四个守卫分支各到一次：带索引 upgrade
    跳过、缺索引 upgrade 补建、带索引 downgrade 摘除、缺索引 downgrade 跳过。"""
    migration_path = Path("alembic/versions/0048_doc_ingest_retire_idx.py")
    spec = importlib.util.spec_from_file_location("doc_ingest_retire_idx", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    # 0048 同时覆盖 documents 与 notification（outbox）两张域的表。
    documents_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                # create_all 库已带索引：upgrade 的守卫必须跳过。
                migration.upgrade()
                migration.upgrade()
                for table, index_name in _SECONDARY_INDEXES:
                    assert index_name in _index_names(connection, table)
                    connection.execute(text(f"DROP INDEX {index_name}"))
                # 缺索引（存量库形态）：upgrade 补建、downgrade 摘除、再跑跳过。
                migration.upgrade()
                migration.downgrade()
                migration.downgrade()
                for table, index_name in _SECONDARY_INDEXES:
                    assert index_name not in _index_names(connection, table)
                migration.upgrade()
                for table, index_name in _SECONDARY_INDEXES:
                    assert index_name in _index_names(connection, table)
    finally:
        engine.dispose()
