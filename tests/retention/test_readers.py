"""Dashboard / operations read-model tests."""

from __future__ import annotations

from datetime import timedelta

from retention_helpers import build_engine, fixed_now

from app.chat.schema import chat_message_table, chat_metadata
from app.documents.schema import (
    documents_metadata,
    ingestion_jobs_table,
    knowledge_submissions_table,
)
from app.identity.schema import identity_metadata, identity_user_table
from app.platform.database import core_metadata
from app.platform.observability import (
    ApiRead,
    LatencyRead,
    ObservabilityMetricsError,
    ObservabilityMetricsRead,
    ObservabilityReadRequest,
    SamplingRead,
)
from app.retention.readers import DashboardReadModels
from app.usage.schema import quota_request_table, usage_metadata


class FakeObservabilityPort:
    def __init__(self, *, data_state: str = "available") -> None:
        self.data_state = data_state
        self.calls: list[ObservabilityReadRequest] = []

    def record(self, sample) -> None:  # pragma: no cover - unused in readers
        del sample

    def read(self, request: ObservabilityReadRequest) -> ObservabilityMetricsRead:
        self.calls.append(request)
        if self.data_state == "unavailable":
            raise ObservabilityMetricsError("observability_metrics_unavailable", "down", {}, 503)
        api = ApiRead(
            sampled_request_weight=100.0,
            server_error_weight=2.0,
            error_rate=0.02 if self.data_state == "available" else None,
            latency=(
                LatencyRead(30, 420, 900, 100.0)
                if self.data_state == "available"
                else LatencyRead(None, None, None, 0.0)
            ),
        )
        return ObservabilityMetricsRead(
            window="7d",
            start_at_utc=fixed_now() - timedelta(days=7),
            end_at_utc=fixed_now(),
            api=api,
            sampling=SamplingRead(0.1, 1.0, True),
            data_state=self.data_state,  # type: ignore[arg-type]
        )


class RaisingObservabilityPort(FakeObservabilityPort):
    def read(self, request: ObservabilityReadRequest) -> ObservabilityMetricsRead:
        raise AssertionError("operations reader must not call the observability port")


