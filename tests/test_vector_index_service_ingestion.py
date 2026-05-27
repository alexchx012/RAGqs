from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.ingestion import (
    DocumentLoaderRegistry,
    IndexingJobStatus,
    InMemoryIndexingJobStore,
    PostgresIndexingJobStore,
    SQLiteIndexingJobStore,
)
from app.knowledge.catalog import (
    InMemoryKnowledgeCatalog,
    PostgresKnowledgeCatalog,
    SQLiteKnowledgeCatalog,
)
from app.services.vector_index_service import (
    VectorIndexService,
    _build_default_document_catalog,
    _build_default_job_store,
)


class RecordingSplitter:
    def __init__(self):
        self.calls = []

    def split_document(self, content: str, file_path: str) -> list[Document]:
        self.calls.append((content, file_path))
        return [
            Document(page_content="chunk one", metadata={"h1": "Graph"}),
            Document(page_content="chunk two", metadata={"h1": "Graph", "h2": "State"}),
        ]


class RecordingVectorStore:
    def __init__(self):
        self.deleted_sources = []
        self.deleted_document_ids = []
        self.added_documents = []

    def delete_by_source(self, source: str) -> int:
        self.deleted_sources.append(source)
        return 0

    def delete_by_document_id(self, document_id: str) -> int:
        self.deleted_document_ids.append(document_id)
        return 0

    def add_documents(self, documents: list[Document]) -> list[str]:
        self.added_documents.extend(documents)
        return [f"doc-{index}" for index, _ in enumerate(documents)]


class FailingSplitter:
    def split_document(self, content: str, file_path: str) -> list[Document]:
        raise RuntimeError("split failed")


def test_vector_index_service_uses_loader_and_normalized_chunk_metadata(tmp_path):
    file_path = tmp_path / "graph.md"
    file_path.write_text("# Graph\n\nLangGraph content", encoding="utf-8")
    splitter = RecordingSplitter()
    vector_store = RecordingVectorStore()
    job_store = InMemoryIndexingJobStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=splitter,
        vector_store=vector_store,
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=job_store,
    )
    normalized_path = file_path.resolve().as_posix()

    job = service.index_single_file(str(file_path))

    assert job.status is IndexingJobStatus.SUCCEEDED
    assert job_store.get(job.job_id) is job
    assert job.total_chunks == 2
    assert job.indexed_chunks == 2
    assert splitter.calls == [("# Graph\n\nLangGraph content", normalized_path)]
    assert vector_store.deleted_sources == [normalized_path]
    assert vector_store.deleted_document_ids == [job.document_id]
    assert len(vector_store.added_documents) == 2
    first, second = vector_store.added_documents
    assert first.metadata["document_id"] == job.document_id
    assert first.metadata["chunk_id"] == f"{job.document_id}:000000"
    assert first.metadata["content_hash"]
    assert first.metadata["heading_path"] == "Graph"
    assert second.metadata["chunk_id"] == f"{job.document_id}:000001"
    assert second.metadata["heading_path"] == "Graph > State"
    assert second.metadata["_source"] == normalized_path
    assert second.metadata["_file_name"] == "graph.md"


def test_vector_index_service_can_create_pending_job_without_indexing(tmp_path):
    file_path = tmp_path / "queued.md"
    file_path.write_text("# Queued\n\nBackground indexing", encoding="utf-8")
    vector_store = RecordingVectorStore()
    job_store = InMemoryIndexingJobStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=RecordingSplitter(),
        vector_store=vector_store,
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=job_store,
    )

    job = service.create_pending_indexing_job(str(file_path), space_id="finance")

    assert job.status is IndexingJobStatus.PENDING
    assert job.space_id == "finance"
    assert job.source_path == file_path.resolve().as_posix()
    assert job_store.get(job.job_id) is job
    assert vector_store.deleted_document_ids == []
    assert vector_store.added_documents == []


def test_vector_index_service_runs_existing_pending_job(tmp_path):
    file_path = tmp_path / "queued.md"
    file_path.write_text("# Queued\n\nBackground indexing", encoding="utf-8")
    vector_store = RecordingVectorStore()
    job_store = InMemoryIndexingJobStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=RecordingSplitter(),
        vector_store=vector_store,
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=job_store,
    )
    pending_job = service.create_pending_indexing_job(str(file_path), space_id="finance")

    completed_job = service.run_indexing_job(pending_job.job_id)

    assert completed_job is pending_job
    assert completed_job.status is IndexingJobStatus.SUCCEEDED
    assert completed_job.total_chunks == 2
    assert completed_job.indexed_chunks == 2
    assert vector_store.deleted_document_ids == [pending_job.document_id]
    assert vector_store.deleted_sources == [file_path.resolve().as_posix()]
    assert len(vector_store.added_documents) == 2


def test_vector_index_service_can_select_sqlite_job_store_by_config(tmp_path):
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=RecordingSplitter(),
        vector_store=RecordingVectorStore(),
        loader_registry=DocumentLoaderRegistry.default(),
        settings=SimpleNamespace(
            indexing_job_store_provider="sqlite",
            indexing_job_store_sqlite_path=str(tmp_path / "indexing-jobs.db"),
        ),
    )

    assert isinstance(service.job_store, SQLiteIndexingJobStore)


def test_vector_index_service_default_job_store_prefers_grouped_storage_settings(tmp_path):
    settings = SimpleNamespace(
        storage=SimpleNamespace(
            indexing_job_store_provider="sqlite",
            indexing_job_store_sqlite_path=str(tmp_path / "grouped-indexing-jobs.db"),
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
        )
    )

    store = _build_default_job_store(settings)

    assert isinstance(store, SQLiteIndexingJobStore)
    assert store.db_path == tmp_path / "grouped-indexing-jobs.db"


