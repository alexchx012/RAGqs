"""Reconciliation runs and findings tests."""

from __future__ import annotations

from datetime import timedelta

from retention_helpers import build_engine, fixed_now

from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.retention.repository import SqlAlchemyRetentionRepository
from app.retention.schema import (
    retention_metadata,
    retention_reconciliation_findings_table,
    retention_reconciliation_runs_table,
)


class FakeDocumentsPort:
    def __init__(self) -> None:
        self.finalize_calls: list[tuple[str, str]] = []
        self.finalize_error: PlatformError | None = None

    def purge_retained_versions(self, *, limit: int = 100) -> list[str]:
        del limit
        return []

    def finalize_deletion(self, *, document_id: str, deletion_id: str) -> dict[str, object]:
        self.finalize_calls.append((document_id, deletion_id))
        if self.finalize_error is not None:
            raise self.finalize_error
        return {"document_id": document_id, "state": "deleted"}


class FakeGcCoordinator:
    def __init__(self) -> None:
        self.handoff_calls: list[tuple[str, str, list[str]]] = []

    def handoff(
        self, *, candidate_generation_id: str, reconciliation_run_id: str, component_ids: list[str]
    ) -> dict[str, object]:
        self.handoff_calls.append((candidate_generation_id, reconciliation_run_id, component_ids))
        return {
            "operation_id": f"gc:{reconciliation_run_id}:{candidate_generation_id}",
            "state": "purged",
            "receipt_json": {},
        }


def _make_reconciliation(engine, documents_port, *, now=None, gc_coordinator=None):
    from app.retention.reconcile import ReconciliationService

    clock = now or (lambda connection=None: fixed_now())
    repository = SqlAlchemyRetentionRepository(engine, now=clock)
    return repository, ReconciliationService(
        repository=repository,
        documents_port=documents_port,
        gc_coordinator=gc_coordinator or FakeGcCoordinator(),
        engine=engine,
        now=clock,
    )


def test_document_deletion_reconciliation_keeps_audit_run_without_orphan_findings() -> None:

    from app.documents.schema import (
        document_deletions_table,
        documents_metadata,
        documents_table,
    )

    engine = build_engine()
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            documents_table.insert().values(
                id="doc_1",
                space_id="space_1",
                lifecycle_status="pending_delete",
                version=2,
                name="Plan",
                normalized_name="plan",
                uploaded_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            document_deletions_table.insert().values(
                id="deletion_1",
                document_id="doc_1",
                requested_by_user_id="admin",
                version=1,
                status="pending_delete",
                requested_at_utc=now,
                notification_redaction_operation_id="redact:deletion_1",
                notification_redaction_receipt_json={},
                physical_cleanup_json={},
            )
        )
    repository, reconciliation = _make_reconciliation(engine, FakeDocumentsPort())
    result = reconciliation.run(scope="document-deletions", limit=10)
    assert result["counts"] == {"info": 0, "repairable": 0, "blocking": 0}
    findings = repository.list_open_findings(scope="document-deletions")
    assert findings == []


