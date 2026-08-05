from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config
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

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert CORE_TABLE_NAMES <= tables
    assert "identity_user" not in tables
    assert "provider_call" not in tables
    assert "outbox_event" not in tables


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


def test_explicit_sqlite_development_provider_uses_compatible_engine_options() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )

    engine = create_engine_for_settings(settings)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    engine.dispose()
