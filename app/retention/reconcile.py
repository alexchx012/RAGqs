"""Source-of-truth reconciliation with owner-only repair.

Expected sets come from PostgreSQL plus object manifests; derived state
(index/graph/cache/projection) is only ever compared, never trusted as a
source for deletion or repair. Destructive repairs go exclusively through
owner contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from app.platform.errors import PlatformError

from . import facts
from .gc_handoff import GenerationGcCoordinator
from .ports import DocumentsCleanupPort
from .repository import SqlAlchemyRetentionRepository
from .schema import retention_reconciliation_findings_table

_DOCUMENT_DELETIONS_SCOPE = "document-deletions"
_INDEX_GENERATIONS_SCOPE = "index-generations"


class ReconciliationService:
    def __init__(
        self,
        *,
        repository: SqlAlchemyRetentionRepository,
        documents_port: DocumentsCleanupPort,
        gc_coordinator: GenerationGcCoordinator,
        engine: Any,
        now: Any,
    ) -> None:
        self._repository = repository
        self._documents = documents_port
        self._gc = gc_coordinator
        self._engine = engine
        self._now = now

    def run(self, *, scope: str, limit: int = 100) -> Mapping[str, Any]:
        if scope not in (_DOCUMENT_DELETIONS_SCOPE, _INDEX_GENERATIONS_SCOPE):
            raise PlatformError("validation_error", "unknown reconciliation scope", {}, 422)
        if scope == _DOCUMENT_DELETIONS_SCOPE:
            return self._run_document_deletions(limit=limit)
        return self._run_index_generations(limit=limit)

    def _run_document_deletions(self, *, limit: int) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            now = self._now(connection)
            pending = facts.list_pending_document_deletions(connection, limit=limit)
            snapshot = {
                "scope": _DOCUMENT_DELETIONS_SCOPE,
                "pending_deletions": len(pending),
                "as_of": now.isoformat(),
            }
        run_id = self._repository.create_run(
            scope=_DOCUMENT_DELETIONS_SCOPE, source_snapshot=snapshot
        )
        counts = {"info": 0, "repairable": 0, "blocking": 0}
        with self._engine.begin() as connection:
            for deletion in pending:
                document_id = str(deletion["document_id"])
                deletion_id = str(deletion["id"])
                if not document_id or not deletion_id:
                    self._repository.add_finding(
                        run_id=run_id,
                        category="blocking",
                        resource_type="document_deletion",
                        resource_id=document_id or "unknown",
                        detail="deletion record has an incomplete source identity",
                        repairable=False,
                        connection=connection,
                    )
                    counts["blocking"] += 1
                    continue
                self._repository.add_finding(
                    run_id=run_id,
                    category="repairable",
                    resource_type="document_deletion",
                    resource_id=document_id,
                    detail=f"pending document deletion {deletion_id} ready for finalize",
                    repairable=True,
                    connection=connection,
                )
                counts["repairable"] += 1
        self._repository.complete_run(run_id, counts=counts)
        return {"run_id": run_id, "scope": _DOCUMENT_DELETIONS_SCOPE, "counts": counts}

    def _run_index_generations(self, *, limit: int) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            now = self._now(connection)
            candidates = facts.gc_candidate_generations(connection, now=now, limit=limit)
            blocked = facts.gc_blocked_rollback_candidate(connection)
            snapshot = {
                "scope": _INDEX_GENERATIONS_SCOPE,
                "candidates": len(candidates),
                "as_of": now.isoformat(),
            }
        run_id = self._repository.create_run(
            scope=_INDEX_GENERATIONS_SCOPE, source_snapshot=snapshot
        )
        counts = {"info": 0, "repairable": 0, "blocking": 0}
        with self._engine.begin() as connection:
            if blocked is not None:
                self._repository.add_finding(
                    run_id=run_id,
                    category="blocking",
                    resource_type="index_generation",
                    resource_id=blocked["rollback_candidate_id"],
                    detail="generation is the rollback candidate and must stay blocked",
                    repairable=False,
                    connection=connection,
                )
                counts["blocking"] += 1
            for candidate in candidates:
                self._repository.add_finding(
                    run_id=run_id,
                    category="repairable",
                    resource_type="index_generation",
                    resource_id=str(candidate["id"]),
                    detail=f"generation {candidate['id']} ({candidate['status']}) eligible for GC handoff",
                    repairable=True,
                    connection=connection,
                )
                counts["repairable"] += 1
        self._repository.complete_run(run_id, counts=counts)
        return {"run_id": run_id, "scope": _INDEX_GENERATIONS_SCOPE, "counts": counts}

    def repair_document_deletion(
        self, *, finding_id: str, document_id: str, deletion_id: str
    ) -> Mapping[str, Any]:
        """Owner-contract repair: finalize exactly one pending document deletion."""
        try:
            result = self._documents.finalize_deletion(
                document_id=document_id, deletion_id=deletion_id
            )
        except PlatformError as error:
            if error.code in {"deletion_cleanup_blocked", "deletion_conflict"}:
                self._repository.mark_finding(
                    finding_id,
                    status="open",
                    detail=f"finalize deferred: {error.code}",
                )
                return {"finding_id": finding_id, "state": "blocked", "code": error.code}
            raise
        if result.get("state") == "deleted":
            self._repository.mark_finding(finding_id, status="repaired")
            return {"finding_id": finding_id, "state": "repaired"}
        self._repository.mark_finding(
            finding_id,
            status="open",
            detail=f"finalize in progress: {result.get('state')}",
        )
        return {"finding_id": finding_id, "state": "in_progress"}

    def repair_generation_gc(
        self, *, finding_id: str, candidate_generation_id: str
    ) -> Mapping[str, Any]:
        run_id = self._repository_run_id_for_finding(finding_id)
        with self._engine.connect() as connection:
            component_ids = facts.graph_component_ids(
                connection, generation_id=candidate_generation_id
            )
        try:
            result = self._gc.handoff(
                candidate_generation_id=candidate_generation_id,
                reconciliation_run_id=run_id,
                component_ids=component_ids,
            )
        except PlatformError as error:
            self._repository.mark_finding(
                finding_id,
                status="open",
                detail=f"GC handoff deferred: {error.code}",
            )
            return {"finding_id": finding_id, "state": "blocked", "code": error.code}
        if result.get("state") == "purged":
            self._repository.mark_finding(finding_id, status="repaired")
            return {"finding_id": finding_id, "state": "repaired"}
        self._repository.mark_finding(
            finding_id,
            status="open",
            detail=f"GC handoff pending: {result.get('state')}",
        )
        return {"finding_id": finding_id, "state": "in_progress"}

    def _repository_run_id_for_finding(self, finding_id: str) -> str:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(retention_reconciliation_findings_table.c.run_id).where(
                    retention_reconciliation_findings_table.c.id == finding_id
                )
            ).scalar_one_or_none()
            if row is None:
                raise PlatformError("not_found", "finding was not found", {}, 404)
            return str(row)
