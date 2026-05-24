from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.api import file as file_api
from app.ingestion import DocumentLoaderRegistry, InMemoryIndexingJobStore, IndexingJob
from app.providers import IngestionResult, RetrievalRequest, RetrievalResult
from app.services.rag_agent_service import RagAgentService
from app.services.vector_index_service import VectorIndexService
from app.tools.knowledge_tool import get_current_knowledge_space_id


class RecordingSplitter:
    def split_document(self, content: str, file_path: str) -> list[Document]:
        return [Document(page_content=content, metadata={"h1": "Guide"})]


class RecordingVectorStore:
    def __init__(self):
        self.deleted_document_ids = []
        self.added_documents = []

    def delete_by_document_id(self, document_id: str) -> int:
        self.deleted_document_ids.append(document_id)
        return 1

    def delete_by_source(self, source: str) -> int:
        return 0

    def add_documents(self, documents: list[Document]) -> list[str]:
        self.added_documents.extend(documents)
        return [f"doc-{index}" for index, _ in enumerate(documents)]


def test_vector_index_service_records_documents_by_knowledge_space(tmp_path):
    from app.knowledge.catalog import DocumentStatus, InMemoryKnowledgeCatalog

    file_path = tmp_path / "guide.md"
    file_path.write_text("# Guide\n\nfinance policy", encoding="utf-8")
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
    record = service.get_document(space_id="finance", document_id=job.document_id)

    assert job.space_id == "finance"
    assert record is not None
    assert record.status is DocumentStatus.INDEXED
    assert record.space_id == "finance"
    assert record.latest_job_id == job.job_id
    assert vector_store.added_documents[0].metadata["space_id"] == "finance"
    assert service.list_documents(space_id="finance") == [record]


def test_sqlite_knowledge_catalog_persists_spaces_documents_and_deletions(tmp_path):
    from app.knowledge.catalog import DocumentStatus, SQLiteKnowledgeCatalog

    job = IndexingJob.create(
        document_id="doc-1",
        source_path="/docs/guide.md",
        job_id="job-1",
        space_id="finance",
    )
    job.start()
    job.complete(total_chunks=2, indexed_chunks=2)
    first_catalog = SQLiteKnowledgeCatalog(tmp_path / "documents.db")
    first_catalog.ensure_space("finance", name="Finance", description="Finance docs")
    indexed = first_catalog.upsert_from_job(job)
    deleted = first_catalog.mark_deleted("finance", indexed.document_id)

    second_catalog = SQLiteKnowledgeCatalog(tmp_path / "documents.db")
    spaces = second_catalog.list_spaces()
    loaded = second_catalog.get_document("finance", "doc-1")

    assert any(space.space_id == "finance" and space.name == "Finance" for space in spaces)
    assert deleted.status is DocumentStatus.DELETED
    assert loaded is not None
    assert loaded.status is DocumentStatus.DELETED
    assert loaded.file_name == "guide.md"
    assert loaded.latest_job_id == "job-1"
    assert [record.document_id for record in second_catalog.list_documents("finance")] == ["doc-1"]


def test_vector_index_service_can_select_sqlite_document_catalog_by_config(tmp_path):
    from app.knowledge.catalog import SQLiteKnowledgeCatalog

    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=RecordingSplitter(),
        vector_store=RecordingVectorStore(),
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=InMemoryIndexingJobStore(),
        settings=SimpleNamespace(
            indexing_job_store_provider="memory",
            document_catalog_provider="sqlite",
            document_catalog_sqlite_path=str(tmp_path / "documents.db"),
        ),
    )

    assert isinstance(service.document_catalog, SQLiteKnowledgeCatalog)


def test_vector_index_service_deletes_and_rebuilds_documents(tmp_path):
    from app.knowledge.catalog import DocumentStatus, InMemoryKnowledgeCatalog

    file_path = tmp_path / "guide.md"
    file_path.write_text("# Guide", encoding="utf-8")
    vector_store = RecordingVectorStore()
    service = VectorIndexService(
        upload_path=str(tmp_path),
        document_splitter=RecordingSplitter(),
        vector_store=vector_store,
        loader_registry=DocumentLoaderRegistry.default(),
        job_store=InMemoryIndexingJobStore(),
        document_catalog=InMemoryKnowledgeCatalog(),
    )
    job = service.index_single_file(str(file_path), space_id="finance")

    deleted = service.delete_document(space_id="finance", document_id=job.document_id)
    rebuilt_job = service.rebuild_document(space_id="finance", document_id=job.document_id)
    record = service.get_document(space_id="finance", document_id=job.document_id)

    assert deleted.status is DocumentStatus.DELETED
    assert vector_store.deleted_document_ids.count(job.document_id) >= 2
    assert rebuilt_job.document_id == job.document_id
    assert record.status is DocumentStatus.INDEXED


class FakeUploadFile:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


class SpaceAwareIngestionProvider:
    def __init__(self, job: IndexingJob):
        self.job = job
        self.indexed = []

    def index_file(self, file_path: str, space_id: str = "default") -> IngestionResult:
        self.indexed.append((Path(file_path), space_id))
        self.job.space_id = space_id
        return IngestionResult(
            success=True,
            source=file_path,
            document_count=1,
            metadata={"indexing_job": self.job},
        )


