"""Read-model dataclasses for the public graph build domain."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

GraphBuildState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
GraphAvailability = Literal["disabled", "ready", "stale"]
GraphFailureClass = Literal[
    "graph_source_changed",
    "graph_stage_grant_expired",
    "graph_component_stage_failed",
    "graph_release_failed",
    "graph_provider_failed",
    "graph_worker_unexpected",
]


@dataclass(frozen=True, slots=True)
class GraphRunRecord:
    """One persisted graph build run row, including fence-bound lease fields."""

    graph_build_id: str
    version: int
    state: GraphBuildState
    initiator_identity_id: str
    source_revision: int
    source_manifest_id: str
    source_manifest_hash: str
    source_head_fence: int
    publications: tuple[dict[str, str], ...]
    target_generation_id: str
    target_generation_fence: str
    component_manifest_slot: str
    component_stage_id: str | None
    grant_operation_id: str
    grant_expires_at: datetime
    config_snapshot: Mapping[str, Any]
    plan_snapshot: Mapping[str, Any]
    estimated_primary_model_calls: int
    actual_primary_model_calls: int
    actual_provider_calls: int
    current_attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    fencing_token: str | None
    failure_class: str | None
    failure_reason: str | None
    graph_generation_id: str | None
    index_generation_id: str | None
    activation_receipt_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class GraphRunView:
    """The public `latest_run` read-model shape shared by create/cancel/current."""

    graph_build_id: str
    version: int
    state: GraphBuildState
    source_revision: int
    estimated_primary_model_calls: int
    actual_primary_model_calls: int
    actual_provider_calls: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_class: str | None
    graph_generation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_build_id": self.graph_build_id,
            "version": self.version,
            "state": self.state,
            "source_revision": self.source_revision,
            "estimated_primary_model_calls": self.estimated_primary_model_calls,
            "actual_usage": (
                {
                    "primary_model_calls": self.actual_primary_model_calls,
                    "provider_calls": self.actual_provider_calls,
                }
                if self.actual_provider_calls > 0
                else None
            ),
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at) if self.started_at is not None else None,
            "completed_at": _iso(self.completed_at) if self.completed_at is not None else None,
            "failure_class": self.failure_class,
            "graph_generation_id": self.graph_generation_id,
            "allowed_actions": ["cancel"] if self.state in {"queued", "running"} else [],
        }

    @classmethod
    def from_record(cls, record: GraphRunRecord | None) -> GraphRunView | None:
        if record is None:
            return None
        return cls(
            graph_build_id=record.graph_build_id,
            version=record.version,
            state=record.state,
            source_revision=record.source_revision,
            estimated_primary_model_calls=record.estimated_primary_model_calls,
            actual_primary_model_calls=record.actual_primary_model_calls,
            actual_provider_calls=record.actual_provider_calls,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            failure_class=record.failure_class,
            graph_generation_id=record.graph_generation_id,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphRunView:
        created_at = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
        started_at_value = value.get("started_at")
        completed_at_value = value.get("completed_at")
        return cls(
            graph_build_id=str(value["graph_build_id"]),
            version=int(value["version"]),
            state=value["state"],  # type: ignore[arg-type]
            source_revision=int(value["source_revision"]),
            estimated_primary_model_calls=int(value["estimated_primary_model_calls"]),
            actual_primary_model_calls=int(
                (value.get("actual_usage") or {}).get("primary_model_calls", 0)
            ),
            actual_provider_calls=int((value.get("actual_usage") or {}).get("provider_calls", 0)),
            created_at=created_at,
            started_at=(
                datetime.fromisoformat(str(started_at_value).replace("Z", "+00:00"))
                if started_at_value is not None
                else None
            ),
            completed_at=(
                datetime.fromisoformat(str(completed_at_value).replace("Z", "+00:00"))
                if completed_at_value is not None
                else None
            ),
            failure_class=(
                str(value["failure_class"]) if value.get("failure_class") is not None else None
            ),
            graph_generation_id=(
                str(value["graph_generation_id"])
                if value.get("graph_generation_id") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ActiveGraphComponent:
    """Indexing-owned facts about the active generation's public graph component."""

    generation_id: str
    component_state: str
    source_revision: int
    source_manifest_hash: str
    source_head_fence: int
    activated_at: datetime | None


@dataclass(frozen=True, slots=True)
class GraphActivationReceipt:
    """Persisted `ReleaseGraphComponent.state=activated` receipt."""

    state: str
    active_generation_id: str
    graph_generation_id: str
    source_revision: int
    source_manifest_hash: str
    source_head_fence: int
    activation_receipt_id: str


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ActiveGraphComponent",
    "GraphActivationReceipt",
    "GraphAvailability",
    "GraphFailureClass",
    "GraphRunRecord",
    "GraphRunView",
    "GraphBuildState",
]
