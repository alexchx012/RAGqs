from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.exc import IntegrityError

from alembic import command
from alembic.config import Config


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _legacy_receipts_table(metadata: MetaData) -> Table:
    return Table(
        "retention_hook_receipts",
        metadata,
        Column("operation_id", String(128), primary_key=True),
        Column("kind", String(32), nullable=False),
        Column("target_id", String(128), nullable=False),
        Column("receipt_json", JSON, nullable=False),
        Column("state", String(32), nullable=False),
        Column("attempt_count", Integer, nullable=False),
        Column("last_error", String(256), nullable=True),
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
        Column("updated_at_utc", DateTime(timezone=True), nullable=False),
        CheckConstraint(
            "kind IN ('index_gc','graph_component_gc','account_compaction')",
            name="ck_retention_hook_receipts_kind",
        ),
        CheckConstraint(
            "state IN ('requested','accepted','blocked','completed','terminal','purged')",
            name="ck_retention_hook_receipts_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_retention_hook_receipts_attempts",
        ),
        Index("ix_retention_receipts_kind_state", "kind", "state"),
    )


def test_receipt_migration_removes_dead_retry_ledger_and_preserves_receipts(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'retention.sqlite3'}"
    engine = create_engine(database_url)
    legacy_metadata = MetaData()
    legacy_receipts = _legacy_receipts_table(legacy_metadata)
    legacy_metadata.create_all(engine)
    created_at = datetime(2026, 8, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            legacy_receipts.insert().values(
                operation_id="compact:cleanup_1",
                kind="account_compaction",
                target_id="u_1",
                receipt_json={"compacted_count": 3},
                state="accepted",
                attempt_count=3,
                last_error=None,
                created_at_utc=created_at,
                updated_at_utc=created_at,
            )
        )
        connection.execute(
            legacy_receipts.insert().values(
                operation_id="compact:cleanup_requested",
                kind="account_compaction",
                target_id="u_2",
                receipt_json={"compacted_count": 0},
                state="requested",
                attempt_count=0,
                last_error=None,
                created_at_utc=created_at,
                updated_at_utc=created_at,
            )
        )
    engine.dispose()

    config = _config(database_url)
    command.stamp(config, "0018_retention_ops")
    command.upgrade(config, "0019_simplify_retention_receipts")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "attempt_count" not in {
            column["name"] for column in inspector.get_columns("retention_hook_receipts")
        }
        assert "ix_retention_receipts_kind_state" not in {
            index["name"] for index in inspector.get_indexes("retention_hook_receipts")
        }
        checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("retention_hook_receipts")
        }
        assert "requested" not in checks["ck_retention_hook_receipts_state"]
        assert "ck_retention_hook_receipts_attempts" not in checks

        receipts = Table("retention_hook_receipts", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            receipt = (
                connection.execute(
                    select(receipts).where(receipts.c.operation_id == "compact:cleanup_1")
                )
                .mappings()
                .one()
            )
            requested_receipt = (
                connection.execute(
                    select(receipts).where(receipts.c.operation_id == "compact:cleanup_requested")
                )
                .mappings()
                .one()
            )
        assert dict(receipt)["receipt_json"] == {"compacted_count": 3}
        assert dict(receipt)["state"] == "accepted"
        assert dict(requested_receipt)["receipt_json"] == {"compacted_count": 0}
        assert dict(requested_receipt)["state"] == "accepted"

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    receipts.insert().values(
                        operation_id="compact:cleanup_2",
                        kind="account_compaction",
                        target_id="u_2",
                        receipt_json={},
                        state="requested",
                        last_error=None,
                        created_at_utc=created_at,
                        updated_at_utc=created_at,
                    )
                )
    finally:
        engine.dispose()

    command.downgrade(config, "0018_retention_ops")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "attempt_count" in {
            column["name"] for column in inspector.get_columns("retention_hook_receipts")
        }
        assert "ix_retention_receipts_kind_state" in {
            index["name"] for index in inspector.get_indexes("retention_hook_receipts")
        }
        checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("retention_hook_receipts")
        }
        assert "requested" in checks["ck_retention_hook_receipts_state"]
        assert "ck_retention_hook_receipts_attempts" in checks

        receipts = Table("retention_hook_receipts", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            receipt = (
                connection.execute(
                    select(receipts).where(receipts.c.operation_id == "compact:cleanup_1")
                )
                .mappings()
                .one()
            )
        assert dict(receipt)["attempt_count"] == 0
    finally:
        engine.dispose()
