from app.providers import SessionStoreProvider
from app.providers.postgres_session import PostgresSessionStoreProvider


def test_postgres_session_store_persists_messages_and_searchable_summaries():
    database = FakePostgresDatabase()
    store = PostgresSessionStoreProvider(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )

    store.append_message("s1", "user", "What is production RAG history?")
    store.append_message(
        "s1",
        "assistant",
        "Postgres keeps transcripts for multi-instance deployments.",
        {"source": "postgres"},
    )
    store.append_message("s2", "user", "Another topic")

    assert isinstance(store, SessionStoreProvider)
    assert database.dsns == ["postgresql://rag:secret@db/ragqs"]
    assert [message.content for message in store.get_messages("s1")] == [
        "What is production RAG history?",
        "Postgres keeps transcripts for multi-instance deployments.",
    ]
    assert store.get_messages("s1")[1].metadata == {"source": "postgres"}
    assert [summary.session_id for summary in store.list_sessions()] == ["s2", "s1"]
    assert [summary.session_id for summary in store.list_sessions(query="production")] == [
        "s1"
    ]
    assert store.clear("s1") is True
    assert store.get_messages("s1") == []


def test_postgres_session_store_defers_connection_until_first_operation():
    database = FakePostgresDatabase()

    store = PostgresSessionStoreProvider(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )

    assert database.connect_count == 0

    store.list_sessions()

    assert database.connect_count == 1


class FakePostgresDatabase:
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
    def __init__(self, database: FakePostgresDatabase):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeCursor(self.database)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, database: FakePostgresDatabase):
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
        if normalized.startswith("insert into session_messages"):
            session_id, role, content, metadata_json, created_at = params
            self.database.rows.append(
                {
                    "id": self.database.next_id,
                    "session_id": session_id,
                    "role": role,
                    "content": content,
                    "metadata_json": metadata_json,
                    "created_at": created_at,
                }
            )
            self.database.next_id += 1
            self.results = []
            return self
        if normalized.startswith("select role, content, metadata_json, created_at"):
            session_id = params[0]
            self.results = [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "metadata_json": row["metadata_json"],
                    "created_at": row["created_at"],
                }
                for row in sorted(self.database.rows, key=lambda item: item["id"])
                if row["session_id"] == session_id
            ]
            return self
        if normalized.startswith("select id, session_id"):
            self.results = sorted(self.database.rows, key=lambda item: item["id"])
            return self
        if normalized.startswith("delete from session_messages"):
            session_id = params[0]
            self.database.rows = [
                row for row in self.database.rows if row["session_id"] != session_id
            ]
            self.results = []
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self.results)
