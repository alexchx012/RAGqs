"""Ports for backup/restore orchestration.

Concrete dump/snapshot mechanics (Postgres dump tools, object-storage SDK
snapshots, Milvus/OpenSearch/graph clients) stay behind these protocols.
The orchestration layer only coordinates identity, ordering, gates,
validation and idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ObjectFact:
    """An opaque object identity with size and checksum, as recorded in the
    object manifest and in Postgres records."""

    object_key: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PostgresBackupPort(Protocol):
    def snapshot(self) -> str:
        """Produce a Postgres backup snapshot; returns an opaque reference."""

    def restore(self, reference: str) -> None:
        """Restore Postgres from the snapshot referenced by `reference`."""


class ObjectSnapshotPort(Protocol):
    def snapshot(self) -> str:
        """Produce an object-storage snapshot; returns an opaque reference."""

    def restore(self, reference: str) -> None:
        """Restore object storage from the snapshot referenced by `reference`."""


class ObjectManifestPort(Protocol):
    def collect_object_facts(self) -> list[ObjectFact]:
        """Collect per-object facts (identity, size, checksum) for the manifest.

        Facts come from Postgres records joined with object storage; derived
        indexes are never consulted.
        """


class FactValidationPort(Protocol):
    def expected_object_facts(self) -> list[ObjectFact]:
        """Object facts as recorded by the restored Postgres side."""

    def actual_object_facts(self) -> list[ObjectFact]:
        """Object facts as observed on the restored object-storage side."""


class DerivedRebuildPort(Protocol):
    def list_resources(self, stage: str) -> list[str]:
        """Resource identities the given derived stage must rebuild."""

    def rebuild(self, stage: str, resource_id: str) -> None:
        """Rebuild one derived resource from restored facts.

        Raises on failure; the orchestrator records the classification and
        retries only this resource.
        """


class PostGateValidationPort(Protocol):
    def validate_post_gate(self) -> list[str]:
        """Return blocking post-gate findings (empty list when the gate may
        open): active version/publication consistency, index generation
        completeness, orphaned/duplicate/missing entries, declared
        hierarchical/graph index checks."""


class NoopPostgresBackup:
    """Default adapter for environments without a real dump backend."""

    def snapshot(self) -> str:
        return "postgres-snapshot:noop"

    def restore(self, reference: str) -> None:
        del reference


class NoopObjectSnapshot:
    def snapshot(self) -> str:
        return "object-snapshot:noop"

    def restore(self, reference: str) -> None:
        del reference


class EmptyObjectManifest:
    def collect_object_facts(self) -> list[ObjectFact]:
        return []
