"""Production backup/restore provider adapters.

These adapters back the five backup ports with the real stores: the Postgres
engine itself (logical table export/import inside one snapshot transaction)
and the configured S3-compatible object store (backup namespace, per-key
copies). Orchestration identity, ordering, gates and idempotency stay in the
backup service and worker; this module only implements the provider mechanics.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import MetaData, inspect, select

from app.documents.schema import (
    document_versions_table,
    documents_table,
    knowledge_submissions_table,
    publications_table,
)
from app.identity.schema import identity_user_table
from app.indexing.embedding import EmbeddingUsageContext
from app.indexing.models import IndexChunk
from app.indexing.schema import index_chunks_table, index_generation_heads_table
from app.platform.errors import PlatformError
from app.platform.storage import ObjectMetadata, ObjectStorePort
from app.usage.ledger import OwnershipSnapshot

from .ports import ObjectFact
from .schema import backup_objects_table, restore_sessions_table

# Backup/restore orchestration bookkeeping is live process state, not business
# fact data: the restore running right now reads its own session rows, so a
# snapshot must neither capture nor overwrite these tables.
_ORCHESTRATION_TABLES = frozenset(
    {
        "alembic_version",
        "backup_sets",
        "backup_components",
        "backup_objects",
        "backup_policy",
        "backup_schedule_occurrences",
        "backup_write_gate",
        "backup_cleanup_targets",
        "maintenance_gate",
        "ops_idempotency_commands",
        "repair_targets",
        "restore_sessions",
        "restore_stages",
        "restore_targets",
    }
)

_MANIFEST_MEMBER = "_manifest.json"
_SNAPSHOT_INDEX_MEMBER = "_index.json"
_RESTORE_USAGE_ACTOR = "backup_restore_rebuild"
_USAGE_DEADLINE = timedelta(hours=1)


def _archive_object_prefix(target_prefix: str) -> str:
    return f"{target_prefix}/postgres"


@dataclass(frozen=True, slots=True)
class _ObjectRecord:
    """A business object identity as recorded by Postgres (the fact source)."""

    object_key: str
    recorded_sha256: str | None


def business_object_records(engine: Any) -> list[_ObjectRecord]:
    """Object keys recorded by Postgres, joined per domain table.

    Objects whose owning rows released them (purged versions, cleaned
    submission objects) are not facts and stay out of the manifest.
    """

    records: dict[str, _ObjectRecord] = {}
    with engine.connect() as connection:
        version_rows = connection.execute(
            select(
                document_versions_table.c.original_object_key,
                document_versions_table.c.content_hash_sha256,
            ).where(
                document_versions_table.c.original_object_key.is_not(None),
                document_versions_table.c.status.not_in(("purging", "purged")),
            )
        )
        for row in version_rows:
            key = str(row.original_object_key)
            sha = row.content_hash_sha256
            records[key] = _ObjectRecord(key, str(sha) if sha else None)
        submission_rows = connection.execute(
            select(
                knowledge_submissions_table.c.private_object_key,
                knowledge_submissions_table.c.content_hash_sha256,
            ).where(knowledge_submissions_table.c.private_object_cleaned_at_utc.is_(None))
        )
        for row in submission_rows:
            key = str(row.private_object_key)
            if key in records:
                continue
            sha = row.content_hash_sha256
            records[key] = _ObjectRecord(key, str(sha) if sha else None)
        avatar_rows = connection.execute(
            select(identity_user_table.c.avatar_url).where(
                identity_user_table.c.avatar_url.is_not(None),
                identity_user_table.c.avatar_url.like("object://%"),
            )
        )
        for row in avatar_rows:
            key = str(row.avatar_url).removeprefix("object://")
            if key and key not in records:
                records[key] = _ObjectRecord(key, None)
    return [records[key] for key in sorted(records)]


def _observed_sha256(metadata: ObjectMetadata, content: bytes) -> str:
    if metadata.checksum_sha256:
        return str(metadata.checksum_sha256)
    return hashlib.sha256(content).hexdigest()


def _collect_object_facts(
    engine: Any, object_store: ObjectStorePort, *, strict: bool
) -> list[ObjectFact]:
    """Observe every recorded business object on the object store.

    Strict mode backs the backup-time manifest: an unreadable or
    checksum-conflicting object fails the collection, because a backup must
    not claim restorability it cannot prove. Lenient mode backs restore-time
    validation: absent objects are simply missing from the observed side, so
    the orchestration opens the matching repair targets.
    """

    facts: list[ObjectFact] = []
    for record in business_object_records(engine):
        try:
            content, metadata = object_store.get(record.object_key)
        except KeyError:
            if strict:
                raise
            continue
        observed_sha = _observed_sha256(metadata, content)
        if strict and record.recorded_sha256 and record.recorded_sha256 != observed_sha:
            raise PlatformError(
                "backup_object_checksum_mismatch",
                "Recorded object checksum does not match the stored object",
                {"object_key": record.object_key},
                502,
            )
        facts.append(
            ObjectFact(
                object_key=record.object_key,
                size_bytes=int(metadata.size_bytes),
                sha256=observed_sha,
                metadata={"content_type": str(metadata.content_type)},
            )
        )
    return facts


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, memoryview):
        return bytes(value).hex()
    return str(value)


def _coerce_column_value(column: Any, value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        type_object = column.type.python_type
    except NotImplementedError:
        type_object = None
    if type_object is datetime:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _reflected_tables(connection: Any, table_names: Sequence[str]) -> MetaData:
    metadata = MetaData()
    metadata.reflect(bind=connection, only=list(table_names))
    return metadata


def _business_table_names(connection: Any) -> list[str]:
    return sorted(
        name
        for name in inspect(connection).get_table_names()
        if name not in _ORCHESTRATION_TABLES and not name.startswith("sqlite_")
    )


class ProductionPostgresBackup:
    """Logical Postgres snapshot as a compressed per-table archive.

    snapshot() exports every business table inside a single REPEATABLE READ
    transaction (Postgres) so inter-table references stay consistent, streams
    rows as JSONL members of a zip archive into the backup namespace, and
    returns the archive object key as the opaque reference. restore() replays
    the archive into the configured database (the restore target is always
    the engine this adapter was built with); delete() removes the archive.
    """

    def __init__(self, engine: Any, object_store: ObjectStorePort, target_prefix: str) -> None:
        self._engine = engine
        self._object_store = object_store
        self._target_prefix = target_prefix.rstrip("/")

    def _snapshot_transaction(self, connection: Any) -> Any:
        if connection.dialect.name == "postgresql":
            return connection.execution_options(isolation_level="REPEATABLE READ")
        return connection

    def snapshot(self) -> str:
        snapshot_id = uuid.uuid4().hex
        reference = f"{_archive_object_prefix(self._target_prefix)}/{snapshot_id}.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with self._engine.connect() as connection:
                transaction = self._snapshot_transaction(connection)
                with transaction.begin():
                    table_names = _business_table_names(connection)
                    metadata = _reflected_tables(connection, table_names)
                    for table in metadata.sorted_tables:
                        archive.writestr(
                            f"{table.name}.jsonl",
                            "\n".join(
                                json.dumps(dict(row), default=_json_default)
                                for row in connection.execute(
                                    select(table).execution_options(yield_per=500)
                                ).mappings()
                            ),
                        )
                    archive.writestr(
                        _MANIFEST_MEMBER,
                        json.dumps({"tables": [table.name for table in metadata.sorted_tables]}),
                    )
        content = buffer.getvalue()
        self._object_store.put(
            reference,
            content,
            ObjectMetadata(
                content_type="application/zip",
                size_bytes=len(content),
                checksum_sha256=hashlib.sha256(content).hexdigest(),
            ),
        )
        return reference

    def restore(self, reference: str) -> None:
        content, _metadata = self._object_store.get(reference)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            payload = json.loads(archive.read(_MANIFEST_MEMBER))
            tables = [str(name) for name in payload["tables"]]
            rows_by_table = {
                name: [
                    json.loads(line)
                    for line in archive.read(f"{name}.jsonl").decode("utf-8").splitlines()
                    if line
                ]
                for name in tables
            }
        with self._engine.connect() as connection:
            transaction = self._snapshot_transaction(connection)
            with transaction.begin():
                metadata = _reflected_tables(connection, tables)
                # Parent tables first on export means reverse order deletes
                # children before their parents; inserts replay in FK order.
                for table in reversed(metadata.sorted_tables):
                    connection.execute(table.delete())
                for table in metadata.sorted_tables:
                    rows = rows_by_table.get(table.name, [])
                    if not rows:
                        continue
                    columns = {column.name: column for column in table.columns}
                    prepared = [
                        {
                            key: _coerce_column_value(columns[key], value)
                            for key, value in row.items()
                            if key in columns
                        }
                        for row in rows
                    ]
                    connection.execute(table.insert(), prepared)
                self._resync_identity_sequences(connection, metadata)

    def delete(self, reference: str) -> None:
        self._object_store.delete(reference)

    @staticmethod
    def _resync_identity_sequences(connection: Any, metadata: MetaData) -> None:
        if connection.dialect.name != "postgresql":
            return
        for table in metadata.sorted_tables:
            for column in table.columns:
                if not column.autoincrement or not column.primary_key:
                    continue
                # Identifiers come from the database's own reflection.
                table_name = str(table.name).replace('"', '""')
                column_name = str(column.name).replace('"', '""')
                sequence_name = connection.exec_driver_sql(
                    "SELECT pg_get_serial_sequence(%s, %s)",
                    (str(table.name), str(column.name)),
                ).scalar()
                if sequence_name is None:
                    continue
                # The replay must not rewind the sequence below ids that
                # concurrent writers (the restore orchestration itself) have
                # already consumed: resume from whichever source is ahead.
                safe_sequence = str(sequence_name).replace("'", "''")
                next_value = connection.exec_driver_sql(
                    "SELECT GREATEST("
                    f'COALESCE((SELECT MAX("{column_name}") FROM "{table_name}"), 0) + 1, '
                    f"(SELECT last_value + CASE WHEN is_called THEN 1 ELSE 0 END FROM {safe_sequence})"
                    ")"
                ).scalar()
                if next_value is None:
                    continue
                connection.exec_driver_sql(
                    "SELECT setval(%s, %s, false)",
                    (str(sequence_name), int(next_value)),
                )


class ProductionObjectSnapshot:
    """Per-key object snapshot into a unique backup prefix.

    snapshot() copies every recorded business object under
    ``{target_prefix}/objects/{snapshot_id}/`` and writes a self-describing
    index object, so delete() can enumerate the copies later without any
    storage listing API. restore() copies the snapshot contents back into the
    business namespace; the object fact validation of the orchestration layer
    verifies size/sha256 afterwards and opens repair targets on mismatch.
    """

    def __init__(self, engine: Any, object_store: ObjectStorePort, target_prefix: str) -> None:
        self._engine = engine
        self._object_store = object_store
        self._target_prefix = target_prefix.rstrip("/")

    def snapshot(self) -> str:
        backup_prefix = f"{self._target_prefix}/objects/{uuid.uuid4().hex}"
        copied: list[str] = []
        for record in business_object_records(self._engine):
            self._object_store.copy(record.object_key, f"{backup_prefix}/{record.object_key}")
            copied.append(record.object_key)
        payload = json.dumps({"keys": copied}).encode("utf-8")
        self._object_store.put(
            f"{backup_prefix}/{_SNAPSHOT_INDEX_MEMBER}",
            payload,
            ObjectMetadata(content_type="application/json", size_bytes=len(payload)),
        )
        return backup_prefix

    def restore(self, reference: str) -> None:
        backup_prefix = reference.rstrip("/")
        content, _metadata = self._object_store.get(f"{backup_prefix}/{_SNAPSHOT_INDEX_MEMBER}")
        keys = [str(key) for key in json.loads(content.decode("utf-8"))["keys"]]
        for key in keys:
            self._object_store.copy(f"{backup_prefix}/{key}", key)

    def delete(self, reference: str) -> None:
        backup_prefix = reference.rstrip("/")
        index_key = f"{backup_prefix}/{_SNAPSHOT_INDEX_MEMBER}"
        if self._object_store.exists(index_key):
            content, _metadata = self._object_store.get(index_key)
            keys = [str(key) for key in json.loads(content.decode("utf-8"))["keys"]]
        else:
            keys = []
        for key in keys:
            child_key = f"{backup_prefix}/{key}"
            if self._object_store.exists(child_key):
                self._object_store.delete(child_key)
        if self._object_store.exists(index_key):
            self._object_store.delete(index_key)


class ProductionObjectManifest:
    """Backup-time object manifest built from Postgres records joined with
    the object store (strict: a missing or conflicting object fails the
    backup instead of being recorded as restorable)."""

    def __init__(self, engine: Any, object_store: ObjectStorePort) -> None:
        self._engine = engine
        self._object_store = object_store

    def collect_object_facts(self) -> list[ObjectFact]:
        return _collect_object_facts(self._engine, self._object_store, strict=True)


class ProductionFactValidation:
    """Restore-time fact comparison: expected = the backup's manifest rows,
    actual = the object store observed after the restore."""

    def __init__(self, engine: Any, object_store: ObjectStorePort) -> None:
        self._engine = engine
        self._object_store = object_store

    def expected_object_facts(self) -> list[ObjectFact]:
        backup_id = self._active_restore_backup_id()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    backup_objects_table.c.object_key,
                    backup_objects_table.c.size_bytes,
                    backup_objects_table.c.sha256,
                    backup_objects_table.c.metadata_json,
                )
                .where(backup_objects_table.c.backup_id == backup_id)
                .order_by(backup_objects_table.c.object_key)
            )
            return [
                ObjectFact(
                    object_key=str(row.object_key),
                    size_bytes=int(row.size_bytes),
                    sha256=str(row.sha256),
                    metadata=dict(row.metadata_json or {}),
                )
                for row in rows
            ]

    def actual_object_facts(self) -> list[ObjectFact]:
        return _collect_object_facts(self._engine, self._object_store, strict=False)

    def _active_restore_backup_id(self) -> str:
        # 'blocked' sessions are still the one active restore (the durable
        # mutex covers accepted/running/blocked): repair retries re-run fact
        # validation while blocked.
        with self._engine.connect() as connection:
            backup_id = (
                connection.execute(
                    select(restore_sessions_table.c.backup_id).where(
                        restore_sessions_table.c.status.in_(("accepted", "running", "blocked"))
                    )
                )
                .scalars()
                .first()
            )
        if backup_id is None:
            raise PlatformError(
                "restore_validation_without_session",
                "Fact validation requires an active restore session",
                {},
                409,
            )
        return str(backup_id)


class ProductionDerivedRebuild:
    """Derived-stage rebuild backed by the platform's real rebuild surfaces.

    milvus/sparse rebuild one document's external index entries from the
    restored ``index_chunks`` facts (delete + stage + publish through the
    configured backends). The cache stage invalidates one document's prefix
    cache entries. summary/graph need no external rebuild: their hierarchical
    and graph structures are Postgres-resident facts restored by the Postgres
    stage, so those stages legitimately run with an empty resource list.
    """

    def __init__(
        self,
        engine: Any,
        *,
        dense_writer: Any | None = None,
        sparse_provider: Any | None = None,
        prefix_cache: Any | None = None,
        now: Any | None = None,
    ) -> None:
        self._engine = engine
        self._dense_writer = dense_writer
        self._sparse_provider = sparse_provider
        self._prefix_cache = prefix_cache
        self._now = now or (lambda: datetime.now(UTC))

    def list_resources(self, stage: str) -> list[str]:
        if stage in ("milvus", "sparse"):
            writer = self._dense_writer if stage == "milvus" else self._sparse_provider
            if writer is None:
                return []
            return self._documents_with_chunks()
        if stage == "cache":
            if self._prefix_cache is None:
                return []
            return self._documents_with_chunks()
        if stage in ("summary", "graph"):
            return []
        raise PlatformError(
            "restore_stage_unknown", "Unknown derived restore stage", {"stage": stage}, 422
        )

    def rebuild(self, stage: str, resource_id: str) -> None:
        if stage == "milvus":
            self._rebuild_document_index(self._dense_writer, resource_id, uses_embedding=True)
            return
        if stage == "sparse":
            self._rebuild_document_index(self._sparse_provider, resource_id, uses_embedding=False)
            return
        if stage == "cache":
            if self._prefix_cache is None:
                raise PlatformError(
                    "restore_rebuild_unavailable",
                    "No prefix cache is configured",
                    {"stage": stage},
                    500,
                )
            self._prefix_cache.invalidate_document(
                document_id=resource_id, reason="backup_restore_rebuild"
            )
            return
        raise PlatformError(
            "restore_stage_unknown", "Unknown derived restore stage", {"stage": stage}, 422
        )

    def _active_generation_id(self, connection: Any) -> str | None:
        return connection.execute(
            select(index_generation_heads_table.c.active_generation_id).where(
                index_generation_heads_table.c.id == "instance"
            )
        ).scalar_one_or_none()

    def _documents_with_chunks(self) -> list[str]:
        with self._engine.connect() as connection:
            generation_id = self._active_generation_id(connection)
            if generation_id is None:
                return []
            rows = connection.execute(
                select(index_chunks_table.c.document_id)
                .where(
                    index_chunks_table.c.generation_id == str(generation_id),
                    index_chunks_table.c.indexable.is_(True),
                )
                .distinct()
                .order_by(index_chunks_table.c.document_id)
            )
            return [str(row.document_id) for row in rows]

    def _rebuild_document_index(
        self, writer: Any, document_id: str, *, uses_embedding: bool
    ) -> None:
        if writer is None:
            raise PlatformError(
                "restore_rebuild_unavailable",
                "No index backend is configured for this stage",
                {},
                500,
            )
        with self._engine.connect() as connection:
            generation_id = self._active_generation_id(connection)
            if generation_id is None:
                return
            rows = (
                connection.execute(
                    select(index_chunks_table)
                    .where(
                        index_chunks_table.c.generation_id == str(generation_id),
                        index_chunks_table.c.document_id == document_id,
                    )
                    .order_by(index_chunks_table.c.publication_id, index_chunks_table.c.id)
                )
                .mappings()
                .all()
            )
        publications: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for row in rows:
            key = (str(row["publication_id"]), str(row["document_version_id"]))
            publications.setdefault(key, []).append(row)
        writer.delete_document(document_id)
        for (publication_id, document_version_id), group in publications.items():
            chunks = [
                IndexChunk(
                    chunk_id=str(row["id"]),
                    generation_id=str(row["generation_id"]),
                    publication_id=str(row["publication_id"]),
                    document_id=str(row["document_id"]),
                    document_version_id=str(row["document_version_id"]),
                    space_id=str(row["space_id"]),
                    text=str(row["text"]),
                    embedding_text=str(row["embedding_text"]),
                    locator=dict(row["locator_json"] or {}),
                    snippet=row["snippet"],
                    media_kind=str(row["media_kind"]),
                    manifest_hash=str(row["manifest_hash"]),
                    metadata=dict(row["metadata_json"] or {}),
                    sparse_text=row["sparse_text"],
                    indexable=bool(row["indexable"]),
                )
                for row in group
            ]
            attempt_id = f"restore_{uuid.uuid4().hex}"
            writer.stage_chunks(
                attempt_id,
                publication_id,
                document_id,
                document_version_id,
                chunks,
                fencing_token=1,
                expected_generation_id=str(generation_id),
                usage_context=(
                    self._usage_context(
                        attempt_id, str(generation_id), publication_id, chunks[0].space_id
                    )
                    if uses_embedding
                    else None
                ),
            )
            writer.publish_staged(attempt_id, publication_id)

    def _usage_context(
        self, attempt_id: str, generation_id: str, publication_id: str, space_id: str
    ) -> EmbeddingUsageContext:
        ownership = OwnershipSnapshot(
            actor_user_id=_RESTORE_USAGE_ACTOR,
            actor_role_snapshot="system",
            actor_department_id_snapshot=None,
            quota_subject_user_id=None,
            cost_center_key=_RESTORE_USAGE_ACTOR,
            space_id=space_id or None,
            source_space_ids=(space_id,) if space_id else (),
        )
        return EmbeddingUsageContext(
            execution_kind=_RESTORE_USAGE_ACTOR,
            execution_id=f"{_RESTORE_USAGE_ACTOR}:{attempt_id}",
            attempt_id=attempt_id,
            generation_id=generation_id,
            publication_id=publication_id,
            deadline_utc=self._now() + _USAGE_DEADLINE,
            replay_generation=0,
            ownership=ownership,
        )


class ProductionPostGateValidation:
    """Post-gate checks over restored facts: active version/publication
    consistency and per-object size/checksum verification."""

    def __init__(self, engine: Any, object_store: ObjectStorePort) -> None:
        self._engine = engine
        self._object_store = object_store

    def validate_post_gate(self) -> list[str]:
        findings = self._active_version_findings()
        findings.extend(self._object_findings())
        return findings

    def _active_version_findings(self) -> list[str]:
        findings: list[str] = []
        with self._engine.connect() as connection:
            documents = connection.execute(
                select(
                    documents_table.c.id,
                    documents_table.c.active_version_id,
                ).where(documents_table.c.active_version_id.is_not(None))
            )
            for document in documents:
                document_id = str(document.id)
                active_version_id = str(document.active_version_id)
                version = connection.execute(
                    select(document_versions_table.c.status).where(
                        document_versions_table.c.id == active_version_id
                    )
                ).scalar_one_or_none()
                if version is None:
                    findings.append(f"active_version_missing:{document_id}")
                    continue
                if str(version) != "active":
                    findings.append(f"active_version_not_active:{document_id}")
                    continue
                published = (
                    connection.execute(
                        select(publications_table.c.id).where(
                            publications_table.c.document_version_id == active_version_id,
                            publications_table.c.status == "active",
                        )
                    )
                    .scalars()
                    .first()
                )
                if published is None:
                    findings.append(f"active_publication_missing:{document_id}")
        return findings

    def _object_findings(self) -> list[str]:
        findings: list[str] = []
        for record in business_object_records(self._engine):
            try:
                content, metadata = self._object_store.get(record.object_key)
            except KeyError:
                findings.append(f"object_missing:{record.object_key}")
                continue
            observed = _observed_sha256(metadata, content)
            if record.recorded_sha256 and record.recorded_sha256 != observed:
                findings.append(f"object_checksum_mismatch:{record.object_key}")
        return findings
