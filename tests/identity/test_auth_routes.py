from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.platform.app_factory import create_platform_app
from app.platform.config import AuthSettings, load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.platform.storage import MemoryObjectStore
from app.usage.schema import usage_metadata


def settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
        }
    )


def test_auth_routes_issue_refresh_and_reject_a_logged_out_access_token() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        login = client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        assert login.status_code == 200
        assert set(login.json()) == {"token", "user"}
        assert login.cookies.get("refresh_token")
        csrf_token = login.cookies.get("csrf_token")
        assert csrf_token

        token = login.json()["token"]
        me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["username"] == "alice"

        sessions = client.get("/v1/auth/sessions", headers={"Authorization": f"Bearer {token}"})
        assert sessions.status_code == 200
        assert sessions.json()["items"][0]["current"] is True

        profile = client.patch(
            "/v1/users/me/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={"display_name": "Alice Smith"},
        )
        assert profile.status_code == 200
        assert profile.json()["display_name"] == "Alice Smith"

        preferences = client.put(
            "/v1/users/me/preferences",
            headers={"Authorization": f"Bearer {token}"},
            json={"theme": "dark", "chat_font_size": "large", "ab_opt_out": True},
        )
        assert preferences.status_code == 200
        assert preferences.json()["theme"] == "dark"
        assert (
            client.get(
                "/v1/users/me/preferences", headers={"Authorization": f"Bearer {token}"}
            ).json()
            == preferences.json()
        )

        refreshed = client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf_token})
        assert refreshed.status_code == 200
        assert set(refreshed.json()) == {"token"}

        logged_out = client.post(
            "/v1/auth/logout",
            headers={"Authorization": f"Bearer {refreshed.json()['token']}"},
        )
        assert logged_out.status_code == 204
        repeated_logout = client.post(
            "/v1/auth/logout",
            headers={"Authorization": f"Bearer {refreshed.json()['token']}"},
        )
        assert repeated_logout.status_code == 204

        revoked = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {refreshed.json()['token']}"},
        )
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "session_revoked"


def test_current_session_deletion_is_idempotent() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        login = client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        token = login.json()["token"]
        session_id = service.authenticate_access_token(token).auth_session_id
        first = client.delete(
            f"/v1/auth/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        repeated = client.delete(
            f"/v1/auth/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first.status_code == 204
    assert repeated.status_code == 204


def test_avatar_route_replaces_the_current_users_avatar() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    service = IdentityAccessService(engine, configured.auth, object_store=MemoryObjectStore())
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    token = service.login(username="alice", password="Password1").access_token
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/users/me/avatar",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("avatar.png", b"fake-png", "image/png")},
        )

    assert response.status_code == 200
    assert response.json()["avatar_url"].startswith("object://avatars/")


def test_application_startup_reconciles_the_declared_admin_roster() -> None:
    configured = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
            "RAG_AUTH_ADMIN_ROSTER": "retained",
        }
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    initial = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            admin_roster=("retained", "removed"),
        ),
        revocation_port=NoopGenerationRevocationPort(),
    )
    initial.provision_user(
        username="retained",
        password="Password1",
        real_name="Retained",
        display_name="Retained",
        role="admin",
        department_id=None,
    )
    initial.provision_user(
        username="removed",
        password="Password1",
        real_name="Removed",
        display_name="Removed",
        role="admin",
        department_id=None,
    )
    removed_token = initial.login(username="removed", password="Password1").access_token
    service = IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
    )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        response = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {removed_token}"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_revoked"


def _cookie_secure_env() -> dict[str, str]:
    return {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
    }


def _login_client(env: dict[str, str]) -> TestClient:
    configured = load_platform_settings(env)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": service},
    )
    app = create_platform_app(configured, runtime=runtime)
    client = TestClient(app)
    client.__enter__()
    return client


def test_cookie_secure_follows_explicit_configuration() -> None:
    with _login_client(_cookie_secure_env() | {"RAG_AUTH_COOKIE_SECURE": "true"}) as client:
        login = client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        assert login.status_code == 200
        cookies = "; ".join(login.headers.get_list("set-cookie"))
        assert "Secure" in cookies

    with _login_client(_cookie_secure_env() | {"RAG_AUTH_COOKIE_SECURE": "false"}) as client:
        login = client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        assert login.status_code == 200
        cookies = "; ".join(login.headers.get_list("set-cookie"))
        assert "Secure" not in cookies


def test_cookie_secure_defaults_to_profile_in_development() -> None:
    with _login_client(_cookie_secure_env()) as client:
        login = client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        assert login.status_code == 200
        cookies = "; ".join(login.headers.get_list("set-cookie"))
        assert "Secure" not in cookies
