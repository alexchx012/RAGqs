"""Production startup rejects missing judge configuration before lifespan work."""

from __future__ import annotations

import pytest

from app.platform.app_factory import create_platform_app
from app.platform.config import PlatformConfigurationError, load_platform_settings
from app.platform.runtime import PlatformRuntime


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


def _production_settings(**overrides: str | None):
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://objects.example.test",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-test",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_BUSINESS_TIMEZONE": "UTC",
        "RAG_EVALUATION_JUDGE_BASE_URL": "https://judge.example.test/v1",
        "RAG_EVALUATION_JUDGE_API_KEY": "judge-api-secret",
    }
    for key, value in overrides.items():
        if value is None:
            values.pop(key)
        else:
            values[key] = value
    return load_platform_settings(values).model_copy(update={"profile": "production"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"RAG_EVALUATION_JUDGE_BASE_URL": ""},
        {"RAG_EVALUATION_JUDGE_BASE_URL": "   "},
        {"RAG_EVALUATION_JUDGE_API_KEY": None},
        {"RAG_EVALUATION_JUDGE_API_KEY": "   "},
        {
            "RAG_EVALUATION_JUDGE_BASE_URL": "",
            "RAG_EVALUATION_JUDGE_API_KEY": None,
        },
    ],
)
def test_production_startup_rejects_missing_evaluation_judge_configuration(
    overrides: dict[str, str | None],
) -> None:
    settings = _production_settings(**overrides)
    facade = _StartupAlertFacade()
    runtime = PlatformRuntime(
        settings,
        adapters={
            "startup_configuration_alert_port": facade,
        },
    )

    with pytest.raises(
        PlatformConfigurationError,
        match="production evaluation judge configuration is incomplete",
    ):
        create_platform_app(settings, runtime=runtime)

    assert facade.calls == []
