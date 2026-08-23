"""Prefix-cache facts for contextual retrieval.

The provider remains authoritative for prompt-token accounting. This module
only records whether a deterministic prefix identity is still current and
keeps generation cleanup separate from document/index invalidation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from .generation import GenerationManager
from .models import Generation


class PrefixCacheUnavailable(Exception):
    """The cache provider is unavailable; callers must bypass it."""


@dataclass(frozen=True, slots=True)
class PrefixCacheKey:
    instance_id: str
    space_id: str
    document_id: str
    document_version_id: str
    unit_id: str
    model_revision: str
    prompt_schema_version: str
    metadata_version: str
    tokenization_config_version: str

    def identity(self) -> str:
        fields = (
            self.instance_id,
            self.space_id,
            self.document_id,
            self.document_version_id,
            self.unit_id,
            self.model_revision,
            self.prompt_schema_version,
            self.metadata_version,
            self.tokenization_config_version,
        )
        return "\n".join(field.replace("\\", "\\\\").replace("\n", "\\n") for field in fields)


@dataclass(frozen=True, slots=True)
class PrefixCacheEntry:
    key: PrefixCacheKey
    prefix: str
    generation_id: str
    warmup_completed_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class PrefixCacheDecision:
    mode: Literal["ready", "warm", "no-cache"]
    outage: bool = False
    reason: str = "new-prefix"


class PrefixCacheStore(Protocol):
    def load(self, key: PrefixCacheKey, generation_id: str) -> PrefixCacheEntry | None: ...

    def save(self, entry: PrefixCacheEntry) -> None: ...

    def delete(self, key: PrefixCacheKey, generation_id: str) -> None: ...

    def delete_where(
        self,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
        generation_id: str | None = None,
    ) -> int: ...


class InMemoryPrefixCacheStore:
    def __init__(self) -> None:
        self._entries: dict[str, PrefixCacheEntry] = {}

    @staticmethod
    def _identity(key: PrefixCacheKey, generation_id: str) -> str:
        return f"{key.identity()}\n{generation_id}"

    def load(self, key: PrefixCacheKey, generation_id: str) -> PrefixCacheEntry | None:
        return self._entries.get(self._identity(key, generation_id))

    def save(self, entry: PrefixCacheEntry) -> None:
        self._entries[self._identity(entry.key, entry.generation_id)] = entry

    def delete(self, key: PrefixCacheKey, generation_id: str) -> None:
        self._entries.pop(self._identity(key, generation_id), None)

    def delete_where(
        self,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
        generation_id: str | None = None,
    ) -> int:
        selected = [
            entry
            for entry in self._entries.values()
            if (document_id is None or entry.key.document_id == document_id)
            and (
                document_version_id is None or entry.key.document_version_id == document_version_id
            )
            and (generation_id is None or entry.generation_id == generation_id)
        ]
        for entry in selected:
            self._entries.pop(self._identity(entry.key, entry.generation_id), None)
        return len(selected)


class PrefixCacheManager:
    """Idempotent prefix-fact registry with an explicit outage bypass."""

    def __init__(self, store: PrefixCacheStore | None = None) -> None:
        self._store = store or InMemoryPrefixCacheStore()
        self._lock = threading.RLock()

    def begin_prefix(
        self,
        key: PrefixCacheKey,
        *,
        prefix: str,
        generation_id: str,
    ) -> PrefixCacheDecision:
        with self._lock:
            return self._begin_prefix(
                key,
                prefix=prefix,
                generation_id=generation_id,
            )

    def _begin_prefix(
        self,
        key: PrefixCacheKey,
        *,
        prefix: str,
        generation_id: str,
    ) -> PrefixCacheDecision:
        try:
            existing = self._store.load(key, generation_id)
        except PrefixCacheUnavailable:
            return PrefixCacheDecision("no-cache", True, "cache_provider_outage")
        if existing is not None and existing.prefix == prefix:
            if existing.generation_id != generation_id:
                try:
                    self._store.save(
                        PrefixCacheEntry(
                            key,
                            prefix,
                            generation_id,
                            existing.warmup_completed_at_utc,
                        )
                    )
                except PrefixCacheUnavailable:
                    return PrefixCacheDecision("no-cache", True, "cache_provider_outage")
            return PrefixCacheDecision("ready", False, "prefix-current")
        if existing is not None:
            self._store.delete(key, generation_id)
        return PrefixCacheDecision("warm", False, "prefix-changed")

    def complete_prefix(
        self,
        key: PrefixCacheKey,
        *,
        prefix: str,
        generation_id: str,
        now_utc: datetime,
    ) -> None:
        timestamp = now_utc.astimezone(UTC) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
        with self._lock:
            try:
                self._store.save(PrefixCacheEntry(key, prefix, generation_id, timestamp))
            except PrefixCacheUnavailable:
                # The provider calls already completed. Cache admission remains
                # unavailable and the next document bypasses rather than guessing.
                return

    def invalidate_document_version(
        self, *, document_id: str, document_version_id: str, reason: str
    ) -> int:
        del reason
        with self._lock:
            try:
                return self._store.delete_where(
                    document_id=document_id, document_version_id=document_version_id
                )
            except PrefixCacheUnavailable:
                return 0

    def invalidate_document(self, *, document_id: str, reason: str) -> int:
        del reason
        with self._lock:
            try:
                return self._store.delete_where(document_id=document_id)
            except PrefixCacheUnavailable:
                return 0

    def delete_generation(self, *, generation_id: str) -> int:
        with self._lock:
            try:
                return self._store.delete_where(generation_id=generation_id)
            except PrefixCacheUnavailable:
                return 0


def prefix_cleanup_eligibility(
    generation: Generation,
    *,
    generation_manager: GenerationManager,
    now_utc: datetime,
    index_changes_processed: bool = False,
    gc_authorized: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Return whether a whole generation's prefix facts may be removed.

    Document-version invalidation is intentionally separate: consuming an
    index change can invalidate one version while the retired generation still
    needs its other prefix facts for rollback.
    """

    if generation.status == "purged":
        return True, ()
    if generation.status != "retired":
        return False, (f"generation_{generation.status}",)
    reasons: list[str] = []
    rollback_until = generation.rollback_until_utc
    rollback_window_active = rollback_until is not None and rollback_until > now_utc
    if (
        rollback_window_active
        and generation_manager.rollback_candidate_id == generation.generation_id
    ):
        reasons.append("rollback_candidate")
    if generation_manager.has_generation_lease(generation.generation_id):
        reasons.append("active_query_lease")
    if not index_changes_processed and not gc_authorized:
        reasons.append("cleanup_not_authorized")
    if reasons:
        return False, tuple(dict.fromkeys(reasons))
    return True, ()


__all__ = [
    "InMemoryPrefixCacheStore",
    "PrefixCacheDecision",
    "PrefixCacheEntry",
    "PrefixCacheKey",
    "PrefixCacheManager",
    "PrefixCacheStore",
    "PrefixCacheUnavailable",
    "prefix_cleanup_eligibility",
]
