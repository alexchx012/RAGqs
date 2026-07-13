from app.security.session_store import SessionStore


def test_create_session_returns_token_bound_to_user(tmp_path):
    store = SessionStore(tmp_path / "auth.sqlite3")

    session = store.create_session("user-1", ttl_seconds=3600)

    assert session.user_id == "user-1"
    assert session.revoked is False
    assert len(session.token) > 20


def test_get_valid_session_returns_session_for_fresh_token(tmp_path):
    store = SessionStore(tmp_path / "auth.sqlite3")
    session = store.create_session("user-1", ttl_seconds=3600)

    assert store.get_valid_session(session.token) == session


def test_get_valid_session_returns_none_for_unknown_token(tmp_path):
    store = SessionStore(tmp_path / "auth.sqlite3")

    assert store.get_valid_session("does-not-exist") is None


def test_get_valid_session_returns_none_for_expired_token(tmp_path):
    store = SessionStore(tmp_path / "auth.sqlite3")
    session = store.create_session("user-1", ttl_seconds=-1)

    assert store.get_valid_session(session.token) is None


def test_get_valid_session_returns_none_after_revoke(tmp_path):
    store = SessionStore(tmp_path / "auth.sqlite3")
    session = store.create_session("user-1", ttl_seconds=3600)

    store.revoke_session(session.token)

    assert store.get_valid_session(session.token) is None


def test_revoke_all_for_user_invalidates_every_session_for_that_user_only(tmp_path):
    store = SessionStore(tmp_path / "auth.sqlite3")
    first = store.create_session("user-1", ttl_seconds=3600)
    second = store.create_session("user-1", ttl_seconds=3600)
    other_user = store.create_session("user-2", ttl_seconds=3600)

    store.revoke_all_for_user("user-1")

    assert store.get_valid_session(first.token) is None
    assert store.get_valid_session(second.token) is None
    assert store.get_valid_session(other_user.token) is not None
