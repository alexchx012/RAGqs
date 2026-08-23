from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from app.documents.public_graph import PublicGraphSourceSnapshot
from app.platform.errors import PlatformError

from .generation import GenerationManager
from .models import GenerationComponentReaderLease, IndexGenerationGcReceipt


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _snapshot_to_mapping(snapshot: Any) -> dict[str, Any]:
    return {
        "schema_version": int(snapshot.schema_version),
        "source_revision": int(snapshot.source_revision),
        "source_manifest_id": str(snapshot.source_manifest_id),
        "source_manifest_hash": str(snapshot.source_manifest_hash),
        "publications": [dict(item) for item in snapshot.publications],
    }


def _snapshot_from_mapping(value: Mapping[str, Any]) -> PublicGraphSourceSnapshot:
    return PublicGraphSourceSnapshot(
        schema_version=int(value["schema_version"]),
        source_revision=int(value["source_revision"]),
        source_manifest_id=str(value["source_manifest_id"]),
        source_manifest_hash=str(value["source_manifest_hash"]),
        publications=tuple(dict(item) for item in value.get("publications", ())),
    )


def _grant_to_mapping(grant: GraphComponentStageGrant) -> dict[str, Any]:
    return {
        "graph_build_id": grant.graph_build_id,
        "target_generation_id": grant.target_generation_id,
        "target_generation_fence": grant.target_generation_fence,
        "source_snapshot": _snapshot_to_mapping(grant.source_snapshot),
        "source_manifest_hash": grant.source_manifest_hash,
        "source_head_fence": grant.source_head_fence,
        "component_manifest_slot": grant.component_manifest_slot,
        "expires_at": grant.expires_at.isoformat(),
        "operation_id": grant.operation_id,
    }


def _grant_from_mapping(value: Mapping[str, Any]) -> GraphComponentStageGrant:
    return GraphComponentStageGrant(
        graph_build_id=str(value["graph_build_id"]),
        target_generation_id=str(value["target_generation_id"]),
        target_generation_fence=str(value["target_generation_fence"]),
        source_snapshot=_snapshot_from_mapping(value["source_snapshot"]),
        source_manifest_hash=str(value["source_manifest_hash"]),
        source_head_fence=int(value["source_head_fence"]),
        component_manifest_slot=str(value["component_manifest_slot"]),
        expires_at=datetime.fromisoformat(str(value["expires_at"])),
        operation_id=str(value["operation_id"]),
    )


def _stage_receipt_to_mapping(receipt: GraphComponentStageReceipt) -> dict[str, Any]:
    return {
        "graph_build_id": receipt.graph_build_id,
        "target_generation_id": receipt.target_generation_id,
        "target_generation_fence": receipt.target_generation_fence,
        "source_revision": receipt.source_revision,
        "source_manifest_hash": receipt.source_manifest_hash,
        "source_head_fence": receipt.source_head_fence,
        "component_manifest_slot": receipt.component_manifest_slot,
        "graph_resource_manifest_hash": receipt.graph_resource_manifest_hash,
        "graph_resource_ids": list(receipt.graph_resource_ids),
        "build_receipt_hash": receipt.build_receipt_hash,
        "component_stage_id": receipt.component_stage_id,
    }


def _stage_receipt_from_mapping(value: Mapping[str, Any]) -> GraphComponentStageReceipt:
    return GraphComponentStageReceipt(
        graph_build_id=str(value["graph_build_id"]),
        target_generation_id=str(value["target_generation_id"]),
        target_generation_fence=str(value["target_generation_fence"]),
        source_revision=int(value["source_revision"]),
        source_manifest_hash=str(value["source_manifest_hash"]),
        source_head_fence=int(value["source_head_fence"]),
        component_manifest_slot=str(value["component_manifest_slot"]),
        graph_resource_manifest_hash=str(value["graph_resource_manifest_hash"]),
        graph_resource_ids=tuple(str(item) for item in value["graph_resource_ids"]),
        build_receipt_hash=str(value["build_receipt_hash"]),
        component_stage_id=str(value["component_stage_id"]),
    )


def _release_receipt_to_mapping(receipt: GraphComponentReleaseReceipt) -> dict[str, Any]:
    return {
        "state": receipt.state,
        "active_generation_id": receipt.active_generation_id,
        "graph_generation_id": receipt.graph_generation_id,
        "source_revision": receipt.source_revision,
        "source_manifest_hash": receipt.source_manifest_hash,
        "source_head_fence": receipt.source_head_fence,
        "activation_receipt_id": receipt.activation_receipt_id,
    }


def _release_receipt_from_mapping(value: Mapping[str, Any]) -> GraphComponentReleaseReceipt:
    return GraphComponentReleaseReceipt(
        state=str(value["state"]),
        active_generation_id=str(value["active_generation_id"]),
        graph_generation_id=str(value["graph_generation_id"]),
        source_revision=int(value["source_revision"]),
        source_manifest_hash=str(value["source_manifest_hash"]),
        source_head_fence=int(value["source_head_fence"]),
        activation_receipt_id=str(value["activation_receipt_id"]),
    )


