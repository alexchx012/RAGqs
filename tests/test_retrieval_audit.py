from types import SimpleNamespace

import pytest

from app.api import chat as chat_api
from app.models.request import ChatRequest
from app.observability import retrieval_audit
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


def test_postgres_retrieval_audit_store_persists_and_filters_records():
    database = FakePostgresRetrievalAuditDatabase()
    store = retrieval_audit.PostgresRetrievalAuditStore(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )
    first = _record(session_id="s1", space_id="finance", trace_id="trace-a")
    store.append(first)
    store.append(_record(session_id="s2", space_id="hr", trace_id="trace-b"))

    assert database.dsns == ["postgresql://rag:secret@db/ragqs"]
    assert store.list_records(session_id="s1") == [first]
    assert store.list_records(space_id="finance") == [first]
    assert store.list_records(trace_id="trace-a") == [first]
    assert store.list_records(session_id="s2", limit=0) == []
    assert store.list_records(session_id="s1")[0].sources == [{"index": 1, "fileName": "rag.md"}]
    assert store.list_records(session_id="s1")[0].retrieval == {
        "debug": {"stages": ["retrieve"]}
    }


def test_postgres_retrieval_audit_store_defers_connection_until_first_operation():
    database = FakePostgresRetrievalAuditDatabase()

    store = retrieval_audit.PostgresRetrievalAuditStore(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )

    assert database.connect_count == 0

    store.list_records()

    assert database.connect_count == 1


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
async def test_retrieval_audit_does_not_store_private_reasoning_content(monkeypatch):
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    class AuditThinkingModel:
        def __init__(self):
            self.calls = []
            self.bound_tools = []

        def bind_tools(self, tools):
            self.bound_tools = tools
            return self

        def invoke(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return AIMessage(
                    content="",
                    additional_kwargs={
                        "reasoning_content": "private-audit-reasoning",
                        "deepseek_tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "demo_tool", "arguments": "{}"},
                            }
                        ],
                    },
                    tool_calls=[
                        {
                            "name": "demo_tool",
                            "args": {},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="audited public answer")

    class AuditThinkingProvider:
        def __init__(self):
            self.model = AuditThinkingModel()

        def create_chat_model(self, streaming: bool = True):
            return self.model

    class AuditToolExecutor:
        def execute(self, request):
            return {"name": request["name"], "output": "tool answer", "metadata": {}}

    @tool("demo_tool")
    def demo_tool() -> str:
        """Return a demo tool response."""
        return "demo"

    def failing_agent_factory(model, tools, checkpointer):
        raise AssertionError("legacy create_agent path should not be used")

    audit_store = InMemoryRetrievalAuditStore()
    monkeypatch.setattr(rag_service_module, "get_current_trace_id", lambda: "trace-private")
    service = RagAgentService(
        streaming=False,
        chat_model_provider=AuditThinkingProvider(),
        agent_factory=failing_agent_factory,
        tools=[demo_tool],
        retriever_provider=StaticRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        retrieval_audit_store_provider=audit_store,
        tool_executor=AuditToolExecutor(),
        agent_runtime="explicit_graph",
    )

    await service.query_with_trace("What is RAG?", session_id="s1")

    records = audit_store.list_records(session_id="s1")
    assert len(records) == 1
    assert "reasoning_content" not in repr(records)
    assert "private-audit-reasoning" not in repr(records)
    assert "deepseek_tool_calls" not in repr(records)


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


class FakePostgresRetrievalAuditDatabase:
    def __init__(self):
        self.rows = []
        self.connect_count = 0
        self.dsns = []
        self.next_id = 1

    def connect(self, dsn: str):
        self.connect_count += 1
        if dsn not in self.dsns:
            self.dsns.append(dsn)
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakePostgresRetrievalAuditDatabase):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeCursor(self.database)


class FakeCursor:
    def __init__(self, database: FakePostgresRetrievalAuditDatabase):
        self.database = database
        self.results = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql: str, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create table") or normalized.startswith("create index"):
            self.results = []
            return self
        if normalized.startswith("insert into retrieval_audits"):
            (
                audit_id,
                trace_id,
                session_id,
                space_id,
                question,
                answer,
                sources_json,
                retrieval_json,
                created_at,
            ) = params
            self.database.rows.append(
                {
                    "id": self.database.next_id,
                    "audit_id": audit_id,
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "space_id": space_id,
                    "question": question,
                    "answer": answer,
                    "sources_json": sources_json,
                    "retrieval_json": retrieval_json,
                    "created_at": created_at,
                }
            )
            self.database.next_id += 1
            self.results = []
            return self
        if normalized.startswith("select audit_id"):
            self.results = self._filter_audits(normalized, params)
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self.results)

    def _filter_audits(self, normalized: str, params):
        rows = sorted(self.database.rows, key=lambda row: row["id"], reverse=True)
        if " where " not in normalized:
            limit = params[0]
            return rows[:limit]

        filtered = rows
        param_index = 0
        for field in ("session_id", "space_id", "trace_id"):
            if f"{field} = %s" in normalized:
                expected = params[param_index]
                param_index += 1
                filtered = [row for row in filtered if row[field] == expected]
        limit = params[param_index]
        return filtered[:limit]
