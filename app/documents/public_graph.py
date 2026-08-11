from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Connection, Engine

from app.platform.database import _insert_do_nothing
from app.platform.errors import PlatformError

from .domain import canonical_request_fingerprint
from .schema import (
    documents_instance_counters_table,
    public_graph_source_changes_table,
    public_graph_source_consumers_table,
    public_graph_source_heads_table,
    public_graph_source_manifests_table,
)


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(15)}"


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PublicGraphSourceSnapshot:
    schema_version: int
    source_revision: int
    source_manifest_id: str
    source_manifest_hash: str
    publications: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class PublicGraphSourceHead:
    source_revision: int
    source_manifest_id: str | None
    source_manifest_hash: str | None
    source_head_fence: int


@dataclass(frozen=True, slots=True)
class PublicGraphSourceHeadValidationReceipt:
    current: bool
    head: PublicGraphSourceHead


@dataclass(frozen=True, slots=True)
class PublicGraphSourceConsumptionReceipt:
    operation_id: str
    consumer_kind: Literal["indexing", "public_graph"]
    consumer_id: str
    source_revision: int
    source_manifest_hash: str
    source_head_fence: int | None
    state: Literal["held", "discarded"]
    acknowledged_at: datetime


class PublicGraphSourceService:
    """Owner-side source snapshot/head/consumer acknowledgement contract."""

    def __init__(
        self,
        engine: Engine,
        *,
        now: Any = _now,
        trusted_consumers: Mapping[str, set[str] | frozenset[str]] | None = None,
        outbox_port: Any | None = None,
    ) -> None:
        self._engine = engine
        self._now = now
        self._outbox_port = outbox_port
        configured = trusted_consumers or {}
        self._trusted_consumers = {
            "indexing": frozenset(configured.get("indexing", set())),
            "public_graph": frozenset(configured.get("public_graph", set())),
        }

    def record_source_change(
        self,
        *,
        space_id: str,
        document_id: str,
        change_type: Literal["publish", "replace", "reindex", "restore", "delete"],
        publications: Sequence[Mapping[str, str]],
        connection: Connection | None = None,
    ) -> PublicGraphSourceSnapshot:
        if space_id != "public":
            raise PlatformError("validation_error", "Only public space has a graph source", {}, 422)
        normalized_items: list[dict[str, str]] = []
        required_fields = (
            "document_id",
            "document_version_id",
            "publication_id",
            "content_manifest_id",
            "content_manifest_hash",
        )
        for item in publications:
            if not isinstance(item, Mapping):
                raise PlatformError("validation_error", "Publication source is invalid", {}, 422)
            normalized_item = {field: str(item.get(field, "")).strip() for field in required_fields}
            if any(not value for value in normalized_item.values()):
                raise PlatformError(
                    "validation_error",
                    "Publication source requires immutable manifest identifiers",
                    {},
                    422,
                )
            normalized_items.append(normalized_item)
        normalized = tuple(normalized_items)
        if connection is not None:
            return self._record_source_change(
                connection,
                space_id=space_id,
                document_id=document_id,
                change_type=change_type,
                normalized=normalized,
            )
        with self._engine.begin() as connection:
            return self._record_source_change(
                connection,
                space_id=space_id,
                document_id=document_id,
                change_type=change_type,
                normalized=normalized,
            )

    def _record_source_change(
        self,
        connection: Connection,
        *,
        space_id: str,
        document_id: str,
        change_type: str,
        normalized: tuple[dict[str, str], ...],
    ) -> PublicGraphSourceSnapshot:
        self._locked_head(connection)
        revision = self._next_counter(connection, "public_source_revision")
        fence = self._next_counter(connection, "public_source_head_fence")
        manifest_id = _id("source_manifest")
        payload = {
            "schema_version": 1,
            "source_revision": revision,
            "source_manifest_id": manifest_id,
            "publications": list(normalized),
        }
        manifest_hash = canonical_request_fingerprint(payload)
        now = self._now()
        connection.execute(
            public_graph_source_manifests_table.insert().values(
                id=manifest_id,
                source_revision=revision,
                source_manifest_hash=manifest_hash,
                schema_version=1,
                publications_json=list(normalized),
                source_head_fence=fence,
                created_at_utc=now,
            )
        )
        connection.execute(
            public_graph_source_changes_table.insert().values(
                id=_id("source_change"),
                source_revision=revision,
                space_id=space_id,
                document_id=document_id,
                change_type=change_type,
                manifest_id=manifest_id,
                created_at_utc=now,
            )
        )
        connection.execute(
            update(public_graph_source_heads_table)
            .where(public_graph_source_heads_table.c.id == "public")
            .values(
                source_revision=revision,
                source_manifest_id=manifest_id,
                source_manifest_hash=manifest_hash,
                source_head_fence=fence,
                updated_at_utc=now,
            )
        )
        outbox_port = self._outbox_port
        if outbox_port is None:
            raise PlatformError(
                "public_source_outbox_unavailable",
                "Public graph source outbox is not configured",
                {"retryable": True},
                503,
            )
        outbox_port.publish_public_graph_source_change(
            source_revision=revision,
            source_manifest_id=manifest_id,
            source_manifest_hash=manifest_hash,
            document_id=document_id,
            change_type=change_type,
            occurred_at=now,
            connection=connection,
        )
        return PublicGraphSourceSnapshot(
            schema_version=1,
            source_revision=revision,
            source_manifest_id=manifest_id,
            source_manifest_hash=manifest_hash,
            publications=normalized,
        )

    @staticmethod
    def _next_counter(connection: Connection, counter_name: str) -> int:
        _insert_do_nothing(
            connection,
            documents_instance_counters_table,
            {"counter_name": counter_name, "value": 0},
            index_elements=["counter_name"],
        )
        counter = (
            connection.execute(
                select(documents_instance_counters_table)
                .where(documents_instance_counters_table.c.counter_name == counter_name)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        current = int(counter["value"])
        next_value = current + 1
        result = connection.execute(
            update(documents_instance_counters_table)
            .where(
                and_(
                    documents_instance_counters_table.c.counter_name == counter_name,
                    documents_instance_counters_table.c.value == current,
                )
            )
            .values(value=next_value)
        )
        if result.rowcount != 1:
            raise RuntimeError("Could not allocate the next public source counter")
        return next_value

    def get_snapshot(self, *, source_revision: int) -> PublicGraphSourceSnapshot:
        if source_revision < 1:
            raise PlatformError("validation_error", "source_revision is invalid", {}, 422)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(public_graph_source_manifests_table).where(
                        public_graph_source_manifests_table.c.source_revision == source_revision
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PlatformError(
                "public_graph_source_revision_not_found",
                "Public graph source revision was not found",
                {},
                404,
            )
        publications = tuple(dict(item) for item in (row["publications_json"] or []))
        payload = {
            "schema_version": int(row["schema_version"]),
            "source_revision": int(row["source_revision"]),
            "source_manifest_id": str(row["id"]),
            "publications": list(publications),
        }
        if canonical_request_fingerprint(payload) != row["source_manifest_hash"]:
            raise PlatformError(
                "public_graph_source_manifest_invalid",
                "Public graph source manifest is invalid",
                {},
                409,
            )
        return PublicGraphSourceSnapshot(
            schema_version=int(row["schema_version"]),
            source_revision=int(row["source_revision"]),
            source_manifest_id=str(row["id"]),
            source_manifest_hash=str(row["source_manifest_hash"]),
            publications=publications,
        )

    def _locked_head(self, connection: Connection) -> Mapping[str, Any]:
        now = self._now()
        _insert_do_nothing(
            connection,
            public_graph_source_heads_table,
            {
                "id": "public",
                "source_revision": 0,
                "source_manifest_id": None,
                "source_manifest_hash": None,
                "source_head_fence": 0,
                "updated_at_utc": now,
            },
            index_elements=["id"],
        )
        return (
            connection.execute(
                select(public_graph_source_heads_table)
                .where(public_graph_source_heads_table.c.id == "public")
                .with_for_update()
            )
            .mappings()
            .one()
        )

    @staticmethod
    def _head_from_row(row: Mapping[str, Any]) -> PublicGraphSourceHead:
        return PublicGraphSourceHead(
            source_revision=int(row["source_revision"]),
            source_manifest_id=(
                str(row["source_manifest_id"]) if row["source_manifest_id"] is not None else None
            ),
            source_manifest_hash=(
                str(row["source_manifest_hash"])
                if row["source_manifest_hash"] is not None
                else None
            ),
            source_head_fence=int(row["source_head_fence"]),
        )

    def _validate_current_head_locked(
        self,
        connection: Connection,
        *,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
    ) -> PublicGraphSourceHead:
        head = self._head_from_row(self._locked_head(connection))
        if (
            head.source_revision != source_revision
            or head.source_manifest_hash != source_manifest_hash
            or head.source_head_fence != source_head_fence
        ):
            raise PlatformError(
                "graph_source_changed",
                "The public graph source has changed",
                {
                    "head": {
                        "source_revision": head.source_revision,
                        "source_manifest_id": head.source_manifest_id,
                        "source_manifest_hash": head.source_manifest_hash,
                        "source_head_fence": head.source_head_fence,
                    }
                },
                409,
            )
        return head

    def get_current_head(self) -> PublicGraphSourceHead:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(public_graph_source_heads_table).where(
                        public_graph_source_heads_table.c.id == "public"
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return PublicGraphSourceHead(0, None, None, 0)
        return self._head_from_row(row)

    def validate_current_head(
        self,
        *,
        source_revision: int,
        source_manifest_hash: str,
        source_head_fence: int,
    ) -> PublicGraphSourceHeadValidationReceipt:
        if source_revision < 1 or not source_manifest_hash or source_head_fence < 1:
            raise PlatformError("validation_error", "Public graph source head is invalid", {}, 422)
        with self._engine.begin() as connection:
            head = self._validate_current_head_locked(
                connection,
                source_revision=source_revision,
                source_manifest_hash=source_manifest_hash,
                source_head_fence=source_head_fence,
            )
        return PublicGraphSourceHeadValidationReceipt(current=True, head=head)

    def acknowledge_consumption(
        self,
        *,
        consumer_kind: Literal["indexing", "public_graph"],
        consumer_id: str,
        source_revision: int,
        source_manifest_hash: str,
        purpose: Literal["stage", "release", "rollback", "discard"],
        operation_id: str,
        source_head_fence: int | None = None,
    ) -> PublicGraphSourceConsumptionReceipt:
        if consumer_kind not in {"indexing", "public_graph"} or purpose not in {
            "stage",
            "release",
            "rollback",
            "discard",
        }:
            raise PlatformError("validation_error", "Consumer acknowledgement is invalid", {}, 422)
        if consumer_id not in self._trusted_consumers[consumer_kind]:
            raise PlatformError("consumer_not_trusted", "Consumer is not trusted", {}, 403)
        if purpose == "release" and (source_head_fence is None or source_head_fence < 1):
            raise PlatformError(
                "validation_error", "source_head_fence is required for release", {}, 422
            )
        snapshot = self.get_snapshot(source_revision=source_revision)
        if snapshot.source_manifest_hash != source_manifest_hash:
            raise PlatformError(
                "public_graph_source_manifest_invalid",
                "Source manifest hash does not match",
                {},
                409,
            )
        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    select(public_graph_source_consumers_table).where(
                        public_graph_source_consumers_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if any(
                    existing[key] != value
                    for key, value in {
                        "consumer_kind": consumer_kind,
                        "consumer_id": consumer_id,
                        "source_revision": source_revision,
                        "source_manifest_hash": source_manifest_hash,
                        "purpose": purpose,
                        "source_head_fence": source_head_fence,
                    }.items()
                ):
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "The operation conflicts with a previous acknowledgement",
                        {},
                        409,
                    )
                return self._receipt(existing)
            if purpose == "release":
                self._validate_current_head_locked(
                    connection,
                    source_revision=source_revision,
                    source_manifest_hash=source_manifest_hash,
                    source_head_fence=int(source_head_fence),
                )
            if purpose == "discard":
                existing_discard = (
                    connection.execute(
                        select(public_graph_source_consumers_table).where(
                            and_(
                                public_graph_source_consumers_table.c.consumer_kind
                                == consumer_kind,
                                public_graph_source_consumers_table.c.consumer_id == consumer_id,
                                public_graph_source_consumers_table.c.source_revision
                                == source_revision,
                                public_graph_source_consumers_table.c.purpose == "discard",
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing_discard is not None:
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "The discard acknowledgement already exists",
                        {},
                        409,
                    )
                released = connection.execute(
                    update(public_graph_source_consumers_table)
                    .where(
                        and_(
                            public_graph_source_consumers_table.c.consumer_kind == consumer_kind,
                            public_graph_source_consumers_table.c.consumer_id == consumer_id,
                            public_graph_source_consumers_table.c.source_revision
                            == source_revision,
                            public_graph_source_consumers_table.c.source_manifest_hash
                            == source_manifest_hash,
                            public_graph_source_consumers_table.c.state == "held",
                        )
                    )
                    .values(state="discarded")
                )
                if released.rowcount == 0:
                    raise PlatformError(
                        "consumer_ack_invalid", "No held consumer acknowledgement exists", {}, 409
                    )
                now = self._now()
                connection.execute(
                    public_graph_source_consumers_table.insert().values(
                        id=_id("consumer_ack"),
                        consumer_kind=consumer_kind,
                        consumer_id=consumer_id,
                        source_revision=source_revision,
                        source_manifest_hash=source_manifest_hash,
                        source_head_fence=source_head_fence,
                        purpose="discard",
                        operation_id=operation_id,
                        state="discarded",
                        acknowledged_at_utc=now,
                    )
                )
                return PublicGraphSourceConsumptionReceipt(
                    operation_id=operation_id,
                    consumer_kind=consumer_kind,
                    consumer_id=consumer_id,
                    source_revision=source_revision,
                    source_manifest_hash=source_manifest_hash,
                    source_head_fence=source_head_fence,
                    state="discarded",
                    acknowledged_at=now,
                )
            existing_hold = (
                connection.execute(
                    select(public_graph_source_consumers_table).where(
                        and_(
                            public_graph_source_consumers_table.c.consumer_kind == consumer_kind,
                            public_graph_source_consumers_table.c.consumer_id == consumer_id,
                            public_graph_source_consumers_table.c.source_revision
                            == source_revision,
                            public_graph_source_consumers_table.c.purpose == purpose,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_hold is not None:
                raise PlatformError(
                    "idempotency_key_conflict", "The acknowledgement already exists", {}, 409
                )
            if purpose in {"release", "rollback"}:
                staged = connection.execute(
                    select(public_graph_source_consumers_table.c.id).where(
                        and_(
                            public_graph_source_consumers_table.c.consumer_kind == consumer_kind,
                            public_graph_source_consumers_table.c.consumer_id == consumer_id,
                            public_graph_source_consumers_table.c.source_revision
                            == source_revision,
                            public_graph_source_consumers_table.c.state == "held",
                            public_graph_source_consumers_table.c.purpose == "stage",
                        )
                    )
                ).scalar_one_or_none()
                if staged is None:
                    raise PlatformError(
                        "consumer_ack_invalid", "The consumer has not staged this revision", {}, 409
                    )
            now = self._now()
            connection.execute(
                public_graph_source_consumers_table.insert().values(
                    id=_id("consumer_ack"),
                    consumer_kind=consumer_kind,
                    consumer_id=consumer_id,
                    source_revision=source_revision,
                    source_manifest_hash=source_manifest_hash,
                    source_head_fence=source_head_fence,
                    purpose=purpose,
                    operation_id=operation_id,
                    state="held",
                    acknowledged_at_utc=now,
                )
            )
            return PublicGraphSourceConsumptionReceipt(
                operation_id=operation_id,
                consumer_kind=consumer_kind,
                consumer_id=consumer_id,
                source_revision=source_revision,
                source_manifest_hash=source_manifest_hash,
                source_head_fence=source_head_fence,
                state="held",
                acknowledged_at=now,
            )

    @staticmethod
    def _receipt(row: Mapping[str, Any]) -> PublicGraphSourceConsumptionReceipt:
        return PublicGraphSourceConsumptionReceipt(
            operation_id=str(row["operation_id"]),
            consumer_kind=row["consumer_kind"],
            consumer_id=str(row["consumer_id"]),
            source_revision=int(row["source_revision"]),
            source_manifest_hash=str(row["source_manifest_hash"]),
            source_head_fence=(
                int(row["source_head_fence"]) if row["source_head_fence"] is not None else None
            ),
            state=row["state"],
            acknowledged_at=_utc(row["acknowledged_at_utc"]),
        )


__all__ = [
    "PublicGraphSourceConsumptionReceipt",
    "PublicGraphSourceHead",
    "PublicGraphSourceHeadValidationReceipt",
    "PublicGraphSourceService",
    "PublicGraphSourceSnapshot",
]
