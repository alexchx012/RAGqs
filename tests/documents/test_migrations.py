from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from alembic import command
from alembic.config import Config
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
