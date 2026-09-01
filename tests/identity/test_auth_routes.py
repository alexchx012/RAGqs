from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
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
    outbox_metadata.create_all(engine)
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
    outbox_metadata.create_all(engine)
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
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    object_store = MemoryObjectStore()
    service = IdentityAccessService(engine, configured.auth, object_store=object_store)
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
        # 对象存储继续保存内部 object key，API 响应恒为可加载的相对路径（A12）。
        stored_keys = list(object_store._objects)
        content = client.get(
            "/v1/users/me/avatar",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["avatar_url"] == "/v1/users/me/avatar"
    assert all(key.startswith("avatars/") for key in stored_keys)
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/png"
    assert content.content == b"fake-png"


def test_avatar_route_returns_404_without_an_avatar() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
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
        response = client.get(
            "/v1/users/me/avatar",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "avatar_not_found"


def test_avatar_route_rejects_oversized_files_with_413_without_reading_too_much() -> None:
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
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
        ok = client.post(
            "/v1/users/me/avatar",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("avatar.png", b"fake-png", "image/png")},
        )
        oversized = client.post(
            "/v1/users/me/avatar",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("avatar.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")},
        )

    assert ok.status_code == 200
    assert oversized.status_code == 413
    error = oversized.json()["error"]
    assert error["code"] == "upload_too_large"
    assert error["details"]["max_bytes"] == 5 * 1024 * 1024


def test_application_startup_reconciles_the_declared_admin_roster() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    initial = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
        ),
        revocation_port=NoopGenerationRevocationPort(),
    )
    retained = initial.provision_user(
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
    configured = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
            "RAG_AUTH_ADMIN_ROSTER": str(retained["id"]),
        }
    )
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
    outbox_metadata.create_all(engine)
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


def _avatar_client():
    configured = settings()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    object_store = MemoryObjectStore()
    service = IdentityAccessService(
        engine,
        configured.auth,
        object_store=object_store,
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
    return service, app


def test_avatar_route_accepts_the_session_cookie_without_an_authorization_header() -> None:
    service, app = _avatar_client()

    with TestClient(app) as client:
        login = client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        token = login.json()["token"]
        assert login.cookies.get("refresh_token")
        client.post(
            "/v1/users/me/avatar",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("avatar.png", b"fake-png", "image/png")},
        )
        # 同源 <img src="/v1/users/me/avatar"> 无法携带 Authorization 头：
        # 仅凭会话 Cookie 即可取回自己的头像。
        cookie_only = client.get("/v1/users/me/avatar")
        bearer = client.get("/v1/users/me/avatar", headers={"Authorization": f"Bearer {token}"})
        invalid_bearer = client.get(
            "/v1/users/me/avatar", headers={"Authorization": "Bearer not-a-token"}
        )
        # 撤销该会话后，同一 Cookie 不再是有效凭据。
        principal = service.authenticate_access_token(token)
        service.revoke_session(
            user_id=principal.user_id,
            session_id=principal.auth_session_id,
            reason="user_logout",
        )
        revoked_cookie = client.get("/v1/users/me/avatar")
        client.cookies.clear()
        anonymous = client.get("/v1/users/me/avatar")

    assert cookie_only.status_code == 200
    assert cookie_only.headers["content-type"] == "image/png"
    assert cookie_only.content == b"fake-png"
    assert bearer.status_code == 200
    assert bearer.content == b"fake-png"
    assert invalid_bearer.status_code == 401
    assert invalid_bearer.json()["error"]["code"] == "authentication_required"
    assert revoked_cookie.status_code == 401
    assert revoked_cookie.json()["error"]["code"] == "session_revoked"
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_required"


def test_avatar_route_cookie_auth_without_an_avatar_still_returns_404() -> None:
    _, app = _avatar_client()

    with TestClient(app) as client:
        client.post("/v1/auth/login", json={"username": "alice", "password": "Password1"})
        response = client.get("/v1/users/me/avatar")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "avatar_not_found"
