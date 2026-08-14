"""Generation GC handoff chain tests with fake owner ports."""

from __future__ import annotations

from retention_helpers import build_engine, fixed_now

from app.indexing.models import IndexGenerationGcReceipt
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.retention.gc_handoff import GenerationGcCoordinator
from app.retention.repository import SqlAlchemyRetentionRepository
from app.retention.schema import retention_metadata


class FakeIndexingGcPort:
    def __init__(self, *, request_state: str = "accepted") -> None:
        self.request_state = request_state
        self.request_calls: list[str] = []
        self.complete_calls: list[str] = []

    def request_index_generation_gc(
        self, *, candidate_generation_id: str, reconciliation_run_id: str, operation_id: str
    ) -> IndexGenerationGcReceipt:
        del reconciliation_run_id
        self.request_calls.append(operation_id)
        if self.request_state == "blocked":
            return IndexGenerationGcReceipt(
                operation_id, candidate_generation_id, "blocked", ("active_lease",), True
            )
        if self.request_state == "already_purged":
            return IndexGenerationGcReceipt(operation_id, candidate_generation_id, "already_purged")
        return IndexGenerationGcReceipt(operation_id, candidate_generation_id, "accepted")

    def complete_index_generation_gc(
        self, *, candidate_generation_id: str, operation_id: str
    ) -> IndexGenerationGcReceipt:
        self.complete_calls.append(operation_id)
        return IndexGenerationGcReceipt(operation_id, candidate_generation_id, "already_purged")


class FakeGraphGcPort:
    def __init__(self, *, state: str = "completed") -> None:
        self.state = state
        self.calls: list[str] = []

    def request_public_graph_component_gc(
        self,
        *,
        candidate_generation_id: str,
        graph_component_id: str,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        del candidate_generation_id, reconciliation_run_id
        self.calls.append(operation_id)
        if self.state == "blocked":
            return {
                "state": "blocked",
                "operation_id": operation_id,
                "graph_component_id": graph_component_id,
                "blocking_reasons": ["manifest_mismatch"],
                "retryable": True,
            }
        return {
            "state": self.state,
            "operation_id": operation_id,
            "graph_component_id": graph_component_id,
            "blocking_reasons": [],
            "retryable": False,
        }


def _coordinator(engine, indexing_port, graph_port):
    repository = SqlAlchemyRetentionRepository(engine, now=lambda connection=None: fixed_now())
    return repository, GenerationGcCoordinator(
        repository=repository,
        indexing_gc_port=indexing_port,
        graph_gc_port=graph_port,
    )


def test_blocked_request_is_stored_and_never_completed() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    indexing = FakeIndexingGcPort(request_state="blocked")
    repository, coordinator = _coordinator(engine, indexing, FakeGraphGcPort())
    result = coordinator.handoff(
        candidate_generation_id="gen_1", reconciliation_run_id="run_1", component_ids=[]
    )
    assert result["state"] == "blocked"
    assert indexing.complete_calls == []
    receipt = repository.get_receipt("gc:run_1:gen_1")
    assert receipt is not None
    assert receipt["state"] == "blocked"
    assert receipt["receipt_json"]["blocking_reasons"] == ["active_lease"]


def test_already_purged_is_recorded_without_graph_or_complete() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    indexing = FakeIndexingGcPort(request_state="already_purged")
    graph = FakeGraphGcPort()
    repository, coordinator = _coordinator(engine, indexing, graph)
    result = coordinator.handoff(
        candidate_generation_id="gen_1", reconciliation_run_id="run_1", component_ids=["g_1"]
    )
    assert result["state"] == "purged"
    assert graph.calls == []
    assert indexing.complete_calls == []


def test_accepted_without_components_completes_immediately() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    indexing = FakeIndexingGcPort(request_state="accepted")
    repository, coordinator = _coordinator(engine, indexing, FakeGraphGcPort())
    result = coordinator.handoff(
        candidate_generation_id="gen_1", reconciliation_run_id="run_1", component_ids=[]
    )
    assert result["state"] == "purged"
    assert indexing.complete_calls == ["gc:run_1:gen_1"]
    receipt = repository.get_receipt("gc:run_1:gen_1")
    assert receipt is not None
    assert receipt["state"] == "purged"


def test_blocked_graph_component_defers_completion() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    indexing = FakeIndexingGcPort(request_state="accepted")
    graph = FakeGraphGcPort(state="blocked")
    repository, coordinator = _coordinator(engine, indexing, graph)
    result = coordinator.handoff(
        candidate_generation_id="gen_1", reconciliation_run_id="run_1", component_ids=["g_1"]
    )
    assert result["state"] == "blocked"
    assert indexing.complete_calls == []
    component_receipt = repository.get_receipt("gcc:run_1:g_1")
    assert component_receipt is not None
    assert component_receipt["state"] == "blocked"


def test_completed_graph_component_then_complete_indexing() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    indexing = FakeIndexingGcPort(request_state="accepted")
    graph = FakeGraphGcPort(state="completed")
    repository, coordinator = _coordinator(engine, indexing, graph)
    result = coordinator.handoff(
        candidate_generation_id="gen_1", reconciliation_run_id="run_1", component_ids=["g_1"]
    )
    assert result["state"] == "purged"
    assert graph.calls == ["gcc:run_1:g_1"]
    assert indexing.complete_calls == ["gc:run_1:gen_1"]
    component_receipt = repository.get_receipt("gcc:run_1:g_1")
    assert component_receipt is not None
    assert component_receipt["state"] == "completed"


def test_failed_completion_error_propagates_without_false_success() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)

    class FailingIndexing(FakeIndexingGcPort):
        def complete_index_generation_gc(
            self, *, candidate_generation_id: str, operation_id: str
        ) -> IndexGenerationGcReceipt:
            raise PlatformError("gc_blocked", "not eligible", {}, 409)

    repository, coordinator = _coordinator(engine, FailingIndexing(), FakeGraphGcPort())
    import pytest

    with pytest.raises(PlatformError) as excinfo:
        coordinator.handoff(
            candidate_generation_id="gen_1", reconciliation_run_id="run_1", component_ids=[]
        )
    assert excinfo.value.code == "gc_blocked"