def test_generation_reconciliation_flags_rollback_candidate_blocking() -> None:
    from app.indexing.schema import (
        index_generation_heads_table,
        index_generations_table,
        indexing_metadata,
    )

    engine = build_engine()
    core_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            index_generations_table.insert().values(
                id="gen_active",
                status="active",
                base_revision=0,
                applied_revision=10,
                manifest_json={},
                created_at_utc=now,
                activated_at_utc=now,
            )
        )
        connection.execute(
            index_generations_table.insert().values(
                id="gen_rollback",
                status="retired",
                base_revision=0,
                applied_revision=8,
                manifest_json={},
                created_at_utc=now,
                retired_at_utc=now,
                rollback_until_utc=now,
            )
        )
        connection.execute(
            index_generations_table.insert().values(
                id="gen_retired",
                status="retired",
                base_revision=0,
                applied_revision=5,
                manifest_json={},
                created_at_utc=now,
                retired_at_utc=now,
                rollback_until_utc=None,
            )
        )
        connection.execute(
            index_generations_table.insert().values(
                id="gen_failed",
                status="failed",
                base_revision=0,
                applied_revision=4,
                manifest_json={},
                created_at_utc=now,
                retired_at_utc=now,
                rollback_until_utc=None,
            )
        )
        connection.execute(
            index_generation_heads_table.insert().values(
                id="instance",
                active_generation_id="gen_active",
                rollback_candidate_id="gen_rollback",
                current_revision=10,
                updated_at_utc=now,
            )
        )
    gc = FakeGcCoordinator()
    repository, reconciliation = _make_reconciliation(
        engine, FakeDocumentsPort(), gc_coordinator=gc
    )
    result = reconciliation.run(scope="index-generations", limit=10)
    assert result["counts"] == {"info": 0, "repairable": 1, "blocking": 1}
    findings = repository.list_open_findings(scope="index-generations")
    categories = {finding["category"] for finding in findings}
    assert "blocking" in categories
    repairable = [finding for finding in findings if finding["category"] == "repairable"]
    assert [finding["resource_id"] for finding in repairable] == ["gen_retired"]

    repeated = reconciliation.run(scope="index-generations", limit=10)
    assert repeated["counts"] == {"info": 0, "repairable": 0, "blocking": 0}
    repeated_findings = repository.list_open_findings(scope="index-generations")
    assert {(finding["category"], finding["resource_id"]) for finding in repeated_findings} == {
        ("blocking", "gen_rollback"),
        ("repairable", "gen_retired"),
    }

    with engine.begin() as connection:
        connection.execute(
            index_generation_heads_table.update()
            .where(index_generation_heads_table.c.id == "instance")
            .values(rollback_candidate_id=None)
        )
    released = reconciliation.run(scope="index-generations", limit=10)
    assert released["counts"] == {"info": 0, "repairable": 1, "blocking": 0}
    open_findings = repository.list_open_findings(scope="index-generations")
    assert {(finding["category"], finding["resource_id"]) for finding in open_findings} == {
        ("repairable", "gen_retired"),
        ("repairable", "gen_rollback"),
    }
    released_finding = next(
        finding for finding in open_findings if finding["resource_id"] == "gen_rollback"
    )
    result = reconciliation.repair_generation_gc(
        finding_id=str(released_finding["id"]), candidate_generation_id="gen_rollback"
    )
    assert result["state"] == "repaired"
    assert [call[0] for call in gc.handoff_calls] == ["gen_rollback"]
    with engine.connect() as connection:
        old_blocker_status = (
            connection.execute(
                retention_reconciliation_findings_table.select()
                .where(retention_reconciliation_findings_table.c.resource_id == "gen_rollback")
                .where(retention_reconciliation_findings_table.c.category == "blocking")
            )
            .mappings()
            .one()["status"]
        )
    assert old_blocker_status == "ignored"


def test_generation_reconciliation_reconsiders_released_rollback_after_full_open_page() -> None:
    from app.indexing.schema import (
        index_generation_heads_table,
        index_generations_table,
        indexing_metadata,
    )

    engine = build_engine()
    core_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            index_generations_table.insert().values(
                id="gen_active",
                status="active",
                base_revision=0,
                applied_revision=100,
                manifest_json={},
                created_at_utc=now,
                activated_at_utc=now,
            )
        )
        connection.execute(
            index_generations_table.insert().values(
                id="gen_rollback",
                status="retired",
                base_revision=0,
                applied_revision=99,
                manifest_json={},
                created_at_utc=now,
                retired_at_utc=now,
                rollback_until_utc=now,
            )
        )
        connection.execute(
            index_generations_table.insert(),
            [
                {
                    "id": f"gen_pending_{index:02d}",
                    "status": "retired",
                    "base_revision": 0,
                    "applied_revision": index,
                    "manifest_json": {},
                    "created_at_utc": now,
                    "retired_at_utc": now - timedelta(minutes=1),
                    "rollback_until_utc": None,
                }
                for index in range(50)
            ],
        )
        connection.execute(
            index_generation_heads_table.insert().values(
                id="instance",
                active_generation_id="gen_active",
                rollback_candidate_id="gen_rollback",
                current_revision=100,
                updated_at_utc=now,
            )
        )

    gc = FakeGcCoordinator()
    repository, reconciliation = _make_reconciliation(
        engine, FakeDocumentsPort(), gc_coordinator=gc
    )
    initial = reconciliation.run(scope="index-generations", limit=50)
    assert initial["counts"] == {"info": 0, "repairable": 50, "blocking": 1}

    with engine.begin() as connection:
        connection.execute(
            index_generation_heads_table.update()
            .where(index_generation_heads_table.c.id == "instance")
            .values(rollback_candidate_id=None)
        )

    released = reconciliation.run(scope="index-generations", limit=50)
    assert released["counts"] == {"info": 0, "repairable": 1, "blocking": 0}
    released_finding = next(
        finding
        for finding in repository.list_open_findings(scope="index-generations")
        if finding["resource_id"] == "gen_rollback"
    )
    result = reconciliation.repair_generation_gc(
        finding_id=str(released_finding["id"]), candidate_generation_id="gen_rollback"
    )
    assert result["state"] == "repaired"
    assert [call[0] for call in gc.handoff_calls] == ["gen_rollback"]
    with engine.connect() as connection:
        old_blocker_status = (
            connection.execute(
                retention_reconciliation_findings_table.select()
                .where(retention_reconciliation_findings_table.c.resource_id == "gen_rollback")
                .where(retention_reconciliation_findings_table.c.category == "blocking")
            )
            .mappings()
            .one()["status"]
        )
    assert old_blocker_status == "ignored"


