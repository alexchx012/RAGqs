from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.security import auth as auth_module
from app.security.auth import SimpleAuthProvider, get_current_auth_context


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


def test_disabled_auth_still_returns_default_context_and_ignores_cookies():
    provider = SimpleAuthProvider(_settings(auth_enabled=False))

    context = provider.authenticate({}, cookies={"rag_session": "unrelated-token"})

    assert context.user_id == "local-admin"
    assert context.provider == "disabled"


def test_dev_header_auth_ignores_session_cookie_and_still_uses_headers():
    provider = SimpleAuthProvider(
        _settings(auth_enabled=True, auth_provider="dev_header", auth_dev_users="alice:viewer:hr")
    )

    context = provider.authenticate(
        {"x-rag-user": "alice"}, cookies={"rag_session": "unrelated-token"}
    )

    assert context.user_id == "alice"
    assert context.roles == {"viewer"}


def test_reverse_proxy_auth_ignores_session_cookie_and_still_uses_headers():
    provider = SimpleAuthProvider(_settings(auth_enabled=True, auth_provider="reverse_proxy"))

    context = provider.authenticate(
        {"x-rag-user": "carol", "x-rag-roles": "maintainer", "x-rag-spaces": "ops"},
        cookies={"rag_session": "unrelated-token"},
    )

    assert context.user_id == "carol"
    assert context.roles == {"maintainer"}
    assert context.spaces == {"ops"}


def test_get_current_auth_context_still_works_for_dev_header_provider(monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "config",
        _settings(auth_enabled=True, auth_provider="dev_header", auth_dev_users="alice:viewer:hr"),
    )
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(auth_context=Depends(get_current_auth_context)):
        return {"user_id": auth_context.user_id}

    response = TestClient(app).get("/whoami", headers={"X-RAG-User": "alice"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "alice"


def test_unsupported_provider_still_raises_500():
    provider = SimpleAuthProvider(_settings(auth_enabled=True, auth_provider="bogus"))

    raised = None
    try:
        provider.authenticate({})
    except Exception as exc:
        raised = exc

    assert raised is not None
    assert getattr(raised, "status_code", None) == 500


def test_local_credentials_without_cookie_raises_401():
    provider = SimpleAuthProvider(
        _settings(auth_enabled=True, auth_provider="local_credentials")
    )

    raised = None
    try:
        provider.authenticate({}, cookies={})
    except Exception as exc:
        raised = exc

    assert raised is not None
    assert getattr(raised, "status_code", None) == 401
