"""Runtime wiring for evaluation adapters (default chat_calibration_port)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine

from app.evaluation import OnlineAnswerReplayAdapter, UnavailableAnswerReplayPort
from app.evaluation.calibration_port import EvaluationCalibrationWindowPort
from app.platform.config import load_platform_settings
from app.platform.runtime import build_runtime
from app.usage.budget import BudgetEffortPolicy, BudgetMeterPolicy, BudgetMeterService

from .conftest import build_test_env


def test_runtime_wires_evaluation_adapters() -> None:
    env = build_test_env()
    runtime = env["runtime"]
    assert runtime.resolve("evaluation_repository") is not None
    assert runtime.resolve("evaluation_service") is not None
    assert runtime.resolve("judge_provider") is not None
    assert runtime.resolve("calibration_outbox_port") is not None
    assert runtime.resolve("evaluation_worker") is not None
    assert runtime.resolve("calibration_close_worker") is not None
    assert runtime.resolve("evaluation_usage_submission") is not None


def test_default_chat_calibration_port_is_evaluation_port() -> None:
    env = build_test_env()
    runtime = env["runtime"]
    port = runtime.resolve("chat_calibration_port")
    assert isinstance(port, EvaluationCalibrationWindowPort)


def test_table_missing_get_open_window_returns_none() -> None:
    env = build_test_env()
    # Build a separate engine with NO evaluation tables.
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.chat.schema import chat_metadata
    from app.identity.schema import identity_metadata
    from app.outbox.schema import outbox_metadata
    from app.platform.database import core_metadata

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    port = EvaluationCalibrationWindowPort(engine)
    with engine.connect() as connection:
        assert port.get_open_window(connection, now=env["clock"].now, user_id="u1") is None
    engine.dispose()


class _ExplicitDenseWriter:
    provider_name = "configured-dense"

    def stage_chunks(self, *args, **kwargs):
        del args, kwargs

    def publish_staged(self, *args, **kwargs):
        del args, kwargs

    def discard_staged(self, *args, **kwargs):
        del args, kwargs

    def delete_document_version(self, *args, **kwargs):
        del args, kwargs
        return 0

    def delete_document(self, *args, **kwargs):
        del args, kwargs
        return 0

    def search(self, *args, **kwargs):
        del args, kwargs


class _ExplicitSparseProvider:
    provider_name = "configured-sparse"

    def stage_chunks(self, *args, **kwargs):
        del args, kwargs

    def publish_staged(self, *args, **kwargs):
        del args, kwargs

    def discard_staged(self, *args, **kwargs):
        del args, kwargs

    def delete_document_version(self, *args, **kwargs):
        del args, kwargs
        return 0

    def delete_document(self, *args, **kwargs):
        del args, kwargs
        return 0

    def search(self, *args, **kwargs):
        del args, kwargs


class _ExplicitReranker:
    def rerank(self, query, candidates, profile):
        del query, profile
        return tuple(candidates), None


class _ExplicitGraphExtractor:
    def estimate_primary_model_calls(self, snapshot):
        del snapshot
        return 0

    def extract(self, snapshot, session):
        del snapshot, session


class _StubJudgeProvider:
    def preflight_probe(self) -> None:
        return None

    def judge(self, request):
        del request
        raise AssertionError("judge must not be invoked during runtime assembly")


class _StubChatProvider:
    def generate(self, request):
        del request
        raise AssertionError("chat provider must not be invoked during runtime assembly")


class _FixedClock:
    def now_utc(self, connection=None) -> datetime:
        del connection
        return datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _production_budget_meter(engine) -> BudgetMeterService:
    policy = BudgetMeterPolicy.production(
        efforts={
            effort: BudgetEffortPolicy(
                max_rag_calls=1,
                max_wall_seconds=20,
                max_total_tokens=12000,
                max_estimated_cost_amount=Decimal("1.0000000000"),
                candidate_document_limit=5,
            )
            for effort in ("quick", "think", "deep")
        },
        price_version_id="price-test",
        currency_code="USD",
        cost_estimator=lambda operation, tokens: Decimal("0.001"),
    )
    return BudgetMeterService(engine, _FixedClock(), policy)


def _development_settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
        }
    )


def _production_settings(archive_dir: str):
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
            "RAG_PROVIDER_NAME": "openai-compatible",
            "RAG_PROVIDER_API_KEY": "provider-secret",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
            "RAG_AUTH_ADMIN_ROSTER": "admin",
            "RAG_EVALUATION_JUDGE_BASE_URL": "https://judge.example.test/v1",
            "RAG_EVALUATION_JUDGE_API_KEY": "judge-api-secret",
            "RAG_BACKUP_TARGET_NAMESPACE": "ragqs-test-backups",
            "USER_DELETION_ARCHIVE_DIR": archive_dir,
        }
    )


def test_development_runtime_defaults_to_unavailable_answer_replay() -> None:
    runtime = build_runtime(_development_settings())
    try:
        assert isinstance(runtime.resolve("answer_replay_port"), UnavailableAnswerReplayPort)
    finally:
        runtime.close()


def test_production_runtime_wires_online_answer_replay_adapter(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    runtime = build_runtime(
        _production_settings(str(archive_dir)),
        adapters={
            "database_engine": engine,
            "indexing_dense_writer": _ExplicitDenseWriter(),
            "indexing_sparse_provider": _ExplicitSparseProvider(),
            "indexing_reranker": _ExplicitReranker(),
            "indexing_token_counter": len,
            "indexing_image_ocr": lambda content, context: "ocr",
            "indexing_image_describer": lambda content, context: "description",
            "graph_build_extractor": _ExplicitGraphExtractor(),
            "generation_budget_meter": _production_budget_meter(engine),
            "judge_provider": _StubJudgeProvider(),
            "chat_provider_port": _StubChatProvider(),
        },
    )
    try:
        answer_replay = runtime.resolve("answer_replay_port")
        assert isinstance(answer_replay, OnlineAnswerReplayAdapter)
        assert answer_replay._provider is runtime.resolve("chat_provider_port")
    finally:
        runtime.close()
