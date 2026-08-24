"""Migration coverage for the backup/restore orchestration tables."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from alembic import command
from alembic.config import Config


def test_backup_tables_created_at_head(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'backup.sqlite3'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0032_backup_restore_ops")
    engine = create_engine(database_url, future=True)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    expected = {
        "backup_sets",
        "backup_components",
        "backup_objects",
        "restore_sessions",
        "restore_stages",
        "restore_targets",
        "repair_targets",
        "maintenance_gate",
    }
    assert expected <= tables
    command.downgrade(config, "0031_public_graph_active_facts")
    engine = create_engine(database_url, future=True)
    tables_after = set(inspect(engine).get_table_names())
    engine.dispose()
    assert expected.isdisjoint(tables_after)