def test_generation_repair_ignores_legacy_non_retired_finding() -> None:
    from app.indexing.schema import index_generations_table, indexing_metadata

    engine = build_engine()
    core_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            index_generations_table.insert().values(
                id="gen_failed",
                status="failed",
                base_revision=0,
                applied_revision=4,
                manifest_json={},
                created_at_utc=now,
                retired_at_utc=now,
            )
        )
    gc = FakeGcCoordinator()
    repository, reconciliation = _make_reconciliation(
        engine, FakeDocumentsPort(), gc_coordinator=gc
    )
    run_id = repository.create_run(scope="index-generations", source_snapshot={})
    with engine.begin() as connection:
        finding_id = repository.add_finding(
            run_id=run_id,
            category="repairable",
            resource_type="index_generation",
            resource_id="gen_failed",
            detail="legacy failed candidate",
            repairable=True,
            connection=connection,
        )
    repository.complete_run(run_id, counts={"info": 0, "repairable": 1, "blocking": 0})

    result = reconciliation.repair_generation_gc(
        finding_id=finding_id, candidate_generation_id="gen_failed"
    )

    assert result["state"] == "ignored"
    assert gc.handoff_calls == []
    assert repository.list_open_findings(scope="index-generations") == []


def test_reconciliation_prunes_terminal_history_without_hiding_open_findings() -> None:
    from app.indexing.schema import indexing_metadata

    engine = build_engine()
    core_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    clock = [fixed_now() - timedelta(days=31)]

    def now(connection=None):
        del connection
        return clock[0]

    repository, reconciliation = _make_reconciliation(engine, FakeDocumentsPort(), now=now)

    repaired_run_id = repository.create_run(scope="index-generations", source_snapshot={})
    ignored_run_id = repository.create_run(scope="index-generations", source_snapshot={})
    open_run_id = repository.create_run(scope="index-generations", source_snapshot={})
    with engine.begin() as connection:
        repaired_finding_id = repository.add_finding(
            run_id=repaired_run_id,
            category="repairable",
            resource_type="index_generation",
            resource_id="gen_repaired",
            detail="done",
            repairable=True,
            connection=connection,
        )
        open_finding_id = repository.add_finding(
            run_id=open_run_id,
            category="blocking",
            resource_type="index_generation",
            resource_id="gen_open",
            detail="still open",
            repairable=False,
            connection=connection,
        )
    for run_id in (repaired_run_id, open_run_id):
        repository.complete_run(run_id, counts={"info": 0, "repairable": 0, "blocking": 1})
    repository.fail_run(ignored_run_id, detail="ignored")
    repository.mark_finding(repaired_finding_id, status="repaired")
    ignored_finding_id = next(
        finding["id"]
        for finding in repository.list_open_findings(scope="index-generations")
        if finding["run_id"] == ignored_run_id
    )
    repository.mark_finding(ignored_finding_id, status="ignored")

    clock[0] = fixed_now()
    reconciliation.run(scope="index-generations", limit=10)

    with engine.connect() as connection:
        remaining_run_ids = set(
            connection.execute(retention_reconciliation_runs_table.select()).scalars()
        )
        remaining_finding_ids = set(
            connection.execute(retention_reconciliation_findings_table.select()).scalars()
        )
    assert repaired_run_id not in remaining_run_ids
    assert ignored_run_id not in remaining_run_ids
    assert repaired_finding_id not in remaining_finding_ids
    assert ignored_finding_id not in remaining_finding_ids
    assert open_run_id in remaining_run_ids
    assert open_finding_id in remaining_finding_ids
    assert [
        finding["id"] for finding in repository.list_open_findings(scope="index-generations")
    ] == [open_finding_id]


def test_repair_deferred_when_owner_fence_blocks_finalize() -> None:
    from app.platform.database import core_metadata

    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    documents_port = FakeDocumentsPort()
    documents_port.finalize_error = PlatformError("deletion_cleanup_blocked", "still busy", {}, 409)
    repository, reconciliation = _make_reconciliation(engine, documents_port)
    run_id = repository.create_run(scope="document-deletions", source_snapshot={})
    with engine.begin() as connection:
        finding_id = repository.add_finding(
            run_id=run_id,
            category="repairable",
            resource_type="document_deletion",
            resource_id="doc_1",
            detail="pending",
            repairable=True,
            connection=connection,
        )
        repository.complete_run(run_id, counts={"info": 0, "repairable": 1, "blocking": 0})
    result = reconciliation.repair_document_deletion(
        finding_id=finding_id, document_id="doc_1", deletion_id="deletion_1"
    )
    assert result["state"] == "blocked"
    assert documents_port.finalize_calls == [("doc_1", "deletion_1")]
    finding = repository.list_open_findings(scope="document-deletions")[0]
    assert finding["status"] == "open"
    assert "deferred" in finding["detail"]