@dataclass(frozen=True, slots=True)
class IndexGenerationComponentInput:
    component_kind: str
    target_generation_id: str
    target_generation_fence: str
    source_snapshot: Any
    source_manifest_hash: str
    source_head_fence: int
    operation_id: str


@dataclass(frozen=True, slots=True)
class GraphComponentStageGrant:
    graph_build_id: str
    target_generation_id: str
    target_generation_fence: str
    source_snapshot: Any
    source_manifest_hash: str
    source_head_fence: int
    component_manifest_slot: str
    expires_at: datetime
    operation_id: str


@dataclass(frozen=True, slots=True)
class GraphComponentStageReceipt:
    graph_build_id: str
    target_generation_id: str
    target_generation_fence: str
    source_revision: int
    source_manifest_hash: str
    source_head_fence: int
    component_manifest_slot: str
    graph_resource_manifest_hash: str
    graph_resource_ids: tuple[str, ...]
    build_receipt_hash: str
    component_stage_id: str


@dataclass(frozen=True, slots=True)
class GraphComponentReleaseReceipt:
    state: str
    active_generation_id: str
    graph_generation_id: str
    source_revision: int
    source_manifest_hash: str
    source_head_fence: int
    activation_receipt_id: str


@dataclass(frozen=True, slots=True)
class PublicGraphComponentGcReceipt:
    operation_id: str
    candidate_generation_id: str
    graph_component_id: str
    state: str
    blocking_reasons: tuple[str, ...] = ()
    retryable: bool = False
    component_gc_operation_id: str | None = None


class _GraphComponentCleanupFailure(Exception):
    def __init__(self, error: PlatformError) -> None:
        self.error = error
        super().__init__(error.message)


