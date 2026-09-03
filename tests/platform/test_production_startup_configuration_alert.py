"""Production startup rejects missing judge configuration before lifespan work."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_table,
    outbox_event_table,
    outbox_metadata,
    outbox_recipient_table,
)
from app.platform.app_factory import create_platform_app
from app.platform.config import PlatformConfigurationError, load_platform_settings
from app.platform.database import core_metadata
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
    ("overrides", "expected_missing"),
    [
        ({"RAG_EVALUATION_JUDGE_BASE_URL": ""}, ["RAG_EVALUATION_JUDGE_BASE_URL"]),
        ({"RAG_EVALUATION_JUDGE_BASE_URL": "   "}, ["RAG_EVALUATION_JUDGE_BASE_URL"]),
        ({"RAG_EVALUATION_JUDGE_API_KEY": None}, ["RAG_EVALUATION_JUDGE_API_KEY"]),
        ({"RAG_EVALUATION_JUDGE_API_KEY": "   "}, ["RAG_EVALUATION_JUDGE_API_KEY"]),
        (
            {
                "RAG_EVALUATION_JUDGE_BASE_URL": "",
                "RAG_EVALUATION_JUDGE_API_KEY": None,
            },
            ["RAG_EVALUATION_JUDGE_BASE_URL", "RAG_EVALUATION_JUDGE_API_KEY"],
        ),
    ],
)
def test_production_startup_alerts_missing_evaluation_judge_configuration(
    overrides: dict[str, str | None],
    expected_missing: list[str],
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

    assert facade.calls == [tuple(expected_missing)]


def _alert_test_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    return engine


def _identity_service(engine) -> IdentityAccessService:
    configured = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
        }
    )
    return IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
    )


def test_missing_judge_configuration_alert_reaches_outbox_through_real_adapter() -> None:
    settings = _production_settings(RAG_EVALUATION_JUDGE_BASE_URL="")
    engine = _alert_test_engine()
    identity = _identity_service(engine)
    active_ops = identity.provision_user(
        username="ops-bell",
        password="Password1",
        real_name="Ops Bell",
        display_name="Ops Bell",
        role="ops",
        department_id=None,
    )
    runtime = PlatformRuntime(settings, adapters={"database_engine": engine})

    with pytest.raises(PlatformConfigurationError):
        create_platform_app(settings, runtime=runtime)

    with engine.connect() as connection:
        events = connection.execute(select(outbox_event_table)).mappings().all()
        assert {event["event_type"] for event in events} == {
            "evaluation_judge_configuration_missing"
        }
        payload = events[0]["payload_json"]
        assert payload["missing_variable_names"] == ["RAG_EVALUATION_JUDGE_BASE_URL"]
        recipients = connection.execute(select(outbox_recipient_table)).mappings().all()
    assert {recipient["recipient_user_id"] for recipient in recipients} == {str(active_ops["id"])}
    assert {recipient["recipient_kind"] for recipient in recipients} == {"role_snapshot"}
    assert {recipient["required_role"] for recipient in recipients} == {"ops"}

    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: datetime.now(UTC),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    assert dispatcher.run_consumer_and_finalize(claim, owner="worker-1").status == "delivered"
    with engine.connect() as connection:
        notifications = connection.execute(select(notification_table)).mappings().all()
    assert {notification["title"] for notification in notifications} == {
        "Evaluation judge configuration missing"
    }
