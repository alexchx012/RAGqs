"""Typed ports the retention domain consumes from neighbouring owners.

The retention domain never writes to neighbouring tables; every destructive or
lifecycle effect goes through these owner-provided entries.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.indexing.models import IndexGenerationGcReceipt
from app.outbox.ports import EligibleAccountEventCompactionReceipt


class DocumentsCleanupPort(Protocol):
    def purge_retained_versions(self, *, limit: int = 100) -> list[str]: ...

    def finalize_deletion(self, *, document_id: str, deletion_id: str) -> dict[str, Any]: ...


class IndexingGcPort(Protocol):
    def request_index_generation_gc(
        self,
        *,
        candidate_generation_id: str,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> IndexGenerationGcReceipt: ...

    def complete_index_generation_gc(
        self,
        *,
        candidate_generation_id: str,
        operation_id: str,
    ) -> IndexGenerationGcReceipt: ...


class GraphGcPort(Protocol):
    def request_public_graph_component_gc(
        self,
        *,
        candidate_generation_id: str,
        graph_component_id: str,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> dict[str, Any]: ...


class AccountCompactionPort(Protocol):
    def request_compaction(
        self,
        *,
        operation_id: str,
        user_id: str,
        deletion_id: str,
        retirement_receipt_id: str,
    ) -> EligibleAccountEventCompactionReceipt: ...
