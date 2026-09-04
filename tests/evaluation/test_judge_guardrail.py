"""Judge guardrail contracts: family isolation at startup and fail-closed degradation."""

from __future__ import annotations

import os
import tempfile

import pytest

from app.evaluation.policy import assert_judge_family_isolation, model_family
from app.platform.config import (
    PlatformConfigurationError,
    load_platform_settings,
    validate_startup_settings,
)


def test_model_family_registry_covers_current_deployments() -> None:
    assert model_family(provider="bailian", model="qwen3.7-plus") == "qwen"
    assert model_family(provider="dashscope", model="qwen-vl-plus") == "qwen"
    assert model_family(provider="dashscope", model="deepseek-v4-flash-0731") == "deepseek"
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
                "contextual_retrieval": ("dashscope", "deepseek-v4-flash-0731"),
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
        "RAG_BACKUP_TARGET_NAMESPACE": "ragqs-test-backups",
        # production 校验 os.path.isabs 且会 makedirs + 写探针文件：必须用本平台
        # 绝对且可写的路径，硬编码盘符路径在 Linux CI 上必挂。
        "USER_DELETION_ARCHIVE_DIR": os.path.join(
            tempfile.gettempdir(), "ragqs-user-deletion-archive"
        ),
    }
    values.update(overrides)
    return values


def test_production_startup_rejects_same_family_generation_provider() -> None:
    values = _production_environment(
        RAG_PROVIDER_NAME="qwen",
        RAG_EVALUATION_JUDGE_BASE_URL="https://judge.example.test/v1",
        RAG_EVALUATION_JUDGE_API_KEY="judge-api-secret",
    )
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
    # 生成 provider 自名 deepseek、CR 固定 deepseek-v4-flash-0731（deepseek）与判官 qwen 不同族。
    settings = load_platform_settings(
        _production_environment(
            RAG_PROVIDER_NAME="deepseek",
            RAG_EVALUATION_JUDGE_BASE_URL="https://judge.example.test/v1",
            RAG_EVALUATION_JUDGE_API_KEY="judge-api-secret",
        )
    )

    validate_startup_settings(settings)


def test_non_production_profiles_skip_the_family_guard() -> None:
    values = _production_environment(RAG_PROVIDER_NAME="qwen")
    values["RAG_PLATFORM_PROFILE"] = "development"
    values["RAG_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    values["RAG_PROVIDER_API_KEY"] = ""
    values["RAG_INDEX_CONTEXTUAL_RETRIEVAL_PROVIDER"] = "disabled"

    settings = load_platform_settings(values)

    validate_startup_settings(settings)


def test_production_missing_judge_configuration_rejects_startup() -> None:
    with pytest.raises(
        PlatformConfigurationError,
        match="production evaluation judge configuration is incomplete",
    ):
        load_platform_settings(_production_environment())