class GraphComponentCoordinator:
    """Indexing-owned stage/release/lease contract for the public graph component."""

    def __init__(
        self,
        generation_manager: GenerationManager,
        source_service: Any,
        *,
        consumer_id: str = "indexing",
        graph_store: Any | None = None,
        now: Any = _now,
        grant_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self._generation = generation_manager
        self._source = source_service
        self._consumer_id = consumer_id
        self._graph_store = graph_store
        self._now = now
        self._grant_ttl = grant_ttl
        self._grants: dict[str, GraphComponentStageGrant] = {}
        self._grant_inputs: dict[str, tuple[str, int, str]] = {}
        self._receipts: dict[str, GraphComponentStageReceipt] = {}
        self._release_receipts: dict[str, GraphComponentReleaseReceipt] = {}
        self._lock = RLock()

    @property
    def _repository(self) -> Any | None:
        return getattr(self._generation, "_repository", None)

    @contextmanager
    def _transaction(self, connection: Any | None = None):
        if connection is not None:
            yield connection
            return
        repository = self._repository
        if repository is None:
            yield None
            return
        with repository._engine.begin() as connection:
            yield connection

    def _current_source(
        self, expected_revision: int, *, connection: Any | None = None
    ) -> tuple[Any, Any]:
        source_kwargs = {"connection": connection} if connection is not None else {}
        snapshot = self._source.get_snapshot(source_revision=expected_revision, **source_kwargs)
        head = self._source.get_current_head(**source_kwargs)
        if (
            int(snapshot.source_revision) != expected_revision
            or int(head.source_revision) != expected_revision
            or snapshot.source_manifest_hash != head.source_manifest_hash
        ):
            raise PlatformError(
                "graph_source_changed", "The public graph source has changed", {}, 409
            )
        self._source.validate_current_head(
            source_revision=expected_revision,
            source_manifest_hash=snapshot.source_manifest_hash,
            source_head_fence=int(head.source_head_fence),
            **source_kwargs,
        )
        return snapshot, head

    def _acknowledge(
        self,
        *,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int | None,
        purpose: str,
        operation_id: str,
        connection: Any | None = None,
    ) -> Any:
        acknowledge = getattr(self._source, "acknowledge_consumption", None)
        if acknowledge is None:
            return None
        return acknowledge(
            consumer_kind="indexing",
            consumer_id=self._consumer_id,
            source_revision=source_revision,
            source_manifest_hash=source_manifest_hash,
            source_head_fence=source_head_fence,
            purpose=purpose,
            operation_id=operation_id,
            **({"connection": connection} if connection is not None else {}),
        )

    def _stored_grant(
        self, operation_id: str, *, connection: Any | None = None
    ) -> GraphComponentStageGrant | None:
        repository = self._repository
        if repository is None:
            return self._grants.get(operation_id)
        operation = repository.get_operation(operation_id, connection=connection)
        if operation is None:
            return None
        response = dict(operation.get("response_json") or {})
        if response.get("kind") != "graph_stage_grant":
            return None
        return _grant_from_mapping(response["grant"])

    def _stored_stage_receipt(
        self,
        target_generation_id: str,
        component_stage_id: str,
        *,
        connection: Any | None = None,
    ) -> GraphComponentStageReceipt | None:
        receipt = next(
            (
                value
                for value in self._receipts.values()
                if (
                    value.target_generation_id == target_generation_id
                    and value.component_stage_id == component_stage_id
                )
            ),
            None,
        )
        if receipt is not None:
            return receipt
        repository = self._repository
        if repository is None:
            return None
        generation = repository.get_generation(target_generation_id, connection=connection)
        component = generation.manifest.get("components", {}).get("public_graph", {})
        if component.get("stage_receipt_id") != component_stage_id:
            return None
        return GraphComponentStageReceipt(
            graph_build_id=str(component["graph_build_id"]),
            target_generation_id=target_generation_id,
            target_generation_fence=str(component["target_generation_fence"]),
            source_revision=int(component["source_revision"]),
            source_manifest_hash=str(component["source_manifest_hash"]),
            source_head_fence=int(component["source_head_fence"]),
            component_manifest_slot="public_graph",
            graph_resource_manifest_hash=str(component["graph_resource_manifest_hash"]),
            graph_resource_ids=tuple(str(item) for item in component["graph_resource_ids"]),
            build_receipt_hash=str(component["build_receipt_hash"]),
            component_stage_id=component_stage_id,
        )

    def _authoritative_stage_receipt(
        self, target_generation_id: str, *, connection: Any | None = None
    ) -> GraphComponentStageReceipt | None:
        repository = self._repository
        generation = (
            repository.get_generation(target_generation_id, connection=connection)
            if repository is not None
            else self._generation.get_generation(target_generation_id)
        )
        component = generation.manifest.get("components", {}).get("public_graph", {})
        stage_id = str(
            component.get("stage_receipt_id") or component.get("component_stage_id") or ""
        )
        if not stage_id:
            return None
        return self._stored_stage_receipt(target_generation_id, stage_id, connection=connection)

    def _discard_graph_resources(
        self,
        receipt: GraphComponentStageReceipt,
        *,
        operation_id: str,
        state: str = "disabled",
        connection: Any | None = None,
        acknowledge_source: bool = True,
    ) -> None:
        if connection is not None:
            self._discard_graph_resources_in_transaction(
                receipt,
                operation_id=operation_id,
                state=state,
                connection=connection,
                acknowledge_source=acknowledge_source,
            )
            return
        with self._transaction() as transaction:
            self._discard_graph_resources_in_transaction(
                receipt,
                operation_id=operation_id,
                state=state,
                connection=transaction,
                acknowledge_source=acknowledge_source,
            )

    def _discard_graph_resources_in_transaction(
        self,
        receipt: GraphComponentStageReceipt,
        *,
        operation_id: str,
        state: str,
        connection: Any | None,
        acknowledge_source: bool,
    ) -> None:
        self._generation.set_component_state(
            receipt.target_generation_id,
            "public_graph",
            state,
            manifest={
                "graph_resource_manifest_hash": "",
                "graph_resource_ids": [],
                "stage_receipt_id": None,
                "component_stage_id": None,
                "build_receipt_hash": None,
            },
            **({"connection": connection} if connection is not None else {}),
        )
        if not acknowledge_source:
            return
        self._acknowledge(
            source_revision=receipt.source_revision,
            source_manifest_hash=receipt.source_manifest_hash,
            source_head_fence=receipt.source_head_fence,
            purpose="discard",
            operation_id=operation_id,
            connection=connection,
        )

    def _stale_graph_component(
        self,
        generation: Any,
        component: Mapping[str, Any],
        *,
        operation_id: str,
    ) -> None:
        receipt = self._authoritative_stage_receipt(generation.generation_id)
        if receipt is not None and component.get("graph_resource_ids"):
            self._discard_graph_resources(receipt, operation_id=operation_id, state="stale")
            return
        self._generation.set_component_state(
            generation.generation_id,
            "public_graph",
            "stale",
            manifest={"graph_resource_manifest_hash": "", "graph_resource_ids": []},
        )

    def _graph_component(
        self,
        *,
        generation_id: str,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
        operation_id: str,
    ) -> tuple[Any, Mapping[str, Any], tuple[int, str, int]]:
        active = self._generation.get_generation(generation_id)
        component = active.manifest.get("components", {}).get("public_graph", {})
        expected = (
            int(component.get("source_revision", 0)),
            str(component.get("source_manifest_hash", "")),
            int(component.get("source_head_fence", 0)),
        )
        supplied = (source_revision, source_manifest_hash, source_head_fence)
        if supplied != expected:
            self._stale_graph_component(active, component, operation_id=operation_id)
            raise PlatformError(
                "graph_source_changed", "The public graph source has changed", {}, 409
            )
        return active, component, expected

    def _stop_graph_reader(self, lease_id: str) -> None:
        release = getattr(self._generation, "release_graph_reader_lease", None)
        if release is not None:
            release(lease_id)

    def reserve_graph_component_stage(
        self,
        *,
        graph_build_id: str,
        expected_source_revision: int,
        expected_active_generation_id: str,
        operation_id: str,
        component_input: IndexGenerationComponentInput,
        reservation_guard: Callable[[Any | None], None] | None = None,
        connection: Any | None = None,
    ) -> GraphComponentStageGrant:
        if not graph_build_id or not operation_id:
            raise PlatformError("validation_error", "graph stage request is invalid", {}, 422)
        if (
            component_input.component_kind != "public_graph"
            or component_input.operation_id != operation_id
        ):
            raise PlatformError(
                "processing_receipt_conflict", "graph component input is invalid", {}, 409
            )
        with self._lock:
            input_key = (graph_build_id, expected_source_revision, expected_active_generation_id)
            request_identity: dict[str, Any] = {
                "graph_build_id": graph_build_id,
                "expected_source_revision": expected_source_revision,
                "expected_active_generation_id": expected_active_generation_id,
                "component_input": {
                    "component_kind": component_input.component_kind,
                    "target_generation_id": component_input.target_generation_id,
                    "target_generation_fence": component_input.target_generation_fence,
                    "source_manifest_hash": component_input.source_manifest_hash,
                    "source_head_fence": component_input.source_head_fence,
                    "operation_id": component_input.operation_id,
                },
            }
            with self._transaction(connection) as transaction:
                if reservation_guard is not None:
                    reservation_guard(transaction)
                existing = self._stored_grant(operation_id, connection=transaction)
                if existing is not None:
                    if self._repository is None and self._grant_inputs[operation_id] != input_key:
                        raise PlatformError(
                            "idempotency_key_conflict", "graph stage request conflicts", {}, 409
                        )
                    self._grants[operation_id] = existing
                    self._grant_inputs[operation_id] = input_key
                    return existing
                if self._repository is not None:
                    self._repository.reserve_operation(
                        operation_id,
                        "graph_stage_grant",
                        request_identity,
                        connection=transaction,
                    )
                snapshot, head = self._current_source(
                    expected_source_revision, connection=transaction
                )
                if (
                    int(component_input.source_snapshot.source_revision)
                    != int(snapshot.source_revision)
                    or component_input.source_manifest_hash != snapshot.source_manifest_hash
                    or int(component_input.source_head_fence) != int(head.source_head_fence)
                ):
                    raise PlatformError(
                        "processing_receipt_conflict", "graph component input is invalid", {}, 409
                    )
                active_generation_id = (
                    self._repository.active_generation_id(connection=transaction)
                    if self._repository is not None
                    else self._generation.active_generation_id
                )
                if active_generation_id != expected_active_generation_id:
                    raise PlatformError(
                        "generation_conflict", "active generation has changed", {}, 409
                    )
                target_generation_id = component_input.target_generation_id
                if target_generation_id == expected_active_generation_id:
                    raise PlatformError(
                        "generation_conflict", "graph target must be a staging generation", {}, 409
                    )
                self._acknowledge(
                    source_revision=expected_source_revision,
                    source_manifest_hash=str(snapshot.source_manifest_hash),
                    source_head_fence=int(head.source_head_fence),
                    purpose="stage",
                    operation_id=f"{operation_id}:source-hold",
                    connection=transaction,
                )
                staging_kwargs = {
                    "expected_active_generation_id": expected_active_generation_id,
                    "generation_id": target_generation_id,
                    "manifest": {"graph_build_id": graph_build_id},
                }
                if self._repository is not None:
                    staging = self._repository.create_staging(
                        connection=transaction,
                        **staging_kwargs,
                    )
                else:
                    base_snapshot = tuple(
                        {
                            "document_id": str(publication["document_id"]),
                            "document_version_id": str(publication["document_version_id"]),
                            "publication_id": str(publication["publication_id"]),
                            "manifest": {
                                "content_manifest_id": str(publication["content_manifest_id"]),
                                "content_manifest_hash": str(publication["content_manifest_hash"]),
                            },
                        }
                        for publication in snapshot.publications
                    )
                    staging = self._generation.create_staging(base_snapshot, **staging_kwargs)
                fence = component_input.target_generation_fence
                self._generation.set_component_state(
                    staging.generation_id,
                    "public_graph",
                    "staged",
                    manifest={
                        "graph_generation_id": staging.generation_id,
                        "component_manifest_revision": "public-graph-v1",
                        "reader_lease_binding": staging.generation_id,
                        "target_generation_fence": fence,
                        "source_revision": expected_source_revision,
                        "source_manifest_hash": snapshot.source_manifest_hash,
                        "source_head_fence": int(head.source_head_fence),
                        "graph_build_id": graph_build_id,
                    },
                    **(
                        {"connection": transaction}
                        if self._repository is not None and transaction is not None
                        else {}
                    ),
                )
                grant = GraphComponentStageGrant(
                    graph_build_id=graph_build_id,
                    target_generation_id=staging.generation_id,
                    target_generation_fence=fence,
                    source_snapshot=snapshot,
                    source_manifest_hash=str(snapshot.source_manifest_hash),
                    source_head_fence=int(head.source_head_fence),
                    component_manifest_slot="public_graph",
                    expires_at=self._now() + self._grant_ttl,
                    operation_id=operation_id,
                )
                self._grants[operation_id] = grant
                self._grant_inputs[operation_id] = input_key
                if self._repository is not None:
                    self._repository.complete_operation(
                        operation_id,
                        {"kind": "graph_stage_grant", "grant": _grant_to_mapping(grant)},
                        connection=transaction,
                    )
                return grant

    ReserveGraphComponentStage = reserve_graph_component_stage

    def stage_public_graph_component(
        self,
        grant: GraphComponentStageGrant,
        *,
        graph_resource_manifest_hash: str,
        graph_resource_ids: Sequence[str],
        build_receipt_hash: str,
        lease_guard: Callable[[Any | None], None] | None = None,
    ) -> GraphComponentStageReceipt:
        if grant.expires_at <= self._now():
            raise PlatformError(
                "graph_stage_grant_expired", "graph stage grant has expired", {}, 409
            )
        if not graph_resource_manifest_hash or not build_receipt_hash:
            raise PlatformError("validation_error", "graph component receipt is invalid", {}, 422)
        resources = tuple(str(item) for item in graph_resource_ids)
        if not resources or any(not item for item in resources):
            raise PlatformError(
                "validation_error", "graph component resources are invalid", {}, 422
            )
        with self._lock:
            issued = self._stored_grant(grant.operation_id)
            if issued != grant:
                raise PlatformError(
                    "graph_stage_grant_invalid", "graph stage grant was not issued", {}, 409
                )
            conflicting_receipt: GraphComponentStageReceipt | None = None
            try:
                with self._transaction() as connection:
                    source_kwargs = {"connection": connection} if connection is not None else {}
                    self._source.validate_current_head(
                        source_revision=int(grant.source_snapshot.source_revision),
                        source_manifest_hash=grant.source_manifest_hash,
                        source_head_fence=grant.source_head_fence,
                        **source_kwargs,
                    )
                    existing = self._receipts.get(grant.operation_id)
                    if existing is None:
                        current = self._generation.get_generation(grant.target_generation_id)
                        stage_id = str(
                            current.manifest.get("components", {})
                            .get("public_graph", {})
                            .get("stage_receipt_id", "")
                        )
                        if stage_id:
                            existing = self._stored_stage_receipt(
                                grant.target_generation_id, stage_id, connection=connection
                            )
                    if existing is not None:
                        if (
                            existing.graph_resource_manifest_hash != graph_resource_manifest_hash
                            or existing.graph_resource_ids != resources
                            or existing.build_receipt_hash != build_receipt_hash
                        ):
                            conflicting_receipt = existing
                        else:
                            return existing
                    if conflicting_receipt is None:
                        current = self._generation.get_generation(grant.target_generation_id)
                        component = current.manifest.get("components", {}).get("public_graph", {})
                        if (
                            component.get("target_generation_fence")
                            != grant.target_generation_fence
                        ):
                            raise PlatformError(
                                "generation_conflict", "graph generation fence is invalid", {}, 409
                            )
                        source_revision = int(grant.source_snapshot.source_revision)
                        receipt = GraphComponentStageReceipt(
                            graph_build_id=grant.graph_build_id,
                            target_generation_id=grant.target_generation_id,
                            target_generation_fence=grant.target_generation_fence,
                            source_revision=source_revision,
                            source_manifest_hash=grant.source_manifest_hash,
                            source_head_fence=grant.source_head_fence,
                            component_manifest_slot=grant.component_manifest_slot,
                            graph_resource_manifest_hash=graph_resource_manifest_hash,
                            graph_resource_ids=resources,
                            build_receipt_hash=build_receipt_hash,
                            component_stage_id=_new_id("graph_component_stage"),
                        )
                        if lease_guard is not None:
                            lease_guard(connection)
                        self._generation.set_component_state(
                            grant.target_generation_id,
                            "public_graph",
                            "staged",
                            manifest={
                                "stage_receipt_id": receipt.component_stage_id,
                                "graph_resource_manifest_hash": graph_resource_manifest_hash,
                                "graph_resource_ids": list(resources),
                                "build_receipt_hash": build_receipt_hash,
                            },
                            **source_kwargs,
                        )
                        repository = self._repository
                        if repository is not None:
                            stage_operation_id = (
                                f"{grant.operation_id}:component-stage:{build_receipt_hash}"
                            )
                            repository.reserve_operation(
                                stage_operation_id,
                                "graph_component_stage",
                                {
                                    "grant": _grant_to_mapping(grant),
                                    "graph_resource_manifest_hash": graph_resource_manifest_hash,
                                    "graph_resource_ids": list(resources),
                                    "build_receipt_hash": build_receipt_hash,
                                },
                                connection=connection,
                            )
                            repository.complete_operation(
                                stage_operation_id,
                                {
                                    "kind": "graph_component_stage",
                                    "receipt": _stage_receipt_to_mapping(receipt),
                                },
                                connection=connection,
                            )
            except PlatformError as error:
                if error.code == "graph_source_changed":
                    current = self._generation.get_generation(grant.target_generation_id)
                    component = current.manifest.get("components", {}).get("public_graph", {})
                    if component.get("graph_resource_ids"):
                        self._stale_graph_component(
                            current,
                            component,
                            operation_id=f"{grant.operation_id}:source-changed-discard",
                        )
                    else:
                        self._generation.set_component_state(
                            grant.target_generation_id,
                            "public_graph",
                            "stale",
                            manifest={"graph_resource_manifest_hash": "", "graph_resource_ids": []},
                        )
                        self._acknowledge(
                            source_revision=int(grant.source_snapshot.source_revision),
                            source_manifest_hash=grant.source_manifest_hash,
                            source_head_fence=grant.source_head_fence,
                            purpose="discard",
                            operation_id=f"{grant.operation_id}:source-changed-discard",
                        )
                raise
            if conflicting_receipt is not None:
                if lease_guard is not None:
                    lease_guard(None)
                self._discard_graph_resources(
                    conflicting_receipt,
                    operation_id=f"{grant.operation_id}:conflicting-receipt-discard",
                )
                raise PlatformError(
                    "idempotency_key_conflict", "graph component stage conflicts", {}, 409
                )
            self._receipts[grant.operation_id] = receipt
            return receipt

    StagePublicGraphComponent = stage_public_graph_component

    def release_graph_component(
        self,
        *,
        target_generation_id: str,
        target_generation_fence: str,
        component_stage_id: str,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
        operation_id: str,
        lease_guard: Callable[[Any | None], None] | None = None,
    ) -> GraphComponentReleaseReceipt:
        with self._lock:
            repository = self._repository
            release_operation_id = f"{operation_id}:component-release"
            replay = self._release_receipts.get(operation_id)
            if replay is None and repository is not None:
                stored = repository.get_operation(release_operation_id)
                if stored is not None and stored.get("response_json"):
                    response = dict(stored["response_json"])
                    if response.get("kind") == "graph_component_release":
                        replay = _release_receipt_from_mapping(response["receipt"])
            if replay is not None:
                self._release_receipts[operation_id] = replay
                return replay
            receipt = self._stored_stage_receipt(target_generation_id, component_stage_id)
            if receipt is None:
                authoritative = self._authoritative_stage_receipt(target_generation_id)
                if authoritative is not None:
                    self._discard_graph_resources(
                        authoritative,
                        operation_id=f"{operation_id}:invalid-receipt-discard",
                    )
                    raise PlatformError(
                        "processing_receipt_conflict", "graph component receipt is invalid", {}, 409
                    )
                raise PlatformError(
                    "graph_component_not_found", "graph component stage was not found", {}, 404
                )
            if (
                receipt.target_generation_id != target_generation_id
                or receipt.target_generation_fence != target_generation_fence
                or receipt.source_revision != source_revision
                or receipt.source_manifest_hash != source_manifest_hash
                or receipt.source_head_fence != source_head_fence
            ):
                self._discard_graph_resources(
                    receipt,
                    operation_id=f"{operation_id}:invalid-receipt-discard",
                )
                raise PlatformError(
                    "processing_receipt_conflict", "graph component receipt is invalid", {}, 409
                )
            release_request = {
                "target_generation_id": target_generation_id,
                "target_generation_fence": target_generation_fence,
                "component_stage_id": component_stage_id,
                "source_revision": source_revision,
                "source_manifest_hash": source_manifest_hash,
                "source_head_fence": source_head_fence,
            }
            try:
                with self._transaction() as connection:
                    if lease_guard is not None:
                        lease_guard(connection)
                    if repository is not None:
                        repository.reserve_operation(
                            release_operation_id,
                            "graph_component_release",
                            release_request,
                            connection=connection,
                        )
                    self._source.validate_current_head(
                        source_revision=source_revision,
                        source_manifest_hash=source_manifest_hash,
                        source_head_fence=source_head_fence,
                        **({"connection": connection} if connection is not None else {}),
                    )
                    if lease_guard is not None:
                        lease_guard(connection)
                    self._acknowledge(
                        source_revision=source_revision,
                        source_manifest_hash=source_manifest_hash,
                        source_head_fence=source_head_fence,
                        purpose="release",
                        operation_id=f"{operation_id}:source-release",
                        connection=connection,
                    )
                    self._generation.set_component_state(
                        target_generation_id,
                        "public_graph",
                        "ready",
                        manifest={"component_stage_id": component_stage_id},
                        **({"connection": connection} if connection is not None else {}),
                    )
                    if lease_guard is not None:
                        lease_guard(connection)
                    active = self._generation.release(
                        target_generation_id,
                        **({"connection": connection} if connection is not None else {}),
                    )
                    result = GraphComponentReleaseReceipt(
                        state="activated",
                        active_generation_id=active.generation_id,
                        graph_generation_id=target_generation_id,
                        source_revision=source_revision,
                        source_manifest_hash=source_manifest_hash,
                        source_head_fence=source_head_fence,
                        activation_receipt_id=_new_id("graph_activation"),
                    )
                    if repository is not None:
                        repository.complete_operation(
                            release_operation_id,
                            {
                                "kind": "graph_component_release",
                                "receipt": _release_receipt_to_mapping(result),
                            },
                            connection=connection,
                        )
            except PlatformError as error:
                if error.code == "graph_source_changed":
                    self._discard_graph_resources(
                        receipt,
                        operation_id=f"{operation_id}:source-changed-discard",
                    )
                if repository is not None:
                    repository.record_component_failure("publish")
                raise
            self._release_receipts[operation_id] = result
            return result

    ReleaseGraphComponent = release_graph_component

    def acquire_current_reader_lease(self, *, generation_id: str) -> GenerationComponentReaderLease:
        active = self._generation.get_generation(generation_id)
        component = active.manifest.get("components", {}).get("public_graph", {})
        return self.acquire_reader_lease(
            generation_id=generation_id,
            source_revision=int(component.get("source_revision", 0)),
            source_manifest_hash=str(component.get("source_manifest_hash", "")),
            source_head_fence=int(component.get("source_head_fence", 0)),
            manifest_hash=str(component.get("graph_resource_manifest_hash", "")),
        )

    def release_reader_lease(self, lease: GenerationComponentReaderLease) -> None:
        self._generation.release_graph_reader_lease(lease.lease_id)

    def acquire_reader_lease(
        self,
        *,
        generation_id: str | None = None,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
        manifest_hash: str,
        ttl: timedelta = timedelta(minutes=1),
    ) -> GenerationComponentReaderLease:
        selected_generation_id = generation_id or self._generation.active_generation_id
        active, component, expected_source = self._graph_component(
            generation_id=selected_generation_id,
            source_revision=source_revision,
            source_manifest_hash=source_manifest_hash,
            source_head_fence=source_head_fence,
            operation_id=f"{selected_generation_id}:source-identity-discard",
        )
        if component.get("graph_resource_manifest_hash") != manifest_hash:
            raise PlatformError(
                "graph_unavailable", "graph component manifest is not current", {}, 409
            )

        def validate() -> bool:
            try:
                self._source.validate_current_head(
                    source_revision=expected_source[0],
                    source_manifest_hash=expected_source[1],
                    source_head_fence=expected_source[2],
                )
            except PlatformError:
                return False
            return True

        try:
            return self._generation.acquire_graph_reader_lease(
                generation_id=active.generation_id,
                source_head_fence=expected_source[2],
                manifest_hash=manifest_hash,
                validate_source_head=validate,
                ttl=ttl,
            )
        except PlatformError as error:
            if error.code == "graph_source_changed":
                self._stale_graph_component(
                    active,
                    component,
                    operation_id=f"{active.generation_id}:source-changed-discard",
                )
            raise

    def renew_reader_lease(
        self,
        lease_id: str,
        *,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
        ttl: timedelta = timedelta(minutes=1),
    ) -> GenerationComponentReaderLease:
        try:
            lease = self._generation.get_graph_reader_lease(lease_id)
            active, component, expected_source = self._graph_component(
                generation_id=lease.generation_id,
                source_revision=source_revision,
                source_manifest_hash=source_manifest_hash,
                source_head_fence=source_head_fence,
                operation_id=f"{lease_id}:source-identity-discard",
            )
        except PlatformError as error:
            if error.code == "graph_source_changed":
                self._stop_graph_reader(lease_id)
            raise

        def validate() -> bool:
            try:
                self._source.validate_current_head(
                    source_revision=expected_source[0],
                    source_manifest_hash=expected_source[1],
                    source_head_fence=expected_source[2],
                )
            except PlatformError:
                return False
            return True

        try:
            return self._generation.renew_graph_reader_lease(
                lease_id, validate_source_head=validate, ttl=ttl
            )
        except PlatformError as error:
            if error.code == "graph_source_changed":
                self._stop_graph_reader(lease_id)
                self._stale_graph_component(
                    active,
                    component,
                    operation_id=f"{lease_id}:source-changed-discard",
                )
            raise

    def rollback_generation(
        self,
        *,
        candidate_generation_id: str,
        source_receipt: Mapping[str, Any],
        operation_id: str,
        caller_principal: str | None = None,
    ) -> Any:
        """Restore a generation while retaining graph routing only after a head check."""
        if caller_principal != "ops":
            raise PlatformError("forbidden", "rollback requires ops authority", {}, 403)
        if not operation_id.strip():
            raise PlatformError(
                "validation_error", "rollback operation identity is required", {}, 422
            )
        repository = self._repository
        if repository is None:
            return self._generation.rollback(candidate_generation_id, source_receipt=source_receipt)

        def validate_graph_source(
            source_identity: Mapping[str, Any], *, connection: Any
        ) -> Mapping[str, Any] | None:
            try:
                source_revision = int(source_identity["source_revision"])
                source_manifest_hash = str(source_identity["source_manifest_hash"])
                source_head_fence = int(source_identity["source_head_fence"])
                self._source.validate_current_head(
                    source_revision=source_revision,
                    source_manifest_hash=source_manifest_hash,
                    source_head_fence=source_head_fence,
                    connection=connection,
                )
                receipt = self._acknowledge(
                    source_revision=source_revision,
                    source_manifest_hash=source_manifest_hash,
                    source_head_fence=source_head_fence,
                    purpose="rollback",
                    operation_id=f"{operation_id}:source-rollback",
                    connection=connection,
                )
            except (KeyError, TypeError, ValueError, PlatformError):
                return None
            if receipt is None:
                return None
            return {
                "state": receipt.state,
                "source_revision": receipt.source_revision,
                "source_manifest_hash": receipt.source_manifest_hash,
                "source_head_fence": receipt.source_head_fence,
                "operation_id": receipt.operation_id,
            }

        try:
            with self._lock:
                with repository._engine.begin() as connection:
                    return repository._rollback_after_graph_source_validation(
                        candidate_generation_id,
                        source_receipt=source_receipt,
                        graph_source_validation=lambda source_identity: validate_graph_source(
                            source_identity, connection=connection
                        ),
                        connection=connection,
                    )
        except Exception:
            repository.record_component_failure("rollback")
            raise

    RollbackGeneration = rollback_generation

    def request_index_generation_gc(
        self,
        *,
        candidate_generation_id: str,
        reconciliation_run_id: str,
        operation_id: str,
        caller_principal: str | None = None,
    ) -> IndexGenerationGcReceipt:
        if caller_principal != "retention-ops":
            raise PlatformError("forbidden", "GC request requires retention authority", {}, 403)
        return self._generation.request_index_generation_gc(
            candidate_generation_id,
            reconciliation_run_id=reconciliation_run_id,
            operation_id=operation_id,
        )

    def complete_index_generation_gc(
        self,
        *,
        candidate_generation_id: str,
        operation_id: str,
        caller_principal: str | None = None,
    ) -> IndexGenerationGcReceipt:
        if caller_principal != "retention-ops":
            raise PlatformError("forbidden", "GC completion requires retention authority", {}, 403)
        repository = self._repository
        if repository is None:
            return self._generation.complete_generation_gc(
                candidate_generation_id, operation_id=operation_id
            )
        with self._lock:
            try:
                with repository._engine.begin() as connection:
                    eligibility = repository.gc_eligibility(
                        candidate_generation_id,
                        operation_id=operation_id,
                        connection=connection,
                    )
                    if eligibility.state != "accepted":
                        return eligibility
                    try:
                        repository.record_gc_component_progress(
                            candidate_generation_id,
                            operation_id=operation_id,
                            component_kind="public_graph",
                            state="running",
                            connection=connection,
                        )
                        candidate = repository.get_generation(
                            candidate_generation_id, connection=connection
                        )
                        if candidate.manifest.get("components", {}).get("public_graph"):
                            repository.set_component_state(
                                candidate_generation_id,
                                "public_graph",
                                "disabled",
                                connection=connection,
                            )
                        repository.stop_graph_readers(
                            candidate_generation_id, connection=connection
                        )
                        receipt = self._authoritative_stage_receipt(
                            candidate_generation_id, connection=connection
                        )
                        if receipt is not None:
                            self._discard_graph_resources(
                                receipt,
                                operation_id=f"{operation_id}:component-discard",
                                connection=connection,
                            )
                        repository.remove_graph_component_for_gc(
                            candidate_generation_id, connection=connection
                        )
                        if self._graph_store is not None:
                            self._graph_store.purge_generation(
                                candidate_generation_id,
                                connection=connection,
                            )
                        repository.record_gc_component_progress(
                            candidate_generation_id,
                            operation_id=operation_id,
                            component_kind="public_graph",
                            state="completed",
                            connection=connection,
                        )
                    except Exception as error:
                        if isinstance(error, PlatformError):
                            if error.code == "idempotency_key_conflict":
                                raise
                            failure = error
                        else:
                            failure = PlatformError(
                                "graph_cleanup_failed",
                                "Graph component cleanup failed",
                                {},
                                503,
                            )
                        raise _GraphComponentCleanupFailure(failure) from error
            except _GraphComponentCleanupFailure as failure:
                return repository.record_gc_cleanup_failure(
                    candidate_generation_id,
                    operation_id=operation_id,
                    reason=failure.error.code,
                )
            return repository.complete_generation_gc(
                candidate_generation_id,
                operation_id=operation_id,
            )

    CompleteIndexGenerationGc = complete_index_generation_gc

    def discard_public_graph_component(
        self,
        *,
        graph_build_id: str,
        attempt: int,
        target_generation_id: str,
        component_stage_id: str,
        operation_id: str,
        acknowledge_source: bool = True,
        lease_guard: Callable[[Any | None], None] | None = None,
    ) -> Mapping[str, Any]:
        del graph_build_id, attempt
        with self._lock:
            with self._transaction() as connection:
                if lease_guard is not None:
                    lease_guard(connection)
                receipt = next(
                    (
                        value
                        for value in self._receipts.values()
                        if component_stage_id and value.component_stage_id == component_stage_id
                    ),
                    None,
                )
                if receipt is None and component_stage_id:
                    receipt = self._stored_stage_receipt(
                        target_generation_id, component_stage_id, connection=connection
                    )
                if receipt is None and not component_stage_id:
                    receipt = self._authoritative_stage_receipt(
                        target_generation_id, connection=connection
                    )
                if receipt is None or receipt.target_generation_id != target_generation_id:
                    return {"state": "discarded"}
                self._discard_graph_resources(
                    receipt,
                    operation_id=operation_id,
                    connection=connection,
                    acknowledge_source=acknowledge_source,
                )
                for operation_id_key, value in tuple(self._receipts.items()):
                    if value == receipt:
                        del self._receipts[operation_id_key]
                return {"state": "discarded", "component_stage_id": receipt.component_stage_id}


__all__ = [
    "GraphComponentCoordinator",
    "GraphComponentReleaseReceipt",
    "GraphComponentStageGrant",
    "GraphComponentStageReceipt",
    "IndexGenerationComponentInput",
    "PublicGraphComponentGcReceipt",
]
