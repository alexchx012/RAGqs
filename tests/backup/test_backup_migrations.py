"""Migration coverage for the backup/restore orchestration tables."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect

from alembic import command
from alembic.config import Config
from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata
from app.indexing.schema import indexing_metadata
from app.identity.schema import identity_metadata
from app.outbox.schema import outbox_metadata
from app.platform.database import core_metadata


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _indexes(engine, table: str) -> set[str]:
    return {index["name"] for index in inspect(engine).get_indexes(table)}


def test_backup_tables_created_at_head(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'backup.sqlite3'}"
    config = _config(database_url)
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


OPS_LAYER_TABLES = {
    "backup_policy",
    "backup_schedule_occurrences",
    "ops_idempotency_commands",
    "backup_write_gate",
    "backup_cleanup_targets",
}


def test_backup_ops_layer_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'backup_ops.sqlite3'}"
    config = _config(database_url)
    command.upgrade(config, "0033_backup_ops_layer")
    engine = create_engine(database_url, future=True)
    tables = set(inspect(engine).get_table_names())
    assert OPS_LAYER_TABLES <= tables
    assert "purged_at_utc" in _columns(engine, "backup_sets")
    assert "uq_restore_sessions_active" in _indexes(engine, "restore_sessions")
    engine.dispose()

    command.downgrade(config, "0032_backup_restore_ops")
    engine = create_engine(database_url, future=True)
    tables_after = set(inspect(engine).get_table_names())
    assert OPS_LAYER_TABLES.isdisjoint(tables_after)
    assert "backup_sets" in tables_after
    assert "purged_at_utc" not in _columns(engine, "backup_sets")
    assert "uq_restore_sessions_active" not in _indexes(engine, "restore_sessions")
    engine.dispose()


def _create_legacy_0032_tables(database_url: str) -> None:
    """The pre-0033 shape of the two tables altered by the ops layer."""
    legacy = sa.MetaData()
    sa.Table(
        "backup_sets",
        legacy,
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    sa.Table(
        "restore_sessions",
        legacy,
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("backup_id", sa.String(128), sa.ForeignKey("backup_sets.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_reason", sa.String(512), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    engine = create_engine(database_url, future=True)
    legacy.create_all(engine)
    engine.dispose()


def test_backup_ops_layer_upgrade_guards_against_legacy_0032_schema(tmp_path: Path) -> None:
    """Databases already at 0032 lack the new column/index; the inspect guards
    add exactly those without touching the pre-existing tables."""
    database_url = f"sqlite:///{tmp_path / 'backup_ops_legacy.sqlite3'}"
    _create_legacy_0032_tables(database_url)
    config = _config(database_url)
    command.upgrade(config, "0033_backup_ops_layer")
    engine = create_engine(database_url, future=True)
    assert "purged_at_utc" in _columns(engine, "backup_sets")
    assert "uq_restore_sessions_active" in _indexes(engine, "restore_sessions")
    assert OPS_LAYER_TABLES <= set(inspect(engine).get_table_names())

    # The partial unique index is the durable mutex for the start race: two
    # sessions can never both hold the same active status ('accepted' is the
    # initial status of every new session).
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO backup_sets (id, status, created_at_utc) VALUES ('b1', 'complete', '2026-08-25')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO restore_sessions (id, backup_id, status, created_at_utc, updated_at_utc)"
                " VALUES ('r1', 'b1', 'accepted', '2026-08-25', '2026-08-25')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO restore_sessions (id, backup_id, status, created_at_utc, updated_at_utc)"
                " VALUES ('r2', 'b1', 'completed', '2026-08-25', '2026-08-25')"
            )
        )
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO restore_sessions (id, backup_id, status, created_at_utc, updated_at_utc)"
                    " VALUES ('r3', 'b1', 'accepted', '2026-08-25', '2026-08-25')"
                )
            )
    engine.dispose()


def _insert_occurrence(connection, occurrence_id: str, scheduled_for: str, outcome: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO backup_schedule_occurrences"
            " (id, scheduled_for_utc, outcome, created_at_utc)"
            f" VALUES ('{occurrence_id}', '{scheduled_for}', '{outcome}', '2026-08-25')"
        )
    )


def test_schedule_occurrence_outcome_allows_skipped_missed_at_head(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'backup_head.sqlite3'}"
    command.upgrade(_config(database_url), "head")
    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        _insert_occurrence(connection, "o1", "2026-08-25 02:00:00", "skipped_missed")
    engine.dispose()


def _create_legacy_occurrences_table(database_url: str) -> None:
    """The pre-0034 shape: the outcome check lacks 'skipped_missed'."""
    legacy = sa.MetaData()
    sa.Table(
        "backup_sets",
        legacy,
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    sa.Table(
        "backup_schedule_occurrences",
        legacy,
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("scheduled_for_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("backup_id", sa.String(128), sa.ForeignKey("backup_sets.id"), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scheduled_for_utc", name="uq_backup_schedule_occurrences_slot"),
        sa.CheckConstraint(
            "outcome IN ('executed','skipped_active_backup','skipped_disabled')",
            name="ck_backup_schedule_occurrences_outcome",
        ),
    )
    engine = create_engine(database_url, future=True)
    legacy.create_all(engine)
    # A real 0033 database went through 0012/0013/0016, whose create_all owns
    # the documents/indexing/chat domain tables; the migrations from 0034 to
    # head inspect or alter them.
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    # 真实 0033 数据库早已含 core/identity/outbox 域表；0040 的索引与回填
    # 依赖 outbox_event/outbox_metric/notification_inbox/identity_user。
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    with engine.begin() as connection:
        _insert_occurrence(connection, "o0", "2026-08-20 02:00:00", "executed")
    engine.dispose()


def test_0034_extends_outcome_constraint_on_legacy_databases(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'backup_0034.sqlite3'}"
    _create_legacy_occurrences_table(database_url)
    config = _config(database_url)
    command.stamp(config, "0033_backup_ops_layer")

    engine = create_engine(database_url, future=True)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_occurrence(connection, "o1", "2026-08-25 02:00:00", "skipped_missed")
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        _insert_occurrence(connection, "o2", "2026-08-25 03:00:00", "skipped_missed")
        # Pre-existing rows survive the table rewrite; other outcomes still checked.
        kept = connection.execute(
            sa.text("SELECT outcome FROM backup_schedule_occurrences WHERE id = 'o0'")
        ).scalar_one()
        assert kept == "executed"
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_occurrence(connection, "o3", "2026-08-25 04:00:00", "bogus")
    engine.dispose()

    command.downgrade(config, "0033_backup_ops_layer")
    engine = create_engine(database_url, future=True)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_occurrence(connection, "o4", "2026-08-25 05:00:00", "skipped_missed")
    engine.dispose()
