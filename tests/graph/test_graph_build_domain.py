"""Public graph build domain tests: run state machine, staging, terminal events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
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
from app.graph.models import GraphRunView
from app.graph.models import _iso as graph_iso
from app.graph.schema import (
    graph_build_attempts_table,
    graph_build_operations_table,
    graph_build_runs_table,
    graph_metadata,
    graph_staging_resources_table,
)
from app.graph.service import CANCEL_OP_PREFIX, CREATE_OP_PREFIX
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


class _UnexpectedExtractor:
    def estimate_primary_model_calls(self, snapshot: Any) -> int:
        return len(snapshot.publications)

    def extract(self, snapshot: object, session: object) -> None:
        del snapshot, session
        raise RuntimeError("unexpected graph worker fault")


class _ProviderFailingExtractor:
    def estimate_primary_model_calls(self, snapshot: object) -> int:
        del snapshot
        return 1

    def extract(self, snapshot: object, session: object) -> None:
        del snapshot, session
        raise PlatformError("graph_provider_call_failed", "provider failed", {}, 503)


class _DeadlineAfterFirstFailure:
    def __init__(self) -> None:
        self.primary_calls = 0

    def heartbeat(self) -> None:
        return None

    def deadline_expired(self) -> bool:
        return self.primary_calls > 0

    def primary_call(self, **_kwargs: object) -> object:
        self.primary_calls += 1
        raise PlatformError("graph_provider_dispatch_failed", "dispatch failed", {}, 503)


def _publication(number: int) -> dict[str, str]:
    return {
        "document_id": f"doc_{number}",
        "document_version_id": f"ver_{number}",
        "publication_id": f"pub_{number}",
        "content_manifest_id": f"manifest_{number}",
        "content_manifest_hash": f"hash_{number}",
    }


def _empty_graph_payload(number: int = 1) -> dict[str, object]:
    return {
        "graph": {
            "source": {
                "document_id": f"doc_{number}",
                "content_manifest_id": f"manifest_{number}",
            },
            "nodes": [],
            "edges": [],
        }
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


def test_graph_operations_support_the_maximum_idempotency_key() -> None:
    key = "x" * 256
    operation_id_capacity = graph_build_operations_table.c.operation_id.type.length

    assert operation_id_capacity is not None
    assert operation_id_capacity >= max(len(CREATE_OP_PREFIX), len(CANCEL_OP_PREFIX)) + 1 + len(key)

    _, source, _, _, _, service, _, _ = _build_env()
    _publish(source)
    created = _create(service, key=key)
    assert _create(service, key=key)["graph_build_id"] == created["graph_build_id"]

    cancelled = service.cancel(
        actor_identity_id="user_ops_1",
        graph_build_id=created["graph_build_id"],
        expected_version=1,
        idempotency_key=key,
        request_hash="hash-cancel-max-key",
    ).to_dict()
    replay = service.cancel(
        actor_identity_id="user_ops_1",
        graph_build_id=created["graph_build_id"],
        expected_version=1,
        idempotency_key=key,
        request_hash="hash-cancel-max-key",
    ).to_dict()

    assert replay["version"] == cancelled["version"]


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
        assert call["ownership"].cost_center_key == "system:graph"
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


def test_cancel_running_run_deletes_current_attempt_staging_before_replay() -> None:
    engine, source, _, _, outbox, service, _, _ = _build_env()
    _publish(source)
    created = _create(service, key="create-running-cancel")
    run = service.claim_next(owner="worker-a")
    assert run is not None
    service.write_staging_resource(
        run=run,
        resource_kind="publication_graph",
        resource_id="pub_1",
        payload={"graph": {}},
    )

    cancelled = service.cancel(
        actor_identity_id="user_ops_1",
        graph_build_id=created["graph_build_id"],
        expected_version=run.version,
        idempotency_key="cancel-running",
        request_hash="hash-cancel-running",
    )

    with engine.connect() as connection:
        staged_after_cancel = (
            connection.execute(
                select(graph_staging_resources_table.c.id).where(
                    graph_staging_resources_table.c.run_id == run.graph_build_id
                )
            )
            .scalars()
            .all()
        )

    replay = service.cancel(
        actor_identity_id="user_ops_1",
        graph_build_id=created["graph_build_id"],
        expected_version=run.version,
        idempotency_key="cancel-running",
        request_hash="hash-cancel-running",
    )

    with engine.connect() as connection:
        staged_after_replay = (
            connection.execute(
                select(graph_staging_resources_table.c.id).where(
                    graph_staging_resources_table.c.run_id == run.graph_build_id
                )
            )
            .scalars()
            .all()
        )

    assert cancelled.state == "cancelled"
    assert replay.version == cancelled.version
    assert staged_after_cancel == []
    assert staged_after_replay == []
    assert len(outbox.events) == 1


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


def test_heartbeat_renews_run_and_attempt_leases() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None

    clock.tick(60)
    assert service.heartbeat(run=run, owner="worker-a") is True

    with engine.connect() as connection:
        run_lease = connection.execute(
            select(graph_build_runs_table.c.lease_expires_at_utc).where(
                graph_build_runs_table.c.id == run.graph_build_id
            )
        ).scalar_one()
        attempt_lease = connection.execute(
            select(graph_build_attempts_table.c.lease_expires_at_utc).where(
                graph_build_attempts_table.c.run_id == run.graph_build_id,
                graph_build_attempts_table.c.attempt == run.current_attempt,
            )
        ).scalar_one()
    expected = clock.now + timedelta(minutes=5)
    assert _as_utc(run_lease) == expected
    assert _as_utc(attempt_lease) == expected


def test_expired_heartbeat_cannot_revive_a_lease() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None

    clock.tick(301)

    assert service.heartbeat(run=run, owner="worker-a") is False


def test_expired_attempt_requeue_discards_its_staging_resources() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    service.write_staging_resource(
        run=run,
        resource_kind="publication_graph",
        resource_id="pub_1",
        payload={"graph": {}},
    )

    clock.tick(301)

    assert service.requeue_expired() == 1
    with engine.connect() as connection:
        remaining = connection.execute(
            select(graph_staging_resources_table.c.id).where(
                graph_staging_resources_table.c.run_id == run.graph_build_id,
                graph_staging_resources_table.c.attempt == run.current_attempt,
            )
        ).all()
    assert remaining == []


def test_recovery_skips_an_attempt_that_renewed_after_the_expiry_scan() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, _, _, _, service, _, _ = _build_env(now=clock)
    repository = SqlAlchemyGraphRepository(engine, now=clock)
    _publish(source)
    created = _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    service.write_staging_resource(
        run=run,
        resource_kind="publication_graph",
        resource_id="staged-before-renewal",
        payload={"graph": {}},
    )
    stale_recovery_time = clock.now + timedelta(seconds=301)

    clock.tick(60)
    assert service.heartbeat(run=run, owner="worker-a") is True

    with engine.begin() as connection:
        requeued = repository.invalidate_attempt_and_requeue(
            connection=connection,
            graph_build_id=str(created["graph_build_id"]),
            attempt=run.current_attempt,
            now=stale_recovery_time,
        )
    with engine.connect() as connection:
        state = connection.execute(
            select(graph_build_runs_table.c.state).where(
                graph_build_runs_table.c.id == created["graph_build_id"]
            )
        ).scalar_one()
        staging = (
            connection.execute(
                select(graph_staging_resources_table.c.id).where(
                    graph_staging_resources_table.c.run_id == created["graph_build_id"]
                )
            )
            .scalars()
            .all()
        )

    assert requeued is False
    assert state == "running"
    assert staging != []


def test_stale_expiry_recovery_cannot_discard_reclaimed_attempt_component_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, coordinator, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    first = service.claim_next(owner="worker-a")
    assert first is not None
    service.write_staging_resource(
        run=first,
        resource_kind="publication_graph",
        resource_id="pub_1",
        payload={"graph": {}},
    )
    clock.tick(301)

    stale_cleanup_waiting = Event()
    allow_stale_cleanup = Event()
    stale_results: list[int] = []
    original_discard = service._discard_component_stage

    def pause_stale_cleanup(*args: object, **kwargs: object) -> bool:
        if current_thread().name == "stale-recovery":
            stale_cleanup_waiting.set()
            assert allow_stale_cleanup.wait(timeout=2)
        return original_discard(*args, **kwargs)

    def requeue_stale_attempt() -> None:
        stale_results.append(service.requeue_expired())

    monkeypatch.setattr(service, "_discard_component_stage", pause_stale_cleanup)
    stale_recovery = Thread(target=requeue_stale_attempt, name="stale-recovery")
    try:
        stale_recovery.start()
        assert stale_cleanup_waiting.wait(timeout=2)

        assert service.requeue_expired() == 1
        retry = service.claim_next(owner="worker-b")
        assert retry is not None
        service.write_staging_resource(
            run=retry,
            resource_kind="publication_graph",
            resource_id="pub_1",
            payload={"graph": {}},
        )
        retry_stage_id = service.stage_component(run=retry)

        allow_stale_cleanup.set()
        stale_recovery.join(timeout=2)
        assert not stale_recovery.is_alive()
    finally:
        allow_stale_cleanup.set()
        stale_recovery.join(timeout=2)

    assert stale_results == [0]
    component = coordinator._generation.get_generation(retry.target_generation_id).manifest[
        "components"
    ]["public_graph"]
    assert component["state"] == "staged"
    assert component["stage_receipt_id"] == retry_stage_id


def test_expired_reserved_operation_can_be_reclaimed() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, _, _, _, _, _, _, _ = _build_env(now=clock)
    repository = SqlAlchemyGraphRepository(engine, now=clock)
    operation_id = "gb_create_recoverable"

    with engine.begin() as connection:
        reservation, response, _ = repository.reserve_operation(
            connection=connection,
            operation_id=operation_id,
            kind="graph_build_create",
            request_hash="request-hash",
        )
    assert reservation == "created"
    assert response is None

    clock.tick(301)

    with engine.begin() as connection:
        reservation, response, _ = repository.reserve_operation(
            connection=connection,
            operation_id=operation_id,
            kind="graph_build_create",
            request_hash="request-hash",
        )
        created_at = connection.execute(
            select(graph_build_operations_table.c.created_at_utc).where(
                graph_build_operations_table.c.operation_id == operation_id
            )
        ).scalar_one()
    assert reservation == "created"
    assert response is None
    assert _as_utc(created_at) == clock.now


def test_reclaimed_operation_cannot_be_completed_by_the_old_reservation() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, _, _, _, _, _, _, _ = _build_env(now=clock)
    repository = SqlAlchemyGraphRepository(engine, now=clock)
    operation_id = "gb_create_fenced_reclaim"

    with engine.begin() as connection:
        reservation, response, original_reservation = repository.reserve_operation(
            connection=connection,
            operation_id=operation_id,
            kind="graph_build_create",
            request_hash="request-hash",
        )
    assert reservation == "created"
    assert response is None
    assert original_reservation is not None

    clock.tick(301)

    with engine.begin() as connection:
        reservation, response, reclaimed_reservation = repository.reserve_operation(
            connection=connection,
            operation_id=operation_id,
            kind="graph_build_create",
            request_hash="request-hash",
        )
    assert reservation == "created"
    assert response is None
    assert reclaimed_reservation is not None
    assert reclaimed_reservation != original_reservation

    with engine.begin() as connection:
        with pytest.raises(PlatformError) as error:
            repository.complete_operation(
                connection=connection,
                operation_id=operation_id,
                reservation_created_at=original_reservation,
                response={"run": {"graph_build_id": "old"}},
            )
    assert error.value.code == "idempotency_key_conflict"

    with engine.begin() as connection:
        repository.complete_operation(
            connection=connection,
            operation_id=operation_id,
            reservation_created_at=reclaimed_reservation,
            response={"run": {"graph_build_id": "reclaimed"}},
        )
    with engine.begin() as connection:
        reservation, replay, replay_reservation = repository.reserve_operation(
            connection=connection,
            operation_id=operation_id,
            kind="graph_build_create",
            request_hash="request-hash",
        )

    assert reservation == "replay"
    assert replay == {"run": {"graph_build_id": "reclaimed"}}
    assert replay_reservation is None


def test_reclaimed_create_reservation_fences_the_old_run_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, coordinator, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    original_reserve = coordinator.reserve_graph_component_stage
    old_waiting = Event()
    new_waiting = Event()
    allow_old = Event()
    allow_new = Event()
    old_result: list[object] = []
    new_result: list[object] = []

    def pause_before_stage_grant(*args: object, **kwargs: object) -> object:
        if current_thread().name == "old-create":
            old_waiting.set()
            assert allow_old.wait(timeout=2)
        elif current_thread().name == "new-create":
            new_waiting.set()
            assert allow_new.wait(timeout=2)
        return original_reserve(*args, **kwargs)

    def create_into(result: list[object]) -> None:
        try:
            result.append(_create(service, key="reclaimed-create"))
        except PlatformError as error:
            result.append(error)

    monkeypatch.setattr(coordinator, "reserve_graph_component_stage", pause_before_stage_grant)
    old = Thread(target=create_into, args=(old_result,), name="old-create")
    new = Thread(target=create_into, args=(new_result,), name="new-create")
    try:
        old.start()
        assert old_waiting.wait(timeout=2)
        clock.tick(301)
        new.start()
        assert new_waiting.wait(timeout=2)
        allow_old.set()
        old.join(timeout=2)
        assert not old.is_alive()
        allow_new.set()
        new.join(timeout=2)
        assert not new.is_alive()
    finally:
        allow_old.set()
        allow_new.set()
        old.join(timeout=2)
        new.join(timeout=2)

    assert len(old_result) == 1
    assert isinstance(old_result[0], PlatformError)
    assert old_result[0].code == "idempotency_key_conflict"
    assert len(new_result) == 1
    assert isinstance(new_result[0], dict)
    with engine.begin() as connection:
        runs = connection.execute(select(graph_build_runs_table.c.id)).scalars().all()
    assert runs == [new_result[0]["graph_build_id"]]


def test_reclaimed_cancel_reservation_fences_the_old_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    created = _create(service)
    original_cancel = service._cancel_transaction
    old_waiting = Event()
    new_waiting = Event()
    allow_old = Event()
    allow_new = Event()
    old_result: list[object] = []
    new_result: list[object] = []

    def pause_before_cancel_transaction(*args: object, **kwargs: object) -> object:
        if current_thread().name == "old-cancel":
            old_waiting.set()
            assert allow_old.wait(timeout=2)
        elif current_thread().name == "new-cancel":
            new_waiting.set()
            assert allow_new.wait(timeout=2)
        return original_cancel(*args, **kwargs)

    def cancel_into(result: list[object]) -> None:
        try:
            result.append(
                service.cancel(
                    actor_identity_id="user_ops_1",
                    graph_build_id=str(created["graph_build_id"]),
                    expected_version=int(created["version"]),
                    idempotency_key="reclaimed-cancel",
                    request_hash="cancel-hash",
                )
            )
        except PlatformError as error:
            result.append(error)

    monkeypatch.setattr(service, "_cancel_transaction", pause_before_cancel_transaction)
    old = Thread(target=cancel_into, args=(old_result,), name="old-cancel")
    new = Thread(target=cancel_into, args=(new_result,), name="new-cancel")
    try:
        old.start()
        assert old_waiting.wait(timeout=2)
        clock.tick(301)
        new.start()
        assert new_waiting.wait(timeout=2)
        allow_old.set()
        old.join(timeout=2)
        assert not old.is_alive()
        allow_new.set()
        new.join(timeout=2)
        assert not new.is_alive()
    finally:
        allow_old.set()
        allow_new.set()
        old.join(timeout=2)
        new.join(timeout=2)

    assert len(old_result) == 1
    assert isinstance(old_result[0], PlatformError)
    assert old_result[0].code == "idempotency_key_conflict"
    assert len(new_result) == 1
    assert isinstance(new_result[0], GraphRunView)
    assert new_result[0].state == "cancelled"


def test_expired_attempt_cannot_release_a_graph_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    releases: list[dict[str, object]] = []

    def record_release(**kwargs: object) -> object:
        releases.append(kwargs)
        return object()

    monkeypatch.setattr(service._coordinator, "release_graph_component", record_release)
    clock.tick(301)

    with pytest.raises(PlatformError) as error:
        service.release_component(run=run, component_stage_id="component-stage")

    assert error.value.code == "graph_build_lease_lost"
    assert releases == []


def test_expired_attempt_cannot_write_staging_or_usage() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    engine, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    clock.tick(301)

    with pytest.raises(PlatformError) as staging_error:
        service.write_staging_resource(
            run=run,
            resource_kind="publication_graph",
            resource_id="pub_1",
            payload={"graph": {}},
        )
    with pytest.raises(PlatformError) as usage_error:
        service.record_usage(run=run, primary_model_calls=1, provider_calls=1)

    with engine.connect() as connection:
        staged = (
            connection.execute(
                select(graph_staging_resources_table.c.id).where(
                    graph_staging_resources_table.c.run_id == run.graph_build_id
                )
            )
            .scalars()
            .all()
        )
        actual_usage = connection.execute(
            select(
                graph_build_runs_table.c.actual_primary_model_calls,
                graph_build_runs_table.c.actual_provider_calls,
            ).where(graph_build_runs_table.c.id == run.graph_build_id)
        ).one()

    assert staging_error.value.code == "graph_build_lease_lost"
    assert usage_error.value.code == "graph_build_lease_lost"
    assert staged == []
    assert tuple(actual_usage) == (0, 0)


def test_stage_discards_component_when_lease_expires_before_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, coordinator, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    service.write_staging_resource(
        run=run,
        resource_kind="publication_graph",
        resource_id="pub_1",
        payload={"graph": {}},
    )
    original_stage = coordinator.stage_public_graph_component

    def expire_after_stage(*args: object, **kwargs: object) -> object:
        receipt = original_stage(*args, **kwargs)
        clock.tick(301)
        return receipt

    monkeypatch.setattr(coordinator, "stage_public_graph_component", expire_after_stage)

    with pytest.raises(PlatformError) as error:
        service.stage_component(run=run)

    component = coordinator._generation.get_generation(run.target_generation_id).manifest[
        "components"
    ]["public_graph"]
    assert error.value.code == "graph_build_lease_lost"
    assert component["state"] == "disabled"
    assert component["graph_resource_ids"] == []


def test_stale_stage_conflict_cannot_discard_the_reclaimed_component_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, coordinator, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    first = service.claim_next(owner="worker-a")
    assert first is not None
    service.write_staging_resource(
        run=first,
        resource_kind="publication_graph",
        resource_id="pub_1",
        payload={"graph": {}},
    )
    original_stage = coordinator.stage_public_graph_component
    retries: list[object] = []
    retry_stage_ids: list[str] = []
    recovered = False

    def stage_after_reclaim(*args: object, **kwargs: object) -> object:
        nonlocal recovered
        if not recovered:
            recovered = True
            clock.tick(301)
            assert service.requeue_expired() == 1
            retry = service.claim_next(owner="worker-b")
            assert retry is not None
            retries.append(retry)
            service.write_staging_resource(
                run=retry,
                resource_kind="publication_graph",
                resource_id="pub_1",
                payload={"graph": {}},
            )
            retry_stage_ids.append(service.stage_component(run=retry))
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(coordinator, "stage_public_graph_component", stage_after_reclaim)

    with pytest.raises(PlatformError) as error:
        service.stage_component(run=first)

    assert error.value.code == "graph_build_lease_lost"
    assert len(retries) == 1
    component = coordinator._generation.get_generation(first.target_generation_id).manifest[
        "components"
    ]["public_graph"]
    assert component["state"] == "staged"
    assert component["stage_receipt_id"] == retry_stage_ids[0]


def test_release_rejects_lease_expiry_inside_the_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, coordinator, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    service.write_staging_resource(
        run=run,
        resource_kind="publication_graph",
        resource_id="pub_1",
        payload={"graph": {}},
    )
    component_stage_id = service.stage_component(run=run)
    original_release = coordinator.release_graph_component

    def expire_before_activation(*args: object, **kwargs: object) -> object:
        clock.tick(301)
        return original_release(*args, **kwargs)

    monkeypatch.setattr(coordinator, "release_graph_component", expire_before_activation)

    with pytest.raises(PlatformError) as error:
        service.release_component(run=run, component_stage_id=component_stage_id)

    assert error.value.code == "graph_build_lease_lost"
    assert coordinator._generation.active_generation_id == "generation_initial"


def test_expired_attempt_cannot_complete_successfully() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, _, _, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    clock.tick(301)
    receipt = SimpleNamespace(
        state="activated",
        graph_generation_id="graph-generation",
        active_generation_id="index-generation",
        activation_receipt_id="receipt",
    )

    with pytest.raises(PlatformError) as error:
        service.complete_succeeded(run=run, owner="worker-a", release_receipt=receipt)

    assert error.value.code == "graph_build_lease_lost"
    assert service.current()["latest_run"]["state"] == "running"


def test_requeued_attempt_cannot_write_a_failed_terminal_state() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, _, _, outbox, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)
    run = service.claim_next(owner="worker-a")
    assert run is not None
    clock.tick(301)
    assert service.requeue_expired() == 1

    result = service.complete_failed(
        run=run,
        owner="worker-a",
        failure_class="graph_worker_unexpected",
        reason="old worker resumed",
    )

    assert result.state == "queued"
    assert service.current()["latest_run"]["state"] == "queued"
    assert outbox.events == []


def test_stale_worker_fault_does_not_discard_reclaimed_attempt_component_stage() -> None:
    clock = FixedClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    _, source, _, usage, _, service, _, _ = _build_env(now=clock)
    _publish(source)
    _create(service)

    class _StaleWorkerFaultExtractor:
        retry = None
        component_stage_id = ""

        def estimate_primary_model_calls(self, snapshot: object) -> int:
            return len(snapshot.publications)

        def extract(self, snapshot: object, session: object) -> None:
            del snapshot, session
            clock.tick(301)
            assert service.requeue_expired() == 1
            retry = service.claim_next(owner="worker-b")
            assert retry is not None
            service.write_staging_resource(
                run=retry,
                resource_kind="publication_graph",
                resource_id="pub_1",
                payload=_empty_graph_payload(),
            )
            self.retry = retry
            self.component_stage_id = service.stage_component(run=retry)
            raise RuntimeError("worker-a resumed after lease recovery")

    extractor = _StaleWorkerFaultExtractor()
    worker = GraphBuildWorker(service, extractor, usage, owner="worker-a", now=clock)

    stats = worker.run_once()

    assert stats.runs_failed == 1
    assert extractor.retry is not None
    receipt = service.release_component(
        run=extractor.retry,
        component_stage_id=extractor.component_stage_id,
    )
    service.complete_succeeded(run=extractor.retry, owner="worker-b", release_receipt=receipt)
    assert service.current()["latest_run"]["state"] == "succeeded"


def test_worker_heartbeats_while_extracting_publications(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, _, _, _, service, worker, _ = _build_env()
    _publish(source, count=2)
    _create(service)
    calls = 0
    original_heartbeat = service.heartbeat

    def record_heartbeat(*, run, owner: str) -> bool:
        nonlocal calls
        calls += 1
        return original_heartbeat(run=run, owner=owner)

    monkeypatch.setattr(service, "heartbeat", record_heartbeat)

    worker.run_once()

    assert calls >= 2


def test_extractor_stops_retrying_after_the_session_deadline() -> None:
    session = _DeadlineAfterFirstFailure()

    with pytest.raises(PlatformError) as error:
        DeterministicPublicGraphExtractor()._call_with_retries(
            session,  # type: ignore[arg-type]
            resource_id="manifest_1",
            request_fingerprint="fingerprint",
            send=lambda: {},
        )

    assert error.value.code == "graph_provider_dispatch_failed"
    assert session.primary_calls == 1


def test_worker_distinguishes_unknown_faults_from_provider_failures() -> None:
    _, source, _, _, _, service, worker, _ = _build_env(extractor=_UnexpectedExtractor())
    _publish(source)
    _create(service)

    stats = worker.run_once()

    assert stats.runs_failed == 1
    assert service.current()["latest_run"]["failure_class"] == "graph_worker_unexpected"


def test_worker_preserves_provider_failure_class() -> None:
    _, source, _, _, _, service, worker, _ = _build_env(extractor=_ProviderFailingExtractor())
    _publish(source)
    _create(service)

    stats = worker.run_once()

    assert stats.runs_failed == 1
    assert service.current()["latest_run"]["failure_class"] == "graph_provider_failed"


def test_graph_timestamps_are_serialized_as_utc() -> None:
    local_time = datetime(2026, 8, 5, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    assert graph_iso(local_time) == "2026-08-05T04:00:00Z"


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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