def _seed_basics(engine):
    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.insert().values(
                id="u_ops",
                username="ops",
                normalized_username="ops",
                password_hash="x",
                real_name="Ops",
                display_name="Ops",
                role="ops",
                lifecycle_status="active",
                version=1,
                avatar_url=None,
                preferences_json={},
                transition_version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_1",
                document_id="doc_1",
                operation="initial",
                state="succeeded",
                stage=None,
                upload_batch_id=None,
                active_attempt_id=None,
                active_publication_id=None,
                version=1,
                replay_generation=0,
                next_attempt_at_utc=None,
                failure_reason=None,
                degradations_json=[],
                processing_summary_json={
                    "chunk_count": 10,
                    "page_count": 3,
                    "image_count": 1,
                    "table_count": 0,
                    "ocr": {"high_confidence": 3, "low_confidence": 1},
                    "tree": {"tree": 1, "basic": 1},
                    "cr": {},
                },
                usage_json=None,
                ocr_low_confidence=False,
                notification_event_ids_json=[],
                created_by_user_id="u_ops",
                quota_role_snapshot="ops",
                quota_department_id_snapshot=None,
                quota_exempt_reason=None,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_2",
                document_id="doc_2",
                operation="initial",
                state="pending",
                stage=None,
                upload_batch_id=None,
                active_attempt_id=None,
                active_publication_id=None,
                version=1,
                replay_generation=0,
                next_attempt_at_utc=None,
                failure_reason=None,
                degradations_json=[],
                processing_summary_json={},
                usage_json=None,
                ocr_low_confidence=False,
                notification_event_ids_json=[],
                created_by_user_id="u_ops",
                quota_role_snapshot="ops",
                quota_department_id_snapshot=None,
                quota_exempt_reason=None,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            knowledge_submissions_table.insert().values(
                id="sub_1",
                space_id="space_1",
                submitter_user_id="u_ops",
                version=1,
                status="pending",
                file_name="a.pdf",
                media_kind="pdf",
                content_hash_sha256="h",
                private_object_key="k",
                object_manifest_json={},
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            quota_request_table.insert().values(
                quota_request_id="qr_1",
                version=1,
                applicant_user_id="u_ops",
                applicant_role_snapshot="ops",
                applicant_department_id_snapshot=None,
                quota_period="2026-08",
                business_calendar_version_id="v1",
                requested_pages=10,
                status="pending",
                idempotency_fingerprint="f",
                created_at_utc=now,
                updated_at_utc=now,
            )
        )


def _seed_chat(engine):
    with engine.begin() as connection:
        connection.execute(
            chat_message_table.insert().values(
                id="m_1",
                conversation_id="c_1",
                owner_user_id="u_ops",
                role="user",
                content="hello",
                created_at_utc=fixed_now(),
            )
        )


def _reader(engine, port):
    return DashboardReadModels(
        engine=engine, now=lambda connection=None: fixed_now(), observability_metrics=port
    )


def test_ops_dashboard_has_fixed_four_packs_and_port_values() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    port = FakeObservabilityPort(data_state="available")
    response = _reader(engine, port).dashboard(role="ops", window="7d")
    assert [pack["key"] for pack in response["packs"]] == [
        "tasks_health",
        "cost_sentinel",
        "ingestion_quality",
        "todo",
    ]
    tasks = next(pack for pack in response["packs"] if pack["key"] == "tasks_health")
    cards = {card["key"]: card for card in tasks["cards"]}
    assert cards["ingestion_backlog"]["value"] == 1
    assert cards["api_error_rate"]["value"] == 0.02
    assert cards["api_latency"]["value"] == 420
    assert cards["api_latency"]["link"] == "ops.metrics"
    todo = next(pack for pack in response["packs"] if pack["key"] == "todo")
    todo_cards = {card["key"]: card for card in todo["cards"]}
    assert todo_cards["quota_pending"]["value"] == 1
    assert todo_cards["submission_pending"]["value"] == 1
    assert [call.caller for call in port.calls] == ["retention-ops"]
    assert [call.audience for call in port.calls] == ["ops"]


def test_empty_and_unavailable_port_keep_cards_null_without_fabrication() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    port = FakeObservabilityPort(data_state="empty")
    response = _reader(engine, port).dashboard(role="ops", window="7d")
    tasks = next(pack for pack in response["packs"] if pack["key"] == "tasks_health")
    cards = {card["key"]: card for card in tasks["cards"]}
    assert cards["api_error_rate"]["value"] is None
    assert cards["api_latency"]["value"] is None

    unavailable = FakeObservabilityPort(data_state="unavailable")
    response = _reader(engine, unavailable).dashboard(role="ops", window="7d")
    tasks = next(pack for pack in response["packs"] if pack["key"] == "tasks_health")
    cards = {card["key"]: card for card in tasks["cards"]}
    assert cards["api_error_rate"]["value"] is None
    assert cards["ingestion_backlog"]["value"] == 1


def test_admin_dashboard_has_fixed_four_packs_and_no_operational_facets() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    _seed_chat(engine)
    response = _reader(engine, FakeObservabilityPort()).dashboard(role="admin", window="7d")
    assert [pack["key"] for pack in response["packs"]] == [
        "usage_overview",
        "asset_usage",
        "cost_share",
        "quality_quota",
    ]
    for pack in response["packs"]:
        for card in pack["cards"]:
            assert card["threshold"] is None
            assert card["link"] is None
    overview = next(pack for pack in response["packs"] if pack["key"] == "usage_overview")
    cards = {card["key"]: card for card in overview["cards"]}
    assert cards["active_users"]["value"] == 1
    assert cards["question_trend"]["value"] == 1


def test_operations_has_exactly_three_cards_and_never_calls_port() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    response = _reader(engine, RaisingObservabilityPort()).operations(window="7d")
    assert [card["key"] for card in response["cards"]] == [
        "cache_hit_rate",
        "ocr_confidence_dist",
        "graph_basic_split",
    ]
    kinds = {card["key"]: card["kind"] for card in response["cards"]}
    assert kinds == {
        "cache_hit_rate": "stat",
        "ocr_confidence_dist": "distribution",
        "graph_basic_split": "distribution",
    }


def test_window_bounds_cover_today_week_and_month() -> None:
    from app.retention.facts import window_bounds

    now = fixed_now()
    today_start, today_end = window_bounds("today", now)
    assert today_end == now
    assert today_start == now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start, _ = window_bounds("7d", now)
    assert week_start == now - timedelta(days=7)
    month_start, _ = window_bounds("30d", now)
    assert month_start == now - timedelta(days=30)
