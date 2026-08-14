"""Persistence for retention-owned reconciliation runs, findings and receipts."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from .schema import (
    retention_hook_receipts_table,
    retention_reconciliation_findings_table,
    retention_reconciliation_runs_table,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class SqlAlchemyRetentionRepository:
    def __init__(self, engine: Engine, *, now: Any) -> None:
        self._engine = engine
        self._now = now

    def _current_time(self, connection: Connection | None = None) -> datetime:
        now = self._now(connection) if connection is not None else self._now()
        return now.astimezone() if now.tzinfo is None else now

    # ---- reconciliation runs ----

    def create_run(self, *, scope: str, source_snapshot: Mapping[str, Any]) -> str:
        run_id = _new_id("recon")
        with self._engine.begin() as connection:
            connection.execute(
                retention_reconciliation_runs_table.insert().values(
                    id=run_id,
                    scope=scope,
                    status="running",
                    source_snapshot_json=dict(source_snapshot),
                    finding_counts_json={"info": 0, "repairable": 0, "blocking": 0},
                    started_at_utc=self._current_time(connection),
                )
            )
        return run_id

    def complete_run(
        self,
        run_id: str,
        *,
        counts: Mapping[str, int],
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(retention_reconciliation_runs_table)
                .where(retention_reconciliation_runs_table.c.id == run_id)
                .values(
                    status="completed",
                    finding_counts_json=dict(counts),
                    completed_at_utc=self._current_time(connection),
                )
            )

    def fail_run(self, run_id: str, *, detail: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(retention_reconciliation_runs_table)
                .where(retention_reconciliation_runs_table.c.id == run_id)
                .values(status="failed", completed_at_utc=self._current_time(connection))
            )
            connection.execute(
                retention_reconciliation_findings_table.insert().values(
                    id=_new_id("finding"),
                    run_id=run_id,
                    category="blocking",
                    resource_type="reconciliation",
                    resource_id=run_id,
                    detail=detail[:512],
                    repairable=0,
                    status="open",
                    created_at_utc=self._current_time(connection),
                    updated_at_utc=self._current_time(connection),
                )
            )

    # ---- findings ----

    def add_finding(
        self,
        *,
        run_id: str,
        category: str,
        resource_type: str,
        resource_id: str,
        detail: str,
        repairable: bool,
        connection: Connection,
    ) -> str:
        finding_id = _new_id("finding")
        now = self._current_time(connection)
        connection.execute(
            retention_reconciliation_findings_table.insert().values(
                id=finding_id,
                run_id=run_id,
                category=category,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail[:512],
                repairable=1 if repairable else 0,
                status="open",
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        return finding_id

    def list_open_findings(
        self, *, scope: str | None = None, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        with self._engine.connect() as connection:
            query = (
                select(retention_reconciliation_findings_table)
                .join(
                    retention_reconciliation_runs_table,
                    retention_reconciliation_runs_table.c.id
                    == retention_reconciliation_findings_table.c.run_id,
                )
                .where(retention_reconciliation_findings_table.c.status == "open")
                .order_by(retention_reconciliation_findings_table.c.created_at_utc)
                .limit(limit)
            )
            if scope is not None:
                query = query.where(retention_reconciliation_runs_table.c.scope == scope)
            rows = connection.execute(query).mappings()
            return [dict(row) for row in rows]

    def mark_finding(
        self,
        finding_id: str,
        *,
        status: str,
        hook_operation_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if hook_operation_id is not None:
            values["hook_operation_id"] = hook_operation_id
        if detail is not None:
            values["detail"] = detail[:512]
        with self._engine.begin() as connection:
            connection.execute(
                update(retention_reconciliation_findings_table)
                .where(retention_reconciliation_findings_table.c.id == finding_id)
                .values(**values, updated_at_utc=self._current_time(connection))
            )

    # ---- hook receipts ----

    def upsert_receipt(
        self,
        *,
        operation_id: str,
        kind: str,
        target_id: str,
        receipt_json: Mapping[str, Any],
        state: str,
        error: str | None = None,
        connection: Connection,
    ) -> None:
        now = self._current_time(connection)
        connection.execute(
            retention_hook_receipts_table.insert().values(
                operation_id=operation_id,
                kind=kind,
                target_id=target_id,
                receipt_json=dict(receipt_json),
                state=state,
                attempt_count=0,
                last_error=error,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )

    def touch_receipt(
        self,
        *,
        operation_id: str,
        receipt_json: Mapping[str, Any],
        state: str,
        error: str | None = None,
        connection: Connection,
    ) -> None:
        values: dict[str, Any] = {
            "receipt_json": dict(receipt_json),
            "state": state,
            "last_error": error,
            "attempt_count": retention_hook_receipts_table.c.attempt_count + 1,
            "updated_at_utc": self._current_time(connection),
        }
        connection.execute(
            update(retention_hook_receipts_table)
            .where(retention_hook_receipts_table.c.operation_id == operation_id)
            .values(**values)
        )

    def get_receipt(self, operation_id: str) -> Mapping[str, Any] | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(retention_hook_receipts_table).where(
                        retention_hook_receipts_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    def store_receipt(
        self,
        *,
        operation_id: str,
        kind: str,
        target_id: str,
        receipt_json: Mapping[str, Any],
        state: str,
        error: str | None = None,
    ) -> None:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(retention_hook_receipts_table).where(
                        retention_hook_receipts_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                self.upsert_receipt(
                    operation_id=operation_id,
                    kind=kind,
                    target_id=target_id,
                    receipt_json=receipt_json,
                    state=state,
                    error=error,
                    connection=connection,
                )
            else:
                self.touch_receipt(
                    operation_id=operation_id,
                    receipt_json=receipt_json,
                    state=state,
                    error=error,
                    connection=connection,
                )

    def list_due_receipts(self, *, kind: str, limit: int = 100) -> list[Mapping[str, Any]]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(retention_hook_receipts_table)
                .where(
                    retention_hook_receipts_table.c.kind == kind,
                    retention_hook_receipts_table.c.state.in_(("requested", "accepted", "blocked")),
                )
                .order_by(retention_hook_receipts_table.c.updated_at_utc)
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]
