"""Reconciliation runs and findings tests."""

from __future__ import annotations

from retention_helpers import build_engine, fixed_now

from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.retention.repository import SqlAlchemyRetentionRepository
from app.retention.schema import retention_metadata


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
    def handoff(
        self, *, candidate_generation_id: str, reconciliation_run_id: str, component_ids: list[str]
    ) -> dict[str, object]:
        del component_ids
        return {
            "operation_id": f"gc:{reconciliation_run_id}:{candidate_generation_id}",
            "state": "purged",
            "receipt_json": {},
        }


def _make_reconciliation(engine, documents_port):
    from app.retention.reconcile import ReconciliationService

    repository = SqlAlchemyRetentionRepository(engine, now=lambda connection=None: fixed_now())
    return repository, ReconciliationService(
        repository=repository,
        documents_port=documents_port,
        gc_coordinator=FakeGcCoordinator(),
        engine=engine,
        now=lambda connection=None: fixed_now(),
    )


def test_document_deletion_reconciliation_records_repairable_findings() -> None:

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
    assert result["counts"] == {"info": 0, "repairable": 1, "blocking": 0}
    findings = repository.list_open_findings(scope="document-deletions")
    assert len(findings) == 1
    assert findings[0]["resource_type"] == "document_deletion"
    assert findings[0]["resource_id"] == "doc_1"
    assert int(findings[0]["repairable"]) == 1


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
            index_generation_heads_table.insert().values(
                id="instance",
                active_generation_id="gen_active",
                rollback_candidate_id="gen_rollback",
                current_revision=10,
                updated_at_utc=now,
            )
        )
    repository, reconciliation = _make_reconciliation(engine, FakeDocumentsPort())
    result = reconciliation.run(scope="index-generations", limit=10)
    assert result["counts"] == {"info": 0, "repairable": 1, "blocking": 1}
    findings = repository.list_open_findings(scope="index-generations")
    categories = {finding["category"] for finding in findings}
    assert "blocking" in categories
    repairable = [finding for finding in findings if finding["category"] == "repairable"]
    assert [finding["resource_id"] for finding in repairable] == ["gen_retired"]


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
