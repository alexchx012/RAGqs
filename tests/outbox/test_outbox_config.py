"""Configuration contract for the outbox notifications change."""

from __future__ import annotations

import pytest
from _helpers import make_settings

from app.platform.config import load_platform_settings


def test_outbox_settings_default_to_spec_retention_values() -> None:
    settings = make_settings()

    assert settings.outbox.notification_retention_days == 90
    assert settings.outbox.outbox_delivered_retention_days == 30


def test_outbox_settings_accept_positive_integer_retention() -> None:
    settings = make_settings(
        RAG_OUTBOX_NOTIFICATION_RETENTION_DAYS="45",
        RAG_OUTBOX_DELIVERED_RETENTION_DAYS="7",
    )

    assert settings.outbox.notification_retention_days == 45
    assert settings.outbox.outbox_delivered_retention_days == 7


@pytest.mark.parametrize(
    "overrides",
    [
        {"RAG_OUTBOX_NOTIFICATION_RETENTION_DAYS": "0"},
        {"RAG_OUTBOX_NOTIFICATION_RETENTION_DAYS": "-3"},
        {"RAG_OUTBOX_DELIVERED_RETENTION_DAYS": "0"},
        {"RAG_OUTBOX_DELIVERED_RETENTION_DAYS": "-1"},
    ],
)
def test_outbox_settings_reject_non_positive_retention(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "development",
                "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                "RAG_PROVIDER_NAME": "fake",
                "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
                **overrides,
            }
        )


def test_outbox_settings_reject_unknown_outbox_keys() -> None:
    with pytest.raises(ValueError, match="unknown or legacy configuration keys"):
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "development",
                "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                "RAG_PROVIDER_NAME": "fake",
                "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
                "RAG_OUTBOX_NOTIFICATION_RETENTION_DAYS": "90",
                "RAG_OUTBOX_UNKNOWN_OPTION": "1",
            }
        )
