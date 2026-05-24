from datetime import UTC, datetime

import pytest

from app.ingestion import (
    DocumentLoaderRegistry,
    DocumentMetadataNormalizer,
    InMemoryIndexingJobStore,
    IndexingJob,
    IndexingJobStatus,
    SQLiteIndexingJobStore,
)


def test_loader_registry_reads_utf8_text_and_markdown_files(tmp_path):
    text_path = tmp_path / "guide.txt"
    markdown_path = tmp_path / "playbook.md"
    text_path.write_text("知识库文本内容", encoding="utf-8")
    markdown_path.write_text("# Title\n\nMarkdown body", encoding="utf-8")

    registry = DocumentLoaderRegistry.default()

    text_docs = registry.load(text_path)
    markdown_docs = registry.load(markdown_path)

    assert text_docs[0].page_content == "知识库文本内容"
    assert text_docs[0].metadata["source_path"] == text_path.resolve().as_posix()
    assert text_docs[0].metadata["extension"] == ".txt"
    assert markdown_docs[0].page_content == "# Title\n\nMarkdown body"
    assert markdown_docs[0].metadata["source_path"] == markdown_path.resolve().as_posix()
    assert markdown_docs[0].metadata["extension"] == ".md"


def test_loader_registry_rejects_unsupported_file_types(tmp_path):
    pdf_path = tmp_path / "unsupported.pdf"
    pdf_path.write_text("not supported yet", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported document extension"):
        DocumentLoaderRegistry.default().load(pdf_path)


def test_metadata_normalizer_assigns_stable_document_ids_and_chunk_metadata(tmp_path):
    source_path = tmp_path / "kb.md"
    source_path.write_text("# Graph\n\nLangGraph content", encoding="utf-8")
    normalizer = DocumentMetadataNormalizer()

    original = normalizer.document_metadata(source_path, "# Graph\n\nLangGraph content")
    changed = normalizer.document_metadata(source_path, "# Graph\n\nUpdated content")
    chunk = normalizer.chunk_metadata(
        document_metadata=original,
        chunk_index=3,
        headings={"h1": "Graph", "h2": "State"},
    )

    assert original["document_id"] == changed["document_id"]
    assert original["content_hash"] != changed["content_hash"]
    assert original["source_path"] == source_path.resolve().as_posix()
    assert original["extension"] == ".md"
    assert chunk["chunk_id"] == f"{original['document_id']}:000003"
    assert chunk["document_id"] == original["document_id"]
    assert chunk["heading_path"] == "Graph > State"
    assert chunk["_source"] == original["source_path"]
    assert chunk["_extension"] == ".md"
    assert chunk["_file_name"] == "kb.md"


def test_indexing_job_tracks_success_partial_and_failed_terminal_states():
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    job = IndexingJob.create(
        document_id="doc-1",
        source_path="/docs/a.md",
        job_id="job-1",
        created_at=now,
    )

    assert job.status is IndexingJobStatus.PENDING

    job.start(now=now)
    job.complete(total_chunks=3, indexed_chunks=2, errors=["chunk 3 failed"], now=now)

    assert job.status is IndexingJobStatus.PARTIAL
    assert job.total_chunks == 3
    assert job.indexed_chunks == 2
    assert job.errors == ["chunk 3 failed"]
    assert job.started_at == now
    assert job.completed_at == now

    failed = IndexingJob.create(document_id="doc-2", source_path="/docs/b.md", job_id="job-2")
    failed.start(now=now)
    failed.complete(total_chunks=2, indexed_chunks=0, errors=["loader failed"], now=now)

    assert failed.status is IndexingJobStatus.FAILED

    succeeded = IndexingJob.create(document_id="doc-3", source_path="/docs/c.md", job_id="job-3")
    succeeded.start(now=now)
    succeeded.complete(total_chunks=1, indexed_chunks=1, now=now)

    assert succeeded.status is IndexingJobStatus.SUCCEEDED


def test_in_memory_indexing_job_store_saves_gets_and_filters_jobs():
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    succeeded = IndexingJob.create(
        document_id="doc-a",
        source_path="/docs/a.md",
        job_id="job-a",
        created_at=now,
    )
    succeeded.start(now=now)
    succeeded.complete(total_chunks=1, indexed_chunks=1, now=now)
    failed = IndexingJob.create(
        document_id="doc-b",
        source_path="/docs/b.md",
        job_id="job-b",
        created_at=now,
    )
    failed.start(now=now)
    failed.complete(total_chunks=1, indexed_chunks=0, errors=["split failed"], now=now)
    store = InMemoryIndexingJobStore()

    assert store.save(succeeded) is succeeded
    store.save(failed)

    assert store.get("job-a") is succeeded
    assert store.get("missing") is None
    assert store.list() == [succeeded, failed]
    assert store.list(document_id="doc-a") == [succeeded]
    assert store.list(status=IndexingJobStatus.FAILED) == [failed]


def test_sqlite_indexing_job_store_persists_gets_and_filters_jobs(tmp_path):
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    succeeded = IndexingJob.create(
        document_id="doc-a",
        source_path="/docs/a.md",
        space_id="finance",
        job_id="job-a",
        created_at=now,
    )
    succeeded.start(now=now)
    succeeded.complete(total_chunks=2, indexed_chunks=2, now=now)
    failed = IndexingJob.create(
        document_id="doc-b",
        source_path="/docs/b.md",
        space_id="legal",
        job_id="job-b",
        created_at=now,
    )
    failed.start(now=now)
    failed.complete(total_chunks=1, indexed_chunks=0, errors=["split failed"], now=now)

    first_store = SQLiteIndexingJobStore(tmp_path / "indexing-jobs.db")
    first_store.save(succeeded)
    first_store.save(failed)

    second_store = SQLiteIndexingJobStore(tmp_path / "indexing-jobs.db")

    assert second_store.get("job-a").status is IndexingJobStatus.SUCCEEDED
    assert second_store.get("job-a").space_id == "finance"
    assert second_store.get("job-b").errors == ["split failed"]
    assert [job.job_id for job in second_store.list()] == ["job-a", "job-b"]
    assert [job.job_id for job in second_store.list(document_id="doc-a")] == ["job-a"]
    assert [job.job_id for job in second_store.list(status=IndexingJobStatus.FAILED)] == [
        "job-b"
    ]
