from types import SimpleNamespace

import pytest

from app.api import chat as chat_api
from app.models.request import ChatRequest
from app.observability.retrieval_audit import (
    InMemoryRetrievalAuditStore,
    RetrievalAuditRecord,
    SQLiteRetrievalAuditStore,
)
from app.providers import InMemorySessionStoreProvider, RetrievalRequest, RetrievalResult
from app.services import rag_agent_service as rag_service_module
from app.services.rag_agent_service import RagAgentService


class StaticRetriever:
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return RetrievalResult(
            query=request.query,
            documents=[],
            debug={"stages": ["retrieve"], "top_k": request.top_k},
        )


class FakeAgent:
    async def ainvoke(self, input, config):
        return {"messages": [SimpleNamespace(content="audited answer")]}


def fake_agent_factory(model, tools, checkpointer):
    return FakeAgent()


def _record(**overrides) -> RetrievalAuditRecord:
    values = {
        "trace_id": "trace-1",
        "session_id": "session-1",
        "space_id": "default",
        "question": "What is RAG?",
        "answer": "Retrieval augmented generation.",
        "sources": [{"index": 1, "fileName": "rag.md"}],
        "retrieval": {"debug": {"stages": ["retrieve"]}},
    }
    values.update(overrides)
    return RetrievalAuditRecord(**values)


def test_in_memory_retrieval_audit_store_filters_records_by_session_space_and_trace():
    store = InMemoryRetrievalAuditStore()
    first = store.append(_record(session_id="s1", space_id="finance", trace_id="trace-a"))
    store.append(_record(session_id="s2", space_id="hr", trace_id="trace-b"))

    assert store.list_records(session_id="s1") == [first]
    assert store.list_records(space_id="finance") == [first]
    assert store.list_records(trace_id="trace-a") == [first]
    assert store.list_records(session_id="s2", limit=0) == []


def test_sqlite_retrieval_audit_store_persists_json_payloads(tmp_path):
    db_path = tmp_path / "retrieval-audits.sqlite3"
    first_store = SQLiteRetrievalAuditStore(db_path)
    first_store.append(_record(session_id="s1", space_id="finance", trace_id="trace-a"))

    second_store = SQLiteRetrievalAuditStore(db_path)
    records = second_store.list_records(session_id="s1", limit=5)

    assert len(records) == 1
    assert records[0].trace_id == "trace-a"
    assert records[0].space_id == "finance"
    assert records[0].sources == [{"index": 1, "fileName": "rag.md"}]
    assert records[0].retrieval == {"debug": {"stages": ["retrieve"]}}


@pytest.mark.asyncio
async def test_rag_agent_service_records_retrieval_audit_with_current_trace_id(monkeypatch):
    audit_store = InMemoryRetrievalAuditStore()
    monkeypatch.setattr(rag_service_module, "get_current_trace_id", lambda: "trace-from-request")
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        retriever_provider=StaticRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        retrieval_audit_store_provider=audit_store,
        retrieval_top_k=4,
        agent_runtime="legacy",
    )

    result = await service.query_with_trace("What is RAG?", session_id="s1", space_id="finance")

    records = audit_store.list_records(session_id="s1")
    assert len(records) == 1
    assert records[0].trace_id == "trace-from-request"
    assert records[0].session_id == "s1"
    assert records[0].space_id == "finance"
    assert records[0].question == "What is RAG?"
    assert records[0].answer == result["answer"]
    assert records[0].retrieval["debug"] == {"stages": ["retrieve"], "top_k": 4}


@pytest.mark.asyncio
async def test_chat_api_lists_retrieval_audits(monkeypatch):
    class FakeRagService:
        def list_retrieval_audits(
            self,
            *,
            session_id: str | None = None,
            space_id: str | None = None,
            trace_id: str | None = None,
            limit: int = 50,
        ):
            assert session_id == "s1"
            assert space_id == "finance"
            assert trace_id == "trace-a"
            assert limit == 10
            return [
                _record(
                    trace_id="trace-a",
                    session_id="s1",
                    space_id="finance",
                    question="What is RAG?",
                )
            ]

    monkeypatch.setattr(chat_api, "rag_agent_service", FakeRagService())

    response = await chat_api.list_retrieval_audits(
        session_id="s1",
        space_id="finance",
        trace_id="trace-a",
        limit=10,
    )

    assert response["code"] == 200
    assert response["data"]["count"] == 1
    assert response["data"]["audits"][0]["traceId"] == "trace-a"
    assert response["data"]["audits"][0]["sessionId"] == "s1"
    assert response["data"]["audits"][0]["spaceId"] == "finance"
    assert response["data"]["audits"][0]["retrieval"] == {"debug": {"stages": ["retrieve"]}}


@pytest.mark.asyncio
async def test_chat_api_records_audit_when_request_has_trace_header(monkeypatch):
    audit_store = InMemoryRetrievalAuditStore()
    monkeypatch.setattr(rag_service_module, "get_current_trace_id", lambda: "trace-api")
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        retriever_provider=StaticRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        retrieval_audit_store_provider=audit_store,
        agent_runtime="legacy",
    )
    monkeypatch.setattr(chat_api, "rag_agent_service", service)

    response = await chat_api.chat(ChatRequest(Id="s1", Question="What is RAG?", spaceId="finance"))

    assert response["data"]["success"] is True
    assert audit_store.list_records(trace_id="trace-api")[0].space_id == "finance"
