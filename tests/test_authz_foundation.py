from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import chat as chat_api
from app.api import file as file_api
from app.providers import SessionSummary
from app.security import auth as auth_module
from app.security.auth import AuthContext, SimpleAuthProvider, require_space_access


def test_super_admin_role_grants_wildcard_permission():
    context = AuthContext(user_id="root", roles={"super_admin"}, spaces=set())

    assert context.has_permission("document:delete")
    assert context.has_permission("user:manage")


def test_department_admin_role_only_grants_user_manage():
    context = AuthContext(user_id="dept-lead", roles={"department_admin"}, spaces=set())

    assert context.has_permission("user:manage")
    assert not context.has_permission("document:delete")
    assert not context.has_permission("chat:write")


def test_department_admin_role_grants_space_read_and_manage():
    context = AuthContext(user_id="dept-lead", roles={"department_admin"}, spaces=set())

    assert context.has_permission("space:read")
    assert context.has_permission("space:manage")
    assert not context.has_permission("space:write")


def test_disabled_auth_returns_local_admin_for_compatibility():
    provider = SimpleAuthProvider(_settings(auth_enabled=False))

    context = provider.authenticate({})

    assert context.user_id == "local-admin"
    assert context.roles == {"super_admin"}
    assert context.spaces == {"*"}
    assert context.has_permission("chat:write")
    assert context.has_permission("document:delete")
    require_space_access(context, "finance")


def test_dev_header_auth_maps_roles_and_spaces():
    provider = SimpleAuthProvider(
        _settings(
            auth_enabled=True,
            auth_provider="dev_header",
            auth_dev_users="alice:viewer|uploader:hr|finance",
        )
    )

    context = provider.authenticate({"x-rag-user": "alice"})

    assert context.user_id == "alice"
    assert context.roles == {"viewer", "uploader"}
    assert context.spaces == {"hr", "finance"}
    assert context.has_permission("chat:write")
    assert context.has_permission("document:upload")


def test_space_check_rejects_unassigned_space():
    provider = SimpleAuthProvider(
        _settings(
            auth_enabled=True,
            auth_provider="dev_header",
            auth_dev_users="alice:viewer:hr",
        )
    )
    context = provider.authenticate({"x-rag-user": "alice"})

    response = _client_error(lambda: require_space_access(context, "finance"))

    assert response.status_code == 403
    assert response.detail == "user is not allowed to access knowledge space: finance"


