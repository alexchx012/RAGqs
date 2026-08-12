"""Public graph build domain tests: run state machine, staging, terminal events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

from app.documents.public_graph import PublicGraphSourceService
from app.documents.schema import documents_metadata
from app.graph import (
    DeterministicPublicGraphExtractor,
    GenerationGraphAvailability,
    GraphBuildService,
    GraphBuildWorker,
    RepositoryActivatedReceiptVerifier,
    SqlAlchemyGraphRepository,
)
from app.graph.schema import graph_build_runs_table, graph_metadata
from app.indexing import GenerationManager, GraphComponentCoordinator
from app.platform.errors import PlatformError


@dataclass
class FixedClock:
    now: datetime

    def tick(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def __call__(self) -> datetime:
        return self.now


class _RecordingUsage:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def prepare_provider_call(self, **kwargs: object) -> str:
        call_id = f"pcall_{len(self.calls)}"
        self.calls.append({"phase": "prepared", "call_id": call_id, **kwargs})
        return call_id

    def mark_dispatching(self, provider_call_id: str, **kwargs: object) -> bool:
        del kwargs
        self.calls.append({"phase": "dispatching", "call_id": provider_call_id})
        return True

    def complete_provider_call(self, **kwargs: object) -> str:
        self.calls.append({"phase": "completed", **kwargs})
        return str(kwargs["provider_call_id"])

    def mark_not_sent(self, provider_call_id: str) -> None:
        self.calls.append({"phase": "not_sent", "call_id": provider_call_id})

    def mark_unknown(self, provider_call_id: str) -> None:
        self.calls.append({"phase": "unknown", "call_id": provider_call_id})


class _RecordingOutbox:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish_completed(self, **event: object) -> str:
        self.events.append(event)
        return f"evt_{len(self.events)}"


class _RecordingSourceOutbox:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish_public_graph_source_change(self, **event: object) -> str:
        self.events.append(event)
        return f"src_evt_{len(self.events)}"


class _FailingExtractor:
    def estimate_primary_model_calls(self, snapshot: object) -> int:
        raise PlatformError("graph_build_estimate_unavailable", "estimate unavailable", {}, 503)

    def extract(self, snapshot: object, session: object) -> None:
        raise AssertionError("extract should not be reached")


class _SourceChangingExtractor(DeterministicPublicGraphExtractor):
    def __init__(self, source: PublicGraphSourceService) -> None:
        self._source = source

    def extract(self, snapshot: object, session: object) -> None:
        self._source.record_source_change(
            space_id="public",
            document_id="doc_9",
            change_type="publish",
            publications=[_publication(9)],
        )
        super().extract(snapshot, session)  # type: ignore[arg-type]


def _publication(number: int) -> dict[str, str]:
    return {
        "document_id": f"doc_{number}",
        "document_version_id": f"ver_{number}",
        "publication_id": f"pub_{number}",
        "content_manifest_id": f"manifest_{number}",
        "content_manifest_hash": f"hash_{number}",
    }


def _build_env(now: FixedClock | None = None, *, extractor: object | None = None):
    clock = now or FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    documents_metadata.create_all(engine)
    graph_metadata.create_all(engine)
    source = PublicGraphSourceService(
        engine,
        trusted_consumers={"indexing": {"indexing"}, "public_graph": {"public_graph"}},
        outbox_port=_RecordingSourceOutbox(),
    )
    manager = GenerationManager(now=clock)
    coordinator = GraphComponentCoordinator(manager, source, consumer_id="indexing")
    repository = SqlAlchemyGraphRepository(engine, now=clock)
    usage = _RecordingUsage()
    outbox = _RecordingOutbox()
    verifier = RepositoryActivatedReceiptVerifier(repository)
    service = GraphBuildService(
        engine,
        repository=repository,
        source=source,
        coordinator=coordinator,
        availability=GenerationGraphAvailability(manager),
        extractor=extractor or DeterministicPublicGraphExtractor(),
        outbox=outbox,
        verifier=verifier,
        now=clock,
    )
    worker = GraphBuildWorker(
        service, extractor or DeterministicPublicGraphExtractor(), usage, now=clock
    )
    return engine, source, coordinator, usage, outbox, service, worker, clock


def _publish(source: PublicGraphSourceService, count: int = 2) -> None:
    source.record_source_change(
        space_id="public",
        document_id=f"doc_{1}",
        change_type="publish",
        publications=[_publication(number) for number in range(1, count + 1)],
    )


def _create(service: GraphBuildService, *, key: str = "k1", revision: int = 1) -> dict[str, object]:
    view = service.create(
        initiator_identity_id="user_ops_1",
        expected_source_revision=revision,
        idempotency_key=key,
        request_hash=f"hash-{key}-{revision}",
    )
    return view.to_dict()


def test_create_queued_run_contract() -> None:
    _, source, _, _, outbox, service, _, _ = _build_env()
    _publish(source)
    view = _create(service)
    assert view["state"] == "queued"
    assert view["version"] == 1
    assert view["source_revision"] == 1
    assert view["estimated_primary_model_calls"] == 2
    assert view["actual_usage"] is None
    assert view["graph_generation_id"] is None
    assert view["allowed_actions"] == ["cancel"]
    assert outbox.events == []


def test_create_idempotent_same_key_replays_first_result() -> None:
    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    first = _create(service, key="idem", revision=1)
    second = _create(service, key="idem", revision=1)
    assert second["graph_build_id"] == first["graph_build_id"]
    assert second["version"] == first["version"]


def test_create_same_key_different_request_conflicts() -> None:
    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    _create(service, key="idem", revision=1)
    with pytest.raises(PlatformError) as error:
        _create(service, key="idem", revision=0)
    assert error.value.code == "idempotency_key_conflict"


def test_create_rejects_wrong_revision() -> None:
    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    with pytest.raises(PlatformError) as error:
        _create(service, revision=99)
    assert error.value.code == "graph_source_changed"


def test_create_rejects_empty_source() -> None:
    _, _, _, _, _, service, _, _ = _build_env()
    with pytest.raises(PlatformError) as error:
        _create(service, revision=0)
    assert error.value.code == "graph_source_empty"


def test_create_rejects_when_run_active() -> None:
    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    _create(service)
    with pytest.raises(PlatformError) as error:
        _create(service, key="k2")
    assert error.value.code == "graph_build_in_progress"


def test_create_estimate_unavailable_returns_503() -> None:
    _, source, _, _, _, service, _, _ = _build_env(extractor=_FailingExtractor())
    _publish(source)
    with pytest.raises(PlatformError) as error:
        _create(service)
    assert error.value.code == "graph_build_estimate_unavailable"


def test_worker_build_succeeds_and_publishes_single_event() -> None:
    _, source, _, usage, outbox, service, worker, _ = _build_env()
    _publish(source)
    created = _create(service)
    stats = worker.run_once()
    assert stats.builds_processed == 1
    assert stats.runs_failed == 0
    current = service.current()
    assert current["graph_availability"] == "ready"
    assert current["active_generation"]["graph_generation_id"].startswith("graph_generation_")
    assert current["latest_run"]["state"] == "succeeded"
    assert (
        current["latest_run"]["graph_generation_id"]
        == current["active_generation"]["graph_generation_id"]
    )
    assert current["latest_run"]["allowed_actions"] == []
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["status"] == "succeeded"
    assert event["graph_generation_id"] == current["active_generation"]["graph_generation_id"]
    assert event["recipient_user_id"] == "user_ops_1"
    assert event["transition_version"] == current["latest_run"]["version"]
    completed = [call for call in usage.calls if call["phase"] == "completed"]
    assert len(completed) == 2
    for call in completed:
        assert call["ownership"].cost_center_key == "public"
        assert call["result"] == "succeeded"
    assert created["graph_build_id"] == current["latest_run"]["graph_build_id"]


def test_current_disabled_before_any_build() -> None:
    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    current = service.current()
    assert current["graph_availability"] == "disabled"
    assert current["active_generation"] is None
    assert current["latest_run"] is None
    assert current["source_revision"] == 1


def test_source_change_makes_ready_component_stale() -> None:
    _, source, _, _, _, service, worker, _ = _build_env()
    _publish(source)
    _create(service)
    worker.run_once()
    assert service.current()["graph_availability"] == "ready"
    source.record_source_change(
        space_id="public",
        document_id="doc_3",
        change_type="publish",
        publications=[_publication(3)],
    )
    current = service.current()
    assert current["graph_availability"] == "stale"
    assert current["active_generation"]["source_revision"] == 1
    assert current["source_revision"] == 2


def test_cancel_queued_run_contract() -> None:
    _, source, _, _, outbox, service, _, _ = _build_env()
    _publish(source)
    created = _create(service, key="create")
    cancelled = service.cancel(
        actor_identity_id="user_ops_1",
        graph_build_id=created["graph_build_id"],
        expected_version=1,
        idempotency_key="cancel-1",
        request_hash="hash-cancel",
    ).to_dict()
    assert cancelled["state"] == "cancelled"
    assert cancelled["version"] == 2
    assert cancelled["allowed_actions"] == []
    assert len(outbox.events) == 1
    assert outbox.events[0]["status"] == "cancelled"
    assert outbox.events[0].get("graph_generation_id") is None
    replay = service.cancel(
        actor_identity_id="user_ops_1",
        graph_build_id=created["graph_build_id"],
        expected_version=1,
        idempotency_key="cancel-1",
        request_hash="hash-cancel",
    ).to_dict()
    assert replay["version"] == cancelled["version"]
    assert len(outbox.events) == 1
    with pytest.raises(PlatformError) as error:
        service.cancel(
            actor_identity_id="user_ops_1",
            graph_build_id=created["graph_build_id"],
            expected_version=2,
            idempotency_key="cancel-2",
            request_hash="hash-cancel-2",
        )
    assert error.value.code == "graph_build_not_cancellable"


def test_cancel_version_conflict() -> None:
    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    created = _create(service)
    with pytest.raises(PlatformError) as error:
        service.cancel(
            actor_identity_id="user_ops_1",
            graph_build_id=created["graph_build_id"],
            expected_version=7,
            idempotency_key="cancel-x",
            request_hash="hash-cancel-x",
        )
    assert error.value.code == "version_conflict"


def test_lease_loss_requeues_same_run_without_terminal_event() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, _, _, outbox, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    first = service.claim_next(owner="worker-a")
    assert first is not None
    assert first.state == "running"
    assert first.current_attempt == 1
    first_fence = first.fencing_token
    clock.tick(600)
    assert service.requeue_expired() == 1
    second = service.claim_next(owner="worker-b")
    assert second is not None
    assert second.current_attempt == 2
    assert second.fencing_token != first_fence
    assert outbox.events == []
    with engine.begin() as connection:
        row = connection.execute(
            update(graph_build_runs_table)
            .where(graph_build_runs_table.c.id == second.graph_build_id)
            .values(lease_expires_at_utc=clock.now + timedelta(minutes=30))
        )
        assert row.rowcount == 1


def test_source_change_during_build_fails_and_discards() -> None:
    engine_env = _build_env()
    source = engine_env[1]
    service = engine_env[5]
    outbox = engine_env[4]
    usage = engine_env[3]
    clock = engine_env[7]
    extractor = _SourceChangingExtractor(source)
    worker = GraphBuildWorker(service, extractor, usage, now=clock)
    _publish(source)
    _create(service)
    stats = worker.run_once()
    assert stats.runs_failed >= 1
    current = service.current()
    assert current["latest_run"]["state"] == "failed"
    assert current["latest_run"]["failure_class"] == "graph_source_changed"
    assert len(outbox.events) == 1
    assert outbox.events[0]["status"] == "failed"
    assert outbox.events[0]["failure_class"] == "graph_source_changed"
    assert current["graph_availability"] in {"disabled", "stale"}


def test_expired_grant_fails_run_stably() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, _, _, outbox, service, worker, _ = _build_env(now=clock)
    _publish(source)
    created = _create(service)
    with engine.begin() as connection:
        connection.execute(
            update(graph_build_runs_table)
            .where(graph_build_runs_table.c.id == created["graph_build_id"])
            .values(grant_expires_at_utc=clock.now - timedelta(minutes=1))
        )
    stats = worker.run_once()
    assert stats.runs_failed == 1
    current = service.current()
    assert current["latest_run"]["state"] == "failed"
    assert current["latest_run"]["failure_class"] == "graph_stage_grant_expired"
    assert len(outbox.events) == 1
    assert outbox.events[0]["failure_class"] == "graph_stage_grant_expired"


def test_gc_request_contract() -> None:
    _, source, _, _, _, service, worker, _ = _build_env()
    _publish(source)
    _create(service)
    worker.run_once()
    current = service.current()
    candidate = current["active_generation"]["graph_generation_id"]
    with pytest.raises(PlatformError) as error:
        service.request_public_graph_component_gc(
            caller="not-retention",
            candidate_generation_id=candidate,
            graph_component_id="public_graph",
            reconciliation_run_id="recon_1",
            operation_id="gc_1",
        )
    assert error.value.code == "forbidden"
    blocked = service.request_public_graph_component_gc(
        caller="retention-ops",
        candidate_generation_id=candidate,
        graph_component_id="public_graph",
        reconciliation_run_id="recon_1",
        operation_id="gc_1",
    )
    assert blocked["state"] == "blocked"
    assert "active_generation" in blocked["blocking_reasons"]
    replay = service.request_public_graph_component_gc(
        caller="retention-ops",
        candidate_generation_id=candidate,
        graph_component_id="public_graph",
        reconciliation_run_id="recon_1",
        operation_id="gc_1",
    )
    assert replay == blocked
    with pytest.raises(PlatformError) as error:
        service.request_public_graph_component_gc(
            caller="retention-ops",
            candidate_generation_id=candidate,
            graph_component_id="public_graph",
            reconciliation_run_id="recon_other",
            operation_id="gc_1",
        )
    assert error.value.code == "idempotency_key_conflict"
