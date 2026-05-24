from app.providers import SessionStoreProvider
from app.providers.sqlite_session import SQLiteSessionStoreProvider


def test_sqlite_session_store_persists_messages_and_searchable_summaries(tmp_path):
    db_path = tmp_path / "sessions.db"
    first_store = SQLiteSessionStoreProvider(db_path)
    first_store.append_message("s1", "user", "What is durable RAG history?")
    first_store.append_message("s1", "assistant", "SQLite keeps the session transcript.")
    first_store.append_message("s2", "user", "Another topic")
    first_store.close()

    second_store = SQLiteSessionStoreProvider(db_path)

    assert isinstance(second_store, SessionStoreProvider)
    assert [message.content for message in second_store.get_messages("s1")] == [
        "What is durable RAG history?",
        "SQLite keeps the session transcript.",
    ]
    assert [summary.session_id for summary in second_store.list_sessions()] == ["s2", "s1"]
    assert [summary.session_id for summary in second_store.list_sessions(query="transcript")] == [
        "s1"
    ]
    assert second_store.clear("s1") is True
    assert second_store.get_messages("s1") == []
    second_store.close()
