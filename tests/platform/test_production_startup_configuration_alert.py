"""Production lifespan publishes bounded missing-judge configuration alerts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.runtime import PlatformRuntime


class _Calendar:
    def lock_or_verify(self, connection):
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


def _production_settings(**overrides: str | None):
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://objects.example.test",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-test",
        "RAG_PROVIDER_NAME": "fake",
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
    ("overrides", "expected_missing"),
    [
        ({"RAG_EVALUATION_JUDGE_BASE_URL": ""}, ("RAG_EVALUATION_JUDGE_BASE_URL",)),
        ({"RAG_EVALUATION_JUDGE_API_KEY": None}, ("RAG_EVALUATION_JUDGE_API_KEY",)),
        (
            {
                "RAG_EVALUATION_JUDGE_BASE_URL": "",
                "RAG_EVALUATION_JUDGE_API_KEY": None,
            },
            ("RAG_EVALUATION_JUDGE_BASE_URL", "RAG_EVALUATION_JUDGE_API_KEY"),
        ),
    ],
)
def test_production_lifespan_alerts_only_missing_evaluation_judge_variable_names(
    overrides: dict[str, str | None], expected_missing: tuple[str, ...]
) -> None:
    settings = _production_settings(**overrides)
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

    assert facade.calls == [expected_missing]
    assert all("secret" not in variable.casefold() for variable in facade.calls[0])
    assert all("https" not in variable.casefold() for variable in facade.calls[0])
    engine.dispose()
