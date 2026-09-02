"""Dashboard / operations read-model tests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from retention_helpers import build_engine, fixed_now
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from app.chat.schema import chat_message_feedback_table, chat_message_table, chat_metadata
from app.documents.schema import (
    documents_metadata,
    ingestion_attempts_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
)
from app.identity.schema import identity_metadata, identity_user_table
from app.outbox.schema import outbox_metadata
from app.platform.database import core_metadata
from app.platform.observability import (
    ApiRead,
    LatencyRead,
    ObservabilityMetricsError,
    ObservabilityMetricsRead,
    ObservabilityReadRequest,
    SamplingRead,
)
from app.retention.facts import ingestion_quality_facts
from app.retention.readers import DashboardReadModels, OpsJobsReadModel
from app.usage.schema import quota_request_table, usage_event_table, usage_metadata


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


class FakeDocumentsJobsService:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    def list_jobs(self, *, principal: object, limit: int) -> dict[str, list[dict[str, object]]]:
        del principal, limit
        return {"items": self._items}


def _processing_receipt(*, ocr: dict, tree: dict, chunk_count: int = 10) -> dict:
    """Production shape of ``ingestion_jobs.processing_summary_json``.

    The write side stores the full processing receipt (documents service
    ``_json(dict(receipt))``); the summary facts live under
    ``processing_summary`` with the OCR confidence in ``fact.confidence`` and
    the tree routing flag in ``tree_indexed``.
    """

    return {
        "job_id": "job_receipt",
        "attempt_id": "attempt_receipt",
        "processing_config_version": "doc:text:v1",
        "generation_id": "gen_receipt",
        "processing_summary": {
            "chunk_count": chunk_count,
            "page_count": 3,
            "image_count": 1,
            "table_count": 0,
            "ocr": ocr,
            "tree": tree,
            "cr": {},
        },
        "ocr_low_confidence": bool(ocr.get("low_confidence", False)),
        "ocr_low_confidence_fact": ocr.get("fact"),
    }


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
                processing_summary_json=_processing_receipt(
                    ocr={
                        "confidence": 0.98,
                        "low_confidence": False,
                        "reason": "low_confidence",
                        "status": "ok",
                        "fact": {"confidence": 0.98, "page": 1, "region": []},
                        "threshold": 0.9,
                        "threshold_version": "ocr_threshold:0.9",
                    },
                    tree={
                        "tree_indexed": True,
                        "tree_reason": "structure_signal",
                        "section_count": 4,
                    },
                ),
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


def _seed_feedback(engine):
    """Three up-votes now against one up-vote in the previous 7d window."""
    now = fixed_now()
    votes = [("up", now)] * 3 + [("down", now)]
    votes += [("up", now - timedelta(days=8))] + [("down", now - timedelta(days=8))] * 3
    with engine.begin() as connection:
        for index, (vote, created_at) in enumerate(votes):
            connection.execute(
                chat_message_feedback_table.insert().values(
                    message_id="m_1",
                    voter_user_id=f"voter_{index}",
                    vote=vote,
                    down_reason=None,
                    created_at_utc=created_at,
                )
            )


def _reader(engine, port):
    return DashboardReadModels(
        engine=engine, now=lambda connection=None: fixed_now(), observability_metrics=port
    )


def test_ops_jobs_uses_created_age_for_serialized_active_tasks() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    now = fixed_now()
    rows = [
        {
            "job_id": "pending_job",
            "name": "pending.pdf",
            "state": "pending",
            "allowed_actions": [],
            "created_at": (now - timedelta(minutes=3)).isoformat(),
            "next_attempt_at": None,
        },
        {
            "job_id": "running_job",
            "name": "running.pdf",
            "state": "running",
            "allowed_actions": [],
            "created_at": (now - timedelta(minutes=2)).isoformat(),
            "next_attempt_at": None,
        },
        {
            "job_id": "retry_future_job",
            "name": "retry-future.pdf",
            "state": "retry_wait",
            "allowed_actions": [],
            "created_at": (now - timedelta(minutes=4)).isoformat(),
            "next_attempt_at": (now + timedelta(minutes=1)).isoformat(),
        },
        {
            "job_id": "retry_overdue_job",
            "name": "retry-overdue.pdf",
            "state": "retry_wait",
            "allowed_actions": [],
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            "next_attempt_at": (now - timedelta(minutes=1)).isoformat(),
        },
    ]
    reader = OpsJobsReadModel(
        engine=engine,
        now=lambda connection=None: now,
        documents_service=FakeDocumentsJobsService(rows),
    )
    principal = SimpleNamespace(role="ops")

    all_items = reader.jobs(principal=principal, view="all")["items"]
    active_items = reader.jobs(principal=principal, view="active")["items"]

    expected_wait_seconds = {
        "pending_job": 180,
        "running_job": 120,
        "retry_future_job": 240,
        "retry_overdue_job": 300,
    }
    assert {item["job_id"]: item["wait_seconds"] for item in all_items} == expected_wait_seconds
    assert {item["job_id"]: item["wait_seconds"] for item in active_items} == expected_wait_seconds
    assert all(isinstance(item["wait_seconds"], int) for item in all_items)


def test_ingestion_quality_aggregates_json_in_the_database() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    _seed_basics(engine)
    now = fixed_now()
    shared_values = {
        "document_id": "doc_3",
        "operation": "initial",
        "state": "succeeded",
        "stage": None,
        "upload_batch_id": None,
        "active_attempt_id": None,
        "active_publication_id": None,
        "version": 1,
        "replay_generation": 0,
        "next_attempt_at_utc": None,
        "failure_reason": None,
        "degradations_json": [],
        "usage_json": None,
        "notification_event_ids_json": [],
        "created_by_user_id": "u_ops",
        "quota_role_snapshot": "ops",
        "quota_department_id_snapshot": None,
        "quota_exempt_reason": None,
        "created_at_utc": now,
    }

    def _ocr_receipt(confidence: float, low: bool, *, tree_indexed: bool) -> dict:
        return _processing_receipt(
            ocr={
                "confidence": confidence,
                "low_confidence": low,
                "reason": "low_confidence",
                "status": "degraded" if low else "ok",
                "fact": {"confidence": confidence, "page": 1, "region": []},
                "threshold": 0.9,
                "threshold_version": "ocr_threshold:0.9",
            },
            tree={
                "tree_indexed": tree_indexed,
                "tree_reason": "structure_signal" if tree_indexed else "no_structure",
                **({"section_count": 4} if tree_indexed else {}),
            },
        )

    with engine.begin() as connection:
        # low_confidence 布尔优先归低置信桶（即便置信度数值另有分桶）。
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_3",
                **shared_values,
                processing_summary_json=_ocr_receipt(0.42, low=True, tree_indexed=True),
                ocr_low_confidence=True,
                updated_at_utc=now - timedelta(days=29),
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_5",
                **shared_values,
                processing_summary_json=_ocr_receipt(0.97, low=False, tree_indexed=False),
                ocr_low_confidence=False,
                updated_at_utc=now - timedelta(days=29),
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_6",
                **shared_values,
                processing_summary_json=_ocr_receipt(0.92, low=False, tree_indexed=False),
                ocr_low_confidence=False,
                updated_at_utc=now - timedelta(days=29),
            )
        )
        # 图片路由：ocr 无 fact.confidence，不进任何置信度桶，tree 仍计 basic。
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_7",
                **shared_values,
                processing_summary_json=_processing_receipt(
                    ocr={"sample_strategy": "head_middle_tail", "low_confidence": False},
                    tree={"tree_indexed": False, "tree_reason": "image"},
                ),
                ocr_low_confidence=False,
                updated_at_utc=now - timedelta(days=29),
            )
        )
        # 窗口外（31 天前）的任务不计入。
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_4",
                **shared_values,
                processing_summary_json=_ocr_receipt(0.99, low=False, tree_indexed=True),
                ocr_low_confidence=True,
                updated_at_utc=now - timedelta(days=31),
            )
        )

    statements: list[str] = []

    def capture_statement(connection, cursor, statement, parameters, context, executemany) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with engine.connect() as connection:
            facts = ingestion_quality_facts(
                connection,
                start=now - timedelta(days=30),
                end=now,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    # job_1（_seed_basics，0.98）+ job_5（0.97）→ high；job_3 → low（布尔）；
    # job_6（0.92）→ medium；job_7 无置信度事实不进桶。
    assert facts == {
        "ocr_rows": [
            {"label": "high_confidence", "count": 2},
            {"label": "low_confidence", "count": 1},
            {"label": "medium_confidence", "count": 1},
        ],
        # job_1 建树 + job_3 建树 → tree；job_5/6/7 basic。
        "tree_rows": [{"label": "basic", "count": 3}, {"label": "tree", "count": 2}],
        "low_confidence_docs": 1,
        "normal_docs": 4,
    }
    assert any("sum(" in statement.lower() for statement in statements)
    assert not any(
        "select ingestion_jobs.processing_summary_json" in statement.lower()
        for statement in statements
    )


def test_ingestion_quality_compiles_postgresql_json_aggregation() -> None:
    class EmptyResult:
        def all(self) -> list[object]:
            return []

        def one(self) -> tuple[int, int]:
            return (0, 0)

    class CapturingPostgresConnection:
        dialect = postgresql_dialect()

        def __init__(self) -> None:
            self.statements: list[object] = []

        def execute(self, statement: object) -> EmptyResult:
            self.statements.append(statement)
            return EmptyResult()

    connection = CapturingPostgresConnection()
    facts = ingestion_quality_facts(
        connection,  # type: ignore[arg-type]
        start=fixed_now() - timedelta(days=30),
        end=fixed_now(),
    )

    compiled = [
        str(statement.compile(dialect=postgresql_dialect())) for statement in connection.statements
    ]
    assert facts == {
        "ocr_rows": [],
        "tree_rows": [],
        "low_confidence_docs": 0,
        "normal_docs": 0,
    }
    assert any("json_extract_path_text(" in statement for statement in compiled)
    assert any("json_typeof(" in statement for statement in compiled)


def test_ops_dashboard_has_fixed_four_packs_and_port_values() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
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


def test_ingestion_backlog_is_a_current_snapshot_across_dashboard_windows() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    old = fixed_now() - timedelta(days=40)
    with engine.begin() as connection:
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_old_pending",
                document_id="doc_old",
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
                created_at_utc=old,
                updated_at_utc=old,
            )
        )

    today = _reader(engine, FakeObservabilityPort()).dashboard(role="ops", window="today")
    thirty_days = _reader(engine, FakeObservabilityPort()).dashboard(role="ops", window="30d")

    def backlog(response: dict[str, object]) -> object:
        tasks = next(pack for pack in response["packs"] if pack["key"] == "tasks_health")
        return next(card["value"] for card in tasks["cards"] if card["key"] == "ingestion_backlog")

    assert backlog(today) == 2
    assert backlog(thirty_days) == 2


def test_empty_and_unavailable_port_keep_cards_null_without_fabrication() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
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
    outbox_metadata.create_all(engine)
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


def test_dashboard_delta_compares_current_window_with_previous_window() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    _seed_chat(engine)
    _seed_feedback(engine)
    response = _reader(engine, FakeObservabilityPort()).dashboard(role="admin", window="7d")
    overview = next(pack for pack in response["packs"] if pack["key"] == "usage_overview")
    cards = {card["key"]: card for card in overview["cards"]}
    assert cards["question_trend"]["delta"] == {"direction": "up", "text_hint": "+1"}
    quality = next(pack for pack in response["packs"] if pack["key"] == "quality_quota")
    quality_cards = {card["key"]: card for card in quality["cards"]}
    assert quality_cards["thumbs_up_ratio"]["value"] == 0.75
    assert quality_cards["thumbs_up_ratio"]["delta"] == {
        "direction": "up",
        "text_hint": "+50.0%",
    }
    # Active users reflect current lifecycle state only: no previous window exists.
    assert cards["active_users"]["delta"] is None


def test_dashboard_delta_stays_null_without_a_previous_window_baseline() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    _seed_basics(engine)
    response = _reader(engine, FakeObservabilityPort()).dashboard(role="ops", window="7d")
    tasks = next(pack for pack in response["packs"] if pack["key"] == "tasks_health")
    cards = {card["key"]: card for card in tasks["cards"]}
    # The only terminal job lands in the current window, so the previous window
    # holds no failure-rate baseline and none is invented.
    assert cards["failure_rate"]["value"] == 0.0
    assert cards["failure_rate"]["delta"] is None
    # Facts without a time dimension have no previous window to compare against.
    assert cards["ingestion_backlog"]["delta"] is None
    assert cards["api_error_rate"]["delta"] is None


def test_operations_has_exactly_three_cards_and_never_calls_port() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
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


def test_ops_jobs_stale_count_is_global_across_views() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_stale",
                document_id="doc_1",
                operation="initial",
                state="running",
                stage=None,
                upload_batch_id=None,
                active_attempt_id="attempt_1",
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
                created_at_utc=now - timedelta(minutes=5),
                updated_at_utc=now - timedelta(minutes=5),
            )
        )
        connection.execute(
            ingestion_attempts_table.insert().values(
                id="attempt_1",
                job_id="job_stale",
                attempt_number=1,
                cycle_attempt_number=1,
                replay_generation=0,
                state="running",
                lease_owner="worker_1",
                lease_expires_at_utc=now - timedelta(minutes=1),
                fencing_token=1,
                publication_id="pub_1",
                staging_request_json={},
                processing_receipt_json=None,
                failure_class=None,
                failure_reason=None,
                created_at_utc=now - timedelta(minutes=5),
                updated_at_utc=now - timedelta(minutes=5),
            )
        )
    rows = [
        {
            "job_id": "job_stale",
            "name": "stale.pdf",
            "state": "running",
            "allowed_actions": ["cancel"],
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            "next_attempt_at": None,
        },
        {
            "job_id": "job_failed",
            "name": "failed.pdf",
            "state": "failed",
            "allowed_actions": ["replay"],
            "created_at": (now - timedelta(minutes=4)).isoformat(),
            "next_attempt_at": None,
        },
    ]
    reader = OpsJobsReadModel(
        engine=engine,
        now=lambda connection=None: now,
        documents_service=FakeDocumentsJobsService(rows),
    )
    principal = SimpleNamespace(role="ops")

    for view, expected_ids in (
        ("all", {"job_stale", "job_failed"}),
        ("active", {"job_stale"}),
        ("replayable", {"job_failed"}),
        ("stale", {"job_stale"}),
    ):
        response = reader.jobs(principal=principal, view=view)
        assert {item["job_id"] for item in response["items"]} == expected_ids
        assert response["stale_count"] == 1


def test_admin_dashboard_uses_provider_facts_and_expands_real_user_rank() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    now = fixed_now()
    with engine.begin() as connection:
        for index in range(12):
            user_id = f"u_cost_{index}"
            connection.execute(
                identity_user_table.insert().values(
                    id=user_id,
                    username=user_id,
                    normalized_username=user_id,
                    password_hash="x",
                    real_name=user_id,
                    display_name=f"用户 {index}",
                    directory_search_text=user_id,
                    department_id=None,
                    role="user",
                    lifecycle_status="active",
                    version=1,
                    avatar_url=None,
                    preferences_json={},
                    transition_version=1,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            completed = now - timedelta(hours=index + 1)
            connection.execute(
                usage_event_table.insert().values(
                    usage_event_id=f"usage_{index}",
                    event_kind="provider_usage",
                    provider_call_id=f"call_{index}",
                    provider="fake",
                    model="fake-model",
                    operation="chat",
                    price_version_id="price_1",
                    currency_code="CNY",
                    estimated_cost_amount=10 + index,
                    estimated_cost_status="complete",
                    input_tokens=100,
                    prompt_cache_hit_tokens=80 if index < 6 else 20,
                    prompt_cache_miss_tokens=20 if index < 6 else 80,
                    output_tokens=10,
                    execution_kind="chat",
                    execution_id=f"execution_{index}",
                    cost_center_key=f"user:{user_id}",
                    result="succeeded",
                    measurement_sources={},
                    event_fingerprint=f"fingerprint_{index}",
                    ownership_json={
                        "actor_user_id": user_id,
                        "actor_role_snapshot": "user",
                        "quota_subject_user_id": user_id,
                    },
                    started_at_utc=completed,
                    completed_at_utc=completed,
                    effective_calendar_version_id="calendar_1",
                    effective_at_utc=completed,
                    effective_period="2026-08",
                    recorded_calendar_version_id="calendar_1",
                    recorded_at_utc=completed,
                    recorded_period="2026-08",
                    created_at_utc=completed,
                )
            )

    reader = _reader(engine, FakeObservabilityPort())
    compact = reader.dashboard(role="admin", window="7d")
    expanded = reader.dashboard(role="admin", window="7d", expand="user_rank")

    compact_cards = {card["key"]: card for pack in compact["packs"] for card in pack["cards"]}
    expanded_cards = {card["key"]: card for pack in expanded["packs"] for card in pack["cards"]}
    assert compact_cards["user_cost_rank"]["total_count"] == 12
    assert len(compact_cards["user_cost_rank"]["rows"]) == 10
    assert len(expanded_cards["user_cost_rank"]["rows"]) == 12
    assert compact_cards["monthly_llm_cost"]["value"] == 186.0

    ops = reader.dashboard(role="ops", window="7d")
    tasks_health = next(pack for pack in ops["packs"] if pack["key"] == "tasks_health")
    cache = next(
        card
        for card in next(pack for pack in ops["packs"] if pack["key"] == "cost_sentinel")["cards"]
        if card["key"] == "cache_hit_rate"
    )
    assert cache["value"] == 0.5
    assert cache["sparkline"]
    assert tasks_health["cards"]


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
