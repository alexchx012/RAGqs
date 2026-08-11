from __future__ import annotations

from app.documents.schema import DOCUMENTS_TABLE_NAMES, documents_metadata


def test_documents_metadata_owns_the_complete_greenfield_fact_source() -> None:
    expected = {
        "documents",
        "document_versions",
        "document_read_leases",
        "document_version_restore_holds",
        "document_version_cleanup_targets",
        "upload_batches",
        "upload_batch_items",
        "upload_dedup_claims",
        "knowledge_submissions",
        "submission_execution_grants",
        "ingestion_jobs",
        "ingestion_attempts",
        "publications",
        "document_deletions",
        "document_deletion_cleanup_targets",
        "documents_instance_counters",
        "index_revisions",
        "index_changes",
        "public_graph_source_manifests",
        "public_graph_source_changes",
        "public_graph_source_heads",
        "public_graph_source_consumers",
        "documents_idempotency",
    }

    assert expected <= set(documents_metadata.tables)
    assert DOCUMENTS_TABLE_NAMES == frozenset(documents_metadata.tables)
    assert "legacy_documents" not in documents_metadata.tables
