"""Contract tests for the production backup/restore provider adapters."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import create_engine, select

from app.backup.adapters import (
    ProductionDerivedRebuild,
    ProductionFactValidation,
    ProductionObjectManifest,
    ProductionObjectSnapshot,
    ProductionPostGateValidation,
    ProductionPostgresBackup,
    business_object_records,
)
from app.backup.ports import NoopPostgresBackup
from app.backup.schema import (
    backup_metadata,
    backup_objects_table,
    backup_sets_table,
    restore_sessions_table,
)
from app.documents.schema import (
    document_versions_table,
    documents_metadata,
    documents_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    publications_table,
)
from app.identity.schema import identity_metadata, identity_user_table
from app.indexing.schema import index_chunks_table, index_generation_heads_table, indexing_metadata
from app.platform.config import BackupSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime
from app.platform.storage import MemoryObjectStore, ObjectMetadata

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
NAMESPACE = "backup-tests"


def _object_metadata(content: bytes, *, checksum: str | None = None) -> ObjectMetadata:
    return ObjectMetadata(
        content_type="application/octet-stream",
        size_bytes=len(content),
        checksum_sha256=checksum,
    )


@pytest.fixture()
def engine(tmp_path):
    instance = create_engine(f"sqlite+pysqlite:///{tmp_path}/backup-adapters.db", future=True)
    documents_metadata.create_all(instance)
    identity_metadata.create_all(instance)
    indexing_metadata.create_all(instance)
    backup_metadata.create_all(instance)
    yield instance
    instance.dispose()


@pytest.fixture()
def object_store():
    return MemoryObjectStore()


def _seed_document(engine, *, document_id: str = "doc-1", version_id: str = "ver-1") -> None:
    with engine.begin() as connection:
        connection.execute(
            documents_table.insert().values(
                id=document_id,
                space_id="space-1",
                lifecycle_status="active",
                active_version_id=version_id,
                version=1,
                uploaded_at_utc=NOW,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            document_versions_table.insert().values(
                id=version_id,
                document_id=document_id,
                version_number=1,
                status="active",
                content_hash_sha256="a" * 64,
                object_manifest_json={},
                original_object_key=f"documents/{document_id}/{version_id}/original",
                file_name="a.txt",
                media_kind="text",
                size_bytes=11,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )


def _put_business_object(object_store: MemoryObjectStore, key: str, content: bytes) -> str:
    checksum = sha256(content).hexdigest()
    object_store.put(key, content, _object_metadata(content, checksum=checksum))
    return checksum


def _seed_submission(engine, *, submission_id: str = "sub-1") -> str:
    key = f"submissions/{submission_id}/private"
    with engine.begin() as connection:
        connection.execute(
            knowledge_submissions_table.insert().values(
                id=submission_id,
                space_id="space-1",
                submitter_user_id="user-1",
                version=1,
                status="approved",
                file_name="b.txt",
                media_kind="text",
                content_hash_sha256="b" * 64,
                private_object_key=key,
                object_manifest_json={},
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
    return key


def _seed_user(engine, *, user_id: str = "user-1") -> str:
    key = f"avatars/{user_id}"
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.insert().values(
                id=user_id,
                username="user",
                normalized_username="user",
                password_hash="hash",
                real_name="User",
                display_name="User",
                role="member",
                lifecycle_status="active",
                version=1,
                avatar_url=f"object://{key}",
                preferences_json={},
                transition_version=1,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
    return key


def _seed_active_generation(engine, *, generation_id: str = "gen-1") -> None:
    with engine.begin() as connection:
        connection.execute(
            index_generation_heads_table.insert().values(
                id="instance",
                active_generation_id=generation_id,
                current_revision=1,
                updated_at_utc=NOW,
            )
        )


def _seed_chunk(
    engine,
    *,
    chunk_id: str,
    document_id: str = "doc-1",
    document_version_id: str = "ver-1",
    publication_id: str = "pub-1",
    generation_id: str = "gen-1",
    indexable: bool = True,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            index_chunks_table.insert().values(
                id=chunk_id,
                generation_id=generation_id,
                publication_id=publication_id,
                document_id=document_id,
                document_version_id=document_version_id,
                space_id="space-1",
                text=f"text-{chunk_id}",
                embedding_text=f"embed-{chunk_id}",
                locator_json={"page": 1},
                snippet=None,
                media_kind="text",
                manifest_hash="m" * 64,
                metadata_json={"embedding_model": "memory"},
                sparse_text=None,
                indexable=indexable,
            )
        )


class _RecordingWriter:
    backend_kind = "dense"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def delete_document(self, document_id: str, *, generation_id: str | None = None) -> int:
        self.calls.append(("delete", document_id))
        return 1

    def stage_chunks(
        self, attempt_id, publication_id, document_id, document_version_id, chunks, **kwargs
    ):
        self.calls.append(
            (
                "stage",
                attempt_id,
                publication_id,
                document_id,
                document_version_id,
                [chunk.chunk_id for chunk in chunks],
                kwargs.get("usage_context"),
            )
        )
        return "staged"

    def publish_staged(self, attempt_id, publication_id, **kwargs):
        self.calls.append(("publish", attempt_id, publication_id))
        return "published"


class _RecordingCache:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate_document(self, *, document_id: str, reason: str) -> int:
        self.invalidated.append(document_id)
        return 1


# ----------------------------------------------------------------------
# Postgres snapshot adapter
# ----------------------------------------------------------------------


def test_postgres_backup_snapshot_writes_archive_and_restores_rows(engine, object_store) -> None:
    _seed_document(engine)
    adapter = ProductionPostgresBackup(engine, object_store, f"{NAMESPACE}/primary")

    reference = adapter.snapshot()

    assert reference.startswith(f"{NAMESPACE}/primary/postgres/")
    content, metadata = object_store.get(reference)
    assert metadata.size_bytes == len(content)

    with engine.begin() as connection:
        connection.execute(documents_table.delete())
        connection.execute(document_versions_table.delete())
    adapter.restore(reference)

    with engine.connect() as connection:
        documents = connection.execute(select(documents_table.c.id)).scalars().all()
        versions = connection.execute(select(document_versions_table.c.id)).scalars().all()
    assert documents == ["doc-1"]
    assert versions == ["ver-1"]


def test_postgres_backup_snapshot_captures_consistent_state_and_delete_cleans(
    engine, object_store
) -> None:
    _seed_document(engine)
    adapter = ProductionPostgresBackup(engine, object_store, NAMESPACE)

    reference = adapter.snapshot()
    with engine.begin() as connection:
        connection.execute(
            document_versions_table.update()
            .where(document_versions_table.c.id == "ver-1")
            .values(file_name="changed.txt")
        )
    adapter.restore(reference)

    with engine.connect() as connection:
        file_name = connection.execute(
            select(document_versions_table.c.file_name).where(
                document_versions_table.c.id == "ver-1"
            )
        ).scalar_one()
    assert file_name == "a.txt"

    adapter.delete(reference)
    with pytest.raises(KeyError):
        object_store.get(reference)


def test_postgres_backup_snapshot_excludes_orchestration_tables(engine, object_store) -> None:
    _seed_document(engine)
    with engine.begin() as connection:
        connection.execute(
            backup_sets_table.insert().values(id="backup-1", status="creating", created_at_utc=NOW)
        )
    adapter = ProductionPostgresBackup(engine, object_store, NAMESPACE)

    reference = adapter.snapshot()
    content, _metadata = object_store.get(reference)

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = set(archive.namelist())
    assert "documents.jsonl" in members
    assert "backup_sets.jsonl" not in members
    assert "_manifest.json" in members


# ----------------------------------------------------------------------
# Object records, manifest and object snapshot
# ----------------------------------------------------------------------


def test_business_object_records_join_domain_tables(engine) -> None:
    _seed_document(engine)
    submission_key = _seed_submission(engine)
    avatar_key = _seed_user(engine)

    records = business_object_records(engine)

    by_key = {record.object_key: record for record in records}
    assert sorted(by_key) == sorted(
        [
            "documents/doc-1/ver-1/original",
            submission_key,
            avatar_key,
        ]
    )
    assert by_key["documents/doc-1/ver-1/original"].recorded_sha256 == "a" * 64
    assert by_key[submission_key].recorded_sha256 == "b" * 64
    assert by_key[avatar_key].recorded_sha256 is None


def test_business_object_records_skip_purged_versions_and_cleaned_submissions(engine) -> None:
    _seed_document(engine)
    _seed_submission(engine)
    with engine.begin() as connection:
        connection.execute(
            document_versions_table.update()
            .where(document_versions_table.c.id == "ver-1")
            .values(status="purged")
        )
        connection.execute(
            knowledge_submissions_table.update()
            .where(knowledge_submissions_table.c.id == "sub-1")
            .values(private_object_cleaned_at_utc=NOW)
        )

    assert business_object_records(engine) == []


def test_object_manifest_collects_observed_facts(engine, object_store) -> None:
    _seed_document(engine)
    submission_key = _seed_submission(engine)
    avatar_key = _seed_user(engine)
    object_store.put(
        "documents/doc-1/ver-1/original",
        b"hello doc",
        _object_metadata(b"hello doc", checksum="a" * 64),
    )
    object_store.put(
        submission_key,
        b"private-bytes",
        _object_metadata(b"private-bytes", checksum="b" * 64),
    )
    avatar_checksum = _put_business_object(object_store, avatar_key, b"avatar-bytes")
    manifest = ProductionObjectManifest(engine, object_store)

    facts = manifest.collect_object_facts()

    assert [fact.object_key for fact in facts] == sorted(
        [
            "documents/doc-1/ver-1/original",
            submission_key,
            avatar_key,
        ]
    )
    by_key = {fact.object_key: fact for fact in facts}
    assert by_key["documents/doc-1/ver-1/original"].sha256 == "a" * 64
    assert by_key["documents/doc-1/ver-1/original"].size_bytes == len(b"hello doc")
    assert by_key[submission_key].sha256 == "b" * 64
    assert by_key[avatar_key].sha256 == avatar_checksum


def test_object_manifest_strict_mode_fails_on_missing_object(engine, object_store) -> None:
    _seed_document(engine)
    manifest = ProductionObjectManifest(engine, object_store)

    with pytest.raises(KeyError):
        manifest.collect_object_facts()


def test_object_manifest_strict_mode_fails_on_checksum_mismatch(engine, object_store) -> None:
    _seed_document(engine)
    object_store.put(
        "documents/doc-1/ver-1/original",
        b"tampered",
        _object_metadata(b"tampered", checksum="f" * 64),
    )
    manifest = ProductionObjectManifest(engine, object_store)

    with pytest.raises(PlatformError) as error:
        manifest.collect_object_facts()
    assert error.value.code == "backup_object_checksum_mismatch"


def test_object_snapshot_copies_restore_and_delete(engine, object_store) -> None:
    _seed_document(engine)
    original = _put_business_object(object_store, "documents/doc-1/ver-1/original", b"payload-1")
    adapter = ProductionObjectSnapshot(engine, object_store, NAMESPACE)

    reference = adapter.snapshot()

    assert object_store.exists(f"{reference}/documents/doc-1/ver-1/original")
    object_store.delete("documents/doc-1/ver-1/original")

    adapter.restore(reference)

    content, metadata = object_store.get("documents/doc-1/ver-1/original")
    assert content == b"payload-1"
    assert metadata.checksum_sha256 == original

    adapter.delete(reference)

    assert not object_store.exists(f"{reference}/documents/doc-1/ver-1/original")
    assert not object_store.exists(f"{reference}/_index.json")
    assert object_store.exists("documents/doc-1/ver-1/original")


def test_object_snapshot_delete_without_index_is_a_noop(engine, object_store) -> None:
    adapter = ProductionObjectSnapshot(engine, object_store, NAMESPACE)

    adapter.delete(f"{NAMESPACE}/objects/unknown")

    assert not object_store.exists(f"{NAMESPACE}/objects/unknown/_index.json")


# ----------------------------------------------------------------------
# Fact validation
# ----------------------------------------------------------------------


def _seed_backup_manifest(engine, *, backup_id: str = "backup-1") -> None:
    with engine.begin() as connection:
        connection.execute(
            backup_sets_table.insert().values(id=backup_id, status="complete", created_at_utc=NOW)
        )
        connection.execute(
            backup_objects_table.insert().values(
                backup_id=backup_id,
                object_key="documents/doc-1/ver-1/original",
                size_bytes=9,
                sha256="a" * 64,
                metadata_json={"content_type": "application/octet-stream"},
            )
        )
        connection.execute(
            restore_sessions_table.insert().values(
                id="restore-1",
                backup_id=backup_id,
                status="accepted",
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )


def test_fact_validation_expected_reads_active_restore_manifest(engine, object_store) -> None:
    _seed_backup_manifest(engine)
    validation = ProductionFactValidation(engine, object_store)

    expected = validation.expected_object_facts()

    assert len(expected) == 1
    assert expected[0].object_key == "documents/doc-1/ver-1/original"
    assert expected[0].sha256 == "a" * 64


def test_fact_validation_expected_requires_active_restore(engine, object_store) -> None:
    validation = ProductionFactValidation(engine, object_store)

    with pytest.raises(PlatformError) as error:
        validation.expected_object_facts()
    assert error.value.code == "restore_validation_without_session"


def test_fact_validation_actual_skips_missing_objects(engine, object_store) -> None:
    _seed_document(engine)
    _put_business_object(object_store, "documents/doc-1/ver-1/original", b"hello doc")
    validation = ProductionFactValidation(engine, object_store)

    assert len(validation.actual_object_facts()) == 1

    object_store.delete("documents/doc-1/ver-1/original")

    assert validation.actual_object_facts() == []


# ----------------------------------------------------------------------
# Derived rebuild
# ----------------------------------------------------------------------


def test_derived_rebuild_lists_resources_from_restored_chunks(engine) -> None:
    _seed_active_generation(engine)
    _seed_chunk(engine, chunk_id="chunk-1")
    _seed_chunk(engine, chunk_id="chunk-2", document_id="doc-2", publication_id="pub-2")
    _seed_chunk(engine, chunk_id="chunk-3", indexable=False)
    rebuild = ProductionDerivedRebuild(engine, dense_writer=object(), sparse_provider=object())

    assert rebuild.list_resources("milvus") == ["doc-1", "doc-2"]
    assert rebuild.list_resources("sparse") == ["doc-1", "doc-2"]
    assert rebuild.list_resources("summary") == []
    assert rebuild.list_resources("graph") == []


def test_derived_rebuild_empty_without_backends(engine) -> None:
    rebuild = ProductionDerivedRebuild(engine)

    assert rebuild.list_resources("milvus") == []
    assert rebuild.list_resources("sparse") == []
    assert rebuild.list_resources("cache") == []


def test_derived_rebuild_rebuilds_document_through_writer(engine) -> None:
    _seed_active_generation(engine)
    _seed_chunk(engine, chunk_id="chunk-1")
    _seed_chunk(engine, chunk_id="chunk-2", publication_id="pub-2")
    writer = _RecordingWriter()
    rebuild = ProductionDerivedRebuild(engine, dense_writer=writer)

    rebuild.rebuild("milvus", "doc-1")

    assert writer.calls[0] == ("delete", "doc-1")
    stage_calls = [call for call in writer.calls if call[0] == "stage"]
    assert {call[2] for call in stage_calls} == {"pub-1", "pub-2"}
    assert {call[5][0] for call in stage_calls} == {"chunk-1", "chunk-2"}
    assert all(call[6] is not None for call in stage_calls)
    assert [call[0] for call in writer.calls[1:]] == ["stage", "publish", "stage", "publish"]


def test_derived_rebuild_rebuild_is_per_resource(engine) -> None:
    _seed_active_generation(engine)
    _seed_chunk(engine, chunk_id="chunk-1")
    _seed_chunk(engine, chunk_id="chunk-2", document_id="doc-2")
    writer = _RecordingWriter()
    rebuild = ProductionDerivedRebuild(engine, sparse_provider=writer)

    rebuild.rebuild("sparse", "doc-1")

    document_ids = {call[1] for call in writer.calls if call[0] == "delete"}
    assert document_ids == {"doc-1"}
    assert all(call[6] is None for call in writer.calls if call[0] == "stage")


def test_derived_rebuild_invalidates_prefix_cache(engine) -> None:
    _seed_active_generation(engine)
    _seed_chunk(engine, chunk_id="chunk-1")
    cache = _RecordingCache()
    rebuild = ProductionDerivedRebuild(engine, prefix_cache=cache)

    assert rebuild.list_resources("cache") == ["doc-1"]
    rebuild.rebuild("cache", "doc-1")

    assert cache.invalidated == ["doc-1"]


def test_derived_rebuild_rejects_unknown_stage(engine) -> None:
    rebuild = ProductionDerivedRebuild(engine)

    with pytest.raises(PlatformError) as error:
        rebuild.list_resources("unknown")
    assert error.value.code == "restore_stage_unknown"


# ----------------------------------------------------------------------
# Post gate
# ----------------------------------------------------------------------


def _seed_publication(engine, *, publication_id: str = "pub-1", status: str = "active") -> None:
    with engine.begin() as connection:
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job-1",
                document_id="doc-1",
                document_version_id="ver-1",
                operation="initial",
                state="succeeded",
                stage="indexing",
                version=1,
                replay_generation=0,
                degradations_json=[],
                processing_summary_json={},
                ocr_low_confidence=False,
                notification_event_ids_json=[],
                created_by_user_id="user-1",
                quota_role_snapshot="member",
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            publications_table.insert().values(
                id=publication_id,
                document_id="doc-1",
                document_version_id="ver-1",
                job_id="job-1",
                attempt_id="attempt-1",
                generation_id="gen-1",
                status=status,
                resource_manifest_json={},
                created_at_utc=NOW,
                activated_at_utc=NOW,
            )
        )


def test_post_gate_passes_consistent_restored_state(engine, object_store) -> None:
    _seed_document(engine)
    _seed_publication(engine)
    object_store.put(
        "documents/doc-1/ver-1/original",
        b"hello doc",
        _object_metadata(b"hello doc", checksum="a" * 64),
    )
    gate = ProductionPostGateValidation(engine, object_store)

    assert gate.validate_post_gate() == []


def test_post_gate_reports_active_version_and_publication_findings(engine, object_store) -> None:
    _seed_document(engine)
    _seed_publication(engine)
    object_store.put(
        "documents/doc-1/ver-1/original",
        b"hello doc",
        _object_metadata(b"hello doc", checksum="a" * 64),
    )
    gate = ProductionPostGateValidation(engine, object_store)

    findings = gate.validate_post_gate()

    assert findings == []

    with engine.begin() as connection:
        connection.execute(
            document_versions_table.update()
            .where(document_versions_table.c.id == "ver-1")
            .values(status="superseded")
        )

    assert gate.validate_post_gate() == ["active_version_not_active:doc-1"]

    with engine.begin() as connection:
        connection.execute(
            documents_table.update()
            .where(documents_table.c.id == "doc-1")
            .values(active_version_id="missing-version")
        )

    assert gate.validate_post_gate() == ["active_version_missing:doc-1"]


def test_post_gate_reports_missing_active_publication(engine, object_store) -> None:
    _seed_document(engine)
    object_store.put(
        "documents/doc-1/ver-1/original",
        b"hello doc",
        _object_metadata(b"hello doc", checksum="a" * 64),
    )
    gate = ProductionPostGateValidation(engine, object_store)

    assert gate.validate_post_gate() == ["active_publication_missing:doc-1"]


def test_post_gate_reports_object_findings(engine, object_store) -> None:
    _seed_document(engine)
    _seed_publication(engine)
    gate = ProductionPostGateValidation(engine, object_store)

    assert gate.validate_post_gate() == ["object_missing:documents/doc-1/ver-1/original"]

    object_store.put(
        "documents/doc-1/ver-1/original",
        b"tampered",
        _object_metadata(b"tampered", checksum="f" * 64),
    )

    assert gate.validate_post_gate() == ["object_checksum_mismatch:documents/doc-1/ver-1/original"]


# ----------------------------------------------------------------------
# Configuration and assembly
# ----------------------------------------------------------------------


def test_backup_settings_target_key_prefix() -> None:
    unconfigured = BackupSettings()
    assert unconfigured.target_key_prefix is None

    namespace_only = BackupSettings(target_namespace="ragqs-backups")
    assert namespace_only.target_key_prefix == "ragqs-backups"

    full = BackupSettings(target_namespace="ragqs-backups", target_prefix="primary/eu")
    assert full.target_key_prefix == "ragqs-backups/primary/eu"


def test_backup_settings_reject_invalid_targets() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        BackupSettings(target_namespace="Bad Namespace")
    with pytest.raises(pydantic.ValidationError):
        BackupSettings(target_namespace="ns", target_prefix="/absolute")


def _development_environment(**overrides: str) -> dict[str, str]:
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
    }
    values.update(overrides)
    return values


def test_backup_target_env_wiring() -> None:
    settings = load_platform_settings(
        _development_environment(
            RAG_BACKUP_TARGET_NAMESPACE="dev-backups",
            RAG_BACKUP_TARGET_PREFIX="primary",
        )
    )

    assert settings.backup.target_key_prefix == "dev-backups/primary"


def test_production_requires_backup_target_namespace() -> None:
    values = {
        "RAG_PLATFORM_PROFILE": "production",
        "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
        "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
        "RAG_PROVIDER_NAME": "openai-compatible",
        "RAG_PROVIDER_API_KEY": "provider-secret",
        "RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS": "90",
        "RAG_BUSINESS_TIMEZONE": "UTC",
        "RAG_DEBUG": "false",
        "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
        "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
        "RAG_AUTH_ADMIN_ROSTER": "admin",
    }

    with pytest.raises(ValueError, match="backup target namespace"):
        load_platform_settings(values)


def test_runtime_assembly_selects_adapters_by_backup_target() -> None:
    runtime_no_target = build_runtime(load_platform_settings(_development_environment()))
    try:
        assert isinstance(runtime_no_target.resolve("backup_postgres_port"), NoopPostgresBackup)
        assert runtime_no_target.resolve("backup_restore_service") is None
    finally:
        runtime_no_target.close()

    runtime_with_target = build_runtime(
        load_platform_settings(_development_environment(RAG_BACKUP_TARGET_NAMESPACE="dev-backups"))
    )
    try:
        assert isinstance(
            runtime_with_target.resolve("backup_postgres_port"), ProductionPostgresBackup
        )
        assert isinstance(
            runtime_with_target.resolve("backup_object_snapshot_port"), ProductionObjectSnapshot
        )
        assert isinstance(
            runtime_with_target.resolve("backup_object_manifest_port"), ProductionObjectManifest
        )
    finally:
        runtime_with_target.close()


def test_runtime_assembly_passes_restore_adapters_to_orchestration_service(engine) -> None:
    # build_runtime registers the orchestration service only when the backup
    # schema exists; with the schema present the restore adapters must be
    # wired through when a backup target is configured.
    documents_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    backup_metadata.create_all(engine)
    runtime = build_runtime(
        load_platform_settings(_development_environment(RAG_BACKUP_TARGET_NAMESPACE="dev-backups")),
        adapters={"database_engine": engine},
    )
    try:
        service = runtime.resolve("backup_restore_service")
        assert service is not None
        assert isinstance(service._fact_validation, ProductionFactValidation)
        assert isinstance(service._derived_rebuild, ProductionDerivedRebuild)
        assert isinstance(service._post_gate_validation, ProductionPostGateValidation)
    finally:
        runtime.close()


def test_runtime_assembly_keeps_noop_restore_adapters_without_target(engine) -> None:
    documents_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    backup_metadata.create_all(engine)
    runtime = build_runtime(
        load_platform_settings(_development_environment()),
        adapters={"database_engine": engine},
    )
    try:
        service = runtime.resolve("backup_restore_service")
        assert service is not None
        assert service._fact_validation is None
        assert service._derived_rebuild is None
        assert service._post_gate_validation is None
    finally:
        runtime.close()
