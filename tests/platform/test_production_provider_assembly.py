"""Production provider transport assembly tests (tree search / reranker / graph LLM).

配置提供端点时 runtime 装配三类远程传输实现；未配置时既有默认与生产守卫语义
不变；配置不完整时启动即拒绝。
"""

from __future__ import annotations

import pytest

from app.graph import DeterministicPublicGraphExtractor, LlmPublicGraphExtractor
from app.indexing import (
    DashScopeTreeSearchProvider,
    HttpCrossEncoderReranker,
    PageIndexTreeRouter,
    TwoStageReranker,
)
from app.indexing.retrieval import ScoreReranker
from app.platform.config import load_platform_settings
from app.platform.runtime import build_runtime

_BASE = {
    "RAG_PLATFORM_PROFILE": "development",
    "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
    "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
    "RAG_PROVIDER_NAME": "fake",
}


def _provider_env() -> dict[str, str]:
    return {
        "RAG_INDEX_TREE_SEARCH_PROVIDER": "dashscope",
        "RAG_INDEX_TREE_SEARCH_BASE_URL": "https://dashscope.example.test/v1",
        "RAG_INDEX_TREE_SEARCH_API_KEY": "tree-key",
        "RAG_INDEX_RERANKER_PROVIDER": "vllm",
        "RAG_INDEX_RERANKER_BASE_URL": "https://reranker.example.test",
        "RAG_INDEX_RERANKER_API_KEY": "rerank-key",
        "RAG_INDEX_RERANKER_COARSE_REVISION": "r0.6",
        "RAG_INDEX_RERANKER_FINAL_REVISION": "r8",
        "RAG_GRAPH_EXTRACTION_PROVIDER": "llm",
        "RAG_GRAPH_EXTRACTION_BASE_URL": "https://extraction.example.test/v1",
        "RAG_GRAPH_EXTRACTION_API_KEY": "graph-key",
        "RAG_GRAPH_EXTRACTION_MODEL": "graph-llm-v2",
        "RAG_GRAPH_EXTRACTION_PROMPT_VERSION": "public-graph-v2",
    }


