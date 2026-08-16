"""Ports consumed by the public graph build domain.

Every port is an existing platform boundary: documents owns the public source
facts, indexing owns generation/component staging, usage owns provider-call
facts, and outbox owns terminal notification events. The graph domain never
creates or rewrites those facts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.engine import Connection


class PublicGraphSourcePort(Protocol):
    def get_snapshot(
        self, *, source_revision: int, connection: Connection | None = None
    ) -> Any: ...
    def get_current_head(self, *, connection: Connection | None = None) -> Any: ...
    def validate_current_head(
        self,
        *,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
        connection: Connection | None = None,
    ) -> Any: ...
    def acknowledge_consumption(
        self,
        *,
        consumer_kind: str,
        consumer_id: str,
        source_revision: int,
        source_manifest_hash: str,
        purpose: str,
        operation_id: str,
        source_head_fence: int | None = None,
        connection: Connection | None = None,
    ) -> Any: ...


class ActiveGenerationPort(Protocol):
    """Read-only window into indexing's active generation manifest."""

    def active_generation_id(self, *, connection: Connection | None = None) -> str: ...
    def get_generation(
        self, generation_id: str, *, connection: Connection | None = None
    ) -> Any: ...


class GraphComponentCoordinatorPort(Protocol):
    def reserve_graph_component_stage(
        self,
        *,
        graph_build_id: str,
        expected_source_revision: int,
        expected_active_generation_id: str,
        operation_id: str,
        component_input: Any,
        reservation_guard: Callable[[Connection | None], None] | None = None,
        connection: Connection | None = None,
    ) -> Any: ...
    def stage_public_graph_component(
        self,
        *,
        grant: Any,
        graph_resource_manifest_hash: str,
        graph_resource_ids: Sequence[str],
        build_receipt_hash: str,
        lease_guard: Callable[[Connection | None], None] | None = None,
    ) -> Any: ...
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
        lease_guard: Callable[[Connection | None], None] | None = None,
    ) -> Any: ...
    def discard_public_graph_component(
        self,
        *,
        graph_build_id: str,
        attempt: int,
        target_generation_id: str,
        component_stage_id: str,
        operation_id: str,
        acknowledge_source: bool = True,
        lease_guard: Callable[[Connection | None], None] | None = None,
    ) -> Mapping[str, Any]: ...


class GraphBuildOutboxPort(Protocol):
    def publish_completed(
        self,
        *,
        graph_build_id: str,
        status: str,
        source_revision: int,
        transition_version: int,
        occurred_at: datetime,
        recipient_user_id: str,
        graph_generation_id: str | None = None,
        index_generation_id: str | None = None,
        failure_class: str | None = None,
        connection: Connection,
    ) -> str: ...


class GraphExtractionSession(Protocol):
    """Fence-bound staging + usage boundary handed to the graph extractor."""

    @property
    def staging(self) -> GraphStagingWriter: ...

    def primary_call(
        self,
        *,
        operation: str,
        resource_id: str | None,
        request_fingerprint: str,
        send: Callable[[], Any],
    ) -> Any: ...

    def heartbeat(self) -> None: ...

    def deadline_expired(self) -> bool: ...


class GraphStagingWriter(Protocol):
    def stage(
        self, *, resource_kind: str, resource_id: str, payload: Mapping[str, Any]
    ) -> None: ...


class GraphExtractorPort(Protocol):
    def estimate_primary_model_calls(self, snapshot: Any) -> int: ...
    def extract(self, snapshot: Any, session: GraphExtractionSession) -> None: ...


class GraphActivatedReceiptVerifierPort(Protocol):
    def verify_activated_receipt(
        self,
        *,
        aggregate_id: str,
        graph_generation_id: str,
        connection: Connection,
    ) -> bool: ...


__all__ = [
    "ActiveGenerationPort",
    "GraphActivatedReceiptVerifierPort",
    "GraphBuildOutboxPort",
    "GraphComponentCoordinatorPort",
    "GraphExtractionSession",
    "GraphExtractorPort",
    "GraphStagingWriter",
    "PublicGraphSourcePort",
]