def test_chat_api_denies_space_from_client_when_user_lacks_access(monkeypatch):
    _install_auth_settings(
        monkeypatch,
        auth_enabled=True,
        auth_provider="dev_header",
        auth_dev_users="alice:viewer:hr",
    )
    monkeypatch.setattr(
        chat_api,
        "rag_agent_service",
        SimpleNamespace(
            query_with_trace=lambda *args, **kwargs: {
                "answer": "should not run",
                "sources": [],
                "retrieval": {"debug": {}},
            }
        ),
    )
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api")

    response = TestClient(app).post(
        "/api/chat",
        headers={"X-RAG-User": "alice"},
        json={"Id": "s1", "Question": "policy", "spaceId": "finance"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "user is not allowed to access knowledge space: finance"


def test_upload_api_denies_users_without_upload_permission(monkeypatch):
    _install_auth_settings(
        monkeypatch,
        auth_enabled=True,
        auth_provider="dev_header",
        auth_dev_users="viewer:viewer:finance",
    )
    app = FastAPI()
    app.include_router(file_api.router, prefix="/api")

    response = TestClient(app).post(
        "/api/upload?space_id=finance",
        headers={"X-RAG-User": "viewer"},
        files={"file": ("guide.md", b"# Guide", "text/markdown")},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "missing permission: document:upload"


def test_document_delete_api_denies_users_without_document_delete_permission(monkeypatch):
    _install_auth_settings(
        monkeypatch,
        auth_enabled=True,
        auth_provider="dev_header",
        auth_dev_users="viewer:viewer:finance",
    )
    service = SimpleNamespace(
        delete_document=lambda space_id, document_id: (_ for _ in ()).throw(
            AssertionError("delete should not run")
        )
    )
    monkeypatch.setattr(file_api, "vector_index_service", service)
    app = FastAPI()
    app.include_router(file_api.router, prefix="/api")

    response = TestClient(app).delete(
        "/api/knowledge-spaces/finance/documents/doc-1",
        headers={"X-RAG-User": "viewer"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "missing permission: document:delete"


def test_audit_api_denies_users_without_audit_permission(monkeypatch):
    _install_auth_settings(
        monkeypatch,
        auth_enabled=True,
        auth_provider="dev_header",
        auth_dev_users="viewer:viewer:finance",
    )
    monkeypatch.setattr(
        chat_api,
        "rag_agent_service",
        SimpleNamespace(
            list_retrieval_audits=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("audit query should not run")
            )
        ),
    )
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api")

    response = TestClient(app).get(
        "/api/chat/audits?space_id=finance",
        headers={"X-RAG-User": "viewer"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "missing permission: audit:read"


def test_session_routes_filter_and_deny_by_session_space(monkeypatch):
    _install_auth_settings(
        monkeypatch,
        auth_enabled=True,
        auth_provider="dev_header",
        auth_dev_users="alice:viewer:finance",
    )
    service = FakeSessionScopedRagService()
    monkeypatch.setattr(chat_api, "rag_agent_service", service)
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api")
    client = TestClient(app)

    list_response = client.get("/api/chat/sessions", headers={"X-RAG-User": "alice"})

    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["data"]["sessions"]] == [
        "finance-session"
    ]

    detail_response = client.get(
        "/api/chat/session/hr-session",
        headers={"X-RAG-User": "alice"},
    )
    clear_response = client.post(
        "/api/chat/clear",
        headers={"X-RAG-User": "alice"},
        json={"sessionId": "hr-session"},
    )

    assert detail_response.status_code == 403
    assert clear_response.status_code == 403
    assert service.cleared_sessions == []


def _install_auth_settings(monkeypatch, **overrides):
    settings = _settings(**overrides)
    monkeypatch.setattr(auth_module, "config", settings)


def _settings(**overrides):
    values = {
        "auth_enabled": False,
        "auth_provider": "dev_header",
        "auth_user_header": "X-RAG-User",
        "auth_roles_header": "X-RAG-Roles",
        "auth_spaces_header": "X-RAG-Spaces",
        "auth_dev_users": "local-admin:super_admin:*",
        "auth_default_user_id": "local-admin",
        "auth_default_roles": "super_admin",
        "auth_default_spaces": "*",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client_error(callback):
    try:
        callback()
    except Exception as exc:
        return exc
    raise AssertionError("expected exception")


class FakeSessionScopedRagService:
    def __init__(self):
        self.cleared_sessions = []
        self.spaces = {
            "finance-session": {"finance"},
            "hr-session": {"hr"},
        }

    def list_sessions(self, query=None, allowed_space_ids=None):
        summaries = [
            SessionSummary(
                session_id="finance-session",
                title="Finance",
                message_count=2,
                updated_at="2026-05-27T00:00:00Z",
            ),
            SessionSummary(
                session_id="hr-session",
                title="HR",
                message_count=2,
                updated_at="2026-05-27T00:00:00Z",
            ),
        ]
        if allowed_space_ids is None or "*" in allowed_space_ids:
            return summaries
        allowed = set(allowed_space_ids)
        return [
            summary
            for summary in summaries
            if self.spaces.get(summary.session_id, {"default"}).issubset(allowed)
        ]

    def session_space_ids(self, session_id):
        return self.spaces.get(session_id, {"default"})

    def get_session_history(self, session_id):
        return [{"role": "user", "content": session_id, "metadata": {}}]

    def clear_session(self, session_id):
        self.cleared_sessions.append(session_id)
        return True
