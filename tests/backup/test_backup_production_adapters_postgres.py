"""Integration tests: production backup adapters against real PostgreSQL.

Exercises the provider adapters end to end (logical snapshot archive, per-key
object snapshot, fact validation, derived rebuild, post gate) with the real
REPEATABLE READ snapshot transaction, via the same orchestration entry points
the backup worker drives.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from alembic import command
from alembic.config import Config
from app.backup.adapters import (
    ProductionDerivedRebuild,
    ProductionFactValidation,
    ProductionObjectManifest,
    ProductionObjectSnapshot,
    ProductionPostgresBackup,
)
from app.backup.schema import backup_objects_table
from app.backup.service import BackupRestoreService
from app.documents.schema import (
    document_versions_table,
    documents_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    publications_table,
)
from app.identity.schema import identity_user_table
from app.indexing.prefix_cache import PrefixCacheManager
from app.indexing.schema import index_chunks_table, index_generation_heads_table
from app.platform.storage import MemoryObjectStore, ObjectMetadata

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
DOCUMENT_OBJECT_KEY = "documents/doc-it-1/ver-it-1/original"
SUBMISSION_OBJECT_KEY = "submissions/sub-it-1/private"
AVATAR_OBJECT_KEY = "avatars/user-it-1"


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
        self.calls.append(("stage", publication_id, document_id, len(chunks)))
        return "staged"

    def publish_staged(self, attempt_id, publication_id, **kwargs):
        self.calls.append(("publish", publication_id))
        return "published"


class _SpyPostGate:
    def __init__(self, engine: object, object_store: object) -> None:
        from app.backup.adapters import ProductionPostGateValidation

        self._inner = ProductionPostGateValidation(engine, object_store)
        self.calls = 0

    def validate_post_gate(self) -> list[str]:
        self.calls += 1
        return self._inner.validate_post_gate()


def _alembic_config(database_url: str) -> Config:
    config = Config(str(Path("alembic.ini")))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture()
def postgres_engine():
    database_url = os.environ.get("RAGQS_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("PostgreSQL integration environment is not configured")
    command.upgrade(_alembic_config(database_url), "head")
    engine = create_engine(database_url, future=True)

    from sqlalchemy import MetaData

    def clear_business_tables() -> None:
        # A snapshot must capture exactly the rows this test seeds: clear every
        # business table (orchestration bookkeeping is excluded from snapshots
        # anyway) so the restored state is deterministic. Teardown must clear
        # too: leftover rows (index_generation_heads 等) leak into the platform
        # integration tests that share this database and start from empty.
        metadata = MetaData()
        with engine.begin() as connection:
            metadata.reflect(bind=connection)
            for table in reversed(metadata.sorted_tables):
                if table.name != "alembic_version":
                    connection.execute(table.delete())

    clear_business_tables()
    yield engine
    clear_business_tables()
    engine.dispose()


@pytest.fixture()
def object_store():
    return MemoryObjectStore()


def _seed(postgres_engine, object_store: MemoryObjectStore) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(
            identity_user_table.insert().values(
                id="user-it-1",
                username="user",
                normalized_username="user",
                password_hash="hash",
                real_name="User",
                display_name="User",
                role="member",
                lifecycle_status="active",
                version=1,
                avatar_url=f"object://{AVATAR_OBJECT_KEY}",
                preferences_json={},
                transition_version=1,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            documents_table.insert().values(
                id="doc-it-1",
                space_id="space-it-1",
                lifecycle_status="active",
                active_version_id="ver-it-1",
                version=1,
                uploaded_at_utc=NOW,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            document_versions_table.insert().values(
                id="ver-it-1",
                document_id="doc-it-1",
                version_number=1,
                status="active",
                content_hash_sha256="a" * 64,
                object_manifest_json={},
                original_object_key=DOCUMENT_OBJECT_KEY,
                file_name="a.txt",
                media_kind="text",
                size_bytes=9,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            knowledge_submissions_table.insert().values(
                id="sub-it-1",
                space_id="space-it-1",
                submitter_user_id="user-it-1",
                version=1,
                status="approved",
                file_name="b.txt",
                media_kind="text",
                content_hash_sha256="b" * 64,
                private_object_key=SUBMISSION_OBJECT_KEY,
                object_manifest_json={},
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job-it-1",
                document_id="doc-it-1",
                document_version_id="ver-it-1",
                operation="initial",
                state="succeeded",
                stage="indexing",
                version=1,
                replay_generation=0,
                degradations_json=[],
                processing_summary_json={},
                ocr_low_confidence=False,
                notification_event_ids_json=[],
                created_by_user_id="user-it-1",
                quota_role_snapshot="member",
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            publications_table.insert().values(
                id="pub-it-1",
                document_id="doc-it-1",
                document_version_id="ver-it-1",
                job_id="job-it-1",
                attempt_id="attempt-it-1",
                generation_id="gen-it-1",
                status="active",
                resource_manifest_json={},
                created_at_utc=NOW,
                activated_at_utc=NOW,
            )
        )
        connection.execute(
            index_generation_heads_table.insert().values(
                id="instance",
                active_generation_id="gen-it-1",
                current_revision=1,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            index_chunks_table.insert().values(
                id="chunk-it-1",
                generation_id="gen-it-1",
                publication_id="pub-it-1",
                document_id="doc-it-1",
                document_version_id="ver-it-1",
                space_id="space-it-1",
                text="chunk text",
                embedding_text="chunk embedding text",
                locator_json={"page": 1},
                snippet=None,
                media_kind="text",
                manifest_hash="m" * 64,
                metadata_json={"embedding_model": "memory"},
                sparse_text=None,
                indexable=True,
            )
        )
    object_store.put(
        DOCUMENT_OBJECT_KEY,
        b"hello doc",
        ObjectMetadata("text/plain", 9, "a" * 64),
    )
    object_store.put(
        SUBMISSION_OBJECT_KEY,
        b"private-bytes",
        ObjectMetadata("text/plain", 13, "b" * 64),
    )
    object_store.put(
        AVATAR_OBJECT_KEY,
        b"avatar-bytes",
        ObjectMetadata("image/png", 12, sha256(b"avatar-bytes").hexdigest()),
    )


def _build_service(
    postgres_engine, object_store: MemoryObjectStore, *, dense_writer=None, sparse_writer=None
) -> tuple[
    BackupRestoreService, _RecordingWriter, _RecordingWriter, _SpyPostGate, PrefixCacheManager
]:
    dense = dense_writer if dense_writer is not None else _RecordingWriter()
    sparse = sparse_writer if sparse_writer is not None else _RecordingWriter()
    prefix_cache = PrefixCacheManager()
    post_gate = _SpyPostGate(postgres_engine, object_store)
    service = BackupRestoreService(
        postgres_engine,
        postgres_backup=ProductionPostgresBackup(
            postgres_engine, object_store, "integration-backups"
        ),
        object_snapshot=ProductionObjectSnapshot(
            postgres_engine, object_store, "integration-backups"
        ),
        object_manifest=ProductionObjectManifest(postgres_engine, object_store),
        fact_validation=ProductionFactValidation(postgres_engine, object_store),
        derived_rebuild=ProductionDerivedRebuild(
            postgres_engine,
            dense_writer=dense,
            sparse_provider=sparse,
            prefix_cache=prefix_cache,
        ),
        post_gate_validation=post_gate,
    )
    return service, dense, sparse, post_gate, prefix_cache


def test_backup_execute_and_restore_full_flow(postgres_engine, object_store) -> None:
    _seed(postgres_engine, object_store)
    service, dense, sparse, post_gate, _prefix_cache = _build_service(postgres_engine, object_store)

    state = service.create_full_backup_set()
    backup_id = str(state["backup_id"])

    # A1: three components succeeded; the manifest rows carry size/sha256.
    assert state["status"] == "complete"
    components = {component["kind"]: component for component in state["components"]}
    assert {kind: component["status"] for kind, component in components.items()} == {
        "postgres_snapshot": "succeeded",
        "object_store_snapshot": "succeeded",
        "object_manifest": "succeeded",
    }
    assert store_object_exists(object_store, str(components["postgres_snapshot"]["reference"]))
    with postgres_engine.connect() as connection:
        manifest_rows = (
            connection.execute(
                select(
                    backup_objects_table.c.object_key,
                    backup_objects_table.c.size_bytes,
                    backup_objects_table.c.sha256,
                ).where(backup_objects_table.c.backup_id == backup_id)
            )
            .mappings()
            .all()
        )
    assert {str(row["object_key"]) for row in manifest_rows} == {
        DOCUMENT_OBJECT_KEY,
        SUBMISSION_OBJECT_KEY,
        AVATAR_OBJECT_KEY,
    }
    assert all(
        int(row["size_bytes"]) > 0 and len(str(row["sha256"])) == 64 for row in manifest_rows
    )

    # A2/A15: the fixed seven-stage restore drives to completion on real PG.
    restore = service.start_restore(backup_id)
    restore_id = str(restore["restore_id"])
    assert service.reads_closed()

    final = restore
    for _ in range(32):
        final = service.advance_restore(restore_id)
        if str(final["status"]) in ("completed", "failed", "blocked"):
            break

    assert final["status"] == "completed"
    stages = {stage["stage"]: stage for stage in final["stages"]}
    assert set(stages) == {
        "postgres",
        "object_store",
        "milvus",
        "sparse",
        "summary",
        "graph",
        "cache",
    }
    assert all(stage["status"] == "succeeded" for stage in stages.values())
    assert final["repair_targets"] == []
    assert not service.reads_closed()

    # A3: milvus/sparse rebuilt the restored document through the real path.
    assert ("delete", "doc-it-1") in dense.calls
    assert ("stage", "pub-it-1", "doc-it-1", 1) in dense.calls
    assert ("delete", "doc-it-1") in sparse.calls
    # A4: the post gate executed real validation and reopened the reads.
    assert post_gate.calls >= 1


def store_object_exists(object_store: MemoryObjectStore, key: str) -> bool:
    return object_store.exists(key)


def test_restore_routes_checksum_mismatch_into_repair_queue(postgres_engine, object_store) -> None:
    _seed(postgres_engine, object_store)
    service, _dense, _sparse, _post_gate, _prefix_cache = _build_service(
        postgres_engine, object_store
    )

    state = service.create_full_backup_set()
    backup_id = str(state["backup_id"])
    components = {component["kind"]: component for component in state["components"]}
    snapshot_prefix = str(components["object_store_snapshot"]["reference"])

    # Corrupt the backed-up copy of one object: the restore replays the
    # corruption and fact validation must route it into the repair queue.
    object_store.put(
        f"{snapshot_prefix}/{DOCUMENT_OBJECT_KEY}",
        b"corrupted",
        ObjectMetadata("text/plain", 9, "e" * 64),
    )

    restore = service.start_restore(backup_id)
    restore_id = str(restore["restore_id"])
    final = restore
    for _ in range(32):
        final = service.advance_restore(restore_id)
        if str(final["status"]) in ("completed", "failed", "blocked"):
            break

    assert final["status"] == "blocked"
    repairs = {repair["failure_classification"] for repair in final["repair_targets"]}
    assert "object_checksum_mismatch" in repairs
    assert service.reads_closed()
