"""RetentionOpsService: the single facade used by the API and the worker."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.platform.context import current_context
from app.platform.database import SqlAlchemyDatabaseClock, platform_audit_table
from app.platform.errors import PlatformError

from . import facts
from .compaction import AccountCompactionRequester
from .gc_handoff import GenerationGcCoordinator
from .readers import DashboardReadModels, OpsJobsReadModel
from .reconcile import (
    _DOCUMENT_DELETIONS_SCOPE,
    _INDEX_GENERATIONS_SCOPE,
    ReconciliationService,
)
from .repository import SqlAlchemyRetentionRepository

logger = logging.getLogger(__name__)


class RetentionOpsService:
    def __init__(
        self,
        *,
        repository: SqlAlchemyRetentionRepository,
        dashboard: DashboardReadModels,
        ops_jobs: OpsJobsReadModel,
        reconciliation: ReconciliationService,
        gc_coordinator: GenerationGcCoordinator,
        compaction: AccountCompactionRequester,
        engine: Any,
        documents_cleanup_port: Any,
        identity_history_cleanup_port: Any,
    ) -> None:
        self._repository = repository
        self._dashboard = dashboard
        self._ops_jobs = ops_jobs
        self._reconciliation = reconciliation
        self._gc = gc_coordinator
        self._compaction = compaction
        self._engine = engine
        self._clock = SqlAlchemyDatabaseClock(engine)
        self._documents_cleanup = documents_cleanup_port
        self._identity_history_cleanup = identity_history_cleanup_port

    # ---- HTTP read models ----

    def dashboard(
        self, *, principal: Any, window: str, expand: str | None = None
    ) -> dict[str, Any]:
        role = str(getattr(principal, "role", ""))
        if expand is None:
            result = self._dashboard.dashboard(role=role, window=window)
        else:
            result = self._dashboard.dashboard(role=role, window=window, expand=expand)
        if role in {"ops", "admin"}:
            # 配额消耗类查看与库查看审计同口径：尽力落库，绝不阻断读取。
            self._audit_quota_consumption_view(principal=principal, window=window)
        return result

    def _audit_quota_consumption_view(self, *, principal: Any, window: str) -> None:
        try:
            with self._engine.begin() as connection:
                context = current_context()
                connection.execute(
                    platform_audit_table.insert().values(
                        actor_id=str(principal.user_id),
                        resource_type="retention.quota_consumption_view",
                        resource_id=window,
                        request_id=context.request_id if context is not None else "req_retention",
                        occurred_at_utc=self._clock.now_utc(connection),
                        result="succeeded",
                        details_json={},
                    )
                )
        except Exception:  # noqa: BLE001 - 观测读的审计尽力而为
            logger.warning("quota consumption view audit write failed for window %s", window)

    def operations(self, *, window: str) -> dict[str, Any]:
        return self._dashboard.operations(window=window)

    def ops_jobs(self, *, principal: Any, view: str) -> dict[str, Any]:
        return self._ops_jobs.jobs(principal=principal, view=view)

    # ---- worker steps ----

    def purge_due_versions(self, *, limit: int = 100) -> list[str]:
        return list(self._documents_cleanup.purge_retained_versions(limit=limit))

    def prune_identity_history(self, *, limit: int = 100) -> Mapping[str, int]:
        return dict(self._identity_history_cleanup.prune_completed_history(limit=limit))

    def finalize_due_deletions(self, *, limit: int = 100) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            pending = facts.list_pending_document_deletions(connection, limit=limit)
        finalized = 0
        deferred = 0
        for deletion in pending:
            try:
                result = self._documents_cleanup.finalize_deletion(
                    document_id=str(deletion["document_id"]),
                    deletion_id=str(deletion["id"]),
                )
            except PlatformError as error:
                if error.code == "deletion_cleanup_blocked":
                    deferred += 1
                    continue
                raise
            if result.get("state") == "deleted":
                finalized += 1
        return {"finalized": finalized, "deferred": deferred}

    def reconcile(self, *, scope: str, limit: int = 100) -> Mapping[str, Any]:
        return self._reconciliation.run(scope=scope, limit=limit)

    def run_gc_handoffs(self, *, limit: int = 50) -> Mapping[str, Any]:
        findings = self._repository.list_open_findings(scope=_INDEX_GENERATIONS_SCOPE, limit=limit)
        repaired = 0
        blocked = 0
        for finding in findings:
            if int(finding["repairable"]) != 1:
                continue
            if str(finding["resource_type"]) != "index_generation":
                continue
            result = self._reconciliation.repair_generation_gc(
                finding_id=str(finding["id"]),
                candidate_generation_id=str(finding["resource_id"]),
            )
            if result.get("state") == "repaired":
                repaired += 1
            elif result.get("state") == "blocked":
                blocked += 1
        return {"repaired": repaired, "blocked": blocked}

    def run_compaction_requests(self, *, limit: int = 100) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            workflows = facts.retired_workflow_rows(connection, limit=limit)
        completed = 0
        pending = 0
        terminal = 0
        for workflow in workflows:
            result = self._compaction.request_once(
                user_id=str(workflow["user_id"]),
                deletion_id=str(workflow["cleanup_operation_id"]),
                cleanup_operation_id=str(workflow["cleanup_operation_id"]),
                retirement_receipt_id=str(workflow["retirement_receipt_id"]),
            )
            if result.get("state") == "completed":
                completed += 1
            elif result.get("state") == "terminal":
                terminal += 1
            else:
                pending += 1
        return {"completed": completed, "pending": pending, "terminal": terminal}

    def run_document_reconciliation(self, *, limit: int = 100) -> Mapping[str, Any]:
        return self._reconciliation.run(scope=_DOCUMENT_DELETIONS_SCOPE, limit=limit)

    def run_generation_reconciliation(self, *, limit: int = 50) -> Mapping[str, Any]:
        return self._reconciliation.run(scope=_INDEX_GENERATIONS_SCOPE, limit=limit)
