"""Backup/restore orchestration service.

Coordinates one stable `backup_id` across the Postgres snapshot, the
object-storage snapshot and the object manifest, and drives restores through
the fixed stage order of design §2.8 with pre/post gates, per-target
idempotency, invisible isolation with a repair queue, and audit records.

External entry points, operator roles, scheduling and the external restore
protocol are intentionally out of scope for this change (Q2, 2026-08-25);
callers invoke this service programmatically.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, and_, func, select, update

from app.platform.context import current_context
from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError

from .ports import (
    DerivedRebuildPort,
    FactValidationPort,
    ObjectManifestPort,
    ObjectSnapshotPort,
    PostGateValidationPort,
    PostgresBackupPort,
)
from .schema import (
    DERIVED_STAGES,
    FACT_STAGES,
    RESTORE_STAGES,
    backup_components_table,
    backup_objects_table,
    backup_sets_table,
    maintenance_gate_table,
    repair_targets_table,
    restore_sessions_table,
    restore_stages_table,
    restore_targets_table,
)

BACKUP_COMPONENT_KINDS: tuple[str, ...] = (
    "postgres_snapshot",
    "object_store_snapshot",
    "object_manifest",
)

_ACTIVE_RESTORE_STATUSES: tuple[str, ...] = ("accepted", "running")


class _ConcurrentRestoreRejected(Exception):
    """Internal control-flow marker: audit the rejection in a separate
    committed transaction, then surface the public 409."""

    def __init__(self, *, restore_id: str, active: str, occurred_at: datetime) -> None:
        super().__init__("concurrent restore in progress")
        self.restore_id = restore_id
        self.active = active
        self.occurred_at = occurred_at


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class BackupRestoreService:
    def __init__(
        self,
        engine: Any,
        *,
        postgres_backup: PostgresBackupPort,
        object_snapshot: ObjectSnapshotPort,
        object_manifest: ObjectManifestPort,
        fact_validation: FactValidationPort | None = None,
        derived_rebuild: DerivedRebuildPort | None = None,
        post_gate_validation: PostGateValidationPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._postgres_backup = postgres_backup
        self._object_snapshot = object_snapshot
        self._object_manifest = object_manifest
        self._fact_validation = fact_validation
        self._derived_rebuild = derived_rebuild
        self._post_gate_validation = post_gate_validation
        self._now = now or (lambda: datetime.now(UTC))

    def _current_time(self) -> datetime:
        return _utc(self._now())

    # ------------------------------------------------------------------
    # Backup sets
    # ------------------------------------------------------------------

    def create_backup_set(self) -> dict[str, Any]:
        """Create a backup set; the stable backup_id binds all components."""
        backup_id = _new_id("backup")
        now = self._current_time()
        with self._engine.begin() as connection:
            connection.execute(
                backup_sets_table.insert().values(
                    id=backup_id,
                    status="creating",
                    created_at_utc=now,
                )
            )
            for kind in BACKUP_COMPONENT_KINDS:
                connection.execute(
                    backup_components_table.insert().values(
                        id=_new_id("component"),
                        backup_id=backup_id,
                        kind=kind,
                        status="pending",
                        created_at_utc=now,
                    )
                )
            self._audit(
                connection,
                resource_type="backup.set_created",
                resource_id=backup_id,
                result="succeeded",
                occurred_at=now,
            )
        return self.get_backup_set(backup_id)

    def record_manifest_objects(self, backup_id: str, facts: Sequence[Any]) -> dict[str, Any]:
        """Record per-object manifest entries (identity, size, checksum)."""
        now = self._current_time()
        with self._engine.begin() as connection:
            self._require_component(connection, backup_id, "object_manifest")
            for fact in facts:
                metadata = dict(getattr(fact, "metadata", {}) or {})
                connection.execute(
                    backup_objects_table.insert().values(
                        backup_id=backup_id,
                        object_key=str(fact.object_key),
                        size_bytes=int(fact.size_bytes),
                        sha256=str(fact.sha256),
                        metadata_json=metadata,
                    )
                )
            connection.execute(
                update(backup_components_table)
                .where(
                    and_(
                        backup_components_table.c.backup_id == backup_id,
                        backup_components_table.c.kind == "object_manifest",
                    )
                )
                .values(
                    status="succeeded",
                    reference=f"manifest:{len(facts)}",
                    completed_at_utc=now,
                )
            )
            self._maybe_complete_backup_set(connection, backup_id, now)
        return self.get_backup_set(backup_id)

    def complete_snapshot_component(
        self, backup_id: str, *, kind: str, reference: str
    ) -> dict[str, Any]:
        if kind not in ("postgres_snapshot", "object_store_snapshot"):
            raise PlatformError(
                "backup_component_invalid", "Unknown backup component kind", {}, 422
            )
        now = self._current_time()
        with self._engine.begin() as connection:
            self._require_component(connection, backup_id, kind)
            connection.execute(
                update(backup_components_table)
                .where(
                    and_(
                        backup_components_table.c.backup_id == backup_id,
                        backup_components_table.c.kind == kind,
                    )
                )
                .values(status="succeeded", reference=reference, completed_at_utc=now)
            )
            self._maybe_complete_backup_set(connection, backup_id, now)
        return self.get_backup_set(backup_id)

    def fail_component(self, backup_id: str, *, kind: str, reason: str) -> dict[str, Any]:
        now = self._current_time()
        with self._engine.begin() as connection:
            self._require_component(connection, backup_id, kind)
            connection.execute(
                update(backup_components_table)
                .where(
                    and_(
                        backup_components_table.c.backup_id == backup_id,
                        backup_components_table.c.kind == kind,
                    )
                )
                .values(status="failed", failure_reason=reason, completed_at_utc=now)
            )
            connection.execute(
                update(backup_sets_table)
                .where(backup_sets_table.c.id == backup_id)
                .values(status="failed", completed_at_utc=now)
            )
            self._audit(
                connection,
                resource_type="backup.component_failed",
                resource_id=backup_id,
                result="failed",
                details={"kind": kind, "reason": reason},
                occurred_at=now,
            )
        return self.get_backup_set(backup_id)

    def get_backup_set(self, backup_id: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            return self._backup_set_state(connection, backup_id)

    def create_full_backup_set(self) -> dict[str, Any]:
        """Convenience orchestration: snapshot both stores and the manifest."""
        state = self.create_backup_set()
        backup_id = str(state["backup_id"])
        self.complete_snapshot_component(
            backup_id,
            kind="postgres_snapshot",
            reference=self._postgres_backup.snapshot(),
        )
        self.complete_snapshot_component(
            backup_id,
            kind="object_store_snapshot",
            reference=self._object_snapshot.snapshot(),
        )
        return self.record_manifest_objects(backup_id, self._object_manifest.collect_object_facts())

    # ------------------------------------------------------------------
    # Restore sessions and gates
    # ------------------------------------------------------------------

    def start_restore(self, backup_id: str) -> dict[str, Any]:
        """Pre-gates: lock concurrency, close reads, verify components."""
        now = self._current_time()
        try:
            with self._engine.begin() as connection:
                backup = self._backup_set_state_or_none(connection, backup_id)
                if backup is None:
                    raise PlatformError("backup_not_found", "Backup set was not found", {}, 404)
                if backup["status"] != "complete":
                    # Missing components never lead to a partial restore.
                    raise PlatformError(
                        "backup_not_restorable",
                        "Backup set is missing required components",
                        {
                            "backup_id": backup_id,
                            "components": {
                                str(c["kind"]): str(c["status"]) for c in backup["components"]
                            },
                        },
                        409,
                    )
                active = (
                    connection.execute(
                        select(restore_sessions_table.c.id).where(
                            restore_sessions_table.c.status.in_(_ACTIVE_RESTORE_STATUSES)
                        )
                    )
                    .scalars()
                    .all()
                )
                if active:
                    rejected_now = now
                    rejected_active = str(active[0])
                    # The rejection audit must survive the rollback of this
                    # transaction; write it in its own committed transaction.
                    raise _ConcurrentRestoreRejected(
                        restore_id=backup_id, active=rejected_active, occurred_at=rejected_now
                    )
                restore_id = _new_id("restore")
                connection.execute(
                    restore_sessions_table.insert().values(
                        id=restore_id,
                        backup_id=backup_id,
                        status="accepted",
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                for position, stage in enumerate(RESTORE_STAGES):
                    connection.execute(
                        restore_stages_table.insert().values(
                            id=_new_id("stage"),
                            restore_id=restore_id,
                            stage=stage,
                            position=position,
                            status="pending",
                            validated=0,
                        )
                    )
                # Close reads/preview/download and derived index writes; the gate
                # is persisted so workers can read it after a restart.
                gate_updated = connection.execute(
                    update(maintenance_gate_table)
                    .where(maintenance_gate_table.c.id == 1)
                    .values(reads_closed=1, restore_id=restore_id, updated_at_utc=now)
                ).rowcount
                if not gate_updated:
                    connection.execute(
                        maintenance_gate_table.insert().values(
                            id=1,
                            reads_closed=1,
                            restore_id=restore_id,
                            updated_at_utc=now,
                        )
                    )
                self._audit(
                    connection,
                    resource_type="restore.request",
                    resource_id=restore_id,
                    result="succeeded",
                    details={"backup_id": backup_id},
                    occurred_at=now,
                )
            return self.get_restore(restore_id)

        except _ConcurrentRestoreRejected as rejection:
            with self._engine.begin() as connection:
                self._audit(
                    connection,
                    resource_type="restore.request",
                    resource_id=rejection.restore_id,
                    result="failed",
                    details={
                        "reason": "concurrent_restore_in_progress",
                        "active_restore_id": rejection.active,
                    },
                    occurred_at=rejection.occurred_at,
                )
            raise PlatformError(
                "restore_in_progress",
                "Another restore is already active",
                {"active_restore_id": rejection.active},
                409,
            ) from rejection

    def replay_restore(self, restore_id: str) -> dict[str, Any]:
        """Same restore_id replay reuses the first session's state only."""
        state = self.get_restore(restore_id)
        return state

    def get_restore(self, restore_id: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            session = (
                connection.execute(
                    select(restore_sessions_table).where(restore_sessions_table.c.id == restore_id)
                )
                .mappings()
                .one_or_none()
            )
            if session is None:
                raise PlatformError("restore_not_found", "Restore was not found", {}, 404)
            stages = (
                connection.execute(
                    select(restore_stages_table)
                    .where(restore_stages_table.c.restore_id == restore_id)
                    .order_by(restore_stages_table.c.position)
                )
                .mappings()
                .all()
            )
            targets = (
                connection.execute(
                    select(restore_targets_table).where(
                        restore_targets_table.c.restore_id == restore_id
                    )
                )
                .mappings()
                .all()
            )
            repairs = (
                connection.execute(
                    select(repair_targets_table).where(
                        repair_targets_table.c.restore_id == restore_id
                    )
                )
                .mappings()
                .all()
            )
            gate = self._gate_state(connection)
            return {
                "restore_id": restore_id,
                "backup_id": str(session["backup_id"]),
                "status": str(session["status"]),
                "reads_closed": gate["reads_closed"] == 1,
                "stages": [
                    {
                        "stage": str(s["stage"]),
                        "status": str(s["status"]),
                        "validated": bool(s["validated"]),
                    }
                    for s in stages
                ],
                "targets": [
                    {
                        "stage": str(t["stage"]),
                        "resource_id": str(t["resource_id"]),
                        "status": str(t["status"]),
                        "failure_classification": t["failure_classification"],
                        "attempt": int(t["attempt"]),
                    }
                    for t in targets
                ],
                "repair_targets": [
                    {
                        "stage": str(r["stage"]),
                        "resource_id": str(r["resource_id"]),
                        "status": str(r["status"]),
                        "failure_classification": str(r["failure_classification"]),
                        "attempts": int(r["attempts"]),
                    }
                    for r in repairs
                ],
            }

    # ------------------------------------------------------------------
    # Stage execution
    # ------------------------------------------------------------------

    def advance_restore(self, restore_id: str) -> dict[str, Any]:
        """Execute the next runnable stage; stages run in fixed order only."""
        now = self._current_time()
        with self._engine.begin() as connection:
            session = self._locked_session(connection, restore_id)
            status = str(session["status"])
            if status not in _ACTIVE_RESTORE_STATUSES and status != "blocked":
                return self.get_restore(restore_id)
            if status == "blocked":
                # A blocked restore can still retry unfinished repair targets.
                self._retry_repair_targets(connection, restore_id, now)
                if self._has_open_repair_targets(connection, restore_id):
                    return self.get_restore(restore_id)
                connection.execute(
                    update(restore_sessions_table)
                    .where(restore_sessions_table.c.id == restore_id)
                    .values(status="running", updated_at_utc=now)
                )
            stages = (
                connection.execute(
                    select(restore_stages_table)
                    .where(restore_stages_table.c.restore_id == restore_id)
                    .order_by(restore_stages_table.c.position)
                )
                .mappings()
                .all()
            )
            runnable: str | None = None
            for stage in stages:
                stage_status = str(stage["status"])
                if stage_status == "failed":
                    runnable = str(stage["stage"])
                    break
                if stage_status == "running" and str(stage["stage"]) in DERIVED_STAGES:
                    # A worker restart left this derived stage mid-flight; only
                    # its pending/failed targets re-execute.
                    runnable = str(stage["stage"])
                    break
                if stage_status == "pending":
                    # Gate: previous stage must be succeeded AND validated.
                    runnable = str(stage["stage"])
                    break
                # succeeded: continue to next stage
            if runnable is None and all(str(s["status"]) == "succeeded" for s in stages):
                return self._run_post_gate(connection, restore_id, now)
            if runnable is None:
                return self.get_restore(restore_id)
            current_status = next(str(s["status"]) for s in stages if str(s["stage"]) == runnable)
            if current_status == "failed" and runnable in FACT_STAGES:
                # A fact stage failed on consistency mismatches: never re-run
                # the snapshot restore; retry the repair targets instead.
                self._retry_repair_targets(connection, restore_id, now)
                if not self._has_open_repair_targets(connection, restore_id):
                    self._complete_stage(connection, restore_id, runnable, now, validated=True)
                else:
                    connection.execute(
                        update(restore_sessions_table)
                        .where(restore_sessions_table.c.id == restore_id)
                        .values(status="blocked", updated_at_utc=now)
                    )
                return self.get_restore(restore_id)
            connection.execute(
                update(restore_sessions_table)
                .where(restore_sessions_table.c.id == restore_id)
                .values(status="running", updated_at_utc=now)
            )
            self._execute_stage(connection, restore_id, runnable, now)
            refreshed = (
                connection.execute(
                    select(restore_stages_table.c.status).where(
                        restore_stages_table.c.restore_id == restore_id
                    )
                )
                .scalars()
                .all()
            )
            if refreshed and all(s == "succeeded" for s in refreshed):
                return self._run_post_gate(connection, restore_id, now)
            return self.get_restore(restore_id)

    def _execute_stage(
        self, connection: Connection, restore_id: str, stage: str, now: datetime
    ) -> None:
        connection.execute(
            update(restore_stages_table)
            .where(
                and_(
                    restore_stages_table.c.restore_id == restore_id,
                    restore_stages_table.c.stage == stage,
                )
            )
            .values(status="running", started_at_utc=now)
        )
        self._audit(
            connection,
            resource_type="restore.stage_started",
            resource_id=restore_id,
            result="succeeded",
            details={"stage": stage},
            occurred_at=now,
        )
        if stage == "postgres":
            backup_id = connection.execute(
                select(restore_sessions_table.c.backup_id).where(
                    restore_sessions_table.c.id == restore_id
                )
            ).scalar_one()
            reference = self._component_reference(connection, backup_id, "postgres_snapshot")
            self._postgres_backup.restore(reference)
            self._complete_stage(connection, restore_id, stage, now, validated=True)
            return
        if stage == "object_store":
            backup_id = connection.execute(
                select(restore_sessions_table.c.backup_id).where(
                    restore_sessions_table.c.id == restore_id
                )
            ).scalar_one()
            reference = self._component_reference(connection, backup_id, "object_store_snapshot")
            self._object_snapshot.restore(reference)
            self._validate_facts(connection, restore_id, now)
            mismatched = self._has_open_repair_targets(connection, restore_id)
            self._complete_stage(connection, restore_id, stage, now, validated=not mismatched)
            return
        # Derived rebuild stages.
        if self._derived_rebuild is None:
            raise PlatformError(
                "restore_rebuild_unavailable",
                "No derived rebuild port is configured",
                {"stage": stage},
                500,
            )
        resource_ids = self._derived_rebuild.list_resources(stage)
        for resource_id in resource_ids:
            self._upsert_target(connection, restore_id, stage, resource_id, now)
        self._execute_targets(connection, restore_id, stage, now)
        failed = connection.execute(
            select(func.count())
            .select_from(restore_targets_table)
            .where(
                and_(
                    restore_targets_table.c.restore_id == restore_id,
                    restore_targets_table.c.stage == stage,
                    restore_targets_table.c.status == "failed",
                )
            )
        ).scalar_one()
        self._complete_stage(connection, restore_id, stage, now, validated=failed == 0)

    def _complete_stage(
        self,
        connection: Connection,
        restore_id: str,
        stage: str,
        now: datetime,
        *,
        validated: bool,
    ) -> None:
        status = "succeeded" if validated else "failed"
        connection.execute(
            update(restore_stages_table)
            .where(
                and_(
                    restore_stages_table.c.restore_id == restore_id,
                    restore_stages_table.c.stage == stage,
                )
            )
            .values(status=status, validated=int(validated), completed_at_utc=now)
        )
        self._audit(
            connection,
            resource_type=f"restore.stage_{status}",
            resource_id=restore_id,
            result=status,
            details={"stage": stage},
            occurred_at=now,
        )

    def _execute_targets(
        self, connection: Connection, restore_id: str, stage: str, now: datetime
    ) -> None:
        if self._derived_rebuild is None:
            raise PlatformError(
                "restore_rebuild_unavailable",
                "No derived rebuild port is configured",
                {"stage": stage},
                500,
            )
        rebuild = self._derived_rebuild
        targets = (
            connection.execute(
                select(restore_targets_table).where(
                    and_(
                        restore_targets_table.c.restore_id == restore_id,
                        restore_targets_table.c.stage == stage,
                        restore_targets_table.c.status.in_(("pending", "failed")),
                    )
                )
            )
            .mappings()
            .all()
        )
        for target in targets:
            # Succeeded targets are skipped: no repeated side effects.
            resource_id = str(target["resource_id"])
            lease_token = _new_id("lease")
            next_epoch = int(target["fencing_epoch"]) + 1
            connection.execute(
                update(restore_targets_table)
                .where(restore_targets_table.c.id == str(target["id"]))
                .values(
                    status="running",
                    attempt=int(target["attempt"]) + 1,
                    fencing_epoch=next_epoch,
                    lease_token=lease_token,
                    next_retry_at_utc=None,
                    updated_at_utc=now,
                )
            )
            try:
                rebuild.rebuild(stage, resource_id)
            except Exception as exc:  # noqa: BLE001 - classification is per target
                connection.execute(
                    update(restore_targets_table)
                    .where(
                        and_(
                            restore_targets_table.c.restore_id == restore_id,
                            restore_targets_table.c.stage == stage,
                            restore_targets_table.c.resource_id == resource_id,
                        )
                    )
                    .values(
                        status="failed",
                        failure_classification=type(exc).__name__,
                        lease_token=None,
                        next_retry_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                self._open_repair_target(
                    connection,
                    restore_id=restore_id,
                    stage=stage,
                    resource_id=resource_id,
                    classification=type(exc).__name__,
                    detail=str(exc) or type(exc).__name__,
                    now=now,
                )
            else:
                connection.execute(
                    update(restore_targets_table)
                    .where(
                        and_(
                            restore_targets_table.c.restore_id == restore_id,
                            restore_targets_table.c.stage == stage,
                            restore_targets_table.c.resource_id == resource_id,
                        )
                    )
                    .values(
                        status="succeeded",
                        lease_token=None,
                        completed_at_utc=now,
                        updated_at_utc=now,
                    )
                )

    def retry_target(self, restore_id: str, *, stage: str, resource_id: str) -> dict[str, Any]:
        """Retry one unfinished target only; completed targets are no-ops."""
        now = self._current_time()
        with self._engine.begin() as connection:
            self._locked_session(connection, restore_id)
            target = (
                connection.execute(
                    select(restore_targets_table).where(
                        and_(
                            restore_targets_table.c.restore_id == restore_id,
                            restore_targets_table.c.stage == stage,
                            restore_targets_table.c.resource_id == resource_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise PlatformError(
                    "restore_target_not_found", "Restore target was not found", {}, 404
                )
            if str(target["status"]) == "succeeded":
                return self.get_restore(restore_id)
            if self._derived_rebuild is None:
                raise PlatformError(
                    "restore_rebuild_unavailable",
                    "No derived rebuild port is configured",
                    {"stage": stage},
                    500,
                )
            lease_token = _new_id("lease")
            connection.execute(
                update(restore_targets_table)
                .where(restore_targets_table.c.id == str(target["id"]))
                .values(
                    status="running",
                    attempt=int(target["attempt"]) + 1,
                    fencing_epoch=int(target["fencing_epoch"]) + 1,
                    lease_token=lease_token,
                    next_retry_at_utc=None,
                    updated_at_utc=now,
                )
            )
            try:
                self._derived_rebuild.rebuild(stage, resource_id)
            except Exception as exc:  # noqa: BLE001
                connection.execute(
                    update(restore_targets_table)
                    .where(restore_targets_table.c.id == str(target["id"]))
                    .values(
                        status="failed",
                        failure_classification=type(exc).__name__,
                        lease_token=None,
                        next_retry_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                self._open_repair_target(
                    connection,
                    restore_id=restore_id,
                    stage=stage,
                    resource_id=resource_id,
                    classification=type(exc).__name__,
                    detail=str(exc) or type(exc).__name__,
                    now=now,
                )
            else:
                connection.execute(
                    update(restore_targets_table)
                    .where(restore_targets_table.c.id == str(target["id"]))
                    .values(
                        status="succeeded",
                        lease_token=None,
                        completed_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                self._resolve_repair_target(connection, restore_id, stage, resource_id, now)
        return self.get_restore(restore_id)

    # ------------------------------------------------------------------
    # Fact validation and repair queue
    # ------------------------------------------------------------------

    def _validate_facts(self, connection: Connection, restore_id: str, now: datetime) -> None:
        """Compare manifest/Postgres records against restored objects.

        Mismatches keep the affected resources invisible and open repair
        targets; the other fact source is never rolled back or overwritten.
        """
        if self._fact_validation is None:
            return
        expected = {f.object_key: f for f in self._fact_validation.expected_object_facts()}
        actual = {f.object_key: f for f in self._fact_validation.actual_object_facts()}
        for object_key, fact in expected.items():
            observed = actual.get(object_key)
            if observed is None:
                self._open_repair_target(
                    connection,
                    restore_id=restore_id,
                    stage="object_store",
                    resource_id=object_key,
                    classification="object_missing",
                    detail=f"object {object_key} missing from object storage",
                    now=now,
                )
            elif int(observed.size_bytes) != int(fact.size_bytes) or str(observed.sha256) != str(
                fact.sha256
            ):
                self._open_repair_target(
                    connection,
                    restore_id=restore_id,
                    stage="object_store",
                    resource_id=object_key,
                    classification="object_checksum_mismatch",
                    detail=(
                        f"object {object_key} size/sha256 mismatch "
                        f"(expected {fact.size_bytes}/{fact.sha256}, "
                        f"actual {observed.size_bytes}/{observed.sha256})"
                    ),
                    now=now,
                )
        for object_key in actual.keys() - expected.keys():
            self._open_repair_target(
                connection,
                restore_id=restore_id,
                stage="object_store",
                resource_id=object_key,
                classification="object_orphan",
                detail=f"object {object_key} absent from manifest records",
                now=now,
            )
        # Object keys that now agree resolve their open repair targets.
        consistent = {
            key
            for key, fact in expected.items()
            if key in actual
            and int(actual[key].size_bytes) == int(fact.size_bytes)
            and str(actual[key].sha256) == str(fact.sha256)
        }
        for object_key in consistent:
            self._resolve_repair_target(connection, restore_id, "object_store", object_key, now)

    def retry_repair_target(
        self, restore_id: str, *, stage: str, resource_id: str
    ) -> dict[str, Any]:
        """Retry a fact-consistency repair target; rejection preconditions of
        version restore never appear here."""
        now = self._current_time()
        with self._engine.begin() as connection:
            self._locked_session(connection, restore_id)
            repair = (
                connection.execute(
                    select(repair_targets_table).where(
                        and_(
                            repair_targets_table.c.restore_id == restore_id,
                            repair_targets_table.c.stage == stage,
                            repair_targets_table.c.resource_id == resource_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if repair is None:
                raise PlatformError(
                    "repair_target_not_found", "Repair target was not found", {}, 404
                )
            if str(repair["status"]) == "succeeded":
                return self.get_restore(restore_id)
            connection.execute(
                update(repair_targets_table)
                .where(repair_targets_table.c.id == str(repair["id"]))
                .values(attempts=int(repair["attempts"]) + 1, updated_at_utc=now)
            )
            self._audit(
                connection,
                resource_type="restore.repair_retry",
                resource_id=restore_id,
                result="succeeded",
                details={"stage": stage, "resource_id": resource_id},
                occurred_at=now,
            )
            # The repair retried by re-running fact validation: if the sources
            # now agree the target resolves; otherwise it stays open.
            self._validate_facts(connection, restore_id, now)
            resolved = (
                connection.execute(
                    select(func.count())
                    .select_from(repair_targets_table)
                    .where(
                        and_(
                            repair_targets_table.c.restore_id == restore_id,
                            repair_targets_table.c.stage == stage,
                            repair_targets_table.c.resource_id == resource_id,
                            repair_targets_table.c.status == "open",
                        )
                    )
                ).scalar_one()
                == 0
            )
            if resolved:
                self._resolve_repair_target(connection, restore_id, stage, resource_id, now)
        return self.get_restore(restore_id)

    def _open_repair_target(
        self,
        connection: Connection,
        *,
        restore_id: str,
        stage: str,
        resource_id: str,
        classification: str,
        detail: str,
        now: datetime,
    ) -> None:
        existing = connection.execute(
            select(repair_targets_table.c.id).where(
                and_(
                    repair_targets_table.c.restore_id == restore_id,
                    repair_targets_table.c.stage == stage,
                    repair_targets_table.c.resource_id == resource_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        connection.execute(
            repair_targets_table.insert().values(
                id=_new_id("repair"),
                restore_id=restore_id,
                stage=stage,
                resource_id=resource_id,
                failure_classification=classification,
                detail=detail,
                status="open",
                attempts=0,
                next_retry_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        self._audit(
            connection,
            resource_type="restore.fact_mismatch",
            resource_id=restore_id,
            result="failed",
            details={
                "stage": stage,
                "resource_id": resource_id,
                "classification": classification,
            },
            occurred_at=now,
        )

    def _resolve_repair_target(
        self,
        connection: Connection,
        restore_id: str,
        stage: str,
        resource_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            update(repair_targets_table)
            .where(
                and_(
                    repair_targets_table.c.restore_id == restore_id,
                    repair_targets_table.c.stage == stage,
                    repair_targets_table.c.resource_id == resource_id,
                )
            )
            .values(status="succeeded", resolved_at_utc=now, updated_at_utc=now)
        )

    def _retry_repair_targets(self, connection: Connection, restore_id: str, now: datetime) -> None:
        if self._fact_validation is None:
            return
        self._validate_facts(connection, restore_id, now)
        for repair in (
            connection.execute(
                select(repair_targets_table).where(
                    and_(
                        repair_targets_table.c.restore_id == restore_id,
                        repair_targets_table.c.status == "open",
                    )
                )
            )
            .mappings()
            .all()
        ):
            still_open = connection.execute(
                select(repair_targets_table.c.id).where(
                    and_(
                        repair_targets_table.c.id == str(repair["id"]),
                        repair_targets_table.c.status == "open",
                    )
                )
            ).scalar_one_or_none()
            if still_open is None:
                continue
            self._audit(
                connection,
                resource_type="restore.repair_retry",
                resource_id=restore_id,
                result="succeeded",
                details={
                    "stage": str(repair["stage"]),
                    "resource_id": str(repair["resource_id"]),
                },
                occurred_at=now,
            )

    def _has_open_repair_targets(self, connection: Connection, restore_id: str) -> bool:
        return (
            connection.execute(
                select(func.count())
                .select_from(repair_targets_table)
                .where(
                    and_(
                        repair_targets_table.c.restore_id == restore_id,
                        repair_targets_table.c.status == "open",
                    )
                )
            ).scalar_one()
            > 0
        )

    # ------------------------------------------------------------------
    # Post gate
    # ------------------------------------------------------------------

    def _run_post_gate(
        self, connection: Connection, restore_id: str, now: datetime
    ) -> dict[str, Any]:
        blocking: list[str] = []
        if self._post_gate_validation is not None:
            blocking = list(self._post_gate_validation.validate_post_gate())
        if self._has_open_repair_targets(connection, restore_id):
            blocking.append("repair_targets_open")
        if blocking:
            connection.execute(
                update(restore_sessions_table)
                .where(restore_sessions_table.c.id == restore_id)
                .values(
                    status="blocked",
                    failure_reason=";".join(blocking),
                    updated_at_utc=now,
                )
            )
            self._audit(
                connection,
                resource_type="restore.blocked",
                resource_id=restore_id,
                result="failed",
                details={"blocking": blocking},
                occurred_at=now,
            )
            return self.get_restore(restore_id)
        # All gates passed: reopen reads/preview/download and derived writes.
        connection.execute(
            update(maintenance_gate_table)
            .where(maintenance_gate_table.c.id == 1)
            .values(reads_closed=0, restore_id=None, updated_at_utc=now)
        )
        connection.execute(
            update(restore_sessions_table)
            .where(restore_sessions_table.c.id == restore_id)
            .values(status="completed", completed_at_utc=now, updated_at_utc=now)
        )
        self._audit(
            connection,
            resource_type="restore.completed",
            resource_id=restore_id,
            result="succeeded",
            details={},
            occurred_at=now,
        )
        return self.get_restore(restore_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def reads_closed(self) -> bool:
        """Whether reads/preview/download/derived writes are closed."""
        with self._engine.connect() as connection:
            return self._gate_state(connection)["reads_closed"] == 1

    def _gate_state(self, connection: Connection) -> dict[str, Any]:
        row = (
            connection.execute(
                select(maintenance_gate_table).where(maintenance_gate_table.c.id == 1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return {"reads_closed": 0, "restore_id": None}
        return {
            "reads_closed": int(row["reads_closed"]),
            "restore_id": row["restore_id"],
        }

    def _upsert_target(
        self,
        connection: Connection,
        restore_id: str,
        stage: str,
        resource_id: str,
        now: datetime,
    ) -> None:
        existing = connection.execute(
            select(restore_targets_table.c.id).where(
                and_(
                    restore_targets_table.c.restore_id == restore_id,
                    restore_targets_table.c.stage == stage,
                    restore_targets_table.c.resource_id == resource_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        connection.execute(
            restore_targets_table.insert().values(
                id=_new_id("target"),
                restore_id=restore_id,
                stage=stage,
                resource_id=resource_id,
                status="pending",
                attempt=0,
                fencing_epoch=0,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )

    def _require_component(self, connection: Connection, backup_id: str, kind: str) -> None:
        row = connection.execute(
            select(backup_components_table.c.id).where(
                and_(
                    backup_components_table.c.backup_id == backup_id,
                    backup_components_table.c.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise PlatformError(
                "backup_not_found", "Backup set or component was not found", {}, 404
            )

    def _component_reference(self, connection: Connection, backup_id: str, kind: str) -> str:
        reference = connection.execute(
            select(backup_components_table.c.reference).where(
                and_(
                    backup_components_table.c.backup_id == backup_id,
                    backup_components_table.c.kind == kind,
                )
            )
        ).scalar_one()
        return str(reference)

    def _maybe_complete_backup_set(
        self, connection: Connection, backup_id: str, now: datetime
    ) -> None:
        components = (
            connection.execute(
                select(backup_components_table.c.status, backup_components_table.c.kind).where(
                    backup_components_table.c.backup_id == backup_id
                )
            )
            .mappings()
            .all()
        )
        if not components or any(str(c["status"]) != "succeeded" for c in components):
            # Missing any component: the backup set cannot be complete.
            return
        connection.execute(
            update(backup_sets_table)
            .where(backup_sets_table.c.id == backup_id)
            .values(status="complete", completed_at_utc=now)
        )

    def _backup_set_state_or_none(
        self, connection: Connection, backup_id: str
    ) -> dict[str, Any] | None:
        row = (
            connection.execute(select(backup_sets_table).where(backup_sets_table.c.id == backup_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        components = (
            connection.execute(
                select(backup_components_table).where(
                    backup_components_table.c.backup_id == backup_id
                )
            )
            .mappings()
            .all()
        )
        objects = connection.execute(
            select(
                backup_objects_table.c.object_key,
                backup_objects_table.c.size_bytes,
                backup_objects_table.c.sha256,
            )
            .where(backup_objects_table.c.backup_id == backup_id)
            .order_by(backup_objects_table.c.object_key)
        ).mappings()
        return {
            "backup_id": backup_id,
            "status": str(row["status"]),
            "components": [
                {
                    "kind": str(c["kind"]),
                    "status": str(c["status"]),
                    "reference": c["reference"],
                }
                for c in components
            ],
            "object_count": len(objects.all()),
        }

    def _backup_set_state(self, connection: Connection, backup_id: str) -> dict[str, Any]:
        state = self._backup_set_state_or_none(connection, backup_id)
        if state is None:
            raise PlatformError("backup_not_found", "Backup set was not found", {}, 404)
        return state

    def _locked_session(self, connection: Connection, restore_id: str) -> Any:
        session = (
            connection.execute(
                select(restore_sessions_table)
                .where(restore_sessions_table.c.id == restore_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if session is None:
            raise PlatformError("restore_not_found", "Restore was not found", {}, 404)
        return session

    @staticmethod
    def _audit(
        connection: Connection,
        *,
        resource_type: str,
        resource_id: str,
        result: str,
        occurred_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id="backup_orchestration",
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=context.request_id if context is not None else "req_backup",
                occurred_at_utc=occurred_at,
                result=result,
                details_json=details or {},
            )
        )
