"""Contract tests for the backup/restore orchestration capability."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select

from app.backup.ports import ObjectFact
from app.backup.schema import backup_metadata
from app.backup.service import BackupRestoreService
from app.platform.database import core_metadata, platform_audit_table
from app.platform.errors import PlatformError


class _FakePostgresBackup:
    def __init__(self) -> None:
        self.restored: list[str] = []

    def snapshot(self) -> str:
        return "pgsnap-1"

    def restore(self, reference: str) -> None:
        self.restored.append(reference)


class _FakeObjectSnapshot:
    def __init__(self) -> None:
        self.restored: list[str] = []

    def snapshot(self) -> str:
        return "objsnap-1"

    def restore(self, reference: str) -> None:
        self.restored.append(reference)


class _Manifest:
    def __init__(self, facts: list[ObjectFact]) -> None:
        self._facts = facts

    def collect_object_facts(self) -> list[ObjectFact]:
        return list(self._facts)


class _FactValidation:
    """Expected = manifest/Postgres records; actual = observed objects."""

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
        self.fail: set[tuple[str, str]] = set()

    def list_resources(self, stage: str) -> list[str]:
        return list(self.resources.get(stage, []))

    def rebuild(self, stage: str, resource_id: str) -> None:
        self.calls.append((stage, resource_id))
        if (stage, resource_id) in self.fail:
            raise RuntimeError(f"rebuild failed: {stage}/{resource_id}")


class _PostGate:
    def __init__(self, blocking: list[str] | None = None) -> None:
        self.blocking = blocking or []

    def validate_post_gate(self) -> list[str]:
        return list(self.blocking)


FACTS = [
    ObjectFact("documents/d1/v1/original", 100, "a" * 64),
    ObjectFact("documents/d2/v1/original", 200, "b" * 64),
]


@pytest.fixture()
def env():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    core_metadata.create_all(engine)
    backup_metadata.create_all(engine)
    postgres = _FakePostgresBackup()
    objects = _FakeObjectSnapshot()
    facts = _FactValidation()
    facts.expected = list(FACTS)
    facts.actual = list(FACTS)
    derived = _DerivedRebuild()
    derived.resources = {
        "milvus": ["doc:d1", "doc:d2"],
        "sparse": ["doc:d1"],
        "summary": [],
        "graph": [],
        "cache": ["cache:all"],
    }
    post_gate = _PostGate()
    service = BackupRestoreService(
        engine,
        postgres_backup=postgres,
        object_snapshot=objects,
        object_manifest=_Manifest(list(FACTS)),
        fact_validation=facts,
        derived_rebuild=derived,
        post_gate_validation=post_gate,
        now=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )
    return {
        "engine": engine,
        "service": service,
        "postgres": postgres,
        "objects": objects,
        "facts": facts,
        "derived": derived,
        "post_gate": post_gate,
    }


def _full_backup(env) -> str:
    state = env["service"].create_full_backup_set()
    assert state["status"] == "complete"
    return str(state["backup_id"])


def _audit_rows(env) -> list[tuple[str, str, str]]:
    with env["engine"].connect() as connection:
        return [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in connection.execute(
                select(
                    platform_audit_table.c.resource_type,
                    platform_audit_table.c.resource_id,
                    platform_audit_table.c.result,
                )
            ).all()
        ]


# ---------------------------------------------------------------------------
# B1: stable backup_id binding all three components
# ---------------------------------------------------------------------------


def test_backup_set_incomplete_until_all_components_succeed(env):
    service = env["service"]
    state = service.create_backup_set()
    backup_id = str(state["backup_id"])
    assert state["status"] == "creating"
    assert {c["kind"] for c in state["components"]} == {
        "postgres_snapshot",
        "object_store_snapshot",
        "object_manifest",
    }
    # Two components done: still not complete.
    state = service.complete_snapshot_component(backup_id, kind="postgres_snapshot", reference="pg")
    assert state["status"] == "creating"
    state = service.record_manifest_objects(backup_id, FACTS)
    assert state["status"] == "creating"
    assert state["object_count"] == 2
    # All three done: complete, with a stable id across calls.
    state = service.complete_snapshot_component(
        backup_id, kind="object_store_snapshot", reference="obj"
    )
    assert state["status"] == "complete"
    assert service.get_backup_set(backup_id)["backup_id"] == backup_id


def test_backup_set_failed_component_blocks_completion(env):
    service = env["service"]
    state = service.create_backup_set()
    backup_id = str(state["backup_id"])
    state = service.fail_component(backup_id, kind="object_store_snapshot", reason="snapshot error")
    assert state["status"] == "failed"
    with pytest.raises(PlatformError) as excinfo:
        service.start_restore(backup_id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "backup_not_restorable"


# ---------------------------------------------------------------------------
# B2: restore identity, pre-gates, duplicate handling
# ---------------------------------------------------------------------------


def test_start_restore_closes_reads_and_rejects_missing_components(env):
    service = env["service"]
    backup_id = _full_backup(env)
    assert service.reads_closed() is False
    state = service.start_restore(backup_id)
    restore_id = str(state["restore_id"])
    assert state["status"] == "accepted"
    assert state["reads_closed"] is True
    # Replaying the same restore_id reuses the first session's state.
    replay = service.replay_restore(restore_id)
    assert replay["restore_id"] == restore_id
    assert replay["status"] == "accepted"


def test_concurrent_restore_rejected(env):
    service = env["service"]
    backup_id = _full_backup(env)
    service.start_restore(backup_id)
    with pytest.raises(PlatformError) as excinfo:
        service.start_restore(backup_id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "restore_in_progress"


def test_restore_requires_complete_backup_set(env):
    service = env["service"]
    state = service.create_backup_set()
    with pytest.raises(PlatformError) as excinfo:
        service.start_restore(str(state["backup_id"]))
    assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# B3: fixed ordered restore stages
# ---------------------------------------------------------------------------


def test_restore_follows_fixed_stage_order(env):
    service = env["service"]
    backup_id = _full_backup(env)
    restore = service.start_restore(backup_id)
    restore_id = str(restore["restore_id"])
    order: list[str] = []
    statuses: list[list[str]] = []
    for _ in range(7):
        state = service.advance_restore(restore_id)
        stage_map = {s["stage"]: s["status"] for s in state["stages"]}
        statuses.append([stage_map[s] for s in order + []])
        # Record the newly started stage: the first non-terminal stage.
        for stage in state["stages"]:
            if stage["stage"] not in order and stage["status"] != "pending":
                order.append(stage["stage"])
                break
    assert order == [
        "postgres",
        "object_store",
        "milvus",
        "sparse",
        "summary",
        "graph",
        "cache",
    ]
    final = service.get_restore(restore_id)
    assert final["status"] == "completed"
    assert final["reads_closed"] is False


def test_later_stage_never_starts_before_previous_validated(env):
    service = env["service"]
    backup_id = _full_backup(env)
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    state = service.advance_restore(restore_id)
    stages = {s["stage"]: s for s in state["stages"]}
    assert stages["postgres"]["status"] == "succeeded"
    assert stages["postgres"]["validated"] is True
    assert stages["object_store"]["status"] == "pending"


# ---------------------------------------------------------------------------
# B4: fact validation of restored objects
# ---------------------------------------------------------------------------


def test_fact_validation_detects_checksum_mismatch(env):
    service = env["service"]
    backup_id = _full_backup(env)
    # Corrupt one object's checksum on the object-storage side.
    env["facts"].actual = [
        ObjectFact(FACTS[0].object_key, 100, "c" * 64),
        FACTS[1],
    ]
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    service.advance_restore(restore_id)  # postgres
    state = service.advance_restore(restore_id)  # object_store + validation
    stages = {s["stage"]: s for s in state["stages"]}
    assert stages["object_store"]["status"] == "failed"
    assert stages["object_store"]["validated"] is False
    assert stages["milvus"]["status"] == "pending"
    repair_ids = [r["resource_id"] for r in state["repair_targets"]]
    assert FACTS[0].object_key in repair_ids


def test_fact_validation_detects_missing_and_orphan_objects(env):
    service = env["service"]
    backup_id = _full_backup(env)
    env["facts"].actual = [FACTS[0], ObjectFact("documents/orphan/original", 5, "d" * 64)]
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    service.advance_restore(restore_id)
    state = service.advance_restore(restore_id)
    classifications = {
        r["resource_id"]: r["failure_classification"] for r in state["repair_targets"]
    }
    assert classifications[FACTS[1].object_key] == "object_missing"
    assert classifications["documents/orphan/original"] == "object_orphan"


# ---------------------------------------------------------------------------
# B5: invisible isolation + repair queue; blocked restore
# ---------------------------------------------------------------------------


def test_mismatch_keeps_restore_blocked_until_repair_resolves(env):
    service = env["service"]
    backup_id = _full_backup(env)
    env["facts"].actual = [ObjectFact(FACTS[0].object_key, 100, "c" * 64), FACTS[1]]
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    service.advance_restore(restore_id)
    state = service.advance_restore(restore_id)
    assert state["status"] != "completed"
    assert state["reads_closed"] is True
    # Repairing the object storage side resolves the mismatch.
    env["facts"].actual = list(FACTS)
    state = service.retry_repair_target(
        restore_id, stage="object_store", resource_id=FACTS[0].object_key
    )
    repairs = {r["resource_id"]: r["status"] for r in state["repair_targets"]}
    assert repairs[FACTS[0].object_key] == "succeeded"
    state = service.advance_restore(restore_id)
    stages = {s["stage"]: s for s in state["stages"]}
    assert stages["object_store"]["status"] == "succeeded"
    assert stages["object_store"]["validated"] is True


def test_derived_rebuild_failure_opens_repair_and_blocks(env):
    service = env["service"]
    backup_id = _full_backup(env)
    env["derived"].fail = {("milvus", "doc:d2")}
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    service.advance_restore(restore_id)
    service.advance_restore(restore_id)
    state = service.advance_restore(restore_id)  # milvus
    stages = {s["stage"]: s for s in state["stages"]}
    assert stages["milvus"]["status"] == "failed"
    repair = [r for r in state["repair_targets"] if r["resource_id"] == "doc:d2"]
    assert repair and repair[0]["failure_classification"] == "RuntimeError"


def test_post_gate_blocking_findings_keep_reads_closed(env):
    service = env["service"]
    backup_id = _full_backup(env)
    env["post_gate"].blocking = ["publication_inconsistent"]
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    for _ in range(7):
        state = service.advance_restore(restore_id)
    assert state["status"] == "blocked"
    assert state["reads_closed"] is True


# ---------------------------------------------------------------------------
# B6: target-level retry, no repeated side effects, restart recovery
# ---------------------------------------------------------------------------


def test_failed_target_retries_alone_without_repeating_succeeded(env):
    service = env["service"]
    backup_id = _full_backup(env)
    env["derived"].fail = {("milvus", "doc:d2")}
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    service.advance_restore(restore_id)
    service.advance_restore(restore_id)
    state = service.advance_restore(restore_id)
    assert ("milvus", "doc:d1") in env["derived"].calls
    assert env["derived"].calls.count(("milvus", "doc:d1")) == 1
    env["derived"].fail.clear()
    state = service.retry_target(restore_id, stage="milvus", resource_id="doc:d2")
    targets = {(t["stage"], t["resource_id"]): t for t in state["targets"]}
    assert targets[("milvus", "doc:d2")]["status"] == "succeeded"
    assert targets[("milvus", "doc:d2")]["attempt"] == 2
    # d1 was never re-executed.
    assert env["derived"].calls.count(("milvus", "doc:d1")) == 1


def test_restart_continues_from_persisted_state(env):
    service = env["service"]
    backup_id = _full_backup(env)
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    service.advance_restore(restore_id)
    # A fresh service instance over the same database (worker restart).
    restarted = BackupRestoreService(
        env["engine"],
        postgres_backup=env["postgres"],
        object_snapshot=env["objects"],
        object_manifest=_Manifest(list(FACTS)),
        fact_validation=env["facts"],
        derived_rebuild=env["derived"],
        post_gate_validation=env["post_gate"],
        now=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert restarted.reads_closed() is True
    state = restarted.get_restore(restore_id)
    stages = {s["stage"]: s for s in state["stages"]}
    assert stages["postgres"]["status"] == "succeeded"
    for _ in range(6):
        state = restarted.advance_restore(restore_id)
    assert state["status"] == "completed"
    assert state["reads_closed"] is False
    # Postgres restore executed exactly once across the restart.
    assert env["postgres"].restored == ["pgsnap-1"]


def test_retrying_succeeded_target_is_a_noop(env):
    service = env["service"]
    backup_id = _full_backup(env)
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    for _ in range(7):
        service.advance_restore(restore_id)
    before = list(env["derived"].calls)
    service.retry_target(restore_id, stage="milvus", resource_id="doc:d1")
    assert env["derived"].calls == before


# ---------------------------------------------------------------------------
# B7: audit trail
# ---------------------------------------------------------------------------


def test_restore_writes_audit_records(env):
    service = env["service"]
    backup_id = _full_backup(env)
    restore_id = str(service.start_restore(backup_id)["restore_id"])
    for _ in range(7):
        service.advance_restore(restore_id)
    rows = _audit_rows(env)
    resource_types = {r[0] for r in rows}
    assert "backup.set_created" in resource_types
    assert "restore.request" in resource_types
    assert "restore.stage_started" in resource_types
    assert "restore.stage_succeeded" in resource_types
    assert "restore.completed" in resource_types
    # Audit is only stored; no restore API exposes it (no HTTP surface here
    # by design — external entry points are out of scope).
    assert any(r[0] == "restore.request" and r[1] == restore_id for r in rows)


def test_gate_rejection_and_audit_on_concurrent_restore(env):
    service = env["service"]
    backup_id = _full_backup(env)
    service.start_restore(backup_id)
    with pytest.raises(PlatformError):
        service.start_restore(backup_id)
    rows = _audit_rows(env)
    assert any(r[0] == "restore.request" and r[2] == "failed" for r in rows)
