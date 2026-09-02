from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import Engine, and_, delete, select, update
from sqlalchemy.engine import Connection

from app.documents.schema import (
    document_versions_table,
    documents_instance_counters_table,
    documents_table,
    index_changes_table,
    index_revisions_table,
    publications_table,
)
from app.platform.database import _current_timestamp, _insert_do_nothing
from app.platform.errors import PlatformError

from .models import (
    ComponentState,
    Generation,
    GenerationComponentReaderLease,
    GenerationReferenceLease,
    GenerationStatus,
    IndexGenerationGcReceipt,
)
from .observability import (
    COMPONENT_GC_FAILURE_ROUTE,
    COMPONENT_PUBLISH_FAILURE_ROUTE,
    COMPONENT_ROLLBACK_FAILURE_ROUTE,
    record_index_observation,
)
from .schema import (
    index_chunks_table,
    index_generation_changes_table,
    index_generation_heads_table,
    index_generation_leases_table,
    index_generations_table,
    index_graph_components_table,
    index_operations_table,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_GC_COMPONENTS = ("public_graph", "sparse", "vector", "hierarchy", "cache")


def _component_progress(*, completed: bool = False) -> dict[str, dict[str, Any]]:
    state = "completed" if completed else "pending"
    return {
        component: {"state": state, "attempts": 0, "last_error": None}
        for component in _GC_COMPONENTS
    }


def _component_defaults(
    generation_id: str, configuration: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    shared = {"reader_lease_binding": generation_id}
    return {
        "dense": {
            "state": "ready",
            "component_generation_id": f"{generation_id}:dense",
            "component_manifest_revision": "dense-v1",
            **shared,
            "provider": "configured",
            "embedding_model": configuration.get("embedding_model"),
            "embedding_revision": configuration.get("embedding_revision"),
            "embedding_dimension": configuration.get("embedding_dimension"),
            "embedding_metric": configuration.get("embedding_metric"),
        },
        "sparse": {
            "state": "ready",
            "component_generation_id": f"{generation_id}:sparse",
            "component_manifest_revision": "sparse-v1",
            **shared,
            "provider": configuration["provider"],
            "engine": configuration["engine"],
            "analyzer": configuration["analyzer"],
            "engine_revision": configuration["engine_revision"],
            "analyzer_revision": configuration["analyzer_revision"],
            "tokenizer_revision": configuration["tokenizer_revision"],
            "pretokenizer_version": configuration["pretokenizer_version"],
            "schema_hash": configuration["schema_hash"],
            "sparse_schema_hash": configuration["sparse_schema_hash"],
            "config_hash": configuration["config_hash"],
            "implementation_config_hash": configuration["implementation_config_hash"],
            "dimension": configuration.get("embedding_dimension"),
            "metric": configuration.get("embedding_metric"),
            "model_revision": configuration.get("embedding_revision"),
        },
        "hierarchy": {
            "state": "ready",
            "component_generation_id": f"{generation_id}:hierarchy",
            "component_manifest_revision": "hierarchy-v1",
            **shared,
        },
        "public_graph": {
            "graph_generation_id": None,
            "state": "disabled",
            "source_revision": None,
            "source_head_fence": None,
            "component_manifest_revision": "public-graph-v1",
            **shared,
        },
    }


class SqlAlchemyIndexingRepository:
    """Durable indexing state with caller-connection transaction boundaries."""

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], datetime] | None = None,
        rollback_days: int = 7,
        generation_configuration: Mapping[str, Any] | None = None,
        operational_metrics: Any | None = None,
    ) -> None:
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))
        self._rollback_days = rollback_days
        self._generation_builder: (
            Callable[[Generation, Mapping[str, Any], Connection], None] | None
        ) = None
        self._generation_cleanup: Callable[[str, str, str | None], None] | None = None
        self._generation_purge: Callable[[str, Sequence[tuple[str, str]]], None] | None = None
        self._generation_component_purges: dict[
            str, Callable[[str, Sequence[tuple[str, str]]], None]
        ] = {}
        self._retrieval_release_gate: Callable[[str, Connection], bool] | None = None
        self._generation_configuration = dict(generation_configuration or {})
        self._operational_metrics = operational_metrics

    def set_generation_builder(
        self, builder: Callable[[Generation, Mapping[str, Any], Connection], None]
    ) -> None:
        self._generation_builder = builder

    def set_generation_cleanup(self, cleanup: Callable[[str, str, str | None], None]) -> None:
        self._generation_cleanup = cleanup

    def set_generation_purge(self, purge: Callable[[str, Sequence[tuple[str, str]]], None]) -> None:
        self._generation_purge = purge

    def set_generation_component_purge(
        self,
        component_kind: str,
        purge: Callable[[str, Sequence[tuple[str, str]]], None],
    ) -> None:
        if component_kind not in _GC_COMPONENTS:
            raise ValueError("generation GC component kind is invalid")
        self._generation_component_purges[component_kind] = purge

    def set_retrieval_release_gate(self, gate: Callable[[str, Connection], bool]) -> None:
        self._retrieval_release_gate = gate

    def record_component_failure(self, operation: str) -> None:
        routes = {
            "publish": COMPONENT_PUBLISH_FAILURE_ROUTE,
            "rollback": COMPONENT_ROLLBACK_FAILURE_ROUTE,
            "gc": COMPONENT_GC_FAILURE_ROUTE,
        }
        try:
            route = routes[operation]
        except KeyError as error:
            raise ValueError("component lifecycle operation is invalid") from error
        record_index_observation(self._operational_metrics, route, success=False)

    def _configuration_manifest(
        self, configuration_source: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        source = (
            self._generation_configuration
            if configuration_source is None
            else dict(configuration_source)
        )
        configuration = {
            "provider": str(source.get("provider", "memory")),
            "engine": str(source.get("engine", "memory")),
            "analyzer": str(source.get("analyzer", "default")),
            "engine_revision": str(source.get("engine_revision", "v1")),
            "analyzer_revision": str(source.get("analyzer_revision", "v1")),
            "tokenizer_revision": str(source.get("tokenizer_revision", "v1")),
            "pretokenizer_version": str(source.get("pretokenizer_version", "v1")),
            "schema_version": str(source.get("schema_version", "index-chunks-v1")),
            "reranker_provider": str(source.get("reranker_provider", "configured")),
            "image_vlm_provider": str(source.get("image_vlm_provider", "configured")),
            "embedding_model": str(source.get("embedding_model", "configured")),
            "embedding_revision": str(
                source.get("embedding_revision") or source.get("embedding_model") or "configured"
            ),
            "embedding_dimension": source.get("embedding_dimension"),
            "embedding_metric": str(source.get("embedding_metric", "cosine")),
        }
        return {
            **configuration,
            "schema_hash": _fingerprint({"schema_version": configuration["schema_version"]}),
            "sparse_schema_hash": _fingerprint({"schema_version": configuration["schema_version"]}),
            "implementation_config_hash": _fingerprint(configuration),
            "config_hash": _fingerprint(configuration),
        }

    @contextmanager
    def _connection(self, connection: Connection | None) -> Iterator[Connection]:
        if connection is not None:
            yield connection
            return
        with self._engine.begin() as owned:
            yield owned

    def _timestamp(self, connection: Connection) -> datetime:
        try:
            return _utc(_current_timestamp(connection))
        except Exception:
            return _utc(self._now())

    def get_operation(
        self, operation_id: str, *, connection: Connection | None = None
    ) -> Mapping[str, Any] | None:
        """Return an indexing operation receipt without opening a second transaction."""
        with self._connection(connection) as conn:
            row = (
                conn.execute(
                    select(index_operations_table).where(
                        index_operations_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    def reserve_operation(
        self,
        operation_id: str,
        operation_kind: str,
        request: Mapping[str, Any],
        *,
        connection: Connection | None = None,
    ) -> Mapping[str, Any] | None:
        """Reserve a replayable operation, returning a completed response when present."""
        if not operation_id.strip() or not operation_kind.strip():
            raise PlatformError("validation_error", "operation identity is required", {}, 422)
        fingerprint = _fingerprint(request)
        with self._connection(connection) as conn:
            existing = self.get_operation(operation_id, connection=conn)
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise PlatformError(
                        "idempotency_key_conflict", "operation request conflicts", {}, 409
                    )
                return dict(existing.get("response_json") or {}) or None
            now = self._timestamp(conn)
            _insert_do_nothing(
                conn,
                index_operations_table,
                {
                    "operation_id": operation_id,
                    "operation_kind": operation_kind,
                    "request_fingerprint": fingerprint,
                    "state": "reserved",
                    "response_json": None,
                    "created_at_utc": now,
                    "completed_at_utc": None,
                },
                ["operation_id"],
            )
            return None

    def complete_operation(
        self,
        operation_id: str,
        response: Mapping[str, Any],
        *,
        state: str = "completed",
        connection: Connection | None = None,
    ) -> None:
        if state not in {"accepted", "blocked", "completed", "failed"}:
            raise PlatformError("validation_error", "operation state is invalid", {}, 422)
        with self._connection(connection) as conn:
            updated = conn.execute(
                update(index_operations_table)
                .where(index_operations_table.c.operation_id == operation_id)
                .values(
                    state=state,
                    response_json=dict(response),
                    completed_at_utc=self._timestamp(conn),
                )
            )
            if updated.rowcount != 1:
                raise PlatformError("operation_not_found", "operation was not reserved", {}, 404)

    @staticmethod
    def _row_to_generation(row: Mapping[Any, Any]) -> Generation:
        manifest = dict(row["manifest_json"] or {})
        component = dict(manifest.get("components", {}).get("public_graph", {}))
        return Generation(
            generation_id=str(row["id"]),
            status=cast(GenerationStatus, str(row["status"])),
            base_revision=int(row["base_revision"]),
            applied_revision=int(row["applied_revision"]),
            manifest=manifest,
            created_at=_utc(row["created_at_utc"]),
            activated_at=_utc(row["activated_at_utc"]) if row["activated_at_utc"] else None,
            retired_at=_utc(row["retired_at_utc"]) if row["retired_at_utc"] else None,
            rollback_until_utc=(
                _utc(row["rollback_until_utc"]) if row["rollback_until_utc"] else None
            ),
            rollback_applied_revision=(
                int(row["rollback_applied_revision"])
                if row["rollback_applied_revision"] is not None
                else None
            ),
            graph_component_state=cast(ComponentState, str(component.get("state", "disabled"))),
        )

    def _ensure_initialized(self, connection: Connection, *, current_revision: int = 0) -> None:
        now = self._timestamp(connection)
        configuration = self._configuration_manifest({})
        _insert_do_nothing(
            connection,
            index_generations_table,
            {
                "id": "generation_initial",
                "status": "active",
                "base_revision": current_revision,
                "applied_revision": current_revision,
                "manifest_json": {
                    "generation_id": "generation_initial",
                    "indexing_configuration": configuration,
                    "provider": configuration["provider"],
                    "engine_revision": configuration["engine_revision"],
                    "analyzer_revision": configuration["analyzer_revision"],
                    "tokenizer_revision": configuration["tokenizer_revision"],
                    "sparse_schema_hash": configuration["sparse_schema_hash"],
                    "implementation_config_hash": configuration["implementation_config_hash"],
                    "dimension": configuration.get("embedding_dimension"),
                    "metric": configuration.get("embedding_metric"),
                    "model_revision": configuration.get("embedding_revision"),
                    "last_applied_index_change_id": None,
                    "created_at": now.isoformat(),
                    "published_at": now.isoformat(),
                    "rollback_candidate_until": None,
                    "gc_state": "active",
                    "base_revision": current_revision,
                    "applied_revision": current_revision,
                    "components": _component_defaults("generation_initial", configuration),
                    "base_snapshot": [],
                    "built_publications": {},
                },
                "created_at_utc": now,
                "activated_at_utc": now,
                "retired_at_utc": None,
                "rollback_until_utc": None,
                "rollback_applied_revision": current_revision,
            },
            ["id"],
        )
        _insert_do_nothing(
            connection,
            index_generation_heads_table,
            {
                "id": "instance",
                "active_generation_id": "generation_initial",
                "rollback_candidate_id": None,
                "current_revision": current_revision,
                "updated_at_utc": now,
            },
            ["id"],
        )

    def _current_revision(self, connection: Connection, *, lock: bool = False) -> int:
        statement = select(documents_instance_counters_table.c.value).where(
            documents_instance_counters_table.c.counter_name == "index_revision"
        )
        if lock:
            statement = statement.with_for_update()
        value = connection.execute(statement).scalar_one_or_none()
        return int(value or 0)

    def active_generation_id(self, *, connection: Connection | None = None) -> str:
        with self._connection(connection) as conn:
            self._ensure_initialized(conn, current_revision=self._current_revision(conn))
            row = conn.execute(
                select(index_generation_heads_table.c.active_generation_id).where(
                    index_generation_heads_table.c.id == "instance"
                )
            ).scalar_one()
            return str(row)

    def current_revision(self, *, connection: Connection | None = None) -> int:
        with self._connection(connection) as conn:
            self._ensure_initialized(conn, current_revision=self._current_revision(conn))
            return int(
                conn.execute(
                    select(index_generation_heads_table.c.current_revision).where(
                        index_generation_heads_table.c.id == "instance"
                    )
                ).scalar_one()
            )

    def get_generation(
        self, generation_id: str, *, connection: Connection | None = None
    ) -> Generation:
        with self._connection(connection) as conn:
            self._ensure_initialized(conn, current_revision=self._current_revision(conn))
            row = (
                conn.execute(
                    select(index_generations_table).where(
                        index_generations_table.c.id == generation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise PlatformError(
                    "generation_not_found", "Index generation was not found", {}, 404
                )
            return self._row_to_generation(row)

    def list_generations(self, *, connection: Connection | None = None) -> tuple[Generation, ...]:
        with self._connection(connection) as conn:
            self._ensure_initialized(conn, current_revision=self._current_revision(conn))
            rows = (
                conn.execute(select(index_generations_table).order_by(index_generations_table.c.id))
                .mappings()
                .all()
            )
            return tuple(self._row_to_generation(row) for row in rows)

    def read_base_snapshot(
        self, *, connection: Connection
    ) -> tuple[int, tuple[Mapping[str, Any], ...]]:
        revision = self._current_revision(connection, lock=True)
        rows = (
            connection.execute(
                select(
                    documents_table,
                    document_versions_table,
                    publications_table.c.id.label("publication_id"),
                    publications_table.c.resource_manifest_json,
                )
                .select_from(
                    documents_table.join(
                        document_versions_table,
                        document_versions_table.c.id == documents_table.c.active_version_id,
                    ).join(
                        publications_table,
                        and_(
                            publications_table.c.document_id == documents_table.c.id,
                            publications_table.c.document_version_id
                            == document_versions_table.c.id,
                            publications_table.c.status == "active",
                        ),
                    )
                )
                .where(documents_table.c.lifecycle_status == "active")
                .order_by(documents_table.c.id)
            )
            .mappings()
            .all()
        )
        snapshot = tuple(
            {
                "document_id": str(row[documents_table.c.id]),
                "document_version_id": str(row[document_versions_table.c.id]),
                "publication_id": str(row["publication_id"]),
                "space_id": str(row[documents_table.c.space_id]),
                "manifest": dict(row["resource_manifest_json"] or {}),
                "object_key": str(row[document_versions_table.c.original_object_key] or ""),
                "media_kind": str(row[document_versions_table.c.media_kind] or ""),
            }
            for row in rows
        )
        return revision, snapshot

    def _publication_source(
        self,
        connection: Connection,
        *,
        document_id: str,
        document_version_id: str,
        publication_id: str,
        space_id: str,
    ) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(
                    document_versions_table.c.original_object_key,
                    document_versions_table.c.media_kind,
                    publications_table.c.resource_manifest_json,
                )
                .select_from(
                    document_versions_table.join(
                        publications_table,
                        and_(
                            publications_table.c.document_id == document_id,
                            publications_table.c.document_version_id == document_version_id,
                            publications_table.c.id == publication_id,
                        ),
                    )
                )
                .where(
                    document_versions_table.c.id == document_version_id,
                    document_versions_table.c.document_id == document_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError(
                "generation_source_missing", "Documents publication source is unavailable", {}, 409
            )
        return {
            "document_id": document_id,
            "document_version_id": document_version_id,
            "publication_id": publication_id,
            "space_id": space_id,
            "manifest": dict(row["resource_manifest_json"] or {}),
            "object_key": str(row["original_object_key"] or ""),
            "media_kind": str(row["media_kind"] or ""),
        }

    def _record_built_publication(
        self,
        generation_id: str,
        source: Mapping[str, Any],
        *,
        connection: Connection,
    ) -> None:
        generation = self.get_generation(generation_id, connection=connection)
        manifest = dict(generation.manifest)
        built = dict(manifest.get("built_publications", {}))
        publication_id = str(source["publication_id"])
        manifest_identity = {
            "document_id": str(source["document_id"]),
            "document_version_id": str(source["document_version_id"]),
            "content_manifest_hash": str(dict(source["manifest"]).get("content_manifest_hash", "")),
        }
        existing = built.get(publication_id)
        if existing is not None and dict(existing) != manifest_identity:
            raise PlatformError(
                "idempotency_key_conflict", "generation publication build conflicts", {}, 409
            )
        built[publication_id] = manifest_identity
        manifest["built_publications"] = built
        connection.execute(
            update(index_generations_table)
            .where(index_generations_table.c.id == generation_id)
            .values(manifest_json=manifest)
        )

    def _remove_built_publications(
        self,
        generation_id: str,
        *,
        document_id: str,
        keep_document_version_id: str | None,
        connection: Connection,
    ) -> None:
        generation = self.get_generation(generation_id, connection=connection)
        manifest = dict(generation.manifest)
        built = {
            publication_id: dict(item)
            for publication_id, item in dict(manifest.get("built_publications", {})).items()
            if not (
                str(item.get("document_id")) == document_id
                and (
                    keep_document_version_id is None
                    or str(item.get("document_version_id")) != keep_document_version_id
                )
            )
        }
        manifest["built_publications"] = built
        connection.execute(
            update(index_generations_table)
            .where(index_generations_table.c.id == generation_id)
            .values(manifest_json=manifest)
        )

    def _build_publication(
        self,
        generation_id: str,
        source: Mapping[str, Any],
        *,
        connection: Connection,
    ) -> None:
        generation = self.get_generation(generation_id, connection=connection)
        if self._generation_builder is None:
            return
        self._generation_builder(generation, source, connection)
        self._record_built_publication(generation_id, source, connection=connection)

    def ensure_configuration_staging(self, *, connection: Connection | None = None) -> Generation:
        """Create or reuse the staging generation for the configured index shape."""
        with self._connection(connection) as conn:
            current_revision = self._current_revision(conn)
            self._ensure_initialized(conn, current_revision=current_revision)
            head = (
                conn.execute(
                    select(index_generation_heads_table)
                    .where(index_generation_heads_table.c.id == "instance")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            configuration = self._configuration_manifest()
            active = self.get_generation(str(head["active_generation_id"]), connection=conn)
            if dict(active.manifest.get("indexing_configuration", {})) == configuration:
                return active
            staged = conn.execute(
                select(index_generations_table)
                .where(index_generations_table.c.status == "staging")
                .order_by(index_generations_table.c.created_at_utc.desc())
            ).mappings()
            for row in staged:
                if dict(row["manifest_json"] or {}).get("indexing_configuration") == configuration:
                    return self._row_to_generation(row)
            return self.create_staging(
                expected_active_generation_id=str(head["active_generation_id"]),
                manifest={"indexing_configuration": configuration},
                connection=conn,
            )

    def create_staging(
        self,
        base_snapshot: Sequence[Mapping[str, Any]] | None = None,
        *,
        base_revision: int | None = None,
        expected_active_generation_id: str | None = None,
        generation_id: str | None = None,
        manifest: Mapping[str, Any] | None = None,
        connection: Connection | None = None,
    ) -> Generation:
        with self._connection(connection) as conn:
            current_revision, snapshot = self.read_base_snapshot(connection=conn)
            self._ensure_initialized(conn, current_revision=current_revision)
            head = (
                conn.execute(
                    select(index_generation_heads_table)
                    .where(index_generation_heads_table.c.id == "instance")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                expected_active_generation_id
                and str(head["active_generation_id"]) != expected_active_generation_id
            ):
                raise PlatformError("generation_conflict", "active generation has changed", {}, 409)
            revision = current_revision if base_revision is None else base_revision
            if revision < 0 or revision > current_revision:
                raise PlatformError("revision_conflict", "base revision is invalid", {}, 409)
            identifier = generation_id or _new_id("generation")
            existing = (
                conn.execute(
                    select(index_generations_table).where(
                        index_generations_table.c.id == identifier
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return self._row_to_generation(existing)
            now = self._timestamp(conn)
            payload = dict(manifest or {})
            payload.setdefault("indexing_configuration", self._configuration_manifest())
            components = dict(payload.get("components", {}))
            configuration = dict(payload["indexing_configuration"])
            for component_kind, defaults in _component_defaults(identifier, configuration).items():
                components[component_kind] = {
                    **defaults,
                    **dict(components.get(component_kind, {})),
                }
            payload["components"] = components
            payload.update(
                {
                    "generation_id": identifier,
                    "provider": configuration["provider"],
                    "engine_revision": configuration["engine_revision"],
                    "analyzer_revision": configuration["analyzer_revision"],
                    "tokenizer_revision": configuration["tokenizer_revision"],
                    "sparse_schema_hash": configuration["sparse_schema_hash"],
                    "implementation_config_hash": configuration["implementation_config_hash"],
                    "dimension": configuration.get("embedding_dimension"),
                    "metric": configuration.get("embedding_metric"),
                    "model_revision": configuration.get("embedding_revision"),
                    "last_applied_index_change_id": None,
                    "created_at": now.isoformat(),
                    "published_at": None,
                    "rollback_candidate_until": None,
                    "gc_state": "staging",
                    "base_revision": revision,
                    "applied_revision": revision,
                }
            )
            payload["base_snapshot"] = [dict(item) for item in (base_snapshot or snapshot)]
            conn.execute(
                index_generations_table.insert().values(
                    id=identifier,
                    status="staging",
                    base_revision=revision,
                    applied_revision=revision,
                    manifest_json=payload,
                    created_at_utc=now,
                    activated_at_utc=None,
                    retired_at_utc=None,
                    rollback_until_utc=None,
                    rollback_applied_revision=None,
                )
            )
            for source in payload["base_snapshot"]:
                self._build_publication(identifier, source, connection=conn)
            return self.get_generation(identifier, connection=conn)

    def apply_change(
        self,
        generation_id: str,
        revision: int,
        change: Mapping[str, Any],
        *,
        connection: Connection | None = None,
    ) -> Generation:
        if revision < 1:
            raise PlatformError("validation_error", "revision is invalid", {}, 422)
        with self._connection(connection) as conn:
            generation = self.get_generation(generation_id, connection=conn)
            if generation.status not in {"staging", "retired"}:
                raise PlatformError(
                    "generation_not_writable", "generation cannot consume changes", {}, 409
                )
            payload = dict(change)
            payload["revision"] = revision
            if revision <= generation.applied_revision:
                row = conn.execute(
                    select(index_generation_changes_table.c.change_json).where(
                        and_(
                            index_generation_changes_table.c.generation_id == generation_id,
                            index_generation_changes_table.c.revision == revision,
                        )
                    )
                ).scalar_one_or_none()
                if row == payload:
                    return generation
                raise PlatformError("revision_conflict", "revision replay conflicts", {}, 409)
            if revision != generation.applied_revision + 1:
                raise PlatformError(
                    "revision_gap", "index changes must be applied continuously", {}, 409
                )
            now = self._timestamp(conn)
            conn.execute(
                index_generation_changes_table.insert().values(
                    generation_id=generation_id,
                    revision=revision,
                    change_type=str(payload.get("change_type", "publish")),
                    change_json=payload,
                    created_at_utc=now,
                )
            )
            manifest = dict(generation.manifest)
            changes = list(manifest.get("index_changes", []))
            changes.append(payload)
            manifest["index_changes"] = changes
            manifest["last_applied_index_change_id"] = payload.get("change_id")
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == generation_id)
                .values(
                    applied_revision=revision,
                    rollback_applied_revision=(
                        revision
                        if generation.status == "retired"
                        else generation.rollback_applied_revision
                    ),
                    manifest_json=manifest,
                )
            )
            return self.get_generation(generation_id, connection=conn)

    def catch_up_from_documents(
        self,
        generation_id: str,
        *,
        connection: Connection | None = None,
    ) -> Generation:
        """Apply the authoritative Documents stream in contiguous revision order."""
        with self._connection(connection) as conn:
            generation = self.get_generation(generation_id, connection=conn)
            rows = (
                conn.execute(
                    select(
                        index_revisions_table.c.revision,
                        index_changes_table.c.id,
                        index_changes_table.c.change_type,
                        index_changes_table.c.document_id,
                        index_changes_table.c.document_version_id,
                        index_changes_table.c.publication_id,
                        index_changes_table.c.space_id,
                    )
                    .select_from(
                        index_revisions_table.join(
                            index_changes_table,
                            index_changes_table.c.revision_id == index_revisions_table.c.id,
                        )
                    )
                    .where(index_revisions_table.c.revision > generation.applied_revision)
                    .order_by(index_revisions_table.c.revision)
                )
                .mappings()
                .all()
            )
            next_revision = generation.applied_revision + 1
            for row in rows:
                revision = int(row["revision"])
                if revision != next_revision:
                    raise PlatformError(
                        "revision_gap", "documents index changes are not continuous", {}, 409
                    )
                change_type = str(row["change_type"])
                source: Mapping[str, Any] | None = None
                if change_type in {"publish", "replace", "reindex"}:
                    if row["document_version_id"] is None or row["publication_id"] is None:
                        raise PlatformError(
                            "generation_source_missing",
                            "Documents index change has no publication source",
                            {},
                            409,
                        )
                    source = self._publication_source(
                        conn,
                        document_id=str(row["document_id"]),
                        document_version_id=str(row["document_version_id"]),
                        publication_id=str(row["publication_id"]),
                        space_id=str(row["space_id"]),
                    )
                generation = self.apply_change(
                    generation_id,
                    revision,
                    {
                        "change_id": str(row["id"]),
                        "change_type": str(row["change_type"]),
                        "document_id": str(row["document_id"]),
                        "document_version_id": (
                            str(row["document_version_id"])
                            if row["document_version_id"] is not None
                            else None
                        ),
                        "publication_id": (
                            str(row["publication_id"])
                            if row["publication_id"] is not None
                            else None
                        ),
                        "space_id": str(row["space_id"]),
                    },
                    connection=conn,
                )
                if change_type == "delete":
                    conn.execute(
                        delete(index_chunks_table).where(
                            and_(
                                index_chunks_table.c.generation_id == generation_id,
                                index_chunks_table.c.document_id == str(row["document_id"]),
                            )
                        )
                    )
                    self._remove_built_publications(
                        generation_id,
                        document_id=str(row["document_id"]),
                        keep_document_version_id=None,
                        connection=conn,
                    )
                    if self._generation_cleanup is not None:
                        self._generation_cleanup(generation_id, str(row["document_id"]), None)
                elif change_type in {"publish", "replace", "reindex"}:
                    conn.execute(
                        delete(index_chunks_table).where(
                            and_(
                                index_chunks_table.c.generation_id == generation_id,
                                index_chunks_table.c.document_id == str(row["document_id"]),
                            )
                        )
                    )
                    self._remove_built_publications(
                        generation_id,
                        document_id=str(row["document_id"]),
                        keep_document_version_id=(
                            str(row["document_version_id"])
                            if row["document_version_id"] is not None
                            else None
                        ),
                        connection=conn,
                    )
                    if self._generation_cleanup is not None:
                        self._generation_cleanup(
                            generation_id,
                            str(row["document_id"]),
                            (
                                str(row["document_version_id"])
                                if row["document_version_id"] is not None
                                else None
                            ),
                        )
                if self._generation_builder is not None and source is not None:
                    conn.execute(
                        delete(index_chunks_table).where(
                            and_(
                                index_chunks_table.c.generation_id == generation_id,
                                index_chunks_table.c.publication_id == str(row["publication_id"]),
                            )
                        )
                    )
                    self._build_publication(generation_id, source, connection=conn)
                next_revision += 1
            if self._current_revision(conn) >= next_revision:
                raise PlatformError("revision_gap", "documents index changes are missing", {}, 409)
            return generation

    def record_published_chunks(
        self,
        request: Any,
        chunks: Sequence[Any],
        *,
        connection: Connection,
    ) -> None:
        for chunk in chunks:
            existing = (
                connection.execute(
                    select(index_chunks_table).where(
                        and_(
                            index_chunks_table.c.id == chunk.chunk_id,
                            index_chunks_table.c.generation_id == request.expected_generation_id,
                            index_chunks_table.c.publication_id == request.publication_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["manifest_hash"] != chunk.manifest_hash:
                    raise PlatformError(
                        "idempotency_key_conflict", "published chunk conflicts", {}, 409
                    )
                continue
            connection.execute(
                index_chunks_table.insert().values(
                    id=chunk.chunk_id,
                    generation_id=chunk.generation_id,
                    publication_id=chunk.publication_id,
                    document_id=chunk.document_id,
                    document_version_id=chunk.document_version_id,
                    space_id=chunk.space_id,
                    text=chunk.text,
                    embedding_text=chunk.embedding_text,
                    sparse_text=chunk.sparse_text,
                    locator_json=dict(chunk.locator),
                    snippet=chunk.snippet,
                    media_kind=chunk.media_kind,
                    manifest_hash=chunk.manifest_hash,
                    metadata_json=dict(chunk.metadata),
                    indexable=chunk.indexable,
                )
            )

    def set_component_state(
        self,
        generation_id: str,
        component_kind: str,
        state: str,
        *,
        manifest: Mapping[str, Any] | None = None,
        connection: Connection | None = None,
    ) -> Generation:
        if state not in {"staged", "ready", "disabled", "stale", "failed"}:
            raise PlatformError("validation_error", "component state is invalid", {}, 422)
        with self._connection(connection) as conn:
            generation = self.get_generation(generation_id, connection=conn)
            components = dict(generation.manifest.get("components", {}))
            item = dict(components.get(component_kind, {}))
            item["state"] = state
            if manifest:
                item.update(dict(manifest))
            components[component_kind] = item
            updated_manifest = {**dict(generation.manifest), "components": components}
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == generation_id)
                .values(manifest_json=updated_manifest)
            )
            if component_kind == "public_graph":
                component_id = f"{generation_id}:public_graph"
                _insert_do_nothing(
                    conn,
                    index_graph_components_table,
                    {
                        "id": component_id,
                        "generation_id": generation_id,
                        "component_state": state,
                        "target_generation_fence": str(item.get("target_generation_fence", "")),
                        "source_revision": int(item.get("source_revision", 0)),
                        "source_manifest_hash": str(item.get("source_manifest_hash", "")),
                        "source_head_fence": int(item.get("source_head_fence", 0)),
                        "manifest_json": item,
                        "created_at_utc": self._timestamp(conn),
                    },
                    ["id"],
                )
                conn.execute(
                    update(index_graph_components_table)
                    .where(index_graph_components_table.c.id == component_id)
                    .values(
                        component_state=state,
                        target_generation_fence=str(item.get("target_generation_fence", "")),
                        source_revision=int(item.get("source_revision", 0)),
                        source_manifest_hash=str(item.get("source_manifest_hash", "")),
                        source_head_fence=int(item.get("source_head_fence", 0)),
                        manifest_json=item,
                    )
                )
            return self.get_generation(generation_id, connection=conn)

    def remove_graph_component_for_gc(
        self,
        generation_id: str,
        *,
        connection: Connection,
    ) -> Generation:
        conn = connection
        generation = self.get_generation(generation_id, connection=conn)
        components = dict(generation.manifest.get("components", {}))
        graph = dict(components.get("public_graph", {}))
        if graph and (graph.get("state") != "disabled" or graph.get("graph_resource_ids")):
            raise PlatformError(
                "graph_component_cleanup_required",
                "graph component resources must be discarded before GC",
                {},
                409,
            )
        components.pop("public_graph", None)
        updated = conn.execute(
            update(index_generations_table)
            .where(index_generations_table.c.id == generation_id)
            .values(manifest_json={**dict(generation.manifest), "components": components})
        )
        if updated.rowcount != 1:
            raise PlatformError("generation_not_found", "Index generation was not found", {}, 404)
        conn.execute(
            delete(index_graph_components_table).where(
                index_graph_components_table.c.generation_id == generation_id
            )
        )
        return self.get_generation(generation_id, connection=conn)

    def _validate_generation_content(
        self, generation: Generation, *, connection: Connection
    ) -> None:
        generation_id = generation.generation_id
        _, snapshot = self.read_base_snapshot(connection=connection)
        active_documents = connection.execute(
            select(
                documents_table.c.id,
                documents_table.c.active_version_id,
                document_versions_table.c.status,
            )
            .select_from(
                documents_table.outerjoin(
                    document_versions_table,
                    document_versions_table.c.id == documents_table.c.active_version_id,
                )
            )
            .where(documents_table.c.lifecycle_status == "active")
        ).all()
        snapshot_document_ids = [str(item["document_id"]) for item in snapshot]
        if (
            len(snapshot_document_ids) != len(set(snapshot_document_ids))
            or set(snapshot_document_ids) != {str(item[0]) for item in active_documents}
            or any(item[1] is None or item[2] != "active" for item in active_documents)
        ):
            raise PlatformError(
                "release_gate_failed", "active Documents facts are incomplete or ambiguous", {}, 409
            )
        expected_publications = {
            (
                item["document_id"],
                item["document_version_id"],
                item["publication_id"],
                str(item["manifest"].get("content_manifest_hash", "")),
            )
            for item in snapshot
        }
        indexed = connection.execute(
            select(
                index_chunks_table.c.id,
                index_chunks_table.c.document_id,
                index_chunks_table.c.document_version_id,
                index_chunks_table.c.publication_id,
                index_chunks_table.c.manifest_hash,
            ).where(index_chunks_table.c.generation_id == generation_id)
        ).all()
        indexed_by_publication: dict[str, list[Any]] = {}
        for item in indexed:
            indexed_by_publication.setdefault(str(item[3]), []).append(item)
        built_publications = {
            (
                str(item.get("document_id", "")),
                str(item.get("document_version_id", "")),
                str(publication_id),
                str(item.get("content_manifest_hash", "")),
            )
            for publication_id, item in dict(
                generation.manifest.get("built_publications", {})
            ).items()
        }
        if built_publications != expected_publications:
            raise PlatformError(
                "release_gate_failed",
                "generation chunks do not match active Documents publications",
                {},
                409,
            )
        expected_publication_ids = {str(item["publication_id"]) for item in snapshot}
        if set(indexed_by_publication) - expected_publication_ids:
            raise PlatformError("release_gate_failed", "generation contains orphan chunks", {}, 409)
        for source in snapshot:
            publication_id = str(source["publication_id"])
            manifest = dict(source["manifest"])
            chunks = indexed_by_publication.get(publication_id, [])
            expected_count = dict(manifest.get("processing_summary") or {}).get("chunk_count")
            if expected_count is None:
                expected_count = len(manifest.get("stage_resources") or ()) or 1
            chunk_ids = [str(item[0]) for item in chunks]
            expected_identity = (
                str(source["document_id"]),
                str(source["document_version_id"]),
                publication_id,
                str(manifest.get("content_manifest_hash", "")),
            )
            if (
                isinstance(expected_count, bool)
                or not isinstance(expected_count, int)
                or expected_count < 0
                or len(chunks) != expected_count
                or len(chunk_ids) != len(set(chunk_ids))
                or any(
                    (str(item[1]), str(item[2]), str(item[3]), str(item[4])) != expected_identity
                    for item in chunks
                )
            ):
                raise PlatformError(
                    "release_gate_failed",
                    "generation chunks do not match their publication manifests",
                    {},
                    409,
                )
        applied_change_revisions = tuple(
            int(item[0])
            for item in connection.execute(
                select(index_generation_changes_table.c.revision)
                .where(index_generation_changes_table.c.generation_id == generation_id)
                .order_by(index_generation_changes_table.c.revision)
            ).all()
        )
        if applied_change_revisions != tuple(
            range(generation.base_revision + 1, generation.applied_revision + 1)
        ):
            raise PlatformError(
                "release_gate_failed", "generation change log is incomplete", {}, 409
            )
        components = dict(generation.manifest.get("components", {}))
        if not all(
            dict(components.get(kind, {})).get("state") == "ready" for kind in ("dense", "sparse")
        ) or any(item.get("state") not in {"ready", "disabled"} for item in components.values()):
            raise PlatformError(
                "release_gate_failed", "a generation component is not ready", {}, 409
            )
        configuration = dict(generation.manifest.get("indexing_configuration", {}))
        dense = dict(components.get("dense", {}))
        if any(
            dense.get(name) != configuration.get(name)
            for name in (
                "embedding_model",
                "embedding_revision",
                "embedding_dimension",
                "embedding_metric",
            )
        ):
            raise PlatformError(
                "release_gate_failed", "dense component does not match generation manifest", {}, 409
            )
        sparse = dict(components.get("sparse", {}))
        if any(
            sparse.get(name) != configuration.get(name)
            for name in (
                "provider",
                "engine",
                "analyzer",
                "engine_revision",
                "analyzer_revision",
                "tokenizer_revision",
                "pretokenizer_version",
                "schema_hash",
                "config_hash",
                "sparse_schema_hash",
                "implementation_config_hash",
            )
        ):
            raise PlatformError(
                "release_gate_failed",
                "sparse component does not match generation manifest",
                {},
                409,
            )
        graph = dict(components.get("public_graph", {}))
        graph_rows = (
            connection.execute(
                select(index_graph_components_table).where(
                    index_graph_components_table.c.generation_id == generation_id
                )
            )
            .mappings()
            .all()
        )
        if (
            len(graph_rows) > 1
            or (graph_rows and dict(graph_rows[0]["manifest_json"] or {}) != graph)
            or (graph.get("state") != "disabled" and not graph_rows)
        ):
            raise PlatformError(
                "release_gate_failed",
                "public graph component does not match generation manifest",
                {},
                409,
            )

    def release(
        self,
        generation_id: str,
        *,
        expected_active_generation_id: str | None = None,
        current_revision: int | None = None,
        release_gate: Callable[[Generation], bool] | None = None,
        connection: Connection | None = None,
    ) -> Generation:
        try:
            return self._release(
                generation_id,
                expected_active_generation_id=expected_active_generation_id,
                current_revision=current_revision,
                release_gate=release_gate,
                connection=connection,
            )
        except Exception:
            if connection is None:
                self.record_component_failure("publish")
            raise

    def _release(
        self,
        generation_id: str,
        *,
        expected_active_generation_id: str | None = None,
        current_revision: int | None = None,
        release_gate: Callable[[Generation], bool] | None = None,
        connection: Connection | None = None,
    ) -> Generation:
        with self._connection(connection) as conn:
            authoritative_revision = self._current_revision(conn, lock=True)
            revision = authoritative_revision if current_revision is None else current_revision
            if revision != authoritative_revision:
                raise PlatformError(
                    "release_gate_failed", "release revision is not current", {}, 409
                )
            self._ensure_initialized(conn, current_revision=revision)
            head = (
                conn.execute(
                    select(index_generation_heads_table)
                    .where(index_generation_heads_table.c.id == "instance")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            active_id = str(head["active_generation_id"])
            if expected_active_generation_id and expected_active_generation_id != active_id:
                raise PlatformError("generation_conflict", "active generation has changed", {}, 409)
            generation = self.get_generation(generation_id, connection=conn)
            if generation.status == "active":
                return generation
            if generation.status != "staging":
                raise PlatformError(
                    "index_release_blocked", "generation is not releasable", {}, 409
                )
            try:
                generation = self.catch_up_from_documents(generation_id, connection=conn)
                if (
                    generation.applied_revision != revision
                    or self._current_revision(conn, lock=True) != revision
                ):
                    raise PlatformError(
                        "release_gate_failed", "generation has unprocessed revisions", {}, 409
                    )
                if release_gate is not None and not release_gate(generation):
                    raise PlatformError(
                        "release_gate_failed", "generation release gate failed", {}, 409
                    )
                if self._retrieval_release_gate is not None and not self._retrieval_release_gate(
                    generation_id, conn
                ):
                    raise PlatformError(
                        "release_gate_failed",
                        "generation has no released retrieval profile",
                        {},
                        409,
                    )
                self._validate_generation_content(generation, connection=conn)
            except PlatformError:
                failed_manifest = dict(generation.manifest)
                failed_manifest["gc_state"] = "failed"
                conn.execute(
                    update(index_generations_table)
                    .where(index_generations_table.c.id == generation_id)
                    .values(status="failed", manifest_json=failed_manifest)
                )
                raise
            now = self._timestamp(conn)
            rollback_until = now + timedelta(days=self._rollback_days)
            active_manifest = dict(self.get_generation(active_id, connection=conn).manifest)
            active_manifest.update(
                {"rollback_candidate_until": rollback_until.isoformat(), "gc_state": "retired"}
            )
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == active_id)
                .values(
                    status="retired",
                    retired_at_utc=now,
                    rollback_until_utc=rollback_until,
                    rollback_applied_revision=revision,
                    manifest_json=active_manifest,
                )
            )
            released_manifest = dict(generation.manifest)
            released_manifest.update(
                {
                    "published_at": now.isoformat(),
                    "rollback_candidate_until": None,
                    "gc_state": "active",
                }
            )
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == generation_id)
                .values(
                    status="active",
                    activated_at_utc=now,
                    retired_at_utc=None,
                    manifest_json=released_manifest,
                )
            )
            conn.execute(
                update(index_generation_heads_table)
                .where(index_generation_heads_table.c.id == "instance")
                .values(
                    active_generation_id=generation_id,
                    rollback_candidate_id=active_id,
                    current_revision=revision,
                    updated_at_utc=now,
                )
            )
            return self.get_generation(generation_id, connection=conn)

    def rollback(
        self,
        candidate_generation_id: str,
        *,
        current_revision: int | None = None,
        source_receipt: Mapping[str, Any] | None = None,
        release_gate: Callable[[Generation], bool] | None = None,
        connection: Connection | None = None,
    ) -> Generation:
        try:
            return self._rollback(
                candidate_generation_id,
                current_revision=current_revision,
                source_receipt=source_receipt,
                release_gate=release_gate,
                graph_source_validation=None,
                connection=connection,
            )
        except Exception:
            if connection is None:
                self.record_component_failure("rollback")
            raise

    def _rollback_after_graph_source_validation(
        self,
        candidate_generation_id: str,
        *,
        current_revision: int | None = None,
        source_receipt: Mapping[str, Any] | None = None,
        graph_source_validation: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
        release_gate: Callable[[Generation], bool] | None = None,
        connection: Connection,
    ) -> Generation:
        """Rollback command reserved for graph coordination after source validation."""
        try:
            return self._rollback(
                candidate_generation_id,
                current_revision=current_revision,
                source_receipt=source_receipt,
                release_gate=release_gate,
                graph_source_validation=graph_source_validation,
                connection=connection,
            )
        except Exception:
            raise

    def _rollback(
        self,
        candidate_generation_id: str,
        *,
        current_revision: int | None,
        source_receipt: Mapping[str, Any] | None,
        graph_source_validation: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None,
        release_gate: Callable[[Generation], bool] | None,
        connection: Connection | None,
    ) -> Generation:
        with self._connection(connection) as conn:
            revision = (
                self._current_revision(conn) if current_revision is None else current_revision
            )
            head = (
                conn.execute(
                    select(index_generation_heads_table)
                    .where(index_generation_heads_table.c.id == "instance")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            candidate = self.get_generation(candidate_generation_id, connection=conn)
            if (
                candidate.status != "retired"
                or head["rollback_candidate_id"] != candidate_generation_id
            ):
                raise PlatformError(
                    "rollback_not_eligible", "generation is not a rollback candidate", {}, 409
                )
            if candidate.rollback_until_utc and _utc(self._now()) > _utc(
                candidate.rollback_until_utc
            ):
                raise PlatformError("rollback_not_eligible", "rollback window has expired", {}, 409)
            candidate = self.catch_up_from_documents(candidate_generation_id, connection=conn)
            if candidate.rollback_applied_revision != revision:
                raise PlatformError(
                    "rollback_not_eligible", "rollback generation is not caught up", {}, 409
                )
            components = dict(candidate.manifest.get("components", {}))
            graph = dict(components.get("public_graph", {}))
            receipt = dict(source_receipt or {})
            graph_source_current = graph.get("state") != "ready"
            if graph.get("state") == "ready" and graph_source_validation is not None:
                authoritative_receipt = graph_source_validation(
                    {
                        "source_revision": graph.get("source_revision"),
                        "source_manifest_hash": graph.get("source_manifest_hash"),
                        "source_head_fence": graph.get("source_head_fence"),
                    }
                )
                if authoritative_receipt is not None:
                    graph_source_current = True
                    receipt = {
                        **dict(authoritative_receipt),
                        "candidate_generation_id": candidate_generation_id,
                        "applied_revision": revision,
                    }
            receipt_matches = (
                receipt.get("state") == "held"
                and receipt.get("candidate_generation_id") == candidate_generation_id
                and int(receipt.get("applied_revision", -1)) == revision
            )
            if graph.get("state") == "ready":
                receipt_matches = receipt_matches and all(
                    receipt.get(name) == graph.get(name)
                    for name in (
                        "source_revision",
                        "source_manifest_hash",
                        "source_head_fence",
                    )
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
            if self._retrieval_release_gate is not None and not self._retrieval_release_gate(
                candidate_generation_id, conn
            ):
                raise PlatformError(
                    "release_gate_failed",
                    "rollback generation has no released retrieval profile",
                    {},
                    409,
                )
            self._validate_generation_content(candidate, connection=conn)
            if graph.get("state") == "ready":
                if graph_source_validation is None or not graph_source_current:
                    candidate = self.set_component_state(
                        candidate_generation_id,
                        "public_graph",
                        "disabled",
                        manifest={
                            "graph_resource_manifest_hash": "",
                            "graph_resource_ids": [],
                        },
                        connection=conn,
                    )
            active_id = str(head["active_generation_id"])
            now = self._timestamp(conn)
            rollback_until = now + timedelta(days=self._rollback_days)
            active_manifest = dict(self.get_generation(active_id, connection=conn).manifest)
            active_manifest.update(
                {"rollback_candidate_until": rollback_until.isoformat(), "gc_state": "retired"}
            )
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == active_id)
                .values(
                    status="retired",
                    retired_at_utc=now,
                    rollback_until_utc=rollback_until,
                    manifest_json=active_manifest,
                )
            )
            restored_manifest = dict(candidate.manifest)
            restored_manifest.update({"rollback_candidate_until": None, "gc_state": "active"})
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == candidate_generation_id)
                .values(
                    status="active",
                    activated_at_utc=now,
                    retired_at_utc=None,
                    manifest_json=restored_manifest,
                )
            )
            conn.execute(
                update(index_generation_heads_table)
                .where(index_generation_heads_table.c.id == "instance")
                .values(
                    active_generation_id=candidate_generation_id,
                    rollback_candidate_id=active_id,
                    current_revision=revision,
                    updated_at_utc=now,
                )
            )
            return self.get_generation(candidate_generation_id, connection=conn)

    def acquire_reference_lease(
        self,
        *,
        ttl: timedelta = timedelta(minutes=1),
        owner_id: str = "request",
        connection: Connection | None = None,
    ) -> GenerationReferenceLease:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        with self._connection(connection) as conn:
            generation_id = self.active_generation_id(connection=conn)
            now = self._timestamp(conn)
            lease_id = _new_id("generation_lease")
            conn.execute(
                index_generation_leases_table.insert().values(
                    id=lease_id,
                    generation_id=generation_id,
                    component_kind=None,
                    manifest_hash=None,
                    source_head_fence=None,
                    expires_at_utc=now + ttl,
                    released_at_utc=None,
                    lease_kind="reference",
                    owner_id=owner_id,
                    fence_token=1,
                )
            )
            return GenerationReferenceLease(lease_id, generation_id, now + ttl)

    def release_reference_lease(
        self, lease_id: str, *, connection: Connection | None = None
    ) -> None:
        with self._connection(connection) as conn:
            now = self._timestamp(conn)
            conn.execute(
                update(index_generation_leases_table)
                .where(index_generation_leases_table.c.id == lease_id)
                .values(released_at_utc=now)
            )

    def set_current_revision(self, revision: int, *, connection: Connection | None = None) -> None:
        if revision < 0:
            raise PlatformError("validation_error", "index revision is invalid", {}, 422)
        with self._connection(connection) as conn:
            self._ensure_initialized(conn, current_revision=revision)
            current = self.current_revision(connection=conn)
            if revision < current:
                raise PlatformError(
                    "revision_conflict", "index revision cannot move backwards", {}, 409
                )
            conn.execute(
                update(index_generation_heads_table)
                .where(index_generation_heads_table.c.id == "instance")
                .values(current_revision=revision, updated_at_utc=self._timestamp(conn))
            )

    def acquire_graph_reader_lease(
        self,
        *,
        generation_id: str,
        source_head_fence: int,
        manifest_hash: str,
        validate_source_head: Callable[[], bool],
        ttl: timedelta = timedelta(minutes=1),
        connection: Connection | None = None,
    ) -> GenerationComponentReaderLease:
        if source_head_fence < 1 or not manifest_hash or ttl <= timedelta(0):
            raise PlatformError("validation_error", "graph reader lease input is invalid", {}, 422)
        with self._connection(connection) as conn:
            generation = self.get_generation(generation_id, connection=conn)
            component = generation.manifest.get("components", {}).get("public_graph", {})
            if component.get("state") != "ready":
                raise PlatformError(
                    "graph_unavailable", "public graph component is not ready", {}, 409
                )
            if not validate_source_head():
                self.set_component_state(
                    generation.generation_id, "public_graph", "stale", connection=conn
                )
                raise PlatformError(
                    "graph_source_changed", "The public graph source has changed", {}, 409
                )
            now = self._timestamp(conn)
            lease_id = _new_id("graph_lease")
            expires = now + ttl
            conn.execute(
                index_generation_leases_table.insert().values(
                    id=lease_id,
                    generation_id=generation.generation_id,
                    component_kind="public_graph",
                    manifest_hash=manifest_hash,
                    source_head_fence=source_head_fence,
                    expires_at_utc=expires,
                    released_at_utc=None,
                    lease_kind="component",
                    owner_id="indexing",
                    fence_token=1,
                )
            )
            return GenerationComponentReaderLease(
                lease_id,
                generation.generation_id,
                "public_graph",
                manifest_hash,
                source_head_fence,
                expires,
            )

    def get_graph_reader_lease(
        self, lease_id: str, *, connection: Connection | None = None
    ) -> GenerationComponentReaderLease:
        with self._connection(connection) as conn:
            row = (
                conn.execute(
                    select(index_generation_leases_table).where(
                        index_generation_leases_table.c.id == lease_id,
                        index_generation_leases_table.c.lease_kind == "component",
                        index_generation_leases_table.c.released_at_utc.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise PlatformError("lease_not_found", "graph reader lease was not found", {}, 404)
            return GenerationComponentReaderLease(
                lease_id,
                str(row["generation_id"]),
                "public_graph",
                str(row["manifest_hash"]),
                int(row["source_head_fence"]),
                _utc(row["expires_at_utc"]),
            )

    def renew_graph_reader_lease(
        self,
        lease_id: str,
        *,
        validate_source_head: Callable[[], bool],
        ttl: timedelta = timedelta(minutes=1),
        connection: Connection | None = None,
    ) -> GenerationComponentReaderLease:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        with self._connection(connection) as conn:
            now = self._timestamp(conn)
            row = (
                conn.execute(
                    select(index_generation_leases_table)
                    .where(index_generation_leases_table.c.id == lease_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or row["lease_kind"] != "component"
                or row["released_at_utc"] is not None
            ):
                raise PlatformError("lease_not_found", "graph reader lease was not found", {}, 404)
            if _utc(row["expires_at_utc"]) <= now:
                raise PlatformError("lease_expired", "graph reader lease has expired", {}, 409)
            if not validate_source_head():
                conn.execute(
                    update(index_generation_leases_table)
                    .where(index_generation_leases_table.c.id == lease_id)
                    .values(released_at_utc=now)
                )
                self.set_component_state(
                    str(row["generation_id"]), "public_graph", "stale", connection=conn
                )
                raise PlatformError(
                    "graph_source_changed", "The public graph source has changed", {}, 409
                )
            expires = now + ttl
            conn.execute(
                update(index_generation_leases_table)
                .where(index_generation_leases_table.c.id == lease_id)
                .values(expires_at_utc=expires)
            )
            return GenerationComponentReaderLease(
                lease_id,
                str(row["generation_id"]),
                "public_graph",
                str(row["manifest_hash"]),
                int(row["source_head_fence"]),
                expires,
            )

    def request_index_generation_gc(
        self,
        candidate_generation_id: str,
        *,
        reconciliation_run_id: str,
        operation_id: str,
        connection: Connection | None = None,
    ) -> IndexGenerationGcReceipt:
        if not reconciliation_run_id.strip() or not operation_id.strip():
            raise PlatformError("validation_error", "GC operation identity is required", {}, 422)
        request = {
            "candidate_generation_id": candidate_generation_id,
            "reconciliation_run_id": reconciliation_run_id,
        }
        with self._connection(connection) as conn:
            now = self._timestamp(conn)
            fingerprint = _fingerprint(request)
            existing = (
                conn.execute(
                    select(index_operations_table).where(
                        index_operations_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise PlatformError(
                        "idempotency_key_conflict", "GC operation conflicts", {}, 409
                    )
                payload = existing["response_json"] or {}
                payload_state = cast(
                    Literal["accepted", "blocked", "already_purged"],
                    payload.get("state", "blocked"),
                )
                payload_reasons = payload.get("blocking_reasons", ())
                if not isinstance(payload_reasons, (list, tuple)):
                    payload_reasons = ()
                return IndexGenerationGcReceipt(
                    operation_id,
                    candidate_generation_id,
                    payload_state,
                    tuple(str(item) for item in payload_reasons),
                    bool(payload.get("retryable", False)),
                )
            generation = self.get_generation(candidate_generation_id, connection=conn)
            reasons = self._gc_blocking_reasons(candidate_generation_id, now=now, connection=conn)
            state = (
                "already_purged"
                if generation.status == "purged"
                else ("blocked" if reasons else "accepted")
            )
            receipt = {
                "candidate_generation_id": candidate_generation_id,
                "reconciliation_run_id": reconciliation_run_id,
                "state": state,
                "blocking_reasons": list(dict.fromkeys(reasons)),
                "retryable": bool(reasons),
                "component_progress": _component_progress(completed=state == "already_purged"),
            }
            _insert_do_nothing(
                conn,
                index_operations_table,
                {
                    "operation_id": operation_id,
                    "operation_kind": "generation_gc",
                    "request_fingerprint": fingerprint,
                    "state": state,
                    "response_json": receipt,
                    "created_at_utc": now,
                    "completed_at_utc": now,
                },
                ["operation_id"],
            )
            if state == "accepted":
                manifest = dict(generation.manifest)
                manifest["gc_state"] = "purging"
                conn.execute(
                    update(index_generations_table)
                    .where(index_generations_table.c.id == candidate_generation_id)
                    .values(manifest_json=manifest)
                )
            receipt_reasons = receipt["blocking_reasons"]
            if not isinstance(receipt_reasons, (list, tuple)):
                receipt_reasons = ()
            return IndexGenerationGcReceipt(
                operation_id,
                candidate_generation_id,
                cast(Literal["accepted", "blocked", "already_purged"], state),
                tuple(str(item) for item in receipt_reasons),
                bool(receipt["retryable"]),
            )

    def _gc_blocking_reasons(
        self, candidate_generation_id: str, *, now: datetime, connection: Connection
    ) -> list[str]:
        generation = self.get_generation(candidate_generation_id, connection=connection)
        head = (
            connection.execute(
                select(index_generation_heads_table).where(
                    index_generation_heads_table.c.id == "instance"
                )
            )
            .mappings()
            .one()
        )
        active_lease = connection.execute(
            select(index_generation_leases_table.c.id).where(
                and_(
                    index_generation_leases_table.c.generation_id == candidate_generation_id,
                    index_generation_leases_table.c.released_at_utc.is_(None),
                    index_generation_leases_table.c.expires_at_utc > now,
                )
            )
        ).first()
        reasons: list[str] = []
        if generation.status == "active":
            reasons.append("active_generation")
        rollback_until = generation.rollback_until_utc
        if head["rollback_candidate_id"] == candidate_generation_id and (
            rollback_until is None or _utc(now) <= _utc(rollback_until)
        ):
            # 回滚窗口内保持阻塞（A62）；过窗后 GC 不再被回滚候选身份无条件
            # 阻塞——与 rollback 消费路径的窗口判定一致。窗口内租约/消费语义
            # 不变；未设窗口（None）视为窗口未关闭，保持阻塞。
            reasons.append("rollback_candidate")
        if active_lease is not None:
            reasons.append("active_lease")
        if generation.status != "retired":
            reasons.append("generation_not_retired")
        return reasons

    def gc_eligibility(
        self,
        candidate_generation_id: str,
        *,
        operation_id: str,
        connection: Connection,
    ) -> IndexGenerationGcReceipt:
        operation = (
            connection.execute(
                select(index_operations_table).where(
                    index_operations_table.c.operation_id == operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if operation is None:
            raise PlatformError("gc_operation_not_found", "GC operation was not found", {}, 404)
        payload = dict(operation["response_json"] or {})
        if (
            operation["operation_kind"] != "generation_gc"
            or payload.get("candidate_generation_id") != candidate_generation_id
            or not str(payload.get("reconciliation_run_id", "")).strip()
        ):
            raise PlatformError("idempotency_key_conflict", "GC operation conflicts", {}, 409)
        if payload.get("state") != "accepted":
            return IndexGenerationGcReceipt(
                operation_id,
                candidate_generation_id,
                payload.get("state", "blocked"),
                tuple(payload.get("blocking_reasons", ())),
                bool(payload.get("retryable", False)),
            )
        reasons = self._gc_blocking_reasons(
            candidate_generation_id, now=self._timestamp(connection), connection=connection
        )
        if not reasons:
            return IndexGenerationGcReceipt(operation_id, candidate_generation_id, "accepted")
        return IndexGenerationGcReceipt(
            operation_id,
            candidate_generation_id,
            "blocked",
            tuple(dict.fromkeys(reasons)),
            True,
        )

    def stop_graph_readers(self, candidate_generation_id: str, *, connection: Connection) -> None:
        connection.execute(
            update(index_generation_leases_table)
            .where(
                and_(
                    index_generation_leases_table.c.generation_id == candidate_generation_id,
                    index_generation_leases_table.c.lease_kind == "component",
                    index_generation_leases_table.c.released_at_utc.is_(None),
                )
            )
            .values(released_at_utc=self._timestamp(connection))
        )

    def record_gc_cleanup_failure(
        self,
        candidate_generation_id: str,
        *,
        operation_id: str,
        reason: str,
        component_kind: str = "public_graph",
    ) -> IndexGenerationGcReceipt:
        self.record_gc_component_progress(
            candidate_generation_id,
            operation_id=operation_id,
            component_kind=component_kind,
            state="failed",
            error=reason,
        )
        self.record_component_failure("gc")
        return IndexGenerationGcReceipt(
            operation_id, candidate_generation_id, "blocked", (reason,), True
        )

    def record_gc_component_progress(
        self,
        candidate_generation_id: str,
        *,
        operation_id: str,
        component_kind: str,
        state: str,
        error: str | None = None,
        connection: Connection | None = None,
    ) -> Mapping[str, Any]:
        if component_kind not in _GC_COMPONENTS or state not in {
            "pending",
            "running",
            "completed",
            "failed",
        }:
            raise PlatformError("validation_error", "GC component progress is invalid", {}, 422)
        with self._connection(connection) as conn:
            operation = (
                conn.execute(
                    select(index_operations_table).where(
                        index_operations_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if operation is None:
                raise PlatformError("gc_operation_not_found", "GC operation was not found", {}, 404)
            payload = dict(operation["response_json"] or {})
            if (
                operation["operation_kind"] != "generation_gc"
                or payload.get("candidate_generation_id") != candidate_generation_id
            ):
                raise PlatformError("idempotency_key_conflict", "GC operation conflicts", {}, 409)
            progress = {
                name: dict(value)
                for name, value in dict(
                    payload.get("component_progress") or _component_progress()
                ).items()
            }
            current = dict(progress.get(component_kind, {}))
            attempts = int(current.get("attempts", 0))
            if state == "running":
                attempts += 1
            elif state == "failed" and attempts == 0:
                attempts = 1
            progress[component_kind] = {
                "state": state,
                "attempts": attempts,
                "last_error": error if state == "failed" else None,
            }
            payload.update(
                state="accepted",
                blocking_reasons=[],
                retryable=state == "failed",
                component_progress=progress,
            )
            if state == "failed":
                payload["cleanup_failure"] = error
            else:
                payload.pop("cleanup_failure", None)
            conn.execute(
                update(index_operations_table)
                .where(index_operations_table.c.operation_id == operation_id)
                .values(
                    state="accepted",
                    response_json=payload,
                    completed_at_utc=self._timestamp(conn),
                )
            )
            return progress[component_kind]

    def _gc_component_state(
        self,
        candidate_generation_id: str,
        *,
        operation_id: str,
        component_kind: str,
        connection: Connection | None = None,
    ) -> str:
        with self._connection(connection) as conn:
            operation = (
                conn.execute(
                    select(index_operations_table).where(
                        index_operations_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if operation is None:
                raise PlatformError("gc_operation_not_found", "GC operation was not found", {}, 404)
            payload = dict(operation["response_json"] or {})
            if payload.get("candidate_generation_id") != candidate_generation_id:
                raise PlatformError("idempotency_key_conflict", "GC operation conflicts", {}, 409)
            progress = dict(payload.get("component_progress") or _component_progress())
            return str(dict(progress.get(component_kind, {})).get("state", "pending"))

    def complete_generation_gc(
        self,
        candidate_generation_id: str,
        *,
        operation_id: str,
        connection: Connection | None = None,
    ) -> IndexGenerationGcReceipt:
        with self._connection(connection) as conn:
            candidate = self.get_generation(candidate_generation_id, connection=conn)
            eligibility = self.gc_eligibility(
                candidate_generation_id, operation_id=operation_id, connection=conn
            )
            if eligibility.state != "accepted":
                return eligibility
            graph = dict(candidate.manifest.get("components", {}).get("public_graph", {}))
            graph_progress = self._gc_component_state(
                candidate_generation_id,
                operation_id=operation_id,
                component_kind="public_graph",
                connection=conn,
            )
            if graph.get("state") not in {None, "disabled"} and graph_progress != "completed":
                return IndexGenerationGcReceipt(
                    operation_id,
                    candidate_generation_id,
                    "blocked",
                    ("graph_component_cleanup_required",),
                    True,
                )
        if graph_progress != "completed":
            self.record_gc_component_progress(
                candidate_generation_id,
                operation_id=operation_id,
                component_kind="public_graph",
                state="completed",
                connection=connection,
            )
        return self._complete_generation_gc_after_component_cleanup(
            candidate_generation_id,
            operation_id=operation_id,
            connection=connection,
        )

    def _complete_generation_gc_after_component_cleanup(
        self,
        candidate_generation_id: str,
        *,
        operation_id: str,
        connection: Connection | None = None,
    ) -> IndexGenerationGcReceipt:
        with self._connection(connection) as conn:
            publications = tuple(
                (str(row["document_id"]), str(row["document_version_id"]))
                for row in conn.execute(
                    select(
                        index_chunks_table.c.document_id,
                        index_chunks_table.c.document_version_id,
                    )
                    .where(index_chunks_table.c.generation_id == candidate_generation_id)
                    .distinct()
                ).mappings()
            )
        for component_kind in ("sparse", "vector", "hierarchy", "cache"):
            if (
                self._gc_component_state(
                    candidate_generation_id,
                    operation_id=operation_id,
                    component_kind=component_kind,
                    connection=connection,
                )
                == "completed"
            ):
                continue
            self.record_gc_component_progress(
                candidate_generation_id,
                operation_id=operation_id,
                component_kind=component_kind,
                state="running",
                connection=connection,
            )
            purge = self._generation_component_purges.get(component_kind)
            if purge is None and component_kind == "vector":
                purge = self._generation_purge
            try:
                if purge is not None:
                    purge(candidate_generation_id, publications)
            except Exception:
                self.record_gc_component_progress(
                    candidate_generation_id,
                    operation_id=operation_id,
                    component_kind=component_kind,
                    state="failed",
                    error="cleanup_failed",
                    connection=connection,
                )
                self.record_component_failure("gc")
                return IndexGenerationGcReceipt(
                    operation_id,
                    candidate_generation_id,
                    "blocked",
                    (f"{component_kind}_cleanup_failed",),
                    True,
                )
            self.record_gc_component_progress(
                candidate_generation_id,
                operation_id=operation_id,
                component_kind=component_kind,
                state="completed",
                connection=connection,
            )
        with self._connection(connection) as conn:
            operation = (
                conn.execute(
                    select(index_operations_table).where(
                        index_operations_table.c.operation_id == operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if operation is None:
                raise PlatformError("gc_operation_not_found", "GC operation was not found", {}, 404)
            payload = dict(operation["response_json"] or {})
            eligibility = self.gc_eligibility(
                candidate_generation_id, operation_id=operation_id, connection=conn
            )
            if eligibility.state != "accepted":
                return eligibility
            candidate = self.get_generation(candidate_generation_id, connection=conn)
            conn.execute(
                index_chunks_table.delete().where(
                    index_chunks_table.c.generation_id == candidate_generation_id
                )
            )
            conn.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == candidate_generation_id)
                .values(
                    status="purged",
                    manifest_json={**dict(candidate.manifest), "gc_state": "purged"},
                )
            )
            payload["state"] = "already_purged"
            payload["retryable"] = False
            payload["blocking_reasons"] = []
            payload["component_progress"] = {
                name: {
                    **dict(value),
                    "state": "completed",
                    "last_error": None,
                }
                for name, value in dict(
                    payload.get("component_progress") or _component_progress(completed=True)
                ).items()
            }
            conn.execute(
                update(index_operations_table)
                .where(index_operations_table.c.operation_id == operation_id)
                .values(
                    state="completed",
                    response_json=payload,
                    completed_at_utc=self._timestamp(conn),
                )
            )
            return IndexGenerationGcReceipt(operation_id, candidate_generation_id, "already_purged")


class SqlAlchemyGenerationManager:
    """GenerationManager-shaped facade over the durable repository."""

    def __init__(self, repository: SqlAlchemyIndexingRepository) -> None:
        self._repository = repository

    @property
    def active_generation_id(self) -> str:
        return self._repository.active_generation_id()

    @property
    def current_revision(self) -> int:
        return self._repository.current_revision()

    def set_current_revision(self, revision: int) -> None:
        self._repository.set_current_revision(revision)

    def get_generation(self, generation_id: str) -> Generation:
        return self._repository.get_generation(generation_id)

    def list_generations(self) -> tuple[Generation, ...]:
        return self._repository.list_generations()

    def create_staging(
        self, base_snapshot: Sequence[Mapping[str, Any]] | None = None, **kwargs: Any
    ) -> Generation:
        return self._repository.create_staging(base_snapshot, **kwargs)

    def ensure_configuration_staging(self, **kwargs: Any) -> Generation:
        return self._repository.ensure_configuration_staging(**kwargs)

    def apply_change(
        self, generation_id: str, revision: int, change: Mapping[str, Any]
    ) -> Generation:
        return self._repository.apply_change(generation_id, revision, change)

    def catch_up_from_documents(self, generation_id: str, **kwargs: Any) -> Generation:
        return self._repository.catch_up_from_documents(generation_id, **kwargs)

    def set_component_state(
        self, generation_id: str, component_kind: str, state: str, **kwargs: Any
    ) -> Generation:
        return self._repository.set_component_state(generation_id, component_kind, state, **kwargs)

    def release(self, generation_id: str, **kwargs: Any) -> Generation:
        return self._repository.release(generation_id, **kwargs)

    def rollback(self, candidate_generation_id: str, **kwargs: Any) -> Generation:
        return self._repository.rollback(candidate_generation_id, **kwargs)

    def acquire_reference_lease(self, **kwargs: Any) -> GenerationReferenceLease:
        return self._repository.acquire_reference_lease(**kwargs)

    def release_reference_lease(self, lease_id: str) -> None:
        self._repository.release_reference_lease(lease_id)

    def acquire_graph_reader_lease(self, **kwargs: Any) -> GenerationComponentReaderLease:
        return self._repository.acquire_graph_reader_lease(**kwargs)

    def get_graph_reader_lease(self, lease_id: str) -> GenerationComponentReaderLease:
        return self._repository.get_graph_reader_lease(lease_id)

    def renew_graph_reader_lease(
        self, lease_id: str, **kwargs: Any
    ) -> GenerationComponentReaderLease:
        return self._repository.renew_graph_reader_lease(lease_id, **kwargs)

    def release_graph_reader_lease(self, lease_id: str) -> None:
        self._repository.release_reference_lease(lease_id)

    def request_index_generation_gc(
        self, candidate_generation_id: str, **kwargs: Any
    ) -> IndexGenerationGcReceipt:
        return self._repository.request_index_generation_gc(candidate_generation_id, **kwargs)

    def complete_generation_gc(
        self, candidate_generation_id: str, **kwargs: Any
    ) -> IndexGenerationGcReceipt:
        return self._repository.complete_generation_gc(candidate_generation_id, **kwargs)


__all__ = ["SqlAlchemyGenerationManager", "SqlAlchemyIndexingRepository"]
