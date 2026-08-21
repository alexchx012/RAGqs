from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

import app.indexing.embedding as embedding_module
from app.graph.usage import UsageLedgerSubmissionAdapter
from app.indexing.embedding import (
    DEFAULT_EMBEDDING_BASE_URL,
    EmbeddingConfig,
    InMemoryEmbeddingProvider,
    OpenAICompatibleEmbedding,
)
from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot, UsageLedger
from app.usage.price import PriceCatalogService
from app.usage.schema import provider_call_table, usage_event_table, usage_metadata

_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class _FixedClock:
    def now_utc(self, connection=None) -> datetime:
        del connection
        return _NOW


def _config(**overrides: object) -> EmbeddingConfig:
    values: dict[str, object] = {
        "base_url": DEFAULT_EMBEDDING_BASE_URL,
        "api_key": "test-key",
        "model": "text-embedding-v4",
        "revision": "text-embedding-v4",
        "dimension": 4,
        "metric": "cosine",
    }
    values.update(overrides)
    return EmbeddingConfig(**values)  # type: ignore[arg-type]


def _usage_ledger() -> tuple[object, UsageLedger]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = _FixedClock()
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    ledger = UsageLedger(engine, clock, calendar, prices)
    with engine.begin() as connection:
        calendar.lock_or_verify(connection)
        prices.register(
            connection,
            provider="openai-compatible",
            model="text-embedding-v4",
            operation="document_embedding",
            currency_code="USD",
            lines=[
                {
                    "meter": "embedding_input_tokens",
                    "unit": "token",
                    "rate": Decimal("0.000001"),
                },
                {"meter": "vector_count", "unit": "item", "rate": Decimal("0.000001")},
            ],
            effective_from_utc=_NOW - timedelta(minutes=1),
        )
    return engine, ledger


def test_embedding_config_rejects_missing_fields() -> None:
    with pytest.raises(PlatformError) as error:
        EmbeddingConfig(
            base_url="",
            api_key="key",
            model="model",
            revision="rev",
            dimension=8,
            metric="cosine",
        )
    assert error.value.code == "embedding_config_invalid"


def test_memory_embedding_is_deterministic_and_uses_configured_dimension() -> None:
    provider = InMemoryEmbeddingProvider(_config())
    first = provider.embed(["hello world"])
    second = provider.embed(["hello world"])
    assert first == second
    assert len(first[0]) == 4


def test_openai_compatible_embedding_posts_shared_ingest_and_query_config() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]},
        )

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    ingest = provider.embed(["chunk text"])
    query = provider.embed(["user query"])
    assert ingest[0] == (0.1, 0.2, 0.3, 0.4)
    assert query[0] == (0.1, 0.2, 0.3, 0.4)
    assert len(seen) == 2
    assert str(seen[0].url).endswith("/embeddings")
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["model"] == "text-embedding-v4"
    assert seen[0].headers["authorization"] == "Bearer test-key"


def test_openai_compatible_embedding_rejects_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PlatformError) as error:
        provider.embed(["chunk text"])
    assert error.value.code == "embedding_dimension_mismatch"


@pytest.mark.parametrize(
    "payload",
    (
        {"data": []},
        {"data": [{"index": 0}]},
        "not-json",
    ),
)
def test_openai_compatible_embedding_does_not_fabricate_on_failure(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        if payload == "not-json":
            return httpx.Response(200, text="nope")
        return httpx.Response(200, json=payload)

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PlatformError) as error:
        provider.embed(["chunk text"])
    assert error.value.code == "embedding_failed"


def test_openai_compatible_embedding_batches_large_inputs_in_order() -> None:
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        input_list = [str(item) for item in body["input"]]
        requests.append(input_list)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(text[1:]), 0.0, 0.0, 0.0]}
                    for index, text in enumerate(input_list)
                ]
            },
        )

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    vectors = provider.embed([f"t{index}" for index in range(25)])
    assert [vector[0] for vector in vectors] == [float(index) for index in range(25)]
    assert [len(batch) for batch in requests] == [10, 10, 5]


