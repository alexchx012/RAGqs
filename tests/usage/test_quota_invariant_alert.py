"""quota 账本与 caller-owned 事务路径的指纹冲突告警（A1/A3/A4）。

- quota 409 ledger_invariant_conflict 的 details 携带非空 index（A1）；
- 审批 credit（caller-owned TxManager 事务）回滚后 best-effort 告警，409 照常
  冒泡（A3）；
- metering 本地落账（绕过 ledger 4 个告警 wrapper 的路径）回滚后告警（A4）。
documents 发布 debit 路径（A2）在 tests/documents/test_jobs_and_fences.py 用真实
receipt 流程覆盖；usage ledger 既有 wrapper 告警回归在
tests/usage/test_ledger_invariant_alert.py（A5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.schema import identity_metadata, identity_user_table
from app.identity.service import AuthPrincipal
from app.platform.database import SqlAlchemyDatabaseClock, core_metadata
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import LocalMeasurement, OwnershipSnapshot, UsageLedger
from app.usage.metering import _SCOPE_FIELDS, LocalUsageMeterService
from app.usage.ports import NoopOutboxEnqueuePort
from app.usage.price import PriceCatalogService
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import local_usage_meter_table, quota_request_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

_TRIPLE = ["entry_kind", "adjustment_source_namespace", "adjustment_source_id"]


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@dataclass
class RecordingAlertPort:
    calls: list[list[str]] = field(default_factory=list)

    def publish_usage_ledger_invariant_conflict(
        self, *, unique_key_fields: object, provider_call_id: object = None
    ) -> str:
        del provider_call_id
        self.calls.append([str(name) for name in unique_key_fields])
        return "evt_alert"


def _make_engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    return engine


def _ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        cost_center_key="user:u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
    )


def _seed_identity(engine: Engine) -> None:
    identity_metadata.create_all(engine)
    with engine.begin() as connection:
        for uid, username, role in [("u1", "alice", "user"), ("ops1", "op", "ops")]:
            connection.execute(
                identity_user_table.insert().values(
                    id=uid,
                    username=username,
                    normalized_username=username,
                    password_hash="x",
                    real_name=username,
                    display_name=username,
                    department_id=None,
                    role=role,
                    lifecycle_status="active",
                    version=1,
                    preferences_json={},
                    transition_version=1,
                    created_at_utc=NOW,
                    updated_at_utc=NOW,
                )
            )


def _make_quota() -> tuple[object, QuotaService, RecordingAlertPort]:
    engine = _make_engine()
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    port = RecordingAlertPort()
    quota = QuotaService(engine, clock, calendar, invariant_alert_port=port)
    return engine, quota, port


def test_quota_debit_conflict_details_carry_index() -> None:
    engine, quota, port = _make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="op-1",
            publication_id="pub-1",
            quota_subject_user_id="u1",
            pages=10,
            ownership=_ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        with pytest.raises(PlatformError) as conflict:
            quota.append_debit(
                connection,
                quota_operation_id="op-1",
                publication_id="pub-1",
                quota_subject_user_id="u1",
                pages=20,
                ownership=_ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
    assert conflict.value.code == "ledger_invariant_conflict"
    assert conflict.value.status_code == 409
    assert conflict.value.details["index"] == ["quota_operation_id"]
    assert port.calls == []  # quota 自身在调用方事务内不发布（回滚后由调用方边界发布）


def test_quota_credit_conflict_details_carry_index() -> None:
    engine, quota, _port = _make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_credit(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            pages=30,
            adjustment_source_namespace="quota_request",
            adjustment_source_id="qr-1",
            ownership=_ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        with pytest.raises(PlatformError) as conflict:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=50,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="qr-1",
                ownership=_ownership(),
                calendar_lock=lock,
                now=NOW,
            )
    assert conflict.value.details["index"] == _TRIPLE


def test_approve_credit_conflict_publishes_alert_after_rollback() -> None:
    engine, quota, port = _make_quota()
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    _seed_identity(engine)
    requests = QuotaRequestService(engine, clock, calendar, quota, NoopOutboxEnqueuePort())
    applicant = AuthPrincipal(
        user_id="u1",
        auth_session_id="s1",
        username="alice",
        role="user",
        department_id=None,
    )
    approver = AuthPrincipal(
        user_id="ops1",
        auth_session_id="s2",
        username="op",
        role="ops",
        department_id=None,
    )
    created = requests.create(actor=applicant, requested_pages=100, idempotency_key="create-1")
    first = requests.approve(
        actor=approver,
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="approve-1",
    )
    assert first["status"] == "approved"
    assert port.calls == []  # 无冲突不告警

    # 重置回待审（version 一并回退），换 approved_pages 重审 → 同一
    # (entry_kind, namespace, source_id) 下指纹不同 → 409 冲突 → 回滚后告警。
    with engine.begin() as connection:
        connection.execute(
            update(quota_request_table)
            .where(quota_request_table.c.quota_request_id == created["id"])
            .values(status="pending", version=1)
        )
    with pytest.raises(PlatformError) as conflict:
        requests.approve(
            actor=approver,
            request_id=created["id"],
            expected_version=1,
            approved_pages=60,
            idempotency_key="approve-2",
        )
    assert conflict.value.code == "ledger_invariant_conflict"
    assert conflict.value.status_code == 409
    assert port.calls == [_TRIPLE]
    # 事务已回滚：请求行停在首次批准后的状态，冲突轮写入整体消失
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        # 冲突轮整体回滚：请求行停在重置后的待审状态，未推进到二次批准
        assert rows[0]["status"] == "pending"
        assert rows[0]["version"] == 1


def test_metering_finalize_conflict_publishes_alert_after_rollback() -> None:
    engine = _make_engine()
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    port = RecordingAlertPort()
    ledger = UsageLedger(
        engine, clock, calendar, PriceCatalogService(engine, clock), invariant_alert_port=port
    )
    meter = LocalUsageMeterService(ledger, clock)

    def _finalize(page_count: int):
        return meter.finalize(
            execution_kind="ingestion",
            execution_id="attempt-1",
            stage="ocr",
            resource_kind="gpu",
            result="succeeded",
            measurement=LocalMeasurement(
                page_count=page_count,
                input_bytes=20,
                item_count=None,
                gpu_milliseconds=None,
                cpu_milliseconds=None,
                peak_vram_bytes=None,
                measurement_sources={
                    "page_count": "client_measured",
                    "input_bytes": "client_measured",
                },
            ),
            ownership=_ownership(),
            started_at_utc=NOW - timedelta(minutes=1),
        )

    meter.start(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        ownership=_ownership(),
        lease_expires_at_utc=NOW + timedelta(minutes=5),
    )
    result = _finalize(page_count=3)
    replay = _finalize(page_count=3)
    assert result["usage_event_id"] == replay["usage_event_id"]
    assert port.calls == []  # 同指纹幂等重放不告警

    # meter 行重置回 running（模拟计量行保留/压缩后同 scope 重新计量的真实路径）：
    # 二次 finalize 同 scope、不同指纹 → usage_event 唯一键冲突 → 回滚后告警。
    with engine.begin() as connection:
        connection.execute(
            update(local_usage_meter_table)
            .where(local_usage_meter_table.c.meter_id == result["meter_id"])
            .values(status="running", completed_at_utc=None, usage_event_id=None)
        )
    with pytest.raises(PlatformError) as conflict:
        _finalize(page_count=4)
    assert conflict.value.code == "ledger_invariant_conflict"
    assert conflict.value.status_code == 409
    assert port.calls == [list(_SCOPE_FIELDS)]
