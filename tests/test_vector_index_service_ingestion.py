from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.ingestion import (
    DocumentLoaderRegistry,
    InMemoryIndexingJobStore,
    IndexingJobStatus,
    SQLiteIndexingJobStore,
)
from app.services.vector_index_service import VectorIndexService


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
    assert vector_store.deleted_sources == []
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