@pytest.mark.parametrize(
    ("space_id", "cost_center_key", "space_kind"),
    [
        ("public", "public", "public"),
        ("department:dept_1", "department:dept_1", "department"),
    ],
)
def test_document_embedding_persists_claimed_space_usage(
    space_id: str, cost_center_key: str, space_kind: str
) -> None:
    engine, ledger = _usage_ledger()
    context_type = getattr(embedding_module, "EmbeddingUsageContext", None)
    assert context_type is not None, "document embedding metering context must be available"
    context = context_type(
        execution_kind="ingestion",
        execution_id="job_1",
        attempt_id="attempt_1",
        generation_id="generation_1",
        publication_id="publication_1",
        deadline_utc=_NOW + timedelta(minutes=5),
        replay_generation=2,
        ownership=OwnershipSnapshot(
            actor_user_id="user_1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="user_1",
            cost_center_key=cost_center_key,
            space_id=space_id,
            space_kind=space_kind,
            space_owner_user_id=None,
            authorization_version=None,
            fence_token=7,
            source_space_ids=(space_id,),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 3},
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}],
            },
        )

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
        usage_submission=UsageLedgerSubmissionAdapter(ledger),
        now=lambda: _NOW,
    )
    assert provider.embed(["claimed document chunk"], usage_context=context) == (
        (0.1, 0.2, 0.3, 0.4),
    )

    with engine.connect() as connection:
        row = connection.execute(select(usage_event_table)).mappings().one()

    assert row["event_kind"] == "provider_usage"
    assert row["execution_kind"] == "ingestion"
    assert row["execution_id"] == "job_1"
    assert row["attempt_id"] == "attempt_1"
    assert row["generation_id"] == "generation_1"
    assert row["resource_id"] == "publication_1:embedding:0"
    assert row["replay_generation"] == 2
    assert row["cost_center_key"] == cost_center_key
    assert row["ownership_json"] == {
        "actor_user_id": "user_1",
        "actor_role_snapshot": "user",
        "actor_department_id_snapshot": None,
        "quota_subject_user_id": "user_1",
        "cost_center_key": cost_center_key,
        "space_id": space_id,
        "space_kind": space_kind,
        "space_owner_user_id": None,
        "authorization_version": None,
        "fence_token": 7,
        "source_space_ids": [space_id],
    }
    assert row["embedding_input_tokens"] == 3
    assert row["vector_count"] == 1
    assert row["measurement_sources"] == {
        "embedding_input_tokens": "provider_reported",
        "vector_count": "client_measured",
    }


def test_document_embedding_invalid_response_persists_known_failed_usage() -> None:
    engine, ledger = _usage_ledger()
    context_type = getattr(embedding_module, "EmbeddingUsageContext", None)
    assert context_type is not None, "document embedding metering context must be available"
    context = context_type(
        execution_kind="ingestion",
        execution_id="job_1",
        attempt_id="attempt_1",
        generation_id="generation_1",
        publication_id="publication_1",
        deadline_utc=_NOW + timedelta(minutes=5),
        replay_generation=0,
        ownership=OwnershipSnapshot(
            actor_user_id="user_1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="user_1",
            cost_center_key="public",
            space_id="public",
            space_kind="public",
            space_owner_user_id=None,
            authorization_version=None,
            fence_token=7,
            source_space_ids=("public",),
        ),
    )

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": ["invalid-row"]})
        ),
        usage_submission=UsageLedgerSubmissionAdapter(ledger),
        now=lambda: _NOW,
    )

    with pytest.raises(PlatformError) as error:
        provider.embed(["claimed document chunk"], usage_context=context)

    assert error.value.code == "embedding_failed"
    with engine.connect() as connection:
        event = connection.execute(select(usage_event_table)).mappings().one()
        call = connection.execute(select(provider_call_table)).mappings().one()

    assert event["result"] == "failed"
    assert event["cost_center_key"] == "public"
    assert event["embedding_input_tokens"] is None
    assert event["vector_count"] is None
    assert event["measurement_sources"] == {}
    assert call["status"] == "completed"


