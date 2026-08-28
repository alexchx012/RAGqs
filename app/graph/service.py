"""Public graph build service: run state machine, staging, control API facts.

Ownership boundaries:
- documents owns the public source revision/snapshot/head and acknowledges
  graph consumption (consumer_kind="public_graph");
- indexing owns the generation component stage grant, stage/release receipts,
  the atomic active-generation swap and component GC ordering;
- usage owns provider-call facts (the graph never checks quota or writes
  quota_debit);
- outbox owns the terminal `graph_build_completed` event, published exactly
  once per terminal transition with the committed post-transition version.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.indexing.graph import IndexGenerationComponentInput
from app.platform.errors import PlatformError

from .models import ActiveGraphComponent, GraphRunRecord, GraphRunView
from .ports import (
    ActiveGenerationPort,
    GraphActivatedReceiptVerifierPort,
    GraphBuildOutboxPort,
    GraphComponentCoordinatorPort,
    GraphExtractorPort,
    PublicGraphSourcePort,
)
from .repository import SqlAlchemyGraphRepository
from .store import SqlAlchemyPublicGraphStore

GRAPH_CONSUMER_ID = "public_graph"
CREATE_OP_PREFIX = "gb_create"
CANCEL_OP_PREFIX = "gb_cancel"
GC_OP_PREFIX = "gb_gc"

_logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(15)}"


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_hash(parts: Mapping[str, Any]) -> str:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GraphBuildConfiguration:
    provider: str = "openai_compatible"
    model: str = "public-graph-extraction-v1"
    prompt_version: str = "public-graph-v1"
    operation: str = "graph_build"

    def snapshot(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "operation": self.operation,
        }


class GraphBuildService:
    def __init__(
        self,
        engine: Engine,
        *,
        repository: SqlAlchemyGraphRepository | None = None,
        source: PublicGraphSourcePort,
        coordinator: GraphComponentCoordinatorPort,
        availability: ActiveGenerationPort,
        extractor: GraphExtractorPort,
        outbox: GraphBuildOutboxPort,
        verifier: GraphActivatedReceiptVerifierPort,
        configuration: GraphBuildConfiguration | None = None,
        store: SqlAlchemyPublicGraphStore | None = None,
        gc_authorizer: Callable[[str], bool] | None = None,
        now: Callable[[], datetime] = _now,
        grant_ttl: timedelta = timedelta(minutes=10),
        lease_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self._engine = engine
        self._repository = repository or SqlAlchemyGraphRepository(engine, now=now)
        self._source = source
        self._coordinator = coordinator
        self._availability = availability
        self._extractor = extractor
        self._outbox = outbox
        self._verifier = verifier
        self._configuration = configuration or GraphBuildConfiguration()
        self._store = store or SqlAlchemyPublicGraphStore(engine, now=now)
        self._gc_authorizer = gc_authorizer or (lambda caller: caller == "retention-ops")
        self._now = now
        self._grant_ttl = grant_ttl
        self._lease_ttl = lease_ttl

    # --------------------------------------------------------------- create

    def create(
        self,
        *,
        initiator_identity_id: str,
        expected_source_revision: int,
        idempotency_key: str,
        request_hash: str,
        trace_id: str | None = None,
    ) -> GraphRunView:
        operation_id = f"{CREATE_OP_PREFIX}_{idempotency_key}"
        with self._engine.begin() as connection:
            reservation, replay, reservation_created_at = self._repository.reserve_operation(
                connection=connection,
                operation_id=operation_id,
                kind="graph_build_create",
                request_hash=request_hash,
            )
            if reservation == "replay":
                assert replay is not None
                if "error" in (replay or {}):
                    raise _error_from_response(replay)
                return _view_from_response(replay)
        assert reservation_created_at is not None
        try:
            run = self._create_run(
                initiator_identity_id=initiator_identity_id,
                expected_source_revision=expected_source_revision,
                trace_id=trace_id,
                operation_id=operation_id,
                reservation_created_at=reservation_created_at,
            )
        except PlatformError as error:
            self._finish_operation(
                operation_id,
                reservation_created_at,
                {"error": _operation_error_response(error)},
            )
            raise
        view = GraphRunView.from_record(run)
        assert view is not None
        return view

    def _create_run(
        self,
        *,
        initiator_identity_id: str,
        expected_source_revision: int,
        trace_id: str | None,
        operation_id: str,
        reservation_created_at: datetime,
    ) -> GraphRunRecord:
        head, snapshot, estimated_calls, active_generation_id, has_active = self._freeze_source(
            expected_source_revision=expected_source_revision,
            initiator_identity_id=initiator_identity_id,
        )
        if has_active:
            raise PlatformError(
                "graph_build_in_progress", "A graph build run is already active", {}, 409
            )
        graph_build_id = _new_id("gb")
        grant_operation_id = f"{graph_build_id}:stage-grant"
        target_generation_id = f"graph_generation_{graph_build_id}"
        component_input = IndexGenerationComponentInput(
            component_kind="public_graph",
            target_generation_id=target_generation_id,
            target_generation_fence=f"graph_fence_{graph_build_id}",
            source_snapshot=snapshot,
            source_manifest_hash=snapshot.source_manifest_hash,
            source_head_fence=int(head.source_head_fence),
            operation_id=grant_operation_id,
        )
        with self._engine.begin() as connection:
            self._require_operation_reservation(
                operation_id=operation_id,
                reservation_created_at=reservation_created_at,
                connection=connection,
            )
            try:
                grant = self._coordinator.reserve_graph_component_stage(
                    graph_build_id=graph_build_id,
                    expected_source_revision=expected_source_revision,
                    expected_active_generation_id=active_generation_id,
                    operation_id=grant_operation_id,
                    component_input=component_input,
                    reservation_guard=lambda transaction: self._require_operation_reservation(
                        operation_id=operation_id,
                        reservation_created_at=reservation_created_at,
                        connection=transaction,
                    ),
                    connection=connection,
                )
            except PlatformError as error:
                if error.code == "graph_source_changed":
                    raise
                raise PlatformError(
                    "graph_build_stage_unavailable",
                    "Indexing could not reserve a public graph component stage",
                    {"reason": error.code},
                    409,
                ) from error
            self._source.validate_current_head(
                source_revision=int(head.source_revision),
                source_manifest_hash=str(head.source_manifest_hash),
                source_head_fence=int(head.source_head_fence),
                connection=connection,
            )
            if self._repository.has_active_run(connection=connection):
                raise PlatformError(
                    "graph_build_in_progress", "A graph build run is already active", {}, 409
                )
            now = self._now()
            run = GraphRunRecord(
                graph_build_id=graph_build_id,
                version=1,
                state="queued",
                initiator_identity_id=initiator_identity_id,
                source_revision=int(snapshot.source_revision),
                source_manifest_id=str(snapshot.source_manifest_id),
                source_manifest_hash=str(snapshot.source_manifest_hash),
                source_head_fence=int(head.source_head_fence),
                publications=tuple(dict(item) for item in snapshot.publications),
                target_generation_id=str(grant.target_generation_id),
                target_generation_fence=str(grant.target_generation_fence),
                component_manifest_slot=str(grant.component_manifest_slot),
                component_stage_id=None,
                grant_operation_id=str(grant.operation_id),
                grant_expires_at=grant.expires_at,
                config_snapshot=self._configuration.snapshot(),
                plan_snapshot={
                    "strategy": "one-primary-call-per-publication",
                    "estimated_primary_model_calls": estimated_calls,
                },
                estimated_primary_model_calls=estimated_calls,
                actual_primary_model_calls=0,
                actual_provider_calls=0,
                current_attempt=0,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                fencing_token=None,
                failure_class=None,
                failure_reason=None,
                graph_generation_id=None,
                index_generation_id=None,
                activation_receipt_id=None,
                created_at=now,
                started_at=None,
                completed_at=None,
            )
            try:
                self._repository.insert_run(connection=connection, record=run)
            except IntegrityError as error:
                raise PlatformError(
                    "graph_build_in_progress", "A graph build run is already active", {}, 409
                ) from error
            self._source.acknowledge_consumption(
                consumer_kind=GRAPH_CONSUMER_ID,
                consumer_id=GRAPH_CONSUMER_ID,
                source_revision=run.source_revision,
                source_manifest_hash=run.source_manifest_hash,
                purpose="stage",
                operation_id=f"{graph_build_id}:freeze",
                source_head_fence=run.source_head_fence,
                connection=connection,
            )
            self._repository.write_audit(
                connection=connection,
                run_id=graph_build_id,
                attempt=None,
                version=1,
                event_kind="run_created",
                actor=initiator_identity_id,
                trace_id=trace_id,
                details={
                    "source_revision": run.source_revision,
                    "source_manifest_hash": run.source_manifest_hash,
                    "source_head_fence": run.source_head_fence,
                    "target_generation_id": run.target_generation_id,
                    "estimated_primary_model_calls": run.estimated_primary_model_calls,
                },
            )
            view = GraphRunView.from_record(run)
            assert view is not None
            self._repository.complete_operation(
                connection=connection,
                operation_id=operation_id,
                reservation_created_at=reservation_created_at,
                response={"run": view.to_dict()},
            )
        return run

    def _require_operation_reservation(
        self,
        *,
        operation_id: str,
        reservation_created_at: datetime,
        connection: Connection | None,
    ) -> None:
        if connection is not None:
            self._repository.require_operation_reservation(
                connection=connection,
                operation_id=operation_id,
                reservation_created_at=reservation_created_at,
            )
            return
        with self._engine.begin() as transaction:
            self._repository.require_operation_reservation(
                connection=transaction,
                operation_id=operation_id,
                reservation_created_at=reservation_created_at,
            )

    def _freeze_source(
        self,
        *,
        expected_source_revision: int,
        initiator_identity_id: str,
    ) -> tuple[Any, Any, int, str, bool]:
        """Read and freeze the current public source facts without holding locks."""
        try:
            return self._freeze_source_locked(
                expected_source_revision=expected_source_revision,
            )
        except PlatformError as exc:
            # §9.3 审计事实：source revision 冲突。失败事务回滚会抹掉同事务
            # 审计，因此落库走独立的 best-effort 事务（不掩盖原错误）。
            if exc.code == "graph_source_changed":
                self._audit_source_conflict_best_effort(
                    initiator_identity_id=initiator_identity_id,
                    expected_source_revision=expected_source_revision,
                )
            raise

    def _audit_source_conflict_best_effort(
        self,
        *,
        initiator_identity_id: str,
        expected_source_revision: int,
    ) -> None:
        from app.platform.context import current_context
        from app.platform.database import platform_audit_table

        try:
            with self._engine.begin() as connection:
                context = current_context()
                connection.execute(
                    platform_audit_table.insert().values(
                        actor_id=initiator_identity_id,
                        resource_type="graph.build_source",
                        resource_id=f"revision:{expected_source_revision}",
                        request_id=context.request_id if context is not None else "req_graph",
                        occurred_at_utc=_now(),
                        result="graph_source_changed",
                        details_json={"expected_source_revision": expected_source_revision},
                    )
                )
        except Exception:  # noqa: BLE001 - audit must never mask the original failure
            _logger.warning(
                "graph source-conflict audit write failed for revision %s",
                expected_source_revision,
            )

    def _freeze_source_locked(
        self,
        *,
        expected_source_revision: int,
    ) -> tuple[Any, Any, int, str, bool]:
        with self._engine.begin() as connection:
            head = self._source.get_current_head(connection=connection)
            if int(head.source_revision) < 1:
                raise PlatformError(
                    "graph_source_empty", "The public graph source is empty", {}, 422
                )
            if int(head.source_revision) != expected_source_revision:
                raise PlatformError(
                    "graph_source_changed",
                    "The public graph source revision does not match",
                    {"expected_source_revision": expected_source_revision},
                    409,
                )
            try:
                snapshot = self._source.get_snapshot(
                    source_revision=expected_source_revision, connection=connection
                )
            except PlatformError as error:
                if error.code == "public_graph_source_revision_not_found":
                    raise PlatformError(
                        "graph_source_changed",
                        "The public graph source revision was not found",
                        {"expected_source_revision": expected_source_revision},
                        409,
                    ) from error
                raise
            if not snapshot.publications:
                raise PlatformError(
                    "graph_source_empty", "The public graph source is empty", {}, 422
                )
            try:
                estimated_calls = int(self._extractor.estimate_primary_model_calls(snapshot))
            except PlatformError as error:
                if error.code == "graph_build_estimate_unavailable":
                    raise
                raise PlatformError(
                    "graph_build_estimate_unavailable",
                    "The graph build plan cannot be estimated",
                    {},
                    503,
                ) from error
            active_generation_id = str(
                self._availability.active_generation_id(connection=connection)
            )
            has_active = self._repository.has_active_run(connection=connection)
        return head, snapshot, estimated_calls, active_generation_id, has_active

    # ---------------------------------------------------------------- cancel

    def cancel(
        self,
        *,
        actor_identity_id: str,
        graph_build_id: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        trace_id: str | None = None,
    ) -> GraphRunView:
        operation_id = f"{CANCEL_OP_PREFIX}_{idempotency_key}"
        with self._engine.begin() as connection:
            reservation, replay, reservation_created_at = self._repository.reserve_operation(
                connection=connection,
                operation_id=operation_id,
                kind="graph_build_cancel",
                request_hash=request_hash,
            )
            if reservation == "replay":
                assert replay is not None
                if "error" in (replay or {}):
                    raise _error_from_response(replay)
                return _view_from_response(replay)
        assert reservation_created_at is not None
        try:
            with self._engine.begin() as connection:
                run = self._cancel_transaction(
                    connection,
                    actor_identity_id=actor_identity_id,
                    graph_build_id=graph_build_id,
                    expected_version=expected_version,
                    trace_id=trace_id,
                    operation_id=operation_id,
                    reservation_created_at=reservation_created_at,
                )
        except PlatformError as error:
            self._finish_operation(
                operation_id,
                reservation_created_at,
                {"error": _operation_error_response(error)},
            )
            raise
        view = GraphRunView.from_record(run)
        assert view is not None
        self._discard_staging(run)
        return view

    def _cancel_transaction(
        self,
        connection: Any,
        *,
        actor_identity_id: str,
        graph_build_id: str,
        expected_version: int,
        trace_id: str | None,
        operation_id: str,
        reservation_created_at: datetime,
    ) -> GraphRunRecord:
        self._require_operation_reservation(
            operation_id=operation_id,
            reservation_created_at=reservation_created_at,
            connection=connection,
        )
        run = self._repository.get_run(graph_build_id, connection=connection, for_update=True)
        if run is None:
            raise PlatformError("graph_build_not_found", "Graph build run was not found", {}, 404)
        if run.version != expected_version:
            raise PlatformError(
                "version_conflict",
                "The graph run version has changed",
                {"graph_build_id": graph_build_id, "expected_version": expected_version},
                409,
            )
        if run.state not in {"queued", "running"}:
            raise PlatformError(
                "graph_build_not_cancellable", "The graph run is not cancellable", {}, 409
            )
        now = self._now()
        new_fence = _new_id("graph_fence")
        self._repository.transition_run(
            connection=connection,
            graph_build_id=graph_build_id,
            expected_version=run.version,
            to_state="cancelled",
            changes={
                "completed_at_utc": now,
                "lease_owner": None,
                "lease_expires_at_utc": None,
                "heartbeat_at_utc": None,
                "fencing_token": new_fence,
                "failure_class": "cancel_requested",
            },
        )
        self._repository.write_audit(
            connection=connection,
            run_id=graph_build_id,
            attempt=run.current_attempt if run.state == "running" else None,
            version=run.version + 1,
            event_kind="run_cancelled",
            actor=actor_identity_id,
            trace_id=trace_id,
            details={"expected_version": expected_version},
        )
        self._outbox.publish_completed(
            graph_build_id=graph_build_id,
            status="cancelled",
            source_revision=run.source_revision,
            transition_version=run.version + 1,
            occurred_at=now,
            recipient_user_id=run.initiator_identity_id,
            failure_class="cancel_requested",
            connection=connection,
        )
        self._repository.delete_staging_resources(
            connection=connection,
            run_id=graph_build_id,
            attempt=run.current_attempt,
        )
        refreshed = self._repository.get_run(graph_build_id, connection=connection)
        assert refreshed is not None
        view = GraphRunView.from_record(refreshed)
        assert view is not None
        self._repository.complete_operation(
            connection=connection,
            operation_id=operation_id,
            reservation_created_at=reservation_created_at,
            response={"run": view.to_dict()},
        )
        return refreshed

    # ------------------------------------------------------------- current

    def current(self) -> dict[str, Any]:
        with self._engine.begin() as connection:
            head = self._source.get_current_head(connection=connection)
            component = self._active_component(connection)
            latest = self._repository.latest_run(connection=connection)
        availability, active_generation = self._availability_view(head, component)
        latest_view = GraphRunView.from_record(latest)
        return {
            "space_id": "public",
            "source_revision": int(head.source_revision),
            "graph_availability": availability,
            "active_generation": active_generation,
            "latest_run": latest_view.to_dict() if latest_view is not None else None,
        }

    def _active_component(self, connection: Any) -> ActiveGraphComponent | None:
        try:
            generation_id = str(self._availability.active_generation_id(connection=connection))
            generation = self._availability.get_generation(generation_id, connection=connection)
        except PlatformError:
            return None
        component = generation.manifest.get("components", {}).get("public_graph", {})
        if not isinstance(component, Mapping):
            return None
        return ActiveGraphComponent(
            generation_id=generation_id,
            component_state=str(component.get("state", "disabled")),
            source_revision=int(component.get("source_revision", 0) or 0),
            source_manifest_hash=str(component.get("source_manifest_hash", "")),
            source_head_fence=int(component.get("source_head_fence", 0) or 0),
            activated_at=generation.activated_at,
        )

    @staticmethod
    def _availability_view(
        head: Any, component: ActiveGraphComponent | None
    ) -> tuple[str, dict[str, Any] | None]:
        if component is None or component.component_state != "ready":
            return "disabled", None
        head_matches = (
            component.source_revision == int(head.source_revision)
            and component.source_manifest_hash == str(head.source_manifest_hash)
            and component.source_head_fence == int(head.source_head_fence)
        )
        availability = "ready" if head_matches else "stale"
        generation = {
            "graph_generation_id": component.generation_id,
            "source_revision": component.source_revision,
            "built_at": (
                _iso(component.activated_at) if component.activated_at is not None else None
            ),
        }
        return availability, generation

    # ------------------------------------------------------- worker boundary

    def claim_next(self, *, owner: str) -> GraphRunRecord | None:
        with self._engine.begin() as connection:
            return self._repository.claim_next_queued(
                connection=connection,
                owner=owner,
                lease_ttl_seconds=int(self._lease_ttl.total_seconds()),
                now=self._now(),
            )

    def heartbeat(self, *, run: GraphRunRecord, owner: str) -> bool:
        with self._engine.begin() as connection:
            return self._repository.heartbeat(
                connection=connection,
                graph_build_id=run.graph_build_id,
                attempt=run.current_attempt,
                owner=owner,
                fencing_token=run.fencing_token or "",
                lease_ttl_seconds=int(self._lease_ttl.total_seconds()),
                now=self._now(),
            )

    def _require_current_lease(
        self, *, run: GraphRunRecord, connection: Connection | None = None
    ) -> None:
        owner = run.lease_owner
        if owner is None:
            raise PlatformError(
                "graph_build_lease_lost", "The graph run lease or fence no longer matches", {}, 409
            )
        if connection is None:
            renewed = self.heartbeat(run=run, owner=owner)
        else:
            renewed = self._repository.heartbeat(
                connection=connection,
                graph_build_id=run.graph_build_id,
                attempt=run.current_attempt,
                owner=owner,
                fencing_token=run.fencing_token or "",
                lease_ttl_seconds=int(self._lease_ttl.total_seconds()),
                now=self._now(),
            )
        if not renewed:
            raise PlatformError(
                "graph_build_lease_lost", "The graph run lease or fence no longer matches", {}, 409
            )

    def validate_head(self, *, run: GraphRunRecord) -> None:
        """Synchronous current-source-head revalidation before component handoff."""
        self._source.validate_current_head(
            source_revision=run.source_revision,
            source_manifest_hash=run.source_manifest_hash,
            source_head_fence=run.source_head_fence,
        )

    def write_staging_resource(
        self,
        *,
        run: GraphRunRecord,
        resource_kind: str,
        resource_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._require_current_lease(run=run)
        with self._engine.begin() as connection:
            self._repository.write_staging_resource(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
                owner=run.lease_owner or "",
                fencing_token=run.fencing_token or "",
                expected_version=run.version,
                resource_kind=resource_kind,
                resource_id=resource_id,
                payload=payload,
                now=self._now(),
            )

    def record_usage(
        self,
        *,
        run: GraphRunRecord,
        primary_model_calls: int,
        provider_calls: int,
    ) -> None:
        self._require_current_lease(run=run)
        with self._engine.begin() as connection:
            self._repository.add_usage(
                connection=connection,
                graph_build_id=run.graph_build_id,
                attempt=run.current_attempt,
                owner=run.lease_owner or "",
                fencing_token=run.fencing_token or "",
                expected_version=run.version,
                primary_model_calls=primary_model_calls,
                provider_calls=provider_calls,
                now=self._now(),
            )

    def staging_manifest(self, *, run: GraphRunRecord) -> tuple[tuple[str, ...], str]:
        with self._engine.begin() as connection:
            resources = self._repository.list_staging_resources(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
            )
        resource_ids = tuple(f"{item['resource_kind']}:{item['resource_id']}" for item in resources)
        manifest_hash = _canonical_hash({"resource_ids": list(resource_ids)})
        return resource_ids, manifest_hash

    def stage_component(self, *, run: GraphRunRecord) -> str:
        self._require_current_lease(run=run)
        resource_ids, manifest_hash = self.staging_manifest(run=run)
        build_receipt_hash = _canonical_hash(
            {
                "graph_build_id": run.graph_build_id,
                "attempt": run.current_attempt,
                "manifest_hash": manifest_hash,
            }
        )
        grant = _grant_from_record(run)
        with self._engine.begin() as connection:
            self._require_current_lease(run=run, connection=connection)
            resources = self._repository.list_staging_payloads(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
            )
            self._store.activate(
                connection=connection,
                graph_build_id=run.graph_build_id,
                graph_generation_id=run.target_generation_id,
                index_generation_id=run.target_generation_id,
                source_revision=run.source_revision,
                source_head_fence=run.source_head_fence,
                publications=run.publications,
                resources=resources,
            )
        try:
            receipt = self._coordinator.stage_public_graph_component(
                grant=grant,
                graph_resource_manifest_hash=manifest_hash,
                graph_resource_ids=resource_ids,
                build_receipt_hash=build_receipt_hash,
                lease_guard=lambda connection: self._require_current_lease(
                    run=run, connection=connection
                ),
            )
        except Exception:
            self._store.purge_generation(run.target_generation_id)
            raise
        try:
            with self._engine.begin() as connection:
                self._repository.set_stage_receipt(
                    connection=connection,
                    graph_build_id=run.graph_build_id,
                    attempt=run.current_attempt,
                    owner=run.lease_owner or "",
                    fencing_token=run.fencing_token or "",
                    expected_version=run.version,
                    component_stage_id=str(receipt.component_stage_id),
                    now=self._now(),
                )
        except PlatformError:
            self._discard_component_stage(
                run,
                component_stage_id=str(receipt.component_stage_id),
                operation_id=f"{run.graph_build_id}:stage-lease-lost-discard",
                acknowledge_source=False,
            )
            raise
        return str(receipt.component_stage_id)

    def release_component(self, *, run: GraphRunRecord, component_stage_id: str) -> Any:
        self._require_current_lease(run=run)
        return self._coordinator.release_graph_component(
            target_generation_id=run.target_generation_id,
            target_generation_fence=run.target_generation_fence,
            component_stage_id=component_stage_id,
            source_revision=run.source_revision,
            source_manifest_hash=run.source_manifest_hash,
            source_head_fence=run.source_head_fence,
            operation_id=f"{run.graph_build_id}:release",
            lease_guard=lambda connection: self._require_current_lease(
                run=run, connection=connection
            ),
        )

    def complete_succeeded(
        self,
        *,
        run: GraphRunRecord,
        owner: str,
        release_receipt: Any,
    ) -> GraphRunRecord:
        if str(release_receipt.state) != "activated":
            raise PlatformError(
                "graph_release_not_activated",
                "The indexing release receipt is not activated",
                {},
                409,
            )
        with self._engine.begin() as connection:
            locked = self._repository.get_run(
                run.graph_build_id, connection=connection, for_update=True
            )
            now = self._now()
            if locked is None or locked.state != "running":
                raise PlatformError(
                    "graph_build_lease_lost", "The graph run is not running", {}, 409
                )
            if (
                locked.current_attempt != run.current_attempt
                or locked.lease_owner != owner
                or locked.fencing_token != run.fencing_token
                or locked.lease_expires_at is None
                or locked.lease_expires_at <= now
            ):
                raise PlatformError(
                    "graph_build_lease_lost", "The graph run fence changed", {}, 409
                )
            self._repository.transition_run(
                connection=connection,
                graph_build_id=run.graph_build_id,
                expected_version=locked.version,
                to_state="succeeded",
                changes={
                    "completed_at_utc": now,
                    "lease_owner": None,
                    "lease_expires_at_utc": None,
                    "heartbeat_at_utc": None,
                    "fencing_token": None,
                    "graph_generation_id": str(release_receipt.graph_generation_id),
                    "index_generation_id": str(release_receipt.active_generation_id),
                    "activation_receipt_id": str(release_receipt.activation_receipt_id),
                },
            )
            self._source.acknowledge_consumption(
                consumer_kind=GRAPH_CONSUMER_ID,
                consumer_id=GRAPH_CONSUMER_ID,
                source_revision=run.source_revision,
                source_manifest_hash=run.source_manifest_hash,
                purpose="release",
                operation_id=f"{run.graph_build_id}:release",
                source_head_fence=run.source_head_fence,
                connection=connection,
            )
            self._repository.write_audit(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
                version=locked.version + 1,
                event_kind="run_succeeded",
                actor=f"worker:{owner}",
                details={
                    "graph_generation_id": str(release_receipt.graph_generation_id),
                    "index_generation_id": str(release_receipt.active_generation_id),
                    "activation_receipt_id": str(release_receipt.activation_receipt_id),
                },
            )
            self._outbox.publish_completed(
                graph_build_id=run.graph_build_id,
                status="succeeded",
                source_revision=run.source_revision,
                transition_version=locked.version + 1,
                occurred_at=now,
                recipient_user_id=run.initiator_identity_id,
                graph_generation_id=str(release_receipt.graph_generation_id),
                index_generation_id=str(release_receipt.active_generation_id),
                connection=connection,
            )
            self._repository.delete_staging_resources(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
            )
            refreshed = self._repository.get_run(run.graph_build_id, connection=connection)
            assert refreshed is not None
            return refreshed

    def complete_failed(
        self,
        *,
        run: GraphRunRecord,
        owner: str,
        failure_class: str,
        reason: str,
    ) -> GraphRunRecord:
        with self._engine.begin() as connection:
            locked = self._repository.get_run(
                run.graph_build_id, connection=connection, for_update=True
            )
            if locked is None:
                raise PlatformError(
                    "graph_build_not_found", "Graph build run was not found", {}, 404
                )
            if locked.state != "running":
                return locked
            now = self._now()
            if (
                locked.current_attempt != run.current_attempt
                or locked.lease_owner != owner
                or locked.fencing_token != run.fencing_token
                or locked.lease_expires_at is None
                or locked.lease_expires_at <= now
            ):
                return locked
            self._repository.transition_run(
                connection=connection,
                graph_build_id=run.graph_build_id,
                expected_version=locked.version,
                to_state="failed",
                changes={
                    "completed_at_utc": now,
                    "lease_owner": None,
                    "lease_expires_at_utc": None,
                    "heartbeat_at_utc": None,
                    "fencing_token": None,
                    "failure_class": failure_class,
                    "failure_reason": reason[:512],
                },
            )
            self._repository.write_audit(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
                version=locked.version + 1,
                event_kind="run_failed",
                actor=f"worker:{owner}",
                failure_class=failure_class,
                details={"reason": reason[:512]},
            )
            self._outbox.publish_completed(
                graph_build_id=run.graph_build_id,
                status="failed",
                source_revision=run.source_revision,
                transition_version=locked.version + 1,
                occurred_at=now,
                recipient_user_id=run.initiator_identity_id,
                failure_class=failure_class,
                connection=connection,
            )
            self._repository.delete_staging_resources(
                connection=connection,
                run_id=run.graph_build_id,
                attempt=run.current_attempt,
            )
            refreshed = self._repository.get_run(run.graph_build_id, connection=connection)
            assert refreshed is not None
            return refreshed

    def discard_staging(self, *, run: GraphRunRecord) -> None:
        self._discard_staging(run)

    def _discard_component_stage(
        self,
        run: GraphRunRecord,
        *,
        component_stage_id: str | None = None,
        operation_id: str,
        acknowledge_source: bool,
        lease_guard: Callable[[Connection | None], None] | None = None,
    ) -> bool:
        try:
            self._coordinator.discard_public_graph_component(
                graph_build_id=run.graph_build_id,
                attempt=run.current_attempt,
                target_generation_id=run.target_generation_id,
                component_stage_id=component_stage_id or run.component_stage_id or "",
                operation_id=operation_id,
                acknowledge_source=acknowledge_source,
                lease_guard=lease_guard,
            )
        except PlatformError:
            return False
        return True

    def _discard_staging(self, run: GraphRunRecord) -> None:
        """Idempotent post-terminal cleanup; never blocks the terminal state."""
        self._discard_component_stage(
            run,
            operation_id=f"{run.graph_build_id}:discard",
            acknowledge_source=True,
        )
        try:
            self._source.acknowledge_consumption(
                consumer_kind=GRAPH_CONSUMER_ID,
                consumer_id=GRAPH_CONSUMER_ID,
                source_revision=run.source_revision,
                source_manifest_hash=run.source_manifest_hash,
                purpose="discard",
                operation_id=f"{run.graph_build_id}:discard",
                source_head_fence=run.source_head_fence,
            )
        except PlatformError:
            pass

    def requeue_expired(self) -> int:
        now = self._now()
        with self._engine.begin() as connection:
            expired_attempts = self._repository.list_expired_running(connection=connection, now=now)
        recovered = 0
        for graph_build_id, attempt in expired_attempts:
            with self._engine.begin() as connection:
                run = self._repository.get_run(
                    graph_build_id, connection=connection, for_update=True
                )
            if (
                run is None
                or run.state != "running"
                or run.current_attempt != attempt
                or run.lease_expires_at is None
                or run.lease_expires_at > now
            ):
                continue

            def invalidate_expired_attempt(
                connection: Connection | None,
                recovery_graph_build_id: str = graph_build_id,
                recovery_attempt: int = attempt,
            ) -> None:
                if connection is None:
                    with self._engine.begin() as transaction:
                        invalidated = self._repository.invalidate_attempt_and_requeue(
                            connection=transaction,
                            graph_build_id=recovery_graph_build_id,
                            attempt=recovery_attempt,
                            now=now,
                        )
                else:
                    invalidated = self._repository.invalidate_attempt_and_requeue(
                        connection=connection,
                        graph_build_id=recovery_graph_build_id,
                        attempt=recovery_attempt,
                        now=now,
                    )
                if not invalidated:
                    raise PlatformError(
                        "graph_build_lease_lost",
                        "The graph run attempt lease no longer matches",
                        {},
                        409,
                    )

            if not self._discard_component_stage(
                run,
                operation_id=f"{run.graph_build_id}:lease-recovery-discard",
                acknowledge_source=False,
                lease_guard=invalidate_expired_attempt,
            ):
                continue
            recovered += 1
        return recovered

    # ------------------------------------------------------------ GC contract

    def request_public_graph_component_gc(
        self,
        *,
        caller: str,
        candidate_generation_id: str,
        graph_component_id: str,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        if not self._gc_authorizer(caller):
            raise PlatformError("forbidden", "GC request requires retention authority", {}, 403)
        request_hash = _canonical_hash(
            {
                "candidate_generation_id": candidate_generation_id,
                "graph_component_id": graph_component_id,
                "reconciliation_run_id": reconciliation_run_id,
                "operation_id": operation_id,
            }
        )
        stored_operation_id = f"{GC_OP_PREFIX}_{operation_id}"
        with self._engine.begin() as connection:
            reservation, replay, reservation_created_at = self._repository.reserve_operation(
                connection=connection,
                operation_id=stored_operation_id,
                kind="graph_component_gc",
                request_hash=request_hash,
            )
            if reservation == "replay":
                return dict((replay or {}).get("receipt", {}))
        assert reservation_created_at is not None
        indexing = self._coordinator.request_index_generation_gc(
            candidate_generation_id=candidate_generation_id,
            reconciliation_run_id=reconciliation_run_id,
            operation_id=operation_id,
            caller_principal=caller,
        )
        if indexing.state == "blocked":
            receipt = {
                "operation_id": operation_id,
                "candidate_generation_id": candidate_generation_id,
                "graph_component_id": graph_component_id,
                "state": "blocked",
                "blocking_reasons": list(indexing.blocking_reasons),
                "retryable": bool(indexing.retryable),
                "component_gc_operation_id": None,
            }
        elif indexing.state == "already_purged":
            receipt = {
                "operation_id": operation_id,
                "candidate_generation_id": candidate_generation_id,
                "graph_component_id": graph_component_id,
                "state": "already_purged",
                "blocking_reasons": [],
                "retryable": False,
                "component_gc_operation_id": None,
            }
        else:
            with self._engine.begin() as connection:
                self._repository.delete_staging_resources_for_generation(
                    connection=connection, generation_id=candidate_generation_id
                )
            receipt = {
                "operation_id": operation_id,
                "candidate_generation_id": candidate_generation_id,
                "graph_component_id": graph_component_id,
                "state": "completed",
                "blocking_reasons": [],
                "retryable": False,
                "component_gc_operation_id": operation_id,
            }
        self._finish_operation(stored_operation_id, reservation_created_at, {"receipt": receipt})
        return receipt

    def _finish_operation(
        self,
        operation_id: str,
        reservation_created_at: datetime,
        response: Mapping[str, Any],
    ) -> None:
        with self._engine.begin() as connection:
            self._repository.complete_operation(
                connection=connection,
                operation_id=operation_id,
                reservation_created_at=reservation_created_at,
                response=response,
            )


def _grant_from_record(run: GraphRunRecord) -> Any:
    """Reconstruct the frozen stage grant from the persisted run record."""
    from app.documents.public_graph import PublicGraphSourceSnapshot
    from app.indexing.graph import GraphComponentStageGrant

    snapshot = PublicGraphSourceSnapshot(
        schema_version=1,
        source_revision=run.source_revision,
        source_manifest_id=run.source_manifest_id,
        source_manifest_hash=run.source_manifest_hash,
        publications=run.publications,
    )
    return GraphComponentStageGrant(
        graph_build_id=run.graph_build_id,
        target_generation_id=run.target_generation_id,
        target_generation_fence=run.target_generation_fence,
        source_snapshot=snapshot,
        source_manifest_hash=run.source_manifest_hash,
        source_head_fence=run.source_head_fence,
        component_manifest_slot=run.component_manifest_slot,
        expires_at=run.grant_expires_at,
        operation_id=run.grant_operation_id,
    )


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error_from_response(response: Mapping[str, Any]) -> PlatformError:
    error = response.get("error", {})
    status_code = error.get("status_code", 409)
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        status_code = 409
    return PlatformError(
        str(error.get("code", "unknown_error")),
        str(error.get("message", "Operation failed")),
        {},
        status_code,
    )


def _operation_error_response(error: PlatformError) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "status_code": error.status_code,
    }


def _view_from_response(response: Mapping[str, Any]) -> GraphRunView:
    return GraphRunView.from_dict(response["run"])


__all__ = [
    "GRAPH_CONSUMER_ID",
    "GraphBuildConfiguration",
    "GraphBuildService",
]
