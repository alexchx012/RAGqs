"""Scoped outbox compaction entry used by the retention gateway."""

from __future__ import annotations

import pytest
from retention_helpers import build_engine, fixed_now
from sqlalchemy import select

from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.schema import (
    outbox_compaction_command_table,
    outbox_metadata,
    outbox_retirement_command_table,
)
from app.platform.database import core_metadata
from app.platform.errors import PlatformError


def _seed_retirement(engine, *, state: str = "completed"):
    with engine.begin() as connection:
        connection.execute(
            outbox_retirement_command_table.insert().values(
                operation_id="identity-retire:u_1:cleanup_1",
                user_id="u_1",
                deletion_id="cleanup_1",
                input_fingerprint="retire-fingerprint",
                archive_ref="identity-archive:u_1:cleanup_1:cleanup_1",
                archive_checksum="checksum",
                archive_ref_fingerprint="ref-fingerprint",
                mode="inline",
                state=state,
                receipt_json={"inbox_removed": True},
                created_at_utc=fixed_now(),
                completed_at_utc=fixed_now() if state == "completed" else None,
            )
        )


def _lifecycle(engine) -> SqlAlchemyOutboxLifecycle:
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda connection=None: fixed_now(),
        clock=None,
        archive_verifier=None,
    )


def test_scoped_entry_compacts_only_after_completed_retirement() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    _seed_retirement(engine)
    lifecycle = _lifecycle(engine)
    with engine.begin() as connection:
        receipt = lifecycle.request_compaction_for_identity_deletion(
            operation_id="compact:cleanup_1",
            user_id="u_1",
            deletion_id="cleanup_1",
            retirement_receipt_id="identity-retire:u_1:cleanup_1",
            connection=connection,
        )
    assert receipt.state == "completed"
    assert receipt.operation_id == "compact:cleanup_1"
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(outbox_compaction_command_table).where(
                    outbox_compaction_command_table.c.operation_id == "compact:cleanup_1"
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "completed"
    assert row["retirement_receipt_id"] == "identity-retire:u_1:cleanup_1"


def test_scoped_entry_replays_same_operation_without_new_row() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    _seed_retirement(engine)
    lifecycle = _lifecycle(engine)
    with engine.begin() as connection:
        first = lifecycle.request_compaction_for_identity_deletion(
            operation_id="compact:cleanup_1",
            user_id="u_1",
            deletion_id="cleanup_1",
            retirement_receipt_id="identity-retire:u_1:cleanup_1",
            connection=connection,
        )
        second = lifecycle.request_compaction_for_identity_deletion(
            operation_id="compact:cleanup_1",
            user_id="u_1",
            deletion_id="cleanup_1",
            retirement_receipt_id="identity-retire:u_1:cleanup_1",
            connection=connection,
        )
    assert first.state == "completed"
    assert second.state == "completed"
    with engine.connect() as connection:
        count = connection.execute(select(outbox_compaction_command_table.c.operation_id)).scalars()
    assert len(list(count)) == 1


def test_scoped_entry_rejects_mismatched_retirement() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    _seed_retirement(engine)
    lifecycle = _lifecycle(engine)
    with engine.begin() as connection:
        with pytest.raises(PlatformError) as excinfo:
            lifecycle.request_compaction_for_identity_deletion(
                operation_id="compact:cleanup_1",
                user_id="u_other",
                deletion_id="cleanup_1",
                retirement_receipt_id="identity-retire:u_1:cleanup_1",
                connection=connection,
            )
    assert excinfo.value.code == "compaction_prerequisite_missing"


def test_scoped_entry_requires_completed_retirement_state() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    _seed_retirement(engine, state="accepted")
    lifecycle = _lifecycle(engine)
    with engine.begin() as connection:
        with pytest.raises(PlatformError) as excinfo:
            lifecycle.request_compaction_for_identity_deletion(
                operation_id="compact:cleanup_1",
                user_id="u_1",
                deletion_id="cleanup_1",
                retirement_receipt_id="identity-retire:u_1:cleanup_1",
                connection=connection,
            )
    assert excinfo.value.code == "compaction_prerequisite_missing"