def test_document_embedding_completion_failure_marks_sent_call_unknown() -> None:
    engine, ledger = _usage_ledger()
    context_type = getattr(embedding_module, "EmbeddingUsageContext", None)
    assert context_type is not None, "document embedding metering context must be available"
    context = context_type(
        execution_kind="ingestion",
        execution_id="job_1",
        attempt_id="attempt_1",
        generation_id="generation_1",
        publication_id="publication_1",
        deadline_utc=_NOW + timedelta(minutes=5),
        replay_generation=0,
        ownership=OwnershipSnapshot(
            actor_user_id="user_1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="user_1",
            cost_center_key="public",
            space_id="public",
            space_kind="public",
            space_owner_user_id=None,
            authorization_version=None,
            fence_token=7,
            source_space_ids=("public",),
        ),
    )
    delegate = UsageLedgerSubmissionAdapter(ledger)

    class CompletionFailure:
        def prepare_provider_call(self, **kwargs):
            return delegate.prepare_provider_call(**kwargs)

        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            return delegate.mark_dispatching(
                provider_call_id,
                started_at_provider=started_at_provider,
            )

        def complete_provider_call(self, **kwargs):
            del kwargs
            raise RuntimeError("usage storage unavailable")

        def mark_unknown(self, provider_call_id):
            delegate.mark_unknown(provider_call_id)

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]},
            )
        ),
        usage_submission=CompletionFailure(),
        now=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="usage storage unavailable"):
        provider.embed(["claimed document chunk"], usage_context=context)

    with engine.connect() as connection:
        call = connection.execute(select(provider_call_table)).mappings().one()
        events = connection.execute(select(usage_event_table)).all()

    assert call["status"] == "unknown"
    assert events == []


def test_document_embedding_dispatch_failure_marks_prepared_call_not_sent() -> None:
    engine, ledger = _usage_ledger()
    context_type = getattr(embedding_module, "EmbeddingUsageContext", None)
    assert context_type is not None, "document embedding metering context must be available"
    context = context_type(
        execution_kind="ingestion",
        execution_id="job_1",
        attempt_id="attempt_1",
        generation_id="generation_1",
        publication_id="publication_1",
        deadline_utc=_NOW + timedelta(minutes=5),
        replay_generation=0,
        ownership=OwnershipSnapshot(
            actor_user_id="user_1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="user_1",
            cost_center_key="public",
            space_id="public",
            space_kind="public",
            space_owner_user_id=None,
            authorization_version=None,
            fence_token=7,
            source_space_ids=("public",),
        ),
    )
    delegate = UsageLedgerSubmissionAdapter(ledger)

    class DispatchFailure:
        def prepare_provider_call(self, **kwargs):
            return delegate.prepare_provider_call(**kwargs)

        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            del provider_call_id, started_at_provider
            raise RuntimeError("usage dispatch unavailable")

        def mark_not_sent(self, provider_call_id):
            delegate.mark_not_sent(provider_call_id)

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]},
            )
        ),
        usage_submission=DispatchFailure(),
        now=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="usage dispatch unavailable"):
        provider.embed(["claimed document chunk"], usage_context=context)

    with engine.connect() as connection:
        call = connection.execute(select(provider_call_table)).mappings().one()
        events = connection.execute(select(usage_event_table)).all()

    assert call["status"] == "not_sent"
    assert events == []


def test_document_embedding_refuses_to_send_without_usage_submission() -> None:
    context_type = getattr(embedding_module, "EmbeddingUsageContext", None)
    assert context_type is not None, "document embedding metering context must be available"
    context = context_type(
        execution_kind="ingestion",
        execution_id="job_1",
        attempt_id="attempt_1",
        generation_id="generation_1",
        publication_id="publication_1",
        deadline_utc=_NOW + timedelta(minutes=5),
        replay_generation=0,
        ownership=OwnershipSnapshot(
            actor_user_id="user_1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="user_1",
            cost_center_key="public",
            space_id="public",
            space_kind="public",
            space_owner_user_id=None,
            authorization_version=None,
            fence_token=7,
            source_space_ids=("public",),
        ),
    )
    requests: list[httpx.Request] = []
    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]},
            )
        ),
    )

    with pytest.raises(PlatformError) as error:
        provider.embed(["claimed document chunk"], usage_context=context)

    assert error.value.code == "embedding_usage_unavailable"
    assert requests == []
