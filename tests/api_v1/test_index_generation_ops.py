"""`POST /v1/ops/index-generations/rollback` route contract tests (A10).

tests/api_v1 fixture style: hand-built engine/adapters injected into
build_runtime + create_platform_app. Window/catch-up/receipt checks stay inside
IndexGenerationRepository.rollback; these tests verify the HTTP layer forwards
the call and preserves the repository error codes: ops rollback succeeds for an
in-window candidate, non-ops gets 403, missing Idempotency-Key 422,
expired-window / stale-receipt candidates 409 rollback_not_eligible, unknown
body fields 422.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from app.documents.schema import documents_metadata
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.indexing.release_gates import RetrievalReleaseGateService
from app.indexing.schema import index_generations_table, indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime


class _NullObjectStore:
    def exists(self, key: str) -> bool:
        return False


def _gate_metrics() -> list[dict[str, object]]:
    below = [
        {
            "metric": name,
            "direction": "below",
            "absolute_threshold": threshold,
            "allowed_regression": 0.0,
            "min_samples": 1,
            "aggregation": aggregation,
            "severity": "blocking",
        }
        for name, threshold, aggregation in (
            ("p50_ms", 10, "p50"),
            ("p95_ms", 20, "p95"),
            ("p99_ms", 30, "p99"),
            ("error_rate", 1, "rate"),
            ("vram_mb", 10, "max"),
        )
    ]
    above = [
        {
            "metric": name,
            "direction": "above",
            "absolute_threshold": threshold,
            "allowed_regression": 0.0,
            "min_samples": 1,
            "aggregation": "mean",
            "severity": "blocking",
        }
        for name, threshold in (
            ("hit_at_k", 0.8),
            ("mrr", 0.8),
            ("ndcg", 0.8),
            ("refusal", 0.9),
        )
    ]
    return below + above


def _suite() -> dict[str, object]:
    return {
        "acl_assertions": {"space_isolation": "passed"},
        "hardware_profile": {"accelerator": "test"},
        "thresholds": {"p50_ms": 10, "p95_ms": 20, "p99_ms": 30, "error_rate": 1, "vram_mb": 10},
        "samples": {
            name: [{"sample_id": f"{name}-1", "input": name, "expected": "pass"}]
            for name in (
                "phrase_query",
                "proper_noun_query",
                "quoted_exact_query",
                "real_question",
                "acl_filter",
                "sparse_exact_hit",
                "refusal",
                "source_conflict",
            )
        },
        "quality_thresholds": {"hit_at_k": 0.8, "mrr": 0.8, "ndcg": 0.8, "refusal": 0.9},
    }


def _metrics() -> dict[str, float]:
    return {
        "p50_ms": 1,
        "p95_ms": 2,
        "p99_ms": 3,
        "error_rate": 0,
        "vram_mb": 1,
        "hit_at_k": 0.9,
        "mrr": 0.9,
        "ndcg": 0.9,
        "refusal": 1.0,
    }


def make_client(*, role: str):
    from app.indexing import RetrievalProfile, RetrievalReleaseService

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
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    identity = IdentityAccessService(engine, settings.auth)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "object_store": _NullObjectStore(),
        },
    )
    identity.provision_user(
        username="caller",
        password="Password1",
        real_name="caller",
        display_name="caller",
        role=role,
        department_id=None,
    )
    login = identity.login(username="caller", password="Password1")

    # Candidate and successor each hold one released retrieval profile: the
    # real runtime wiring attaches is_released_for_generation to the rollback
    # and activation paths.
    manager = runtime.resolve("indexing_generation_manager")
    releases = RetrievalReleaseService(engine)
    gate = RetrievalReleaseGateService(engine).register(
        version="gate_1",
        hardware_profile={"accelerator": "test"},
        concurrency=1,
        metrics=_gate_metrics(),
    )
    staged = releases.stage(
        generation_id=manager.active_generation_id,
        profile=RetrievalProfile(),
        acceptance_suite=_suite(),
        gate_version_id=str(gate["id"]),
    )
    releases.release(
        str(staged["id"]), metrics=_metrics(), hardware_profile=_suite()["hardware_profile"]
    )
    manager.create_staging([], generation_id="generation_next")
    staged_next = releases.stage(
        generation_id="generation_next",
        profile=RetrievalProfile(),
        acceptance_suite=_suite(),
        gate_version_id=str(gate["id"]),
    )
    releases.release(
        str(staged_next["id"]), metrics=_metrics(), hardware_profile=_suite()["hardware_profile"]
    )
    manager.release("generation_next")

    app = create_platform_app(settings, runtime=runtime)
    client = TestClient(app)
    return client, engine, f"Bearer {login.access_token}", runtime


def _rollback_body(**overrides: Any) -> dict[str, object]:
    body: dict[str, object] = {
        "candidate_generation_id": "generation_initial",
        "source_receipt": {
            "state": "held",
            "candidate_generation_id": "generation_initial",
            "applied_revision": 0,
        },
    }
    body.update(overrides)
    return body


def test_ops_rollback_restores_candidate_inside_window() -> None:
    client, engine, ops_token, runtime = make_client(role="ops")
    try:
        response = client.post(
            "/v1/ops/index-generations/rollback",
            json=_rollback_body(),
            headers={"Authorization": ops_token, "Idempotency-Key": "rollback-1"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["generation_id"] == "generation_initial"
        assert body["status"] == "active"
        assert body["rollback_applied_revision"] == 0
        with engine.connect() as connection:
            initial_status = connection.execute(
                select(index_generations_table.c.status).where(
                    index_generations_table.c.id == "generation_initial"
                )
            ).scalar_one()
            successor_status = connection.execute(
                select(index_generations_table.c.status).where(
                    index_generations_table.c.id == "generation_next"
                )
            ).scalar_one()
        assert (initial_status, successor_status) == ("active", "retired")
    finally:
        runtime.close()


def test_ops_rollback_requires_ops_role_and_idempotency_key() -> None:
    client, _, user_token, runtime = make_client(role="user")
    try:
        forbidden = client.post(
            "/v1/ops/index-generations/rollback",
            json=_rollback_body(),
            headers={"Authorization": user_token, "Idempotency-Key": "rollback-2"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "forbidden"
    finally:
        runtime.close()

    client, _, ops_token, runtime = make_client(role="ops")
    try:
        missing_key = client.post(
            "/v1/ops/index-generations/rollback",
            json=_rollback_body(),
            headers={"Authorization": ops_token},
        )
        assert missing_key.status_code == 422

        unknown_field = client.post(
            "/v1/ops/index-generations/rollback",
            json={**_rollback_body(), "force": True},
            headers={"Authorization": ops_token, "Idempotency-Key": "rollback-3"},
        )
        assert unknown_field.status_code == 422
    finally:
        runtime.close()


def test_ops_rollback_rejects_expired_window_candidate() -> None:
    client, engine, ops_token, runtime = make_client(role="ops")
    try:
        # 与 tests/indexing 的 GC 用例同法：直接回写候选的回滚窗口为已过窗。
        with engine.begin() as connection:
            connection.execute(
                update(index_generations_table)
                .where(index_generations_table.c.id == "generation_initial")
                .values(rollback_until_utc=datetime.now(UTC) - timedelta(days=1))
            )

        response = client.post(
            "/v1/ops/index-generations/rollback",
            json=_rollback_body(),
            headers={"Authorization": ops_token, "Idempotency-Key": "rollback-4"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "rollback_not_eligible"
    finally:
        runtime.close()


def test_ops_rollback_rejects_receipt_that_is_not_caught_up() -> None:
    client, _, ops_token, runtime = make_client(role="ops")
    try:
        stale_receipt = _rollback_body(
            source_receipt={
                "state": "held",
                "candidate_generation_id": "generation_initial",
                "applied_revision": 99,
            }
        )
        response = client.post(
            "/v1/ops/index-generations/rollback",
            json=stale_receipt,
            headers={"Authorization": ops_token, "Idempotency-Key": "rollback-5"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "rollback_not_eligible"
    finally:
        runtime.close()
