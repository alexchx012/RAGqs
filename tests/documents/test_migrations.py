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


def test_submission_contract_migration_adds_nullable_columns_to_legacy_rows(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'submission-contract.sqlite3'}"
    config = _config(database_url)
    command.upgrade(config, "0039_merge_chat_release_gate")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge_submissions (
                        id, space_id, submitter_user_id, version, status, file_name, media_kind,
                        content_hash_sha256, private_object_key, object_manifest_json,
                        created_at_utc, updated_at_utc
                    ) VALUES (
                        'submission_legacy', 'public', 'user_legacy', 1, 'pending', 'legacy.txt',
                        'text/plain', 'legacy-hash', 'submissions/legacy/original', '{}',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                    )
                    """
                )
            )

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
            legacy = connection.execute(
                text(
                    """
                    SELECT submitter_role_snapshot, submitter_department_snapshot,
                           submitter_display_name_snapshot, submitter_department_name_snapshot,
                           invalidated_reason, invalidated_at
                    FROM knowledge_submissions
                    WHERE id = 'submission_legacy'
                    """
                )
            ).mappings().one()
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

        idempotency_columns = {column["name"] for column in inspect(connection).get_columns("documents_idempotency")}
        claim_columns = {column["name"] for column in inspect(connection).get_columns("upload_dedup_claims")}
        idem_rows = connection.execute(text("SELECT * FROM documents_idempotency")).mappings().all()
        claim_row = connection.execute(text("SELECT * FROM upload_dedup_claims")).mappings().one()
        primary_key = inspect(connection).get_pk_constraint("documents_idempotency")["constrained_columns"]

    assert "idempotency_key" not in idempotency_columns
    assert "idempotency_key_hash" in idempotency_columns
    assert {row["idempotency_key_hash"] for row in idem_rows} == {
        hashlib.sha256(b"legacy-secret").hexdigest(),
        hashlib.sha256(b"legacy-second-secret").hexdigest(),
    }
    assert primary_key == ["actor_id", "endpoint", "target_id", "idempotency_key_hash"]
    assert "document_version_id" in claim_columns
    assert claim_row["document_version_id"] == "version_1"
