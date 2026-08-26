"""Judge guardrail contracts: family isolation at startup and fail-closed degradation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.evaluation.judge import UnavailableJudgeProvider
from app.evaluation.policy import assert_judge_family_isolation, model_family
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings, validate_startup_settings
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime, missing_evaluation_judge_configuration


def test_model_family_registry_covers_current_deployments() -> None:
    assert model_family(provider="bailian", model="qwen3.7-plus") == "qwen"
    assert model_family(provider="dashscope", model="qwen-vl-plus") == "qwen"
    assert model_family(provider="dashscope", model="ds-v4-flash") == "deepseek"
    assert model_family(provider="deepseek", model="deepseek-chat") == "deepseek"
    # 自名厂商可按 provider 判族；多族网关（dashscope）不得整体映射。
    assert model_family(provider="deepseek") == "deepseek"
    assert model_family(provider="dashscope", model="text-embedding-v4") is None
    assert model_family(provider="openai-compatible") is None
    assert model_family(provider="meilisearch", model="anything") is None


def test_judge_family_isolation_rejects_same_family_only() -> None:
    with pytest.raises(ValueError, match="judge model family conflicts"):
        assert_judge_family_isolation(
            judge_provider="deepseek",
            judge_model="deepseek-chat",
            pipeline_models={"generation": ("deepseek", "deepseek-v4")},
        )

    assert (
        assert_judge_family_isolation(
            judge_provider="bailian",
            judge_model="qwen3.7-plus",
            pipeline_models={
                "generation": ("deepseek", "deepseek-chat"),
                "contextual_retrieval": ("dashscope", "ds-v4-flash"),
            },
        )
        is None
    )
    # 未知家族不强加约束。
    assert (
        assert_judge_family_isolation(
            judge_provider="bailian",
            judge_model="qwen3.7-plus",
            pipeline_models={"generation": ("openai-compatible", None)},
        )
        is None
    )


def _production_environment(**overrides: str) -> dict[str, str]:
    values = {
        "RAG_PLATFORM_PROFILE": "production",
        "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
        "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
        "RAG_PROVIDER_NAME": "openai-compatible",
        "RAG_PROVIDER_API_KEY": "provider-secret",
        "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PROVIDER": "dashscope",
        "RAG_INDEX_CONTEXTUAL_RETRIEVAL_BASE_URL": "https://dashscope.example.test/v1",
        "RAG_INDEX_CONTEXTUAL_RETRIEVAL_API_KEY": "cr-secret",
        "RAG_BUSINESS_TIMEZONE": "UTC",
        "RAG_DEBUG": "false",
        "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
        "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
        "RAG_AUTH_ADMIN_ROSTER": "admin",
        "USER_DELETION_ARCHIVE_DIR": "C:/var/ragqs/user-archive",
    }
    values.update(overrides)
    return values


def test_production_startup_rejects_same_family_generation_provider() -> None:
    values = _production_environment(RAG_PROVIDER_NAME="qwen")
    values["RAG_PLATFORM_PROFILE"] = "development"
    values["RAG_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    # model_copy 绕过 load 时的异常包装，让 validate_startup_settings 的原始 ValueError 可断言。
    loaded = load_platform_settings(values)
    settings = loaded.model_copy(
        update={
            "profile": "production",
            "database": loaded.database.model_copy(
                update={"url": "postgresql+psycopg://app:secret@db/rag"}
            ),
        }
    )

    with pytest.raises(ValueError, match="judge model family conflicts"):
        validate_startup_settings(settings)


def test_production_startup_accepts_distinct_family_pipeline() -> None:
    # 生成 provider 自名 deepseek、CR 固定 ds-v4-flash（deepseek）与判官 qwen 不同族。
    settings = load_platform_settings(_production_environment(RAG_PROVIDER_NAME="deepseek"))

    validate_startup_settings(settings)


def test_non_production_profiles_skip_the_family_guard() -> None:
    values = _production_environment(RAG_PROVIDER_NAME="qwen")
    values["RAG_PLATFORM_PROFILE"] = "development"
    values["RAG_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    values["RAG_PROVIDER_API_KEY"] = ""
    values["RAG_INDEX_CONTEXTUAL_RETRIEVAL_PROVIDER"] = "disabled"

    settings = load_platform_settings(values)

    validate_startup_settings(settings)


class _Calendar:
    def lock_or_verify(self, connection) -> None:
        del connection


class _StartupAlertFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def publish_missing_evaluation_judge_configuration(
        self,
        *,
        missing_variable_names: tuple[str, ...],
        occurred_at,
        connection,
    ) -> str:
        del occurred_at, connection
        self.calls.append(missing_variable_names)
        return "evt_test"


def test_production_missing_judge_credentials_alert_and_degrade() -> None:
    """A2：生产缺判官凭证 → 不硬拒绝启动，发布告警并降级为 fail-closed 判官。

    组装分支（build_runtime 在 production+缺配置时选择 UnavailableJudgeProvider）
    属既有代码路径，本 change 未改动；这里固化其三个可观察面：缺配置检测、
    降级判官的 fail-closed 行为、平台照常启动并发布告警。
    """
    settings = load_platform_settings(_production_environment())
    assert not settings.evaluation.judge_base_url
    assert settings.evaluation.judge_api_key is None
    assert missing_evaluation_judge_configuration(settings) == (
        "RAG_EVALUATION_JUDGE_BASE_URL",
        "RAG_EVALUATION_JUDGE_API_KEY",
    )

    judge = UnavailableJudgeProvider(environment="production")
    with pytest.raises(PlatformError) as error:
        judge.preflight_probe()
    assert error.value.code == "evaluation_judge_unavailable"
    assert error.value.status_code == 503

    engine = create_engine("sqlite+pysqlite:///:memory:")
    facade = _StartupAlertFacade()
    runtime = PlatformRuntime(
        settings,
        adapters={
            "database_engine": engine,
            "business_calendar": _Calendar(),
            "startup_configuration_alert_port": facade,
        },
    )
    with TestClient(create_platform_app(settings, runtime=runtime)):
        pass
    assert facade.calls == [("RAG_EVALUATION_JUDGE_BASE_URL", "RAG_EVALUATION_JUDGE_API_KEY")]
    engine.dispose()
