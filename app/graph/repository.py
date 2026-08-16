"""SQLAlchemy repository for public graph build runs.

All run state transitions are optimistic-concurrency updates keyed on
``id`` + ``version``: zero affected rows always stops the caller. Claiming a
run, writing staging resources and recording usage are all conditional on the
current attempt, lease owner and fencing token.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.platform.errors import PlatformError

from .models import GraphRunRecord
from .schema import (
    graph_build_attempts_table,
    graph_build_audit_table,
    graph_build_operations_table,
    graph_build_runs_table,
    graph_staging_resources_table,
)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_STATES = frozenset({"queued", "running"})
_OPERATION_RESERVATION_TTL = timedelta(minutes=5)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(15)}"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _insert_operation_if_absent(
    connection: Connection,
    *,
    operation_id: str,
    kind: str,
    request_hash: str,
    created_at: datetime,
) -> bool:
    values = {
        "operation_id": operation_id,
        "kind": kind,
        "request_hash": request_hash,
        "status": "reserved",
        "response_json": None,
        "created_at_utc": created_at,
    }
    if connection.dialect.name == "postgresql":
        statement = (
            pg_insert(graph_build_operations_table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(graph_build_operations_table.c.operation_id)
        )
        return connection.execute(statement).scalar_one_or_none() is not None
    if connection.dialect.name == "sqlite":
        return (
            connection.execute(
                sqlite_insert(graph_build_operations_table)
                .values(**values)
                .on_conflict_do_nothing()
            ).rowcount
            == 1
        )
    try:
        with connection.begin_nested():
            connection.execute(graph_build_operations_table.insert().values(**values))
        return True
    except IntegrityError:
        return False


class SqlAlchemyGraphRepository:
    def __init__(self, engine: Engine, *, now: Any = None) -> None:
        self._engine = engine
        self._now = now or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------------ run IO

    @staticmethod
    def _run(row: Mapping[str, Any]) -> GraphRunRecord:
        return GraphRunRecord(
            graph_build_id=str(row["id"]),
            version=int(row["version"]),
            state=str(row["state"]),
            initiator_identity_id=str(row["initiator_identity_id"]),
            source_revision=int(row["source_revision"]),
            source_manifest_id=str(row["source_manifest_id"]),
            source_manifest_hash=str(row["source_manifest_hash"]),
            source_head_fence=int(row["source_head_fence"]),
            publications=tuple(dict(item) for item in (row["publications_json"] or [])),
            target_generation_id=str(row["target_generation_id"]),
            target_generation_fence=str(row["target_generation_fence"]),
            component_manifest_slot=str(row["component_manifest_slot"]),
            component_stage_id=(
                str(row["component_stage_id"]) if row["component_stage_id"] is not None else None
            ),
            grant_operation_id=str(row["grant_operation_id"]),
            grant_expires_at=_utc(row["grant_expires_at_utc"]),  # type: ignore[arg-type]
            config_snapshot=dict(row["config_snapshot_json"] or {}),
            plan_snapshot=dict(row["plan_snapshot_json"] or {}),
            estimated_primary_model_calls=int(row["estimated_primary_model_calls"]),
            actual_primary_model_calls=int(row["actual_primary_model_calls"]),
            actual_provider_calls=int(row["actual_provider_calls"]),
            current_attempt=int(row["current_attempt"]),
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_expires_at=_utc(row["lease_expires_at_utc"]),
            heartbeat_at=_utc(row["heartbeat_at_utc"]),
            fencing_token=str(row["fencing_token"]) if row["fencing_token"] is not None else None,
            failure_class=str(row["failure_class"]) if row["failure_class"] is not None else None,
            failure_reason=(
                str(row["failure_reason"]) if row["failure_reason"] is not None else None
            ),
            graph_generation_id=(
                str(row["graph_generation_id"]) if row["graph_generation_id"] is not None else None
            ),
            index_generation_id=(
                str(row["index_generation_id"]) if row["index_generation_id"] is not None else None
            ),
            activation_receipt_id=(
                str(row["activation_receipt_id"])
                if row["activation_receipt_id"] is not None
                else None
            ),
            created_at=_utc(row["created_at_utc"]),  # type: ignore[arg-type]
            started_at=_utc(row["started_at_utc"]),
            completed_at=_utc(row["completed_at_utc"]),
        )

    def get_run(
        self, graph_build_id: str, *, connection: Connection | None = None, for_update: bool = False
    ) -> GraphRunRecord | None:
        def _get(conn: Connection) -> GraphRunRecord | None:
            statement = select(graph_build_runs_table).where(
                graph_build_runs_table.c.id == graph_build_id
            )
            if for_update:
                statement = statement.with_for_update()
            row = conn.execute(statement).mappings().one_or_none()
            return self._run(row) if row is not None else None

        if connection is not None:
            return _get(connection)
        with self._engine.begin() as conn:
            return _get(conn)

    def latest_run(self, *, connection: Connection | None = None) -> GraphRunRecord | None:
        def _get(conn: Connection) -> GraphRunRecord | None:
            row = (
                conn.execute(
                    select(graph_build_runs_table)
                    .order_by(graph_build_runs_table.c.created_at_utc.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            return self._run(row) if row is not None else None

        if connection is not None:
            return _get(connection)
        with self._engine.begin() as conn:
            return _get(conn)

    def has_active_run(self, *, connection: Connection) -> bool:
        return (
            connection.execute(
                select(graph_build_runs_table.c.id)
                .where(graph_build_runs_table.c.state.in_(ACTIVE_STATES))
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

    def insert_run(
        self,
        *,
        connection: Connection,
        record: GraphRunRecord,
    ) -> None:
        connection.execute(
            graph_build_runs_table.insert().values(
                id=record.graph_build_id,
                version=record.version,
                state=record.state,
                initiator_identity_id=record.initiator_identity_id,
                source_revision=record.source_revision,
                source_manifest_id=record.source_manifest_id,
                source_manifest_hash=record.source_manifest_hash,
                source_head_fence=record.source_head_fence,
                publications_json=[dict(item) for item in record.publications],
                target_generation_id=record.target_generation_id,
                target_generation_fence=record.target_generation_fence,
                component_manifest_slot=record.component_manifest_slot,
                component_stage_id=record.component_stage_id,
                grant_operation_id=record.grant_operation_id,
                grant_expires_at_utc=record.grant_expires_at,
                config_snapshot_json=dict(record.config_snapshot),
                plan_snapshot_json=dict(record.plan_snapshot),
                estimated_primary_model_calls=record.estimated_primary_model_calls,
                actual_primary_model_calls=record.actual_primary_model_calls,
                actual_provider_calls=record.actual_provider_calls,
                current_attempt=record.current_attempt,
                lease_owner=record.lease_owner,
                lease_expires_at_utc=record.lease_expires_at,
                heartbeat_at_utc=record.heartbeat_at,
                fencing_token=record.fencing_token,
                failure_class=record.failure_class,
                failure_reason=record.failure_reason,
                graph_generation_id=record.graph_generation_id,
                index_generation_id=record.index_generation_id,
                activation_receipt_id=record.activation_receipt_id,
                created_at_utc=record.created_at,
                started_at_utc=record.started_at,
                completed_at_utc=record.completed_at,
            )
        )

    def transition_run(
        self,
        *,
        connection: Connection,
        graph_build_id: str,
        expected_version: int,
        to_state: str,
        changes: Mapping[str, Any],
    ) -> None:
        values = {"version": expected_version + 1, "state": to_state, **dict(changes)}
        result = connection.execute(
            update(graph_build_runs_table)
            .where(
                and_(
                    graph_build_runs_table.c.id == graph_build_id,
                    graph_build_runs_table.c.version == expected_version,
                )
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise PlatformError(
                "version_conflict",
                "The graph run version has changed",
                {"graph_build_id": graph_build_id, "expected_version": expected_version},
                409,
            )

    def set_stage_receipt(
        self,
        *,
        connection: Connection,
        graph_build_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        expected_version: int,
        component_stage_id: str,
        now: datetime,
    ) -> None:
        result = connection.execute(
            update(graph_build_runs_table)
            .where(
                and_(
                    graph_build_runs_table.c.id == graph_build_id,
                    graph_build_runs_table.c.current_attempt == attempt,
                    graph_build_runs_table.c.lease_owner == owner,
                    graph_build_runs_table.c.fencing_token == fencing_token,
                    graph_build_runs_table.c.version == expected_version,
                    graph_build_runs_table.c.state == "running",
                    graph_build_runs_table.c.lease_expires_at_utc > now,
                )
            )
            .values(component_stage_id=component_stage_id)
        )
        if result.rowcount != 1:
            raise PlatformError(
                "graph_build_lease_lost", "The graph run lease or fence no longer matches", {}, 409
            )

    # ------------------------------------------------------------------ claims

    def claim_next_queued(
        self,
        *,
        connection: Connection,
        owner: str,
        lease_ttl_seconds: int,
        now: datetime,
    ) -> GraphRunRecord | None:
        row = (
            connection.execute(
                select(graph_build_runs_table)
                .where(graph_build_runs_table.c.state == "queued")
                .order_by(graph_build_runs_table.c.created_at_utc.asc())
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        run = self._run(row)
        attempt = run.current_attempt + 1
        fencing_token = _new_id("graph_fence")
        started_at = run.started_at if attempt > 1 else now
        connection.execute(
            update(graph_build_runs_table)
            .where(
                and_(
                    graph_build_runs_table.c.id == run.graph_build_id,
                    graph_build_runs_table.c.version == run.version,
                )
            )
            .values(
                version=run.version + 1,
                state="running",
                current_attempt=attempt,
                lease_owner=owner,
                lease_expires_at_utc=now + timedelta(seconds=lease_ttl_seconds),
                heartbeat_at_utc=now,
                fencing_token=fencing_token,
                started_at_utc=started_at,
            )
        )
        connection.execute(
            graph_build_attempts_table.insert().values(
                run_id=run.graph_build_id,
                attempt=attempt,
                lease_owner=owner,
                lease_expires_at_utc=now + timedelta(seconds=lease_ttl_seconds),
                heartbeat_at_utc=now,
                fencing_token=fencing_token,
                outcome="running",
                started_at_utc=now,
                completed_at_utc=None,
            )
        )
        self.write_audit(
            connection=connection,
            run_id=run.graph_build_id,
            attempt=attempt,
            version=run.version + 1,
            event_kind="attempt_claimed",
            actor=f"worker:{owner}",
            details={"fencing_token": fencing_token},
        )
        return self._run(
            connection.execute(
                select(graph_build_runs_table).where(
                    graph_build_runs_table.c.id == run.graph_build_id
                )
            )
            .mappings()
            .one()
        )

    def heartbeat(
        self,
        *,
        connection: Connection,
        graph_build_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        lease_ttl_seconds: int,
        now: datetime,
    ) -> bool:
        result = connection.execute(
            update(graph_build_runs_table)
            .where(
                and_(
                    graph_build_runs_table.c.id == graph_build_id,
                    graph_build_runs_table.c.state == "running",
                    graph_build_runs_table.c.current_attempt == attempt,
                    graph_build_runs_table.c.lease_owner == owner,
                    graph_build_runs_table.c.fencing_token == fencing_token,
                    graph_build_runs_table.c.lease_expires_at_utc > now,
                )
            )
            .values(
                heartbeat_at_utc=now,
                lease_expires_at_utc=now + timedelta(seconds=lease_ttl_seconds),
            )
        )
        if result.rowcount != 1:
            return False
        attempt_result = connection.execute(
            update(graph_build_attempts_table)
            .where(
                and_(
                    graph_build_attempts_table.c.run_id == graph_build_id,
                    graph_build_attempts_table.c.attempt == attempt,
                    graph_build_attempts_table.c.lease_owner == owner,
                    graph_build_attempts_table.c.fencing_token == fencing_token,
                    graph_build_attempts_table.c.outcome == "running",
                    graph_build_attempts_table.c.lease_expires_at_utc > now,
                )
            )
            .values(
                heartbeat_at_utc=now,
                lease_expires_at_utc=now + timedelta(seconds=lease_ttl_seconds),
            )
        )
        if attempt_result.rowcount != 1:
            raise PlatformError(
                "graph_build_lease_lost", "The graph run attempt lease no longer matches", {}, 409
            )
        return True

    def invalidate_attempt_and_requeue(
        self,
        *,
        connection: Connection,
        graph_build_id: str,
        attempt: int,
        now: datetime,
    ) -> bool:
        result = connection.execute(
            update(graph_build_runs_table)
            .where(
                and_(
                    graph_build_runs_table.c.id == graph_build_id,
                    graph_build_runs_table.c.state == "running",
                    graph_build_runs_table.c.current_attempt == attempt,
                    graph_build_runs_table.c.lease_expires_at_utc <= now,
                )
            )
            .values(
                version=graph_build_runs_table.c.version + 1,
                state="queued",
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=None,
            )
        )
        if result.rowcount != 1:
            return False
        connection.execute(
            update(graph_build_attempts_table)
            .where(
                and_(
                    graph_build_attempts_table.c.run_id == graph_build_id,
                    graph_build_attempts_table.c.attempt == attempt,
                    graph_build_attempts_table.c.outcome == "running",
                )
            )
            .values(outcome="invalidated", completed_at_utc=now)
        )
        self.delete_staging_resources(connection=connection, run_id=graph_build_id, attempt=attempt)
        self.write_audit(
            connection=connection,
            run_id=graph_build_id,
            attempt=attempt,
            version=int(
                connection.execute(
                    select(graph_build_runs_table.c.version).where(
                        graph_build_runs_table.c.id == graph_build_id
                    )
                ).scalar_one()
            ),
            event_kind="attempt_invalidated",
            actor="system:lease-recovery",
            details={"reason": "lease_expired"},
        )
        return True

    def list_expired_running(
        self, *, connection: Connection, now: datetime
    ) -> list[tuple[str, int]]:
        rows = connection.execute(
            select(
                graph_build_runs_table.c.id,
                graph_build_runs_table.c.current_attempt,
            ).where(
                and_(
                    graph_build_runs_table.c.state == "running",
                    graph_build_runs_table.c.lease_expires_at_utc <= now,
                )
            )
        ).all()
        return [(str(row[0]), int(row[1])) for row in rows]

    # ----------------------------------------------------------------- staging

    def write_staging_resource(
        self,
        *,
        connection: Connection,
        run_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        expected_version: int,
        resource_kind: str,
        resource_id: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        result = connection.execute(
            graph_staging_resources_table.insert().from_select(
                [
                    graph_staging_resources_table.c.id,
                    graph_staging_resources_table.c.run_id,
                    graph_staging_resources_table.c.attempt,
                    graph_staging_resources_table.c.fencing_token,
                    graph_staging_resources_table.c.resource_kind,
                    graph_staging_resources_table.c.resource_id,
                    graph_staging_resources_table.c.payload_json,
                    graph_staging_resources_table.c.created_at_utc,
                ],
                select(
                    literal(_new_id("graph_stage_resource")),
                    literal(run_id),
                    literal(attempt),
                    literal(fencing_token),
                    literal(resource_kind),
                    literal(resource_id),
                    literal(dict(payload), type_=graph_staging_resources_table.c.payload_json.type),
                    literal(now),
                ).where(
                    and_(
                        graph_build_runs_table.c.id == run_id,
                        graph_build_runs_table.c.state == "running",
                        graph_build_runs_table.c.current_attempt == attempt,
                        graph_build_runs_table.c.lease_owner == owner,
                        graph_build_runs_table.c.fencing_token == fencing_token,
                        graph_build_runs_table.c.version == expected_version,
                        graph_build_runs_table.c.lease_expires_at_utc > now,
                    )
                ),
            )
        )
        if result.rowcount != 1:
            raise PlatformError(
                "graph_build_lease_lost", "The graph run lease or fence no longer matches", {}, 409
            )

    def list_staging_resources(
        self, *, connection: Connection, run_id: str, attempt: int
    ) -> list[Mapping[str, Any]]:
        rows = connection.execute(
            select(
                graph_staging_resources_table.c.resource_kind,
                graph_staging_resources_table.c.resource_id,
            )
            .where(
                and_(
                    graph_staging_resources_table.c.run_id == run_id,
                    graph_staging_resources_table.c.attempt == attempt,
                )
            )
            .order_by(
                graph_staging_resources_table.c.resource_kind,
                graph_staging_resources_table.c.resource_id,
            )
        ).all()
        return [{"resource_kind": str(row[0]), "resource_id": str(row[1])} for row in rows]

    def delete_staging_resources(
        self, *, connection: Connection, run_id: str, attempt: int
    ) -> None:
        connection.execute(
            graph_staging_resources_table.delete().where(
                and_(
                    graph_staging_resources_table.c.run_id == run_id,
                    graph_staging_resources_table.c.attempt == attempt,
                )
            )
        )

    def delete_staging_resources_for_generation(
        self, *, connection: Connection, generation_id: str
    ) -> None:
        run_ids = (
            connection.execute(
                select(graph_build_runs_table.c.id).where(
                    graph_build_runs_table.c.target_generation_id == generation_id
                )
            )
            .scalars()
            .all()
        )
        if not run_ids:
            return
        connection.execute(
            graph_staging_resources_table.delete().where(
                graph_staging_resources_table.c.run_id.in_(list(run_ids))
            )
        )

    # ------------------------------------------------------------------- usage

    def add_usage(
        self,
        *,
        connection: Connection,
        graph_build_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        expected_version: int,
        primary_model_calls: int,
        provider_calls: int,
        now: datetime,
    ) -> None:
        result = connection.execute(
            update(graph_build_runs_table)
            .where(
                and_(
                    graph_build_runs_table.c.id == graph_build_id,
                    graph_build_runs_table.c.state == "running",
                    graph_build_runs_table.c.current_attempt == attempt,
                    graph_build_runs_table.c.lease_owner == owner,
                    graph_build_runs_table.c.fencing_token == fencing_token,
                    graph_build_runs_table.c.version == expected_version,
                    graph_build_runs_table.c.lease_expires_at_utc > now,
                )
            )
            .values(
                actual_primary_model_calls=graph_build_runs_table.c.actual_primary_model_calls
                + primary_model_calls,
                actual_provider_calls=graph_build_runs_table.c.actual_provider_calls
                + provider_calls,
            )
        )
        if result.rowcount != 1:
            raise PlatformError(
                "graph_build_lease_lost", "The graph run lease or fence no longer matches", {}, 409
            )

    # ------------------------------------------------------------------- audit

    def write_audit(
        self,
        *,
        connection: Connection,
        run_id: str,
        attempt: int | None,
        version: int,
        event_kind: str,
        actor: str,
        failure_class: str | None = None,
        trace_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            graph_build_audit_table.insert().values(
                id=_new_id("graph_audit"),
                run_id=run_id,
                attempt=attempt,
                version=version,
                event_kind=event_kind,
                actor=actor,
                failure_class=failure_class,
                trace_id=trace_id,
                details_json=dict(details or {}),
                created_at_utc=self._now(),
            )
        )

    # --------------------------------------------------------------- idempotency

    def reserve_operation(
        self,
        *,
        connection: Connection,
        operation_id: str,
        kind: str,
        request_hash: str,
    ) -> tuple[str, Mapping[str, Any] | None, datetime | None]:
        """Returns a reservation/replay state, response and reservation generation."""
        now = self._now()
        existing = self._operation_row(connection=connection, operation_id=operation_id)
        if existing is not None:
            return self._resolve_operation(
                connection=connection,
                operation_id=operation_id,
                kind=kind,
                request_hash=request_hash,
                now=now,
                existing=existing,
                allow_reclaim=True,
            )
        if _insert_operation_if_absent(
            connection,
            operation_id=operation_id,
            kind=kind,
            request_hash=request_hash,
            created_at=now,
        ):
            return "created", None, now
        concurrent = self._operation_row(connection=connection, operation_id=operation_id)
        if concurrent is None:
            raise PlatformError(
                "idempotency_key_conflict",
                "The idempotency operation could not be reserved",
                {},
                409,
            )
        return self._resolve_operation(
            connection=connection,
            operation_id=operation_id,
            kind=kind,
            request_hash=request_hash,
            now=now,
            existing=concurrent,
            allow_reclaim=True,
        )

    @staticmethod
    def _operation_row(*, connection: Connection, operation_id: str) -> Mapping[str, Any] | None:
        row = (
            connection.execute(
                select(
                    graph_build_operations_table.c.request_hash,
                    graph_build_operations_table.c.status,
                    graph_build_operations_table.c.response_json,
                    graph_build_operations_table.c.created_at_utc,
                ).where(graph_build_operations_table.c.operation_id == operation_id)
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def _resolve_operation(
        self,
        *,
        connection: Connection,
        operation_id: str,
        kind: str,
        request_hash: str,
        now: datetime,
        existing: Mapping[str, Any],
        allow_reclaim: bool,
    ) -> tuple[str, Mapping[str, Any] | None, datetime | None]:
        if str(existing["request_hash"]) != request_hash:
            raise PlatformError(
                "idempotency_key_conflict",
                "The idempotency key was already used for a different request",
                {},
                409,
            )
        if existing["status"] == "completed" and existing["response_json"] is not None:
            return "replay", dict(existing["response_json"]), None
        created_at = _utc(existing["created_at_utc"])
        if (
            allow_reclaim
            and existing["status"] == "reserved"
            and created_at is not None
            and created_at <= now - _OPERATION_RESERVATION_TTL
        ):
            reclaimed = connection.execute(
                update(graph_build_operations_table)
                .where(
                    and_(
                        graph_build_operations_table.c.operation_id == operation_id,
                        graph_build_operations_table.c.kind == kind,
                        graph_build_operations_table.c.request_hash == request_hash,
                        graph_build_operations_table.c.status == "reserved",
                        graph_build_operations_table.c.created_at_utc
                        <= now - _OPERATION_RESERVATION_TTL,
                    )
                )
                .values(created_at_utc=now)
            )
            if reclaimed.rowcount == 1:
                return "created", None, now
            refreshed = self._operation_row(connection=connection, operation_id=operation_id)
            if refreshed is not None:
                return self._resolve_operation(
                    connection=connection,
                    operation_id=operation_id,
                    kind=kind,
                    request_hash=request_hash,
                    now=now,
                    existing=refreshed,
                    allow_reclaim=False,
                )
        raise PlatformError(
            "idempotency_key_conflict",
            "The idempotency key is already being processed",
            {},
            409,
        )

    def complete_operation(
        self,
        *,
        connection: Connection,
        operation_id: str,
        reservation_created_at: datetime,
        response: Mapping[str, Any],
    ) -> None:
        result = connection.execute(
            update(graph_build_operations_table)
            .where(
                and_(
                    graph_build_operations_table.c.operation_id == operation_id,
                    graph_build_operations_table.c.status == "reserved",
                    graph_build_operations_table.c.created_at_utc == reservation_created_at,
                )
            )
            .values(status="completed", response_json=dict(response))
        )
        if result.rowcount != 1:
            raise PlatformError(
                "idempotency_key_conflict",
                "The idempotency operation could not be completed",
                {},
                409,
            )

    def require_operation_reservation(
        self,
        *,
        connection: Connection,
        operation_id: str,
        reservation_created_at: datetime,
    ) -> None:
        row = (
            connection.execute(
                select(
                    graph_build_operations_table.c.status,
                    graph_build_operations_table.c.created_at_utc,
                )
                .where(graph_build_operations_table.c.operation_id == operation_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        created_at = _utc(row["created_at_utc"]) if row is not None else None
        if row is None or row["status"] != "reserved" or created_at != reservation_created_at:
            raise PlatformError(
                "idempotency_key_conflict",
                "The idempotency operation is no longer reserved",
                {},
                409,
            )

    def verified_activated_receipt(
        self,
        *,
        aggregate_id: str,
        graph_generation_id: str,
        connection: Connection,
    ) -> bool:
        row = (
            connection.execute(
                select(
                    graph_build_runs_table.c.graph_generation_id,
                    graph_build_runs_table.c.activation_receipt_id,
                ).where(graph_build_runs_table.c.id == aggregate_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        return (
            str(row["graph_generation_id"]) == graph_generation_id
            and row["activation_receipt_id"] is not None
        )


__all__ = ["ACTIVE_STATES", "SqlAlchemyGraphRepository", "TERMINAL_STATES"]
