"""Generation GC handoff: request -> graph component GC -> complete.

Retention only persists opaque receipts; indexing and public graph keep owning
the actual GC execution and all of its blocking preconditions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .ports import GraphGcPort, IndexingGcPort
from .repository import SqlAlchemyRetentionRepository


def gc_operation_id(reconciliation_run_id: str, candidate_generation_id: str) -> str:
    return f"gc:{reconciliation_run_id}:{candidate_generation_id}"


def graph_operation_id(reconciliation_run_id: str, graph_component_id: str) -> str:
    return f"gcc:{reconciliation_run_id}:{graph_component_id}"


class GenerationGcCoordinator:
    def __init__(
        self,
        *,
        repository: SqlAlchemyRetentionRepository,
        indexing_gc_port: IndexingGcPort,
        graph_gc_port: GraphGcPort,
    ) -> None:
        self._repository = repository
        self._indexing = indexing_gc_port
        self._graph = graph_gc_port

    def handoff(
        self,
        *,
        candidate_generation_id: str,
        reconciliation_run_id: str,
        component_ids: list[str],
    ) -> Mapping[str, Any]:
        operation_id = gc_operation_id(reconciliation_run_id, candidate_generation_id)
        existing = self._repository.get_receipt(operation_id)
        if existing is not None and existing["state"] in ("completed", "purged"):
            return {
                "operation_id": operation_id,
                "state": existing["state"],
                "receipt_json": existing["receipt_json"],
            }
        receipt = self._indexing.request_index_generation_gc(
            candidate_generation_id=candidate_generation_id,
            reconciliation_run_id=reconciliation_run_id,
            operation_id=operation_id,
        )
        receipt_json = {
            "operation_id": receipt.operation_id,
            "candidate_generation_id": receipt.candidate_generation_id,
            "state": receipt.state,
            "blocking_reasons": list(receipt.blocking_reasons),
            "retryable": receipt.retryable,
        }
        if receipt.state == "already_purged":
            self._store(operation_id, "index_gc", candidate_generation_id, receipt_json, "purged")
            return {"operation_id": operation_id, "state": "purged", "receipt_json": receipt_json}
        if receipt.state == "blocked":
            self._store(operation_id, "index_gc", candidate_generation_id, receipt_json, "blocked")
            return {"operation_id": operation_id, "state": "blocked", "receipt_json": receipt_json}
        self._store(operation_id, "index_gc", candidate_generation_id, receipt_json, "accepted")
        if not component_ids:
            completed = self._complete(candidate_generation_id, operation_id)
            return {"operation_id": operation_id, "state": completed, "receipt_json": receipt_json}
        for component_id in component_ids:
            component_op = graph_operation_id(reconciliation_run_id, component_id)
            component_existing = self._repository.get_receipt(component_op)
            if component_existing is not None and component_existing["state"] == "completed":
                continue
            graph_receipt = self._graph.request_public_graph_component_gc(
                candidate_generation_id=candidate_generation_id,
                graph_component_id=component_id,
                reconciliation_run_id=reconciliation_run_id,
                operation_id=component_op,
            )
            graph_json = {
                "operation_id": component_op,
                "candidate_generation_id": candidate_generation_id,
                "graph_component_id": component_id,
                "state": graph_receipt.get("state"),
                "blocking_reasons": list(graph_receipt.get("blocking_reasons") or []),
                "retryable": bool(graph_receipt.get("retryable", False)),
            }
            graph_state = str(graph_receipt.get("state") or "blocked")
            if graph_state == "blocked":
                self._store(
                    component_op,
                    "graph_component_gc",
                    component_id,
                    graph_json,
                    "blocked",
                )
                return {
                    "operation_id": operation_id,
                    "state": "blocked",
                    "receipt_json": receipt_json,
                }
            if graph_state == "already_purged":
                self._store(
                    component_op,
                    "graph_component_gc",
                    component_id,
                    graph_json,
                    "purged",
                )
                continue
            self._store(component_op, "graph_component_gc", component_id, graph_json, "completed")
        completed = self._complete(candidate_generation_id, operation_id)
        return {"operation_id": operation_id, "state": completed, "receipt_json": receipt_json}

    def _complete(self, candidate_generation_id: str, operation_id: str) -> str:
        receipt = self._indexing.complete_index_generation_gc(
            candidate_generation_id=candidate_generation_id,
            operation_id=operation_id,
        )
        state = "purged" if receipt.state == "already_purged" else str(receipt.state)
        if state == "purged":
            self._store(
                operation_id,
                "index_gc",
                candidate_generation_id,
                {
                    "operation_id": receipt.operation_id,
                    "candidate_generation_id": receipt.candidate_generation_id,
                    "state": receipt.state,
                    "blocking_reasons": list(receipt.blocking_reasons),
                    "retryable": receipt.retryable,
                },
                "purged",
            )
        return state

    def _store(
        self,
        operation_id: str,
        kind: str,
        target_id: str,
        receipt_json: Mapping[str, Any],
        state: str,
    ) -> None:
        self._repository.store_receipt(
            operation_id=operation_id,
            kind=kind,
            target_id=target_id,
            receipt_json=receipt_json,
            state=state,
        )
