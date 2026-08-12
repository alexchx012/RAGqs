"""HTTP contract tests for the public graph build ops API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.documents.schema import documents_metadata
from app.graph.schema import graph_metadata
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.usage.schema import usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now

    def __call__(self) -> datetime:
        return self.now


class _AlwaysReleasedRetrieval:
    def is_released_for_generation(self, generation_id: str, connection: Connection) -> bool:
        del generation_id, connection
        return True

    def resolve(self, profile: object, generation_id: str) -> object:
        del generation_id
        return profile


class _RecordingUsage:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def prepare_provider_call(self, **kwargs: object) -> str:
        call_id = f"pcall_{len(self.calls)}"
        self.calls.append({"phase": "prepared", "call_id": call_id, **kwargs})
        return call_id

    def mark_dispatching(self, provider_call_id: str, **kwargs: object) -> bool:
        del kwargs
        return True

    def complete_provider_call(self, **kwargs: object) -> str:
        self.calls.append({"phase": "completed", **kwargs})
        return str(kwargs["provider_call_id"])

    def mark_not_sent(self, provider_call_id: str) -> None:
        self.calls.append({"phase": "not_sent", "call_id": provider_call_id})

    def mark_unknown(self, provider_call_id: str) -> None:
        self.calls.append({"phase": "unknown", "call_id": provider_call_id})


class _NullObjectStore:
    def exists(self, key: str) -> bool:
        return False


def _make_client():
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
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
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    graph_metadata.create_all(engine)
    clock = _FixedClock(NOW)
    from app.identity.service import IdentityAccessService

    identity = IdentityAccessService(engine, settings.auth)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "identity_access": identity,
            "object_store": _NullObjectStore(),
            "retrieval_release_service": _AlwaysReleasedRetrieval(),
            "graph_usage_submission": _RecordingUsage(),
        },
    )
    return TestClient(create_platform_app(settings, runtime=runtime)), runtime


def _seed_user(runtime, role: str, username: str) -> str:
    identity = runtime.resolve("identity_access")
    identity.provision_user(
        username=username,
        password="Password1",
        real_name=username,
        display_name=username,
        role=role,
        department_id=None,
    )
    login = identity.login(username=username, password="Password1")
    return f"Bearer {login.access_token}"


def _publish(runtime) -> None:
    source = runtime.resolve("public_graph_source_service")
    source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[
            {
                "document_id": "doc_1",
                "document_version_id": "ver_1",
                "publication_id": "pub_1",
                "content_manifest_id": "manifest_1",
                "content_manifest_hash": "hash_1",
            }
        ],
    )


def test_role_gating_and_create_contract() -> None:
    client, runtime = _make_client()
    ops_token = _seed_user(runtime, "ops", "ops-user")
    admin_token = _seed_user(runtime, "admin", "admin-user")
    _publish(runtime)
    denied = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 1},
        headers={"Authorization": admin_token, "Idempotency-Key": "g1"},
    )
    assert denied.status_code == 403
    denied_get = client.get("/v1/ops/graph-builds/current", headers={"Authorization": admin_token})
    assert denied_get.status_code == 403
    created = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "g1"},
    )
    assert created.status_code == 202
    body = created.json()
    assert body["state"] == "queued"
    assert body["version"] == 1
    assert body["source_revision"] == 1
    assert body["allowed_actions"] == ["cancel"]
    assert body["actual_usage"] is None
    replay = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "g1"},
    )
    assert replay.status_code == 202
    assert replay.json()["graph_build_id"] == body["graph_build_id"]
    conflict = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "g1-other"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "graph_build_in_progress"
    current = client.get("/v1/ops/graph-builds/current", headers={"Authorization": ops_token})
    assert current.status_code == 200
    assert current.json()["graph_availability"] == "disabled"
    assert current.json()["latest_run"]["state"] == "queued"


def test_wrong_revision_and_empty_source_errors() -> None:
    client, runtime = _make_client()
    ops_token = _seed_user(runtime, "ops", "ops-user")
    wrong = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 42},
        headers={"Authorization": ops_token, "Idempotency-Key": "w1"},
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "graph_source_empty"
    _publish(runtime)
    mismatch = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 42},
        headers={"Authorization": ops_token, "Idempotency-Key": "w2"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "graph_source_changed"


def test_cancel_contract_over_http() -> None:
    client, runtime = _make_client()
    ops_token = _seed_user(runtime, "ops", "ops-user")
    _publish(runtime)
    created = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "c1"},
    ).json()
    conflict = client.post(
        f"/v1/ops/graph-builds/{created['graph_build_id']}/cancel",
        json={"expected_version": 5},
        headers={"Authorization": ops_token, "Idempotency-Key": "c2"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "version_conflict"
    cancelled = client.post(
        f"/v1/ops/graph-builds/{created['graph_build_id']}/cancel",
        json={"expected_version": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "c2-success"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["version"] == 2
    not_cancellable = client.post(
        f"/v1/ops/graph-builds/{created['graph_build_id']}/cancel",
        json={"expected_version": 2},
        headers={"Authorization": ops_token, "Idempotency-Key": "c3"},
    )
    assert not_cancellable.status_code == 409
    assert not_cancellable.json()["error"]["code"] == "graph_build_not_cancellable"
    missing = client.post(
        "/v1/ops/graph-builds/unknown_run/cancel",
        json={"expected_version": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "c4"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "graph_build_not_found"


def test_worker_full_loop_via_runtime() -> None:
    client, runtime = _make_client()
    ops_token = _seed_user(runtime, "ops", "ops-user")
    _publish(runtime)
    created = client.post(
        "/v1/ops/graph-builds",
        json={"expected_source_revision": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "f1"},
    ).json()
    worker = runtime.resolve("graph_build_worker")
    stats = worker.run_once()
    assert stats.builds_processed == 1
    assert stats.runs_failed == 0
    current = client.get("/v1/ops/graph-builds/current", headers={"Authorization": ops_token})
    assert current.status_code == 200
    payload = current.json()
    assert payload["graph_availability"] == "ready"
    assert payload["active_generation"]["graph_generation_id"].startswith("graph_generation_")
    assert payload["latest_run"]["state"] == "succeeded"
    assert (
        payload["latest_run"]["graph_generation_id"]
        == payload["active_generation"]["graph_generation_id"]
    )
    assert payload["latest_run"]["graph_build_id"] == created["graph_build_id"]
    assert payload["latest_run"]["actual_usage"]["primary_model_calls"] == 1