@pytest.mark.asyncio
async def test_upload_api_passes_space_id_and_returns_document_context(tmp_path, monkeypatch):
    job = IndexingJob.create(
        document_id="doc-1",
        source_path=(tmp_path / "guide.md").as_posix(),
        job_id="job-1",
        space_id="finance",
        created_at=datetime(2026, 5, 24, tzinfo=UTC),
    )
    job.start()
    job.complete(total_chunks=1, indexed_chunks=1)
    ingestion_provider = SpaceAwareIngestionProvider(job)
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        file_api,
        "get_default_provider_container",
        lambda: SimpleNamespace(ingestion_provider=ingestion_provider),
    )

    response = await file_api.upload_file(FakeUploadFile("guide.md", b"# Guide"), space_id="finance")
    payload = _json(response)

    assert ingestion_provider.indexed == [(tmp_path / "guide.md", "finance")]
    assert payload["data"]["spaceId"] == "finance"
    assert payload["data"]["indexing"]["space_id"] == "finance"


@pytest.mark.asyncio
async def test_document_lifecycle_api_lists_deletes_and_rebuilds_documents(monkeypatch):
    from app.knowledge.catalog import DocumentRecord, DocumentStatus

    indexed = DocumentRecord(
        document_id="doc-1",
        space_id="finance",
        source_path="/docs/guide.md",
        file_name="guide.md",
        status=DocumentStatus.INDEXED,
        latest_job_id="job-1",
        total_chunks=2,
        indexed_chunks=2,
    )
    deleted = DocumentRecord(
        document_id="doc-1",
        space_id="finance",
        source_path="/docs/guide.md",
        file_name="guide.md",
        status=DocumentStatus.DELETED,
        latest_job_id="job-1",
    )
    rebuilt_job = IndexingJob.create(
        document_id="doc-1",
        source_path="/docs/guide.md",
        job_id="job-2",
        space_id="finance",
    )
    rebuilt_job.start()
    rebuilt_job.complete(total_chunks=2, indexed_chunks=2)

    service = SimpleNamespace(
        list_documents=lambda space_id="default": [indexed],
        get_document=lambda space_id, document_id: indexed,
        delete_document=lambda space_id, document_id: deleted,
        rebuild_document=lambda space_id, document_id: rebuilt_job,
        list_knowledge_spaces=lambda: [SimpleNamespace(space_id="finance", name="Finance")],
    )
    monkeypatch.setattr(file_api, "vector_index_service", service)

    list_payload = _json(await file_api.list_documents("finance"))
    delete_payload = _json(await file_api.delete_document("finance", "doc-1"))
    rebuild_payload = _json(await file_api.rebuild_document("finance", "doc-1"))
    spaces_payload = _json(await file_api.list_knowledge_spaces())

    assert list_payload["data"]["documents"][0]["document_id"] == "doc-1"
    assert delete_payload["data"]["document"]["status"] == "deleted"
    assert rebuild_payload["data"]["indexing"]["job_id"] == "job-2"
    assert spaces_payload["data"]["spaces"][0]["space_id"] == "finance"


def test_rag_agent_retrieve_context_passes_knowledge_space_filter():
    class RecordingRetriever:
        def __init__(self):
            self.requests = []

        def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
            self.requests.append(request)
            return RetrievalResult(query=request.query, documents=[])

    retriever = RecordingRetriever()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=lambda model, tools, checkpointer: object(),
        tools=[],
        retriever_provider=retriever,
        agent_runtime="legacy",
    )

    service.retrieve_context("policy", space_id="finance")

    assert retriever.requests[0].filters == {"space_id": "finance"}


@pytest.mark.asyncio
async def test_legacy_agent_query_enforces_request_space_during_tool_execution():
    class RecordingAgent:
        def __init__(self):
            self.active_spaces = []

        async def ainvoke(self, input, config):
            self.active_spaces.append(get_current_knowledge_space_id())
            return {"messages": [SimpleNamespace(content="answer")]}

    agent = RecordingAgent()

    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=lambda model, tools, checkpointer: agent,
        tools=[],
        retriever_provider=SimpleNamespace(
            retrieve=lambda request: RetrievalResult(query=request.query, documents=[])
        ),
        agent_runtime="legacy",
    )

    await service.query_with_trace("policy", session_id="s1", space_id="finance")

    assert agent.active_spaces == ["finance"]


@pytest.mark.asyncio
async def test_legacy_agent_stream_enforces_request_space_during_tool_execution():
    class StreamingAgent:
        def __init__(self):
            self.active_spaces = []

        async def astream(self, input, config, stream_mode):
            self.active_spaces.append(get_current_knowledge_space_id())

            class AIMessageChunk:
                content_blocks = [{"type": "text", "text": "answer"}]

            yield AIMessageChunk(), {"langgraph_node": "agent"}

    agent = StreamingAgent()
    service = RagAgentService(
        streaming=True,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=lambda model, tools, checkpointer: agent,
        tools=[],
        agent_runtime="legacy",
    )

    chunks = [chunk async for chunk in service.query_stream("policy", session_id="s1", space_id="finance")]

    assert chunks[-1] == {"type": "complete"}
    assert agent.active_spaces == ["finance"]


def _json(response):
    import json

    return json.loads(response.body.decode("utf-8"))
