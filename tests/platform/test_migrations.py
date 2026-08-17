from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config
from app.identity.schema import IDENTITY_TABLE_NAMES, identity_user_table
from app.indexing.schema import INDEXING_TABLE_NAMES, index_chunks_table
from app.platform.config import load_platform_settings
from app.platform.database import CORE_TABLE_NAMES, core_metadata, create_engine_for_settings


def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_empty_database_upgrade_creates_only_core_owned_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_DATABASE_URL", "sqlite:///:memory:")
    database_url = f"sqlite:///{tmp_path / 'empty.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "0001_core_platform_initial")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert CORE_TABLE_NAMES <= tables
    assert "identity_user" not in tables
    assert "provider_call" not in tables
    assert "outbox_event" not in tables


def test_head_upgrade_creates_identity_owned_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'identity.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert IDENTITY_TABLE_NAMES <= tables
    assert INDEXING_TABLE_NAMES <= tables


def test_directory_search_migration_backfills_existing_users(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'directory-search.sqlite3'}"
    config = alembic_config(database_url)
    command.upgrade(config, "0018_retention_ops")
    engine = create_engine(database_url)
    now = datetime(2026, 8, 15, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                identity_user_table.insert().values(
                    id="user_1",
                    username="Alice",
                    normalized_username="alice",
                    password_hash="hash",
                    real_name="Straße",
                    display_name="Ally",
                    department_id=None,
                    role="user",
                    lifecycle_status="active",
                    version=1,
                    avatar_url=None,
                    preferences_json={},
                    transition_version=1,
                    created_at_utc=now,
                    updated_at_utc=now,
                    deletion_requested_at_utc=None,
                    purge_after_at_utc=None,
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            search_text = connection.execute(
                identity_user_table.select()
                .with_only_columns(identity_user_table.c.directory_search_text)
                .where(identity_user_table.c.id == "user_1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert search_text == "alice strasse ally user"


def test_media_kind_migration_accepts_standard_office_mime_values(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'media-kind.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "0013_search_indexing")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    try:
        for table_name in (
            "documents",
            "document_versions",
            "knowledge_submissions",
            "index_chunks",
        ):
            media_kind = next(
                column
                for column in inspector.get_columns(table_name)
                if column["name"] == "media_kind"
            )
            assert media_kind["type"].length == 128
        with engine.begin() as connection:
            for number, media_kind in enumerate(
                (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                start=1,
            ):
                connection.execute(
                    index_chunks_table.insert().values(
                        id=f"chunk_{number}",
                        generation_id="generation_1",
                        publication_id="publication_1",
                        document_id="document_1",
                        document_version_id="version_1",
                        space_id="space_1",
                        text="content",
                        embedding_text="content",
                        sparse_text="content",
                        locator_json={},
                        snippet="content",
                        media_kind=media_kind,
                        manifest_hash="manifest_1",
                        metadata_json={},
                        indexable=True,
                    )
                )
    finally:
        engine.dispose()

    command.downgrade(config, "0013_search_indexing")
    engine = create_engine(database_url)
    try:
        media_kind = next(
            column
            for column in inspect(engine).get_columns("index_chunks")
            if column["name"] == "media_kind"
        )
        assert media_kind["type"].length == 64
    finally:
        engine.dispose()


def test_initial_revision_can_downgrade_to_empty_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'rollback.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(database_url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def test_metadata_declares_core_tables_and_no_domain_ownership() -> None:
    assert set(core_metadata.tables) == CORE_TABLE_NAMES
    assert all(name.startswith("platform_") for name in core_metadata.tables)


@pytest.mark.parametrize(
    "database_url",
    ["sqlite://", "sqlite:///:memory:", "sqlite+pysqlite://", "sqlite+pysqlite:///:memory:"],
)
def test_explicit_sqlite_development_provider_shares_initialized_state_across_threads(
    database_url: str,
) -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": database_url,
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )

    engine = create_engine_for_settings(settings)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE shared_state (value INTEGER NOT NULL)"))
        connection.execute(text("INSERT INTO shared_state (value) VALUES (1)"))

    def read_shared_value() -> int:
        with engine.connect() as connection:
            return connection.execute(text("SELECT value FROM shared_state")).scalar_one()

    with ThreadPoolExecutor(max_workers=1) as executor:
        value = executor.submit(read_shared_value).result()
    engine.dispose()

    assert value == 1


def test_immutable_trigger_history_has_no_unpublished_refresh_revisions() -> None:
    versions = Path("alembic/versions")

    assert not list(versions.glob("000[789]_refresh_immutable_triggers.py"))
    assert 'down_revision: str | None = "0006_outbox_retirement_tombstone"' in (
        versions / "0010_compacted_fields_check.py"
    ).read_text(encoding="utf-8")