def test_provider_settings_load_from_environment() -> None:
    settings = load_platform_settings({**_BASE, **_provider_env()})
    assert settings.index.tree_search_provider == "dashscope"
    assert settings.index.tree_search_base_url == "https://dashscope.example.test/v1"
    assert settings.index.tree_search_model == "ds-v4-flash"
    assert settings.index.reranker_base_url == "https://reranker.example.test"
    assert settings.index.reranker_coarse_model == "qwen3-reranker-0.6b"
    assert settings.index.reranker_final_model == "qwen3-reranker-8b"
    assert settings.index.reranker_final_revision == "r8"
    assert settings.graph.extraction_provider == "llm"
    assert settings.graph.extraction_model == "graph-llm-v2"
    assert settings.graph.extraction_prompt_version == "public-graph-v2"

    defaults = load_platform_settings(dict(_BASE))
    assert defaults.index.tree_search_provider == "disabled"
    assert defaults.index.reranker_base_url is None
    assert defaults.graph.extraction_provider == "deterministic"


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        (
            {"RAG_INDEX_TREE_SEARCH_PROVIDER": "dashscope"},
            "tree search provider requires a base URL",
        ),
        (
            {"RAG_GRAPH_EXTRACTION_PROVIDER": "llm"},
            "graph LLM extraction provider requires a base URL",
        ),
        (
            {"RAG_INDEX_RERANKER_BASE_URL": "https://reranker.example.test"},
            "configured reranker requires a coarse stage revision",
        ),
    ),
)
def test_incomplete_provider_configuration_rejects_startup(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_platform_settings({**_BASE, **overrides})


def test_runtime_assembles_configured_provider_transports() -> None:
    settings = load_platform_settings({**_BASE, **_provider_env()})
    runtime = build_runtime(settings)
    try:
        tree_provider = runtime.resolve("indexing_tree_search_provider")
        assert isinstance(tree_provider, DashScopeTreeSearchProvider)
        assert tree_provider.provider_name == "ds-v4-flash"
        tree_router = runtime.resolve("indexing_tree_router")
        assert isinstance(tree_router, PageIndexTreeRouter)
        indexing_service = runtime.resolve("indexing_service")
        assert indexing_service.retrieval._tree_router is tree_router

        reranker = runtime.resolve("indexing_reranker")
        assert isinstance(reranker, TwoStageReranker)
        coarse = runtime.resolve("indexing_reranker_coarse_model")
        final = runtime.resolve("indexing_reranker_final_model")
        assert isinstance(coarse, HttpCrossEncoderReranker)
        assert isinstance(final, HttpCrossEncoderReranker)
        release = reranker.release
        assert release.provider == "vllm"
        assert release.coarse_model == "qwen3-reranker-0.6b"
        assert release.coarse_revision == "r0.6"
        assert release.final_model == "qwen3-reranker-8b"
        assert release.final_revision == "r8"

        extractor = runtime.resolve("graph_build_extractor")
        assert isinstance(extractor, LlmPublicGraphExtractor)
        assert not isinstance(extractor, DeterministicPublicGraphExtractor)
        graph_build_service = runtime.resolve("graph_build_service")
        assert graph_build_service._configuration.model == "graph-llm-v2"
        assert graph_build_service._configuration.prompt_version == "public-graph-v2"

        tree_client = tree_provider._client
        coarse_client = coarse._client
        extractor_client = extractor._client
        assert not tree_client.is_closed
        assert not coarse_client.is_closed
        assert not extractor_client.is_closed
    finally:
        runtime.close()
    # The long-lived pooled clients are released by PlatformRuntime.close().
    assert tree_client.is_closed
    assert coarse_client.is_closed
    assert extractor_client.is_closed


def test_runtime_defaults_stay_unchanged_without_endpoints() -> None:
    runtime = build_runtime(load_platform_settings(dict(_BASE)))
    try:
        assert runtime.resolve("indexing_tree_search_provider") is None
        assert runtime.resolve("indexing_tree_router") is None
        assert isinstance(runtime.resolve("indexing_reranker"), ScoreReranker)
        # The dev default deterministic extractor is wired into the worker, not
        # registered as a runtime adapter (only config-assembled ones are).
        assert isinstance(
            runtime.resolve("graph_build_worker")._extractor, DeterministicPublicGraphExtractor
        )
    finally:
        runtime.close()


def test_configured_reranker_satisfies_the_production_guard(monkeypatch) -> None:
    import app.evaluation as evaluation_module

    def verify_startup(self) -> None:
        return None

    monkeypatch.setattr(evaluation_module.JudgePreflight, "verify_startup", verify_startup)

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

    _ExplicitSparseProvider = _ExplicitDenseWriter

    from datetime import UTC, datetime
    from decimal import Decimal

    from app.usage.budget import BudgetEffortPolicy, BudgetMeterPolicy, BudgetMeterService

    class _FixedClock:
        def now_utc(self, connection=None) -> datetime:
            del connection
            return datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

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

    import os
    import tempfile

    settings = load_platform_settings(
        {
            **_BASE,
            **_provider_env(),
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
            "RAG_PROVIDER_NAME": "openai-compatible",
            "RAG_PROVIDER_API_KEY": "provider-secret",
            "RAG_EVALUATION_JUDGE_BASE_URL": "https://judge.example.test/v1",
            "RAG_EVALUATION_JUDGE_API_KEY": "judge-secret",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
            "RAG_AUTH_ADMIN_ROSTER": "admin",
            "RAG_BACKUP_TARGET_NAMESPACE": "ragqs-test-backups",
            "USER_DELETION_ARCHIVE_DIR": os.environ.get(
                "TEST_USER_DELETION_ARCHIVE_DIR", tempfile.mkdtemp(prefix="rag-archive-")
            ),
        }
    )
    from sqlalchemy import create_engine

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    budget_meter = BudgetMeterService(engine, _FixedClock(), policy)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            # The config-assembled reranker and graph extractor replace the
            # explicit adapters this guard used to demand.
            "indexing_dense_writer": _ExplicitDenseWriter(),
            "indexing_sparse_provider": _ExplicitSparseProvider(),
            "indexing_token_counter": len,
            "indexing_image_ocr": lambda content, context: "ocr",
            "indexing_image_describer": lambda content, context: "description",
            "generation_budget_meter": budget_meter,
        },
    )
    try:
        assert isinstance(runtime.resolve("indexing_reranker"), TwoStageReranker)
        assert isinstance(runtime.resolve("graph_build_extractor"), LlmPublicGraphExtractor)
    finally:
        runtime.close()
