"""Runtime adapters binding owner entries behind the retention ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.indexing.models import IndexGenerationGcReceipt
from app.outbox.ports import EligibleAccountEventCompactionReceipt


class RuntimeDocumentsCleanupPort:
    def __init__(self, documents_service: Any) -> None:
        self._service = documents_service

    def purge_retained_versions(self, *, limit: int = 100) -> list[str]:
        return list(self._service.purge_retained_versions(limit=limit))

    def finalize_deletion(self, *, document_id: str, deletion_id: str) -> dict[str, Any]:
        return self._service.finalize_deletion(document_id=document_id, deletion_id=deletion_id)


class RuntimeIndexingGcPort:
    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    def request_index_generation_gc(
        self,
        *,
        candidate_generation_id: str,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> IndexGenerationGcReceipt:
        return self._coordinator.request_index_generation_gc(
            candidate_generation_id=candidate_generation_id,
            reconciliation_run_id=reconciliation_run_id,
            operation_id=operation_id,
            caller_principal="retention-ops",
        )

    def complete_index_generation_gc(
        self,
        *,
        candidate_generation_id: str,
        operation_id: str,
    ) -> IndexGenerationGcReceipt:
        return self._coordinator.complete_index_generation_gc(
            candidate_generation_id=candidate_generation_id,
            operation_id=operation_id,
            caller_principal="retention-ops",
        )


class RuntimeGraphGcPort:
    def __init__(self, graph_build_service: Any) -> None:
        self._service = graph_build_service

    def request_public_graph_component_gc(
        self,
        *,
        candidate_generation_id: str,
        graph_component_id: str,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._service.request_public_graph_component_gc(
            caller="retention-ops",
            candidate_generation_id=candidate_generation_id,
            graph_component_id=graph_component_id,
            reconciliation_run_id=reconciliation_run_id,
            operation_id=operation_id,
        )


class RuntimeAccountCompactionPort:
    def __init__(self, engine: Any, gateway: Any) -> None:
        self._engine = engine
        self._gateway = gateway

    def request_compaction(
        self,
        *,
        operation_id: str,
        user_id: str,
        deletion_id: str,
        retirement_receipt_id: str,
    ) -> EligibleAccountEventCompactionReceipt:
        with self._engine.begin() as connection:
            return self._gateway.request_compaction(
                operation_id=operation_id,
                user_id=user_id,
                deletion_id=deletion_id,
                retirement_receipt_id=retirement_receipt_id,
                connection=connection,
            )


def receipt_to_mapping(receipt: object) -> Mapping[str, Any]:
    return {
        field: getattr(receipt, field) for field in getattr(receipt, "__dataclass_fields__", {})
    }