def test_vector_index_service_default_job_store_supports_grouped_postgres_settings():
    settings = SimpleNamespace(
        storage=SimpleNamespace(
            indexing_job_store_provider="postgres",
            indexing_job_store_sqlite_path="data/ignored.sqlite3",
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
        )
    )

    store = _build_default_job_store(settings)

    assert isinstance(store, PostgresIndexingJobStore)
    assert store.dsn == "postgresql://rag:secret@db/ragqs-indexing"


def test_vector_index_service_default_document_catalog_prefers_grouped_storage_settings(
    tmp_path,
):
    settings = SimpleNamespace(
        storage=SimpleNamespace(
            document_catalog_provider="sqlite",
            document_catalog_sqlite_path=str(tmp_path / "grouped-document-catalog.db"),
            document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
        )
    )

    catalog = _build_default_document_catalog(settings)

    assert isinstance(catalog, SQLiteKnowledgeCatalog)
    assert catalog.db_path == tmp_path / "grouped-document-catalog.db"


def test_vector_index_service_default_document_catalog_supports_grouped_postgres_settings():
    settings = SimpleNamespace(
        storage=SimpleNamespace(
            document_catalog_provider="postgres",
            document_catalog_sqlite_path="data/ignored.sqlite3",
            document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
        )
    )

    catalog = _build_default_document_catalog(settings)

    assert isinstance(catalog, PostgresKnowledgeCatalog)
    assert catalog.dsn == "postgresql://rag:secret@db/ragqs-documents"


def test_vector_index_service_records_failed_single_file_jobs(tmp_path):
    file_path = tmp_path / "broken.md"
    file_path.write_text("# Broken", encoding="utf-8")
    job_store = InMemoryIndexingJobStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=FailingSplitter(),
        vector_store=RecordingVectorStore(),
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=job_store,
    )

    with pytest.raises(RuntimeError, match="split failed"):
        service.index_single_file(str(file_path))

    jobs = job_store.list()
    assert len(jobs) == 1
    assert jobs[0].status is IndexingJobStatus.FAILED
    assert jobs[0].source_path == file_path.resolve().as_posix()
    assert jobs[0].errors == ["split failed"]
    assert jobs[0].completed_at is not None


def test_vector_index_service_index_directory_returns_per_file_jobs(tmp_path):
    good_file = tmp_path / "good.md"
    broken_file = tmp_path / "broken.md"
    good_file.write_text("# Good", encoding="utf-8")
    broken_file.write_text("# Broken", encoding="utf-8")

    class PartiallyFailingSplitter:
        def split_document(self, content: str, file_path: str) -> list[Document]:
            if file_path.endswith("broken.md"):
                raise RuntimeError("broken document")
            return [Document(page_content="chunk", metadata={"h1": "Good"})]

    job_store = InMemoryIndexingJobStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=PartiallyFailingSplitter(),
        vector_store=RecordingVectorStore(),
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=job_store,
    )

    result = service.index_directory(str(tmp_path))
    result_data = result.to_dict()

    assert result.success is False
    assert result.total_files == 2
    assert result.success_count == 1
    assert result.fail_count == 1
    assert broken_file.resolve().as_posix() in result.failed_files
    assert len(result.jobs) == 2
    assert len(result_data["jobs"]) == 2
    assert {job["status"] for job in result_data["jobs"]} == {"succeeded", "failed"}
    assert len(job_store.list(status=IndexingJobStatus.SUCCEEDED)) == 1
    assert len(job_store.list(status=IndexingJobStatus.FAILED)) == 1


def test_vector_index_service_index_directory_includes_all_supported_loader_extensions(tmp_path):
    txt_file = tmp_path / "note.txt"
    csv_file = tmp_path / "policies.csv"
    json_file = tmp_path / "faqs.json"
    unsupported_file = tmp_path / "ignored.pdf"
    txt_file.write_text("plain text", encoding="utf-8")
    csv_file.write_text("team,policy\nHR,Remote work\n", encoding="utf-8")
    json_file.write_text('{"question": "What is covered?", "answer": "Benefits"}', encoding="utf-8")
    unsupported_file.write_text("not indexed", encoding="utf-8")

    splitter = RecordingSplitter()
    job_store = InMemoryIndexingJobStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=splitter,
        vector_store=RecordingVectorStore(),
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=job_store,
    )

    result = service.index_directory(str(tmp_path))

    indexed_sources = {job.source_path for job in result.jobs}
    assert result.success is True
    assert result.total_files == 3
    assert result.success_count == 3
    assert indexed_sources == {
        txt_file.resolve().as_posix(),
        csv_file.resolve().as_posix(),
        json_file.resolve().as_posix(),
    }
    assert unsupported_file.resolve().as_posix() not in indexed_sources


def test_vector_index_service_delete_document_cleans_legacy_source_chunks(tmp_path):
    file_path = tmp_path / "legacy.md"
    file_path.write_text("# Legacy\n\nold metadata", encoding="utf-8")
    vector_store = RecordingVectorStore()
    catalog = InMemoryKnowledgeCatalog()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=RecordingSplitter(),
        vector_store=vector_store,
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=InMemoryIndexingJobStore(),
        document_catalog=catalog,
    )
    job = service.index_single_file(str(file_path), space_id="finance")
    vector_store.deleted_sources.clear()
    vector_store.deleted_document_ids.clear()

    service.delete_document(space_id="finance", document_id=job.document_id)

    assert vector_store.deleted_document_ids == [job.document_id]
    assert vector_store.deleted_sources == [file_path.resolve().as_posix()]
