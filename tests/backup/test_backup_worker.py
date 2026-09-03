"""Contract tests for the resident backup maintenance worker (Q5/Q6/Q7)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.backup.ops_service import BackupOpsService
from app.backup.ports import ObjectFact
from app.backup.schema import (
    backup_cleanup_targets_table,
    backup_components_table,
    backup_metadata,
    backup_policy_table,
    backup_schedule_occurrences_table,
    backup_sets_table,
    backup_write_gate_table,
    repair_targets_table,
    restore_sessions_table,
)
from app.backup.service import BackupRestoreService
from app.backup.worker import BackupMaintenanceWorker
from app.backup.write_gate import BackupWriteGateReader, InFlightWriteTracker
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyLeaseStore,
    platform_audit_table,
    platform_lease_table,
)
from app.platform.runtime import PlatformRuntime
from app.platform.worker import WorkerRuntime
from tests._support import build_engine, make_settings

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)  # a Tuesday

_KEY_COUNTER = itertools.count(1)


def _key() -> str:
    return f"k-{next(_KEY_COUNTER)}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _FakePostgresBackup:
    def __init__(self, engine) -> None:
        self._engine = engine
        self.gate_states: list[str] = []
        self.deleted: list[str] = []
        self.restored: list[str] = []
        self.fail_snapshot = False
        self.fail_delete = False
        self.snapshots = 0

    def snapshot(self) -> str:
        if self.fail_snapshot:
            raise RuntimeError("postgres snapshot boom")
        self.snapshots += 1
        # Record the persisted gate state observed while snapshotting: the
        # snapshot protocol must only ever run under a closed gate.
        with self._engine.connect() as connection:
            state = connection.execute(
                select(backup_write_gate_table.c.state).where(backup_write_gate_table.c.id == 1)
            ).scalar_one()
        self.gate_states.append(str(state))
        return f"pgsnap-{self.snapshots}"

    def restore(self, reference: str) -> None:
        self.restored.append(reference)

    def delete(self, reference: str) -> None:
        if self.fail_delete:
            raise RuntimeError("postgres delete boom")
        self.deleted.append(reference)


class _FakeObjectSnapshot:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.restored: list[str] = []
        self.fail_snapshot = False
        self.fail_delete = False
        self.snapshots = 0

    def snapshot(self) -> str:
        if self.fail_snapshot:
            raise RuntimeError("object snapshot boom")
        self.snapshots += 1
        return f"objsnap-{self.snapshots}"

    def restore(self, reference: str) -> None:
        self.restored.append(reference)

    def delete(self, reference: str) -> None:
        if self.fail_delete:
            raise RuntimeError("object delete boom")
        self.deleted.append(reference)


class _Manifest:
    def __init__(self, facts: list[ObjectFact]) -> None:
        self._facts = facts

    def collect_object_facts(self) -> list[ObjectFact]:
        return list(self._facts)


class _FactValidation:
    def __init__(self) -> None:
        self.expected: list[ObjectFact] = []
        self.actual: list[ObjectFact] = []

    def expected_object_facts(self) -> list[ObjectFact]:
        return list(self.expected)

    def actual_object_facts(self) -> list[ObjectFact]:
        return list(self.actual)


class _DerivedRebuild:
    def __init__(self) -> None:
        self.resources: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str]] = []

    def list_resources(self, stage: str) -> list[str]:
        return list(self.resources.get(stage, []))

    def rebuild(self, stage: str, resource_id: str) -> None:
        self.calls.append((stage, resource_id))


class _PostGate:
    def __init__(self, blocking: list[str] | None = None) -> None:
        self.blocking = blocking or []

    def validate_post_gate(self) -> list[str]:
        return list(self.blocking)


class _DrainAfterReadsTracker(InFlightWriteTracker):
    """Simulates in-flight writes draining while the gate is closing: the
    first `reads_before_zero` reads report one write, then it is gone."""

    def __init__(self, reads_before_zero: int) -> None:
        super().__init__()
        self._reads_before_zero = reads_before_zero
        self.reads = 0

    @property
    def in_flight(self) -> int:
        self.reads += 1
        if self._reads_before_zero > 0:
            self._reads_before_zero -= 1
            return 1
        return 0


@pytest.fixture()
def env():
    engine = build_engine()
    backup_metadata.create_all(engine)
    clock = _Clock(NOW)
    postgres = _FakePostgresBackup(engine)
    objects = _FakeObjectSnapshot()
    manifest = _Manifest([])
    facts = _FactValidation()
    derived = _DerivedRebuild()
    post_gate = _PostGate()
    backup_service = BackupRestoreService(
        engine,
        postgres_backup=postgres,
        object_snapshot=objects,
        object_manifest=manifest,
        fact_validation=facts,
        derived_rebuild=derived,
        post_gate_validation=post_gate,
        now=clock,
    )
    gate_reader = BackupWriteGateReader(engine, cache_ttl_seconds=0)
    ops = BackupOpsService(
        engine,
        backup_service=backup_service,
        write_gate_reader=gate_reader,
        now=clock,
    )
    tracker = InFlightWriteTracker()
    runtime = PlatformRuntime(settings=make_settings(), adapters={"database_engine": engine})
    worker_runtime = WorkerRuntime(
        runtime=runtime,
        leases=SqlAlchemyLeaseStore(engine, SqlAlchemyDatabaseClock(engine)),
        now=clock,
        owns_runtime=False,
    )
    yield {
        "engine": engine,
        "clock": clock,
        "postgres": postgres,
        "objects": objects,
        "manifest": manifest,
        "facts": facts,
        "derived": derived,
        "post_gate": post_gate,
        "backup_service": backup_service,
        "ops": ops,
        "tracker": tracker,
        "worker_runtime": worker_runtime,
        "runtime": runtime,
    }
    runtime.close()


def _worker(env, **overrides) -> BackupMaintenanceWorker:
    kwargs = {
        "ops_service": env["ops"],
        "backup_service": env["backup_service"],
        "engine": env["engine"],
        "postgres_backup": env["postgres"],
        "object_snapshot": env["objects"],
        "object_manifest": env["manifest"],
        "write_tracker": env["tracker"],
        "gate_settle_seconds": 0.0,
        "gate_drain_timeout_seconds": 30.0,
        "retention_batch_limit": 50,
        "drain_poll_seconds": 0.01,
        "now": env["clock"],
    }
    kwargs.update(overrides)
    return BackupMaintenanceWorker(env["worker_runtime"], **kwargs)


def _tick(env, worker: BackupMaintenanceWorker):
    # Leases are TTL-based; clearing the table simulates lease expiry between
    # maintenance iterations without sleeping for a minute.
    with env["engine"].begin() as connection:
        connection.execute(delete(platform_lease_table))
    return worker.run_once(owner="test-worker")


def _set_policy(env, **overrides) -> None:
    values = {
        "enabled": 1,
        "frequency": "daily",
        "local_time": "02:00",
        "weekdays": [],
        "timezone": "UTC",
        "keep_last": 7,
        "retention_days": 30,
        "updated_at": NOW - timedelta(days=1),
        "last_scheduled_for": None,
    }
    values.update(overrides)
    with env["engine"].begin() as connection:
        connection.execute(delete(backup_policy_table))
        connection.execute(
            backup_policy_table.insert().values(
                id=1,
                enabled=int(values["enabled"]),
                frequency=values["frequency"],
                local_time=values["local_time"],
                weekdays=list(values["weekdays"]),
                timezone=values["timezone"],
                keep_last=values["keep_last"],
                retention_days=values["retention_days"],
                version=1,
                last_scheduled_for_utc=values["last_scheduled_for"],
                last_outcome=None,
                updated_by=None,
                updated_at_utc=values["updated_at"],
            )
        )


def _set_gate(env, state: str, backup_id: str | None) -> None:
    with env["engine"].begin() as connection:
        connection.execute(delete(backup_write_gate_table))
        connection.execute(
            backup_write_gate_table.insert().values(
                id=1, state=state, backup_id=backup_id, updated_at_utc=env["clock"]()
            )
        )


def _create_backup(env) -> str:
    _, payload = env["ops"].create_backup(
        operator_user_id="op-1", idempotency_key=_key(), request_hash=_key()
    )
    return str(payload["backup_id"])


def _complete_backup(env, backup_id: str, *, completed_at: datetime | None = None) -> None:
    service = env["backup_service"]
    service.complete_snapshot_component(
        backup_id, kind="postgres_snapshot", reference=f"pg-{backup_id}"
    )
    service.complete_snapshot_component(
        backup_id, kind="object_store_snapshot", reference=f"obj-{backup_id}"
    )
    service.record_manifest_objects(backup_id, [])
    if completed_at is not None:
        with env["engine"].begin() as connection:
            connection.execute(
                update(backup_sets_table)
                .where(backup_sets_table.c.id == backup_id)
                .values(completed_at_utc=completed_at)
            )


def _start_restore(env, backup_id: str) -> str:
    _, payload = env["ops"].start_restore(
        operator_user_id="op-1",
        backup_id=backup_id,
        idempotency_key=_key(),
        request_hash=_key(),
    )
    return str(payload["restore_id"])


def _audit_rows(env) -> list[tuple[str, str, str, str]]:
    with env["engine"].connect() as connection:
        return [
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in connection.execute(
                select(
                    platform_audit_table.c.actor_id,
                    platform_audit_table.c.resource_type,
                    platform_audit_table.c.resource_id,
                    platform_audit_table.c.result,
                )
            ).all()
        ]


def _audit_details(env, resource_type: str) -> list[dict]:
    with env["engine"].connect() as connection:
        return [
            dict(row["details_json"])
            for row in connection.execute(
                select(platform_audit_table.c.details_json).where(
                    platform_audit_table.c.resource_type == resource_type
                )
            ).mappings()
        ]


def _occurrences(env) -> list[dict]:
    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(backup_schedule_occurrences_table).order_by(
                    backup_schedule_occurrences_table.c.scheduled_for_utc
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _gate_state(env) -> dict | None:
    with env["engine"].connect() as connection:
        row = (
            connection.execute(
                select(backup_write_gate_table).where(backup_write_gate_table.c.id == 1)
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _policy(env) -> dict:
    with env["engine"].connect() as connection:
        row = (
            connection.execute(select(backup_policy_table).where(backup_policy_table.c.id == 1))
            .mappings()
            .one()
        )
    return dict(row)


def _backup_row(env, backup_id: str) -> dict:
    with env["engine"].connect() as connection:
        row = (
            connection.execute(select(backup_sets_table).where(backup_sets_table.c.id == backup_id))
            .mappings()
            .one()
        )
    return dict(row)


def _components(env, backup_id: str) -> dict[str, tuple[str, str | None, str | None]]:
    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(
                    backup_components_table.c.kind,
                    backup_components_table.c.status,
                    backup_components_table.c.reference,
                    backup_components_table.c.failure_reason,
                ).where(backup_components_table.c.backup_id == backup_id)
            )
            .mappings()
            .all()
        )
    return {str(r["kind"]): (str(r["status"]), r["reference"], r["failure_reason"]) for r in rows}


def _cleanup_targets(env) -> list[dict]:
    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(backup_cleanup_targets_table).order_by(
                    backup_cleanup_targets_table.c.created_at_utc
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _backup_ids(env) -> list[str]:
    with env["engine"].connect() as connection:
        return [
            str(row) for row in connection.execute(select(backup_sets_table.c.id)).scalars().all()
        ]


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_schedule_disabled_policy_does_not_trigger(env) -> None:
    worker = _worker(env)
    # No policy row at all: defaults are disabled.
    stats = _tick(env, worker)
    assert (stats.completed, stats.deferred) == (4, 0)
    assert _occurrences(env) == []
    assert _backup_ids(env) == []

    _set_policy(env, enabled=0)
    stats = _tick(env, worker)
    assert (stats.completed, stats.deferred) == (4, 0)
    assert _occurrences(env) == []
    assert _backup_ids(env) == []


def test_schedule_due_window_claimed_and_creates_backup(env) -> None:
    _set_policy(env)  # daily 02:00 UTC, materialized a day ago
    stats = _tick(env, _worker(env))
    assert (stats.completed, stats.deferred) == (4, 0)

    occurrences = _occurrences(env)
    assert len(occurrences) == 1
    expected_window = datetime(2026, 8, 25, 2, 0, 0, tzinfo=UTC)
    assert _aware(occurrences[0]["scheduled_for_utc"]) == expected_window
    assert occurrences[0]["outcome"] == "executed"
    backup_id = occurrences[0]["backup_id"]
    assert backup_id is not None
    # The execute task ran it to completion within the same tick.
    assert _backup_row(env, backup_id)["status"] == "complete"

    policy = _policy(env)
    assert _aware(policy["last_scheduled_for_utc"]) == expected_window
    assert policy["last_outcome"] == "executed"
    assert (
        "backup_scheduler",
        "backup_schedule.triggered",
        backup_id,
        "succeeded",
    ) in _audit_rows(env)


def test_schedule_window_claim_is_idempotent(env) -> None:
    _set_policy(env)
    worker = _worker(env)
    _tick(env, worker)
    _tick(env, worker)
    assert len(_occurrences(env)) == 1
    assert len(_backup_ids(env)) == 1


def test_schedule_skips_when_backup_active(env) -> None:
    manual_backup_id = _create_backup(env)
    _set_policy(env)
    _tick(env, _worker(env))

    occurrences = _occurrences(env)
    assert len(occurrences) == 1
    assert occurrences[0]["outcome"] == "skipped_active_backup"
    assert occurrences[0]["backup_id"] is None
    # No second backup set was created for the schedule window.
    assert _backup_ids(env) == [manual_backup_id]

    policy = _policy(env)
    assert policy["last_outcome"] == "skipped_active_backup"
    assert policy["last_scheduled_for_utc"] is not None
    assert (
        "backup_scheduler",
        "backup_schedule.skipped_active_backup",
        str(occurrences[0]["id"]),
        "skipped",
    ) in _audit_rows(env)


def test_schedule_only_reruns_the_latest_missed_window(env) -> None:
    _set_policy(env, updated_at=NOW - timedelta(days=3))
    _tick(env, _worker(env))

    occurrences = _occurrences(env)
    assert [_aware(row["scheduled_for_utc"]) for row in occurrences] == [
        datetime(2026, 8, 23, 2, 0, tzinfo=UTC),
        datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
        datetime(2026, 8, 25, 2, 0, tzinfo=UTC),
    ]
    assert [row["outcome"] for row in occurrences] == [
        "skipped_missed",
        "skipped_missed",
        "executed",
    ]
    # Exactly one backup was created: the earlier missed windows never re-run.
    assert len(_backup_ids(env)) == 1
    policy = _policy(env)
    assert _aware(policy["last_scheduled_for_utc"]) == datetime(2026, 8, 25, 2, 0, tzinfo=UTC)
    assert policy["last_outcome"] == "executed"
    rows = _audit_rows(env)
    skipped = [row for row in rows if row[1] == "backup_schedule.skipped_missed"]
    assert len(skipped) == 2
    assert all(row[0] == "backup_scheduler" and row[3] == "skipped" for row in skipped)


def test_schedule_weekly_windows_use_policy_timezone(env) -> None:
    # Weekly on Tuesdays (weekday 1) at 02:00 Asia/Shanghai = 18:00 UTC the
    # previous day; NOW is Tuesday 2026-08-25 12:00 UTC.
    _set_policy(
        env,
        frequency="weekly",
        weekdays=[1],
        timezone="Asia/Shanghai",
        updated_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
    )
    _tick(env, _worker(env))

    occurrences = _occurrences(env)
    assert [_aware(row["scheduled_for_utc"]) for row in occurrences] == [
        datetime(2026, 8, 17, 18, 0, tzinfo=UTC),
        datetime(2026, 8, 24, 18, 0, tzinfo=UTC),
    ]
    assert [row["outcome"] for row in occurrences] == ["skipped_missed", "executed"]


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


def test_execute_completes_backup_under_closed_gate(env) -> None:
    backup_id = _create_backup(env)
    _tick(env, _worker(env))

    assert _backup_row(env, backup_id)["status"] == "complete"
    components = _components(env, backup_id)
    assert components["postgres_snapshot"][:2] == ("succeeded", "pgsnap-1")
    assert components["object_store_snapshot"][:2] == ("succeeded", "objsnap-1")
    assert components["object_manifest"][:2] == ("succeeded", "manifest:0")
    # The provider snapshots only ran once the persisted gate was closed.
    assert env["postgres"].gate_states == ["closed"]
    gate = _gate_state(env)
    assert gate is not None
    assert (gate["state"], gate["backup_id"]) == ("open", None)
    rows = _audit_rows(env)
    assert ("backup_executor", "backup_execution.completed", backup_id, "succeeded") in rows
    assert ("backup_executor", "backup_execution.gate_released", backup_id, "succeeded") in rows


def test_execute_provider_failure_marks_failed_and_cleans_up(env) -> None:
    env["objects"].fail_snapshot = True
    backup_id = _create_backup(env)
    _tick(env, _worker(env))

    assert _backup_row(env, backup_id)["status"] == "failed"
    components = _components(env, backup_id)
    assert components["postgres_snapshot"][0] == "succeeded"
    assert components["object_store_snapshot"][0] == "failed"
    assert "object snapshot boom" in str(components["object_store_snapshot"][2])
    # The gate was released despite the failure.
    assert (_gate_state(env) or {})["state"] == "open"
    rows = _audit_rows(env)
    assert ("backup_executor", "backup_execution.failed", backup_id, "failed") in rows
    assert ("backup_executor", "backup_execution.gate_released", backup_id, "succeeded") in rows

    # The failure created a durable cleanup target for the already-produced
    # postgres snapshot reference; the retention task retried it (from
    # persisted state) and purged the set, all within the same tick.
    targets = _cleanup_targets(env)
    assert [(t["backup_id"], t["status"], t["attempts"]) for t in targets] == [
        (backup_id, "done", 1)
    ]
    assert "object snapshot boom" in str(targets[0]["last_error"])
    assert env["postgres"].deleted == ["pgsnap-1"]
    assert _backup_row(env, backup_id)["purged_at_utc"] is not None
    assert (
        "backup_retention",
        "backup_retention.purged",
        backup_id,
        "succeeded",
    ) in _audit_rows(env)

    # A further tick has nothing left to delete.
    _tick(env, _worker(env))
    assert env["postgres"].deleted == ["pgsnap-1"]


def test_execute_waits_for_inflight_writes_to_drain(env) -> None:
    tracker = _DrainAfterReadsTracker(reads_before_zero=3)
    worker = _worker(env, write_tracker=tracker)
    backup_id = _create_backup(env)
    _tick(env, worker)

    assert _backup_row(env, backup_id)["status"] == "complete"
    # The drain loop polled the shared tracker instance until it reported zero.
    assert tracker.reads >= 4
    assert env["postgres"].gate_states == ["closed"]


def test_execute_drain_timeout_fails_backup_and_releases_gate(env) -> None:
    env["tracker"].inc()
    try:
        worker = _worker(env, gate_drain_timeout_seconds=0.2)
        backup_id = _create_backup(env)
        _tick(env, worker)
    finally:
        env["tracker"].dec()

    assert _backup_row(env, backup_id)["status"] == "failed"
    kind_status, _reference, reason = _components(env, backup_id)["postgres_snapshot"]
    assert kind_status == "failed"
    assert "timed out" in str(reason)
    # No snapshot reference was produced, so no cleanup target exists.
    assert _cleanup_targets(env) == []
    assert (_gate_state(env) or {})["state"] == "open"
    assert (
        "backup_executor",
        "backup_execution.failed",
        backup_id,
        "failed",
    ) in _audit_rows(env)


def test_execute_recovers_stale_gate(env) -> None:
    # A terminal backup still holding the gate belongs to a dead executor.
    backup_id = _create_backup(env)
    env["backup_service"].fail_component(backup_id, kind="postgres_snapshot", reason="boom")
    _set_gate(env, "closed", backup_id)
    worker = _worker(env)
    _tick(env, worker)
    gate = _gate_state(env)
    assert (gate["state"], gate["backup_id"]) == ("open", None)
    assert (
        "backup_executor",
        "backup_execution.gate_recovered",
        backup_id,
        "succeeded",
    ) in _audit_rows(env)

    # Same for a gate referencing a backup that no longer exists.
    _set_gate(env, "closing", "backup_gone")
    _tick(env, worker)
    assert (_gate_state(env) or {})["state"] == "open"
    assert (
        "backup_executor",
        "backup_execution.gate_recovered",
        "backup_gone",
        "succeeded",
    ) in _audit_rows(env)


# ---------------------------------------------------------------------------
# Restore driving
# ---------------------------------------------------------------------------


def test_restore_driven_to_completion(env) -> None:
    backup_id = _create_backup(env)
    _complete_backup(env, backup_id)
    restore_id = _start_restore(env, backup_id)
    assert env["backup_service"].reads_closed() is True

    _tick(env, _worker(env))

    state = env["backup_service"].get_restore(restore_id)
    assert state["status"] == "completed"
    assert all(stage["status"] == "succeeded" for stage in state["stages"])
    # The post-gate reopened reads once the restore finished.
    assert env["backup_service"].reads_closed() is False


def test_restore_stops_when_blocked_on_repair_targets(env) -> None:
    env["facts"].expected = [ObjectFact("documents/d1/original", 100, "a" * 64)]
    env["facts"].actual = []
    backup_id = _create_backup(env)
    _complete_backup(env, backup_id)
    restore_id = _start_restore(env, backup_id)

    worker = _worker(env)
    _tick(env, worker)
    state = env["backup_service"].get_restore(restore_id)
    assert state["status"] == "blocked"
    assert [target["status"] for target in state["repair_targets"]] == ["open"]

    # A blocked session is not driven further; it waits for the operator's
    # repair retry through the ops API.
    _tick(env, worker)
    assert env["backup_service"].get_restore(restore_id)["status"] == "blocked"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_requires_old_and_outside_keep_last(env) -> None:
    # enabled=0: the schedule task stays out of the way of the retention set.
    _set_policy(env, enabled=0, keep_last=1, retention_days=30)
    b_new = _create_backup(env)
    _complete_backup(env, b_new, completed_at=NOW - timedelta(days=1))
    b_young = _create_backup(env)
    _complete_backup(env, b_young, completed_at=NOW - timedelta(days=5))
    b_old = _create_backup(env)
    _complete_backup(env, b_old, completed_at=NOW - timedelta(days=85))

    _tick(env, _worker(env))

    # Only b_old is both older than retention_days and outside keep_last.
    assert _backup_row(env, b_old)["purged_at_utc"] is not None
    assert _backup_row(env, b_young)["purged_at_utc"] is None
    assert _backup_row(env, b_new)["purged_at_utc"] is None
    assert env["postgres"].deleted == [f"pg-{b_old}"]
    assert env["objects"].deleted == [f"obj-{b_old}"]
    targets = _cleanup_targets(env)
    assert [(t["backup_id"], t["status"]) for t in targets] == [(b_old, "done")]
    evaluated = _audit_details(env, "backup_retention.evaluated")
    assert len(evaluated) == 1
    assert evaluated[0]["candidates"] == 1
    assert evaluated[0]["purged"] == 1
    assert (
        "backup_retention",
        "backup_retention.purged",
        b_old,
        "succeeded",
    ) in _audit_rows(env)


def test_retention_keep_last_protects_newest(env) -> None:
    _set_policy(env, enabled=0, keep_last=2, retention_days=30)
    ids = []
    for days in (75, 80, 85):
        backup_id = _create_backup(env)
        _complete_backup(env, backup_id, completed_at=NOW - timedelta(days=days))
        ids.append(backup_id)

    _tick(env, _worker(env))

    # Newest two are protected by keep_last even though all are too old.
    assert _backup_row(env, ids[0])["purged_at_utc"] is None
    assert _backup_row(env, ids[1])["purged_at_utc"] is None
    assert _backup_row(env, ids[2])["purged_at_utc"] is not None


def test_retention_skips_referenced_backups(env) -> None:
    _set_policy(env, enabled=0, keep_last=1, retention_days=30)
    b_new = _create_backup(env)
    _complete_backup(env, b_new, completed_at=NOW - timedelta(days=1))
    b_old = _create_backup(env)
    _complete_backup(env, b_old, completed_at=NOW - timedelta(days=85))
    # A non-terminal restore session references the candidate. Use 'blocked':
    # an 'accepted' session would be driven to completion by the restore task
    # in the same tick, ending the protection before the sweep runs.
    with env["engine"].begin() as connection:
        connection.execute(
            restore_sessions_table.insert().values(
                id="restore_x",
                backup_id=b_old,
                status="blocked",
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )

    worker = _worker(env)
    _tick(env, worker)
    assert _backup_row(env, b_old)["purged_at_utc"] is None
    skipped = _audit_details(env, "backup_retention.skipped")
    assert [row["reason"] for row in skipped] == ["active_restore"]

    # A completed restore no longer references the backup, but its open repair
    # target still does.
    with env["engine"].begin() as connection:
        connection.execute(
            update(restore_sessions_table)
            .where(restore_sessions_table.c.id == "restore_x")
            .values(status="completed", updated_at_utc=NOW)
        )
        connection.execute(
            repair_targets_table.insert().values(
                id="repair_x",
                restore_id="restore_x",
                stage="object_store",
                resource_id="documents/d1",
                failure_classification="fact_mismatch",
                detail="missing object",
                status="open",
                attempts=0,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
    _tick(env, worker)
    assert _backup_row(env, b_old)["purged_at_utc"] is None
    skipped = _audit_details(env, "backup_retention.skipped")
    assert [row["reason"] for row in skipped] == ["active_restore", "open_repair_target"]

    # Once nothing references it anymore, the candidate is purged.
    with env["engine"].begin() as connection:
        connection.execute(
            update(repair_targets_table)
            .where(repair_targets_table.c.id == "repair_x")
            .values(status="succeeded", resolved_at_utc=NOW, updated_at_utc=NOW)
        )
    _tick(env, worker)
    assert _backup_row(env, b_old)["purged_at_utc"] is not None


def test_retention_always_keeps_one_restorable_backup(env) -> None:
    _set_policy(env, enabled=0, keep_last=1, retention_days=30)
    only = _create_backup(env)
    _complete_backup(env, only, completed_at=NOW - timedelta(days=85))

    _tick(env, _worker(env))

    assert _backup_row(env, only)["purged_at_utc"] is None
    assert _audit_details(env, "backup_retention.purged") == []


def test_retention_failed_delete_retries_with_backoff(env) -> None:
    _set_policy(env, enabled=0, keep_last=1, retention_days=30)
    b_new = _create_backup(env)
    _complete_backup(env, b_new, completed_at=NOW - timedelta(days=1))
    b_old = _create_backup(env)
    _complete_backup(env, b_old, completed_at=NOW - timedelta(days=85))
    env["objects"].fail_delete = True

    worker = _worker(env)
    _tick(env, worker)
    targets = _cleanup_targets(env)
    assert [(t["backup_id"], t["status"], t["attempts"]) for t in targets] == [(b_old, "failed", 1)]
    assert _aware(targets[0]["next_retry_at_utc"]) == NOW + timedelta(minutes=5)
    assert "object delete boom" in str(targets[0]["last_error"])
    assert _backup_row(env, b_old)["purged_at_utc"] is None
    assert (
        "backup_retention",
        "backup_retention.failed",
        b_old,
        "failed",
    ) in _audit_rows(env)

    # Not due yet: the immediate next sweep does not re-attempt the deletion.
    _tick(env, worker)
    assert env["postgres"].deleted == [f"pg-{b_old}"]
    assert env["objects"].deleted == []

    # After the backoff window the persisted target is retried and completes.
    env["clock"].value = NOW + timedelta(minutes=6)
    env["objects"].fail_delete = False
    _tick(env, worker)
    targets = _cleanup_targets(env)
    assert [(t["backup_id"], t["status"], t["attempts"]) for t in targets] == [(b_old, "done", 2)]
    # Postgres deletion is re-issued idempotently before the object deletion.
    assert env["postgres"].deleted == [f"pg-{b_old}", f"pg-{b_old}"]
    assert env["objects"].deleted == [f"obj-{b_old}"]
    assert _backup_row(env, b_old)["purged_at_utc"] is not None
