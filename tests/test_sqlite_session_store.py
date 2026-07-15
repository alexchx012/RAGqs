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
    assert [summary.session_id for summary in second_store.list_sessions(query="durable")] == [
        "s1"
    ]
    assert second_store.clear("s1") is True
    assert second_store.get_messages("s1") == []
    second_store.close()


def test_sqlite_session_store_searches_by_title_not_message_body(tmp_path):
    """Sidebar search matches visible titles; body-only hits must not surface."""
    store = SQLiteSessionStoreProvider(tmp_path / "sessions-title.db")
    # Three chats titled 你好, one titled policy. Assistant bodies contain decoys.
    store.append_message("h1", "user", "你好")
    store.append_message("h1", "assistant", "关于 policy 的说明包含字母 p")
    store.append_message("h2", "user", "你好")
    store.append_message("h2", "assistant", "另一段回复")
    store.append_message("h3", "user", "你好")
    store.append_message("h3", "assistant", "第三段回复")
    store.append_message("p1", "user", "policy")
    store.append_message("p1", "assistant", "policy 详情")

    assert [s.session_id for s in store.list_sessions(query="policy")] == ["p1"]
    assert [s.session_id for s in store.list_sessions(query="p")] == ["p1"]
    assert sorted(s.session_id for s in store.list_sessions(query="你")) == ["h1", "h2", "h3"]
    assert sorted(s.session_id for s in store.list_sessions(query="你好")) == ["h1", "h2", "h3"]
    # Body-only keyword must not match
    assert store.list_sessions(query="说明") == []
    store.close()
