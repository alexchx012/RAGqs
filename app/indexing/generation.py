from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from app.platform.errors import PlatformError

from .models import (
    Generation,
    GenerationComponentReaderLease,
    GenerationReferenceLease,
    IndexGenerationGcReceipt,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


@dataclass(slots=True)
class _LeaseRecord:
    lease: GenerationReferenceLease | GenerationComponentReaderLease
    kind: str


class GenerationManager:
    """Owns the single active generation pointer and its release lifecycle."""

    def __init__(
        self,
        *,
        current_revision: int = 0,
        now: Callable[[], datetime] = _now,
        rollback_days: int = 7,
    ) -> None:
        if current_revision < 0 or rollback_days < 1:
            raise ValueError("generation settings are invalid")
        self._now = now
        self._rollback_window = timedelta(days=rollback_days)
        timestamp = _utc(now())
        initial = Generation(
            generation_id="generation_initial",
            status="active",
            base_revision=current_revision,
            applied_revision=current_revision,
            manifest={"components": {}, "base_snapshot": []},
            created_at=timestamp,
            activated_at=timestamp,
        )
        self._generations: dict[str, Generation] = {initial.generation_id: initial}
        self._active_generation_id = initial.generation_id
        self._current_revision = current_revision
        self._rollback_candidate_id: str | None = None
        self._leases: dict[str, _LeaseRecord] = {}
        self._gc_operations: dict[str, tuple[str, IndexGenerationGcReceipt]] = {}
        self._lock = RLock()

    @property
    def active_generation_id(self) -> str:
        with self._lock:
            return self._active_generation_id

    @property
    def rollback_candidate_id(self) -> str | None:
        with self._lock:
            return self._rollback_candidate_id

    @property
    def current_revision(self) -> int:
        with self._lock:
            return self._current_revision

    def set_current_revision(self, revision: int) -> None:
        if revision < 0:
            raise PlatformError("validation_error", "index revision is invalid", {}, 422)
        with self._lock:
            if revision < self._current_revision:
                raise PlatformError(
                    "revision_conflict", "index revision cannot move backwards", {}, 409
                )
            self._current_revision = revision

    def get_generation(self, generation_id: str) -> Generation:
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation is None:
                raise PlatformError(
                    "generation_not_found", "Index generation was not found", {}, 404
                )
            return generation

    def has_generation_lease(self, generation_id: str) -> bool:
        with self._lock:
            return any(
                record.lease.generation_id == generation_id for record in self._leases.values()
            )

    def list_generations(self) -> tuple[Generation, ...]:
        with self._lock:
            return tuple(self._generations.values())

    def create_staging(
        self,
        base_snapshot: Sequence[Mapping[str, Any]],
        *,
        base_revision: int | None = None,
        expected_active_generation_id: str | None = None,
        generation_id: str | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> Generation:
        with self._lock:
            if (
                expected_active_generation_id is not None
                and expected_active_generation_id != self._active_generation_id
            ):
                raise PlatformError("generation_conflict", "active generation has changed", {}, 409)
            revision = self._current_revision if base_revision is None else base_revision
            if revision < 0 or revision > self._current_revision:
                raise PlatformError("revision_conflict", "base revision is invalid", {}, 409)
            identifier = generation_id or _new_id("generation")
            if identifier in self._generations:
                return self._generations[identifier]
            payload = dict(manifest or {})
            payload.setdefault("components", {})
            payload["base_snapshot"] = [dict(item) for item in base_snapshot]
            timestamp = _utc(self._now())
            generation = Generation(
                generation_id=identifier,
                status="staging",
                base_revision=revision,
                applied_revision=revision,
                manifest=payload,
                created_at=timestamp,
            )
            self._generations[identifier] = generation
            return generation

    def apply_change(
        self,
        generation_id: str,
        revision: int,
        change: Mapping[str, Any],
    ) -> Generation:
        if revision < 1:
            raise PlatformError("validation_error", "revision is invalid", {}, 422)
        with self._lock:
            generation = self.get_generation(generation_id)
            if generation.status not in {"staging", "retired"}:
                raise PlatformError(
                    "generation_not_writable", "generation cannot consume changes", {}, 409
                )
            changes = list(generation.manifest.get("index_changes", []))
            if revision <= generation.applied_revision:
                known = next((item for item in changes if int(item["revision"]) == revision), None)
                if known == dict(change) | {"revision": revision}:
                    return generation
                raise PlatformError("revision_conflict", "revision replay conflicts", {}, 409)
            if revision != generation.applied_revision + 1:
                raise PlatformError(
                    "revision_gap", "index changes must be applied continuously", {}, 409
                )
            normalized = dict(change)
            normalized["revision"] = revision
            changes.append(normalized)
            updated = generation.with_updates(
                applied_revision=revision,
                rollback_applied_revision=(
                    revision
                    if generation.status == "retired"
                    else generation.rollback_applied_revision
                ),
                manifest={**dict(generation.manifest), "index_changes": changes},
            )
            self._generations[generation_id] = updated
            return updated

    def set_component_state(
        self,
        generation_id: str,
        component_kind: str,
        state: str,
        *,
        manifest: Mapping[str, Any] | None = None,
    ) -> Generation:
        if state not in {"staged", "ready", "disabled", "stale", "failed"}:
            raise PlatformError("validation_error", "component state is invalid", {}, 422)
        with self._lock:
            generation = self.get_generation(generation_id)
            components = dict(generation.manifest.get("components", {}))
            current = dict(components.get(component_kind, {}))
            current["state"] = state
            if manifest:
                current.update(dict(manifest))
            components[component_kind] = current
            graph_state = generation.graph_component_state
            if component_kind == "public_graph":
                graph_state = state  # type: ignore[assignment]
            updated = generation.with_updates(
                manifest={**dict(generation.manifest), "components": components},
                graph_component_state=graph_state,
            )
            self._generations[generation_id] = updated
            return updated

    def release(
        self,
        generation_id: str,
        *,
        expected_active_generation_id: str | None = None,
        current_revision: int | None = None,
        release_gate: Callable[[Generation], bool] | None = None,
    ) -> Generation:
        with self._lock:
            generation = self.get_generation(generation_id)
            expected = self._active_generation_id
            if expected_active_generation_id is not None:
                expected = expected_active_generation_id
            if expected != self._active_generation_id:
                raise PlatformError("generation_conflict", "active generation has changed", {}, 409)
            revision = self._current_revision if current_revision is None else current_revision
            if generation.status != "staging":
                if generation.status == "active":
                    return generation
                raise PlatformError(
                    "index_release_blocked", "generation is not releasable", {}, 409
                )
            if generation.applied_revision != revision:
                raise PlatformError(
                    "release_gate_failed", "generation has unprocessed revisions", {}, 409
                )
            if release_gate is not None and not release_gate(generation):
                raise PlatformError(
                    "release_gate_failed", "generation release gate failed", {}, 409
                )
            for component in generation.manifest.get("components", {}).values():
                if component.get("state") in {"failed", "stale"}:
                    raise PlatformError(
                        "release_gate_failed", "a generation component failed", {}, 409
                    )
            previous = self._generations[self._active_generation_id]
            timestamp = _utc(self._now())
            retired = previous.with_updates(
                status="retired",
                retired_at=timestamp,
                rollback_until_utc=timestamp + self._rollback_window,
            )
            activated = generation.with_updates(
                status="active", activated_at=timestamp, retired_at=None
            )
            self._generations[retired.generation_id] = retired
            self._generations[activated.generation_id] = activated
            self._active_generation_id = activated.generation_id
            self._rollback_candidate_id = retired.generation_id
            return activated

    def rollback(
        self,
        candidate_generation_id: str,
        *,
        current_revision: int | None = None,
        source_receipt: Mapping[str, Any] | None = None,
        release_gate: Callable[[Generation], bool] | None = None,
    ) -> Generation:
        with self._lock:
            candidate = self.get_generation(candidate_generation_id)
            revision = self._current_revision if current_revision is None else current_revision
            if (
                candidate.status != "retired"
                or self._rollback_candidate_id != candidate_generation_id
            ):
                raise PlatformError(
                    "rollback_not_eligible", "generation is not a rollback candidate", {}, 409
                )
            retired_at = candidate.retired_at or candidate.created_at
            if _utc(self._now()) - _utc(retired_at) > self._rollback_window:
                raise PlatformError("rollback_not_eligible", "rollback window has expired", {}, 409)
            if candidate.rollback_applied_revision != revision:
                raise PlatformError(
                    "rollback_not_eligible", "rollback generation is not caught up", {}, 409
                )
            receipt = dict(source_receipt or {})
            components = dict(candidate.manifest.get("components", {}))
            graph = dict(components.get("public_graph", {}))
            receipt_matches = (
                receipt.get("state") == "held"
                and receipt.get("candidate_generation_id") == candidate_generation_id
                and int(receipt.get("applied_revision", -1)) == revision
            )
            if graph.get("state") == "ready":
                receipt_matches = receipt_matches and all(
                    receipt.get(name) == graph.get(name)
                    for name in ("source_revision", "source_manifest_hash", "source_head_fence")
                )
            if not receipt_matches:
                raise PlatformError(
                    "rollback_not_eligible",
                    "source receipt does not match the rollback candidate",
                    {},
                    409,
                )
            if release_gate is not None and not release_gate(candidate):
                raise PlatformError("release_gate_failed", "rollback release gate failed", {}, 409)
            if graph.get("state") == "ready":
                graph["state"] = "disabled"
                components["public_graph"] = graph
                candidate = candidate.with_updates(
                    graph_component_state="disabled",
                    manifest={**dict(candidate.manifest), "components": components},
                )
            active = self._generations[self._active_generation_id]
            timestamp = _utc(self._now())
            old_active = active.with_updates(
                status="retired",
                retired_at=timestamp,
                rollback_until_utc=timestamp + self._rollback_window,
            )
            restored = candidate.with_updates(
                status="active", activated_at=timestamp, retired_at=None
            )
            self._generations[old_active.generation_id] = old_active
            self._generations[restored.generation_id] = restored
            self._active_generation_id = restored.generation_id
            self._rollback_candidate_id = old_active.generation_id
            return restored

    def acquire_reference_lease(
        self, *, ttl: timedelta = timedelta(minutes=1)
    ) -> GenerationReferenceLease:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        with self._lock:
            lease = GenerationReferenceLease(
                lease_id=_new_id("generation_lease"),
                generation_id=self._active_generation_id,
                expires_at=_utc(self._now()) + ttl,
            )
            self._leases[lease.lease_id] = _LeaseRecord(lease, "generation")
            return lease

    def release_reference_lease(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)

    def acquire_graph_reader_lease(
        self,
        *,
        generation_id: str,
        source_head_fence: int,
        manifest_hash: str,
        validate_source_head: Callable[[], bool],
        ttl: timedelta = timedelta(minutes=1),
    ) -> GenerationComponentReaderLease:
        if source_head_fence < 1 or not manifest_hash:
            raise PlatformError("validation_error", "graph reader lease input is invalid", {}, 422)
        with self._lock:
            generation = self.get_generation(generation_id)
            component = generation.manifest.get("components", {}).get("public_graph", {})
            if component.get("state") != "ready":
                raise PlatformError(
                    "graph_unavailable", "public graph component is not ready", {}, 409
                )
            if not validate_source_head():
                self.set_component_state(generation.generation_id, "public_graph", "stale")
                raise PlatformError(
                    "graph_source_changed", "The public graph source has changed", {}, 409
                )
            lease = GenerationComponentReaderLease(
                lease_id=_new_id("graph_lease"),
                generation_id=generation.generation_id,
                component_kind="public_graph",
                manifest_hash=manifest_hash,
                source_head_fence=source_head_fence,
                expires_at=_utc(self._now()) + ttl,
            )
            self._leases[lease.lease_id] = _LeaseRecord(lease, "graph")
            return lease

    def get_graph_reader_lease(self, lease_id: str) -> GenerationComponentReaderLease:
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None or record.kind != "graph":
                raise PlatformError("lease_not_found", "graph reader lease was not found", {}, 404)
            lease = record.lease
            assert isinstance(lease, GenerationComponentReaderLease)
            return lease

    def renew_graph_reader_lease(
        self,
        lease_id: str,
        *,
        validate_source_head: Callable[[], bool],
        ttl: timedelta = timedelta(minutes=1),
    ) -> GenerationComponentReaderLease:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        with self._lock:
            record = self._leases.get(lease_id)
            if record is None or record.kind != "graph":
                raise PlatformError("lease_not_found", "graph reader lease was not found", {}, 404)
            lease = record.lease
            assert isinstance(lease, GenerationComponentReaderLease)
            if lease.expires_at <= _utc(self._now()):
                raise PlatformError("lease_expired", "graph reader lease has expired", {}, 409)
            if not validate_source_head():
                self._leases.pop(lease_id, None)
                self.set_component_state(lease.generation_id, "public_graph", "stale")
                raise PlatformError(
                    "graph_source_changed", "The public graph source has changed", {}, 409
                )
            renewed = GenerationComponentReaderLease(
                lease_id=lease.lease_id,
                generation_id=lease.generation_id,
                component_kind=lease.component_kind,
                manifest_hash=lease.manifest_hash,
                source_head_fence=lease.source_head_fence,
                expires_at=_utc(self._now()) + ttl,
            )
            self._leases[lease_id] = _LeaseRecord(renewed, "graph")
            return renewed

    def release_graph_reader_lease(self, lease_id: str) -> None:
        self.release_reference_lease(lease_id)

    def request_index_generation_gc(
        self,
        candidate_generation_id: str,
        *,
        reconciliation_run_id: str,
        operation_id: str,
    ) -> IndexGenerationGcReceipt:
        if not reconciliation_run_id.strip() or not operation_id.strip():
            raise PlatformError("validation_error", "GC operation identity is required", {}, 422)
        with self._lock:
            existing = self._gc_operations.get(operation_id)
            if existing is not None:
                existing_run_id, existing_receipt = existing
                if (
                    existing_run_id != reconciliation_run_id
                    or existing_receipt.candidate_generation_id != candidate_generation_id
                ):
                    raise PlatformError(
                        "idempotency_key_conflict", "GC operation conflicts", {}, 409
                    )
                return existing_receipt
            generation = self.get_generation(candidate_generation_id)
            reasons: list[str] = []
            if generation.status == "purged":
                receipt = IndexGenerationGcReceipt(
                    operation_id, candidate_generation_id, "already_purged"
                )
                self._gc_operations[operation_id] = (reconciliation_run_id, receipt)
                return receipt
            if generation.status == "active":
                reasons.append("active_generation")
            if self._rollback_candidate_id == candidate_generation_id:
                reasons.append("rollback_candidate")
            if any(
                record.lease.generation_id == candidate_generation_id
                for record in self._leases.values()
            ):
                reasons.append("active_lease")
            if generation.status != "retired":
                reasons.append("generation_not_retired")
            if reasons:
                receipt = IndexGenerationGcReceipt(
                    operation_id,
                    candidate_generation_id,
                    "blocked",
                    tuple(dict.fromkeys(reasons)),
                    True,
                )
                self._gc_operations[operation_id] = (reconciliation_run_id, receipt)
                return receipt
            receipt = IndexGenerationGcReceipt(operation_id, candidate_generation_id, "accepted")
            self._gc_operations[operation_id] = (reconciliation_run_id, receipt)
            return receipt

    def complete_generation_gc(
        self, candidate_generation_id: str, *, operation_id: str
    ) -> IndexGenerationGcReceipt:
        with self._lock:
            operation = self._gc_operations.get(operation_id)
            if operation is None or operation[1].candidate_generation_id != candidate_generation_id:
                raise PlatformError("gc_operation_not_found", "GC operation was not found", {}, 404)
            reconciliation_run_id, receipt = operation
            if receipt.state != "accepted":
                return receipt
            generation = self.get_generation(candidate_generation_id)
            if generation.status != "retired":
                raise PlatformError(
                    "gc_blocked", "generation is no longer eligible for GC", {}, 409
                )
            self._generations[candidate_generation_id] = generation.with_updates(status="purged")
            completed = IndexGenerationGcReceipt(
                operation_id, candidate_generation_id, "already_purged"
            )
            self._gc_operations[operation_id] = (reconciliation_run_id, completed)
            return completed


IndexGenerationService = GenerationManager


__all__ = ["GenerationManager", "IndexGenerationService"]
