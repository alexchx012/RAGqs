"""Task 11：quota maintenance worker、租约/fence、受保护 CLI（H5）。

语义（正式 spec §5 + task-11-brief + 平台真实 WorkerRuntime/lease/fence）：
- 取消候选仅 pending：关闭业务月（quota_period < period_for(now)）优先于申请人
  inactive；未关闭月但用户非 active 也取消；两者都不是则跳过。
- worker 每次 list 候选一个事务（calendar lock + 单一 DB now），每个候选一个独立
  `run_task`（真实 lease + fence）；mutation callback 内 resolve
  quota_request_service、用 runtime/worker clock 与 run_task 传入的 fenced
  transaction connection 调 `_cancel_transition`（稳定 `WHERE status='pending'`
  条件更新、version 一次递增、cancel_reason/reviewed_at/updated_at 同一 now）。
- FenceViolation/LeaseUnavailable/PlatformError → deferred；成功计 completed。
- limit、重复运行、已审批/已取消幂等（再次运行 completed=0、version 不重复递增）。
- revoke_all：全部 pending 逐 request task，reason=deployment_revocation；
  先 revoke 再常规 run_once，两者 stats 合并。
- CLI：受 RAG_MAINTENANCE_KEY 保护（缺失 → SystemExit(2) 稳定错误；production 下
  run_usage_maintenance_once 同样拒绝）；成功输出不泄露密钥。
- 不造第二套租约系统：全部使用真实 create_worker_runtime + SqlAlchemyLeaseStore。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from alembic import command
from alembic.config import Config
from app.identity.schema import identity_metadata, identity_user_table
from app.platform.config import load_platform_settings
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyLeaseStore,
    core_metadata,
    platform_lease_table,
)
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime
from app.platform.worker import create_worker_runtime
from app.usage import maintenance as maintenance_module
from app.usage.calendar import BusinessCalendarService
from app.usage.maintenance import (
    UsageMaintenanceWorker,
    run_usage_maintenance_once,
)
from app.usage.ports import NoopOutboxEnqueuePort
from app.usage.price import PriceCatalogService
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import (
    business_calendar_version_table,
    quota_request_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MutableClock:
    times: list[datetime]

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.times[0]


def make_env(now: datetime = NOW):
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
            "RAG_MAINTENANCE_KEY": "test-maintenance-key",
        }
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    times = [now]
    clock = MutableClock(times)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    quota = QuotaService(engine, clock, calendar)
    requests = QuotaRequestService(engine, clock, calendar, quota, NoopOutboxEnqueuePort())
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "business_calendar": calendar,
            "price_catalog": prices,
            "quota_service": quota,
            "quota_request_service": requests,
        },
    )
    return engine, requests, runtime, times


def seed_identity(
    engine,
    *,
    user_id: str = "u1",
    username: str | None = None,
    lifecycle_status: str = "active",
) -> None:
    """幂等 seed（test_revoke_all 两次调用同用户；限流测试多用户）。"""
    username = username or user_id
    with engine.begin() as connection:
        existing = connection.execute(
            select(identity_user_table.c.id).where(identity_user_table.c.id == user_id)
        ).scalar_one_or_none()
        if existing is not None:
            return
        connection.execute(
            identity_user_table.insert().values(
                id=user_id,
                username=username,
                normalized_username=username,
                password_hash="x",
                real_name=username,
                display_name=username,
                department_id=None,
                role="user",
                lifecycle_status=lifecycle_status,
                version=1,
                preferences_json={},
                transition_version=1,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )


def seed_pending(engine, requests, user_id: str = "u1") -> str:
    from app.identity.service import AuthPrincipal

    actor = AuthPrincipal(
        user_id=user_id,
        auth_session_id="s1",
        username=user_id,
        role="user",
        department_id=None,
    )
    return requests.create(
        actor=actor, requested_pages=100, idempotency_key=f"k-{user_id}-{id(engine)}"
    )["id"]


def make_worker(engine, requests, runtime):
    return engine, UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime))


def assert_row(engine, request_id: str) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == request_id
                )
            )
            .mappings()
            .one()
        )


def test_worker_prioritizes_closed_period_and_sets_cancellation_audit_fields() -> None:
    """关闭业务月优先于 inactive，且取消审计字段共享 callback 的单一 now。"""
    engine, requests, runtime, times = make_env(now=NOW)
    seed_identity(engine)
    pending_id = seed_pending(engine, requests)
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.update()
            .where(identity_user_table.c.id == "u1")
            .values(lifecycle_status="pending_delete")
        )
    times[0] = SEPTEMBER  # period_closed 与 applicant_inactive 同时成立

    worker = UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime))
    stats = worker.run_once(owner="worker-1")

    assert stats.completed == 1
    assert stats.deferred == 0
    row = assert_row(engine, pending_id)
    assert row["version"] == 2
    assert row["status"] == "cancelled"
    assert row["cancel_reason"] == "period_closed"
    expected_now = SEPTEMBER.replace(tzinfo=None)  # SQLite DateTime round-trip is naive
    assert row["reviewed_at_utc"] == row["updated_at_utc"] == expected_now


def test_worker_cancels_frozen_applicant() -> None:
    engine, requests, runtime, _times = make_env()
    seed_identity(engine)
    pending_id = seed_pending(engine, requests)
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.update()
            .where(identity_user_table.c.id == "u1")
            .values(lifecycle_status="pending_delete")
        )
    worker = UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime))
    stats = worker.run_once(owner="worker-1")
    assert stats.completed == 1
    assert stats.deferred == 0
    row = assert_row(engine, pending_id)
    assert row["status"] == "cancelled"
    assert row["cancel_reason"] == "applicant_inactive"
    assert row["version"] == 2


def test_worker_defers_on_lease_conflict() -> None:
    engine, requests, runtime, _times = make_env()
    seed_identity(engine)
    pending_id = seed_pending(engine, requests)
    # 申请人冻结 → 该 pending 是真实候选（否则 worker 根本不会尝试任务，lease
    # 冲突路径不可达——与 Task 10 review 的 closed-period 时钟陷阱同类）。
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.update()
            .where(identity_user_table.c.id == "u1")
            .values(lifecycle_status="pending_delete")
        )
    worker_runtime = create_worker_runtime(runtime.settings, runtime=runtime)
    worker_runtime.leases.acquire(
        f"usage-maintenance:cancel:{pending_id}", "other-owner", timedelta(seconds=60)
    )
    worker = UsageMaintenanceWorker(worker_runtime)
    stats = worker.run_once(owner="worker-1")
    assert stats.deferred == 1
    assert stats.completed == 0
    row = assert_row(engine, pending_id)
    assert row["status"] == "pending"  # lease 冲突不产生半取消


def test_worker_defers_and_rolls_back_on_post_callback_fence_violation(monkeypatch) -> None:
    engine, requests, runtime, times = make_env()
    seed_identity(engine, lifecycle_status="pending_delete")
    pending_id = seed_pending(engine, requests)
    worker_runtime = create_worker_runtime(runtime.settings, runtime=runtime)
    assert isinstance(worker_runtime.leases, SqlAlchemyLeaseStore)
    original_cancel = requests._cancel_transition

    def expire_lease_after_cancel(connection, *, request_id, reason, now):
        affected = original_cancel(
            connection,
            request_id=request_id,
            reason=reason,
            now=now,
        )
        times[0] = NOW + timedelta(seconds=61)
        return affected

    monkeypatch.setattr(requests, "_cancel_transition", expire_lease_after_cancel)

    stats = UsageMaintenanceWorker(worker_runtime).run_once(owner="worker-1")

    assert stats == maintenance_module.MaintenanceStats(completed=0, deferred=1)
    row = assert_row(engine, pending_id)
    assert row["version"] == 1
    assert row["status"] == "pending"
    assert row["cancel_reason"] is None
    assert row["reviewed_at_utc"] is None
    assert row["updated_at_utc"] == NOW.replace(tzinfo=None)


def test_worker_defers_and_rolls_back_when_callback_raises_platform_error(monkeypatch) -> None:
    engine, requests, runtime, _times = make_env()
    seed_identity(engine, lifecycle_status="pending_delete")
    pending_id = seed_pending(engine, requests)
    worker_runtime = create_worker_runtime(runtime.settings, runtime=runtime)
    assert isinstance(worker_runtime.leases, SqlAlchemyLeaseStore)
    original_cancel = requests._cancel_transition

    def fail_after_cancel(connection, *, request_id, reason, now):
        original_cancel(
            connection,
            request_id=request_id,
            reason=reason,
            now=now,
        )
        raise PlatformError(
            "maintenance_test_failure",
            "Maintenance callback failed",
            {},
            503,
            True,
        )

    monkeypatch.setattr(requests, "_cancel_transition", fail_after_cancel)

    stats = UsageMaintenanceWorker(worker_runtime).run_once(owner="worker-1")

    assert stats == maintenance_module.MaintenanceStats(completed=0, deferred=1)
    row = assert_row(engine, pending_id)
    assert row["version"] == 1
    assert row["status"] == "pending"
    assert row["cancel_reason"] is None
    assert row["reviewed_at_utc"] is None
    assert row["updated_at_utc"] == NOW.replace(tzinfo=None)


def test_revoke_all_cancels_every_pending_via_worker_tasks() -> None:
    engine, requests, runtime, _times = make_env()
    seed_identity(engine)
    seed_pending(engine, requests, user_id="u1")
    seed_identity(engine)  # 幂等 seed（brief 要求修正的主键冲突）
    stats = run_usage_maintenance_once(
        runtime.settings, runtime=runtime, owner="worker-1", revoke_all=True
    )
    assert stats.completed == 1
    assert stats.deferred == 0
    with engine.connect() as connection:
        rows = connection.execute(select(quota_request_table)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["cancel_reason"] == "deployment_revocation"
        assert rows[0]["version"] == 2


def test_cli_entrypoint_declared_in_pyproject() -> None:
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    assert scripts["ragqs-usage-maintenance"] == "app.usage.maintenance:main"


def test_worker_honors_limit_and_repeated_runs_are_idempotent() -> None:
    engine, requests, runtime, times = make_env()
    for uid in ("u1", "u2", "u3"):
        seed_identity(engine, user_id=uid)
        seed_pending(engine, requests, user_id=uid)
    times[0] = SEPTEMBER  # 三笔全部变为 period_closed 候选
    worker = UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime))
    first = worker.run_once(owner="worker-1", limit=2)
    assert first.completed == 2
    assert first.deferred == 0
    with engine.connect() as connection:
        remaining = (
            connection.execute(
                select(quota_request_table.c.quota_request_id).where(
                    quota_request_table.c.status == "pending"
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
    second = worker.run_once(owner="worker-1")
    assert second.completed == 1
    assert second.deferred == 0
    third = worker.run_once(owner="worker-1")
    assert third.completed == 0  # 重复运行幂等
    assert third.deferred == 0
    with engine.connect() as connection:
        rows = connection.execute(select(quota_request_table)).mappings().all()
        assert len(rows) == 3
        for row in rows:
            assert row["status"] == "cancelled"
            assert row["version"] == 2  # version 只递增一次
            assert row["cancel_reason"] == "period_closed"


def test_worker_skips_requests_already_processed_by_approval() -> None:
    engine, requests, runtime, _times = make_env()
    seed_identity(engine)
    pending_id = seed_pending(engine, requests)
    from app.identity.service import AuthPrincipal

    ops = AuthPrincipal(
        user_id="ops1", auth_session_id="s2", username="ops", role="ops", department_id=None
    )
    requests.approve(
        actor=ops,
        request_id=pending_id,
        expected_version=1,
        approved_pages=50,
        idempotency_key="approve-before-worker",
    )
    worker = UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime))
    stats = worker.run_once(owner="worker-1")
    assert stats.completed == 0
    assert stats.deferred == 0
    row = assert_row(engine, pending_id)
    assert row["status"] == "approved"
    assert row["version"] == 2


def test_batch_cancel_methods_conditional_and_idempotent() -> None:
    engine, requests, _runtime, _times = make_env()
    seed_identity(engine)
    seed_identity(engine, user_id="u2")
    p1 = seed_pending(engine, requests, user_id="u1")
    p2 = seed_pending(engine, requests, user_id="u2")
    with engine.begin() as connection:
        now = requests._clock.now_utc(connection)
        assert (
            requests.cancel_for_account(
                connection, user_id="u1", reason="account_deletion", now=now
            )
            == 1
        )
        assert (
            requests.cancel_for_account(
                connection, user_id="u1", reason="account_deletion", now=now
            )
            == 0
        )  # 已取消幂等（条件更新 rowcount 0）
        assert (
            requests.revoke_all_pending(connection, reason="deployment_revocation", now=now) == 1
        )  # 仅剩 u2 的 pending
        assert requests.revoke_all_pending(connection, reason="deployment_revocation", now=now) == 0
        with pytest.raises(ValueError, match="cancel reason"):
            requests.revoke_all_pending(connection, reason="", now=now)
    row1 = assert_row(engine, p1)
    row2 = assert_row(engine, p2)
    assert row1["status"] == "cancelled"
    assert row1["cancel_reason"] == "account_deletion"
    assert row1["version"] == 2
    assert row2["status"] == "cancelled"
    assert row2["cancel_reason"] == "deployment_revocation"
    assert row2["version"] == 2


def test_cancel_closed_periods_only_cancels_closed_and_is_idempotent() -> None:
    engine, requests, _runtime, times = make_env()
    seed_identity(engine)
    seed_identity(engine, user_id="u2")
    p1 = seed_pending(engine, requests, user_id="u1")
    p2 = seed_pending(engine, requests, user_id="u2")
    times[0] = SEPTEMBER
    with engine.begin() as connection:
        lock = requests.calendar.lock_or_verify(connection)
        now = requests._clock.now_utc(connection)
        assert requests.cancel_closed_periods(connection, calendar_lock=lock, now=now) == 2
        assert requests.cancel_closed_periods(connection, calendar_lock=lock, now=now) == 0
    assert assert_row(engine, p1)["cancel_reason"] == "period_closed"
    assert assert_row(engine, p2)["cancel_reason"] == "period_closed"


@pytest.mark.parametrize("maintenance_key", [None, "", " \t "])
def test_cli_main_fails_without_nonblank_maintenance_key(
    maintenance_key: str | None, monkeypatch, capsys
) -> None:
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
    }
    if maintenance_key is not None:
        values["RAG_MAINTENANCE_KEY"] = maintenance_key
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit) as exit_info:
        maintenance_module.main([])

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ragqs-usage-maintenance: RAG_MAINTENANCE_KEY is required\n"


def test_cli_main_runs_and_does_not_leak_key(tmp_path, monkeypatch, capsys) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'maintenance.db').as_posix()}"
    secret = "cli-secret-key-do-not-leak"
    for key, value in {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": url,
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
        "RAG_MAINTENANCE_KEY": secret,
    }.items():
        monkeypatch.setenv(key, value)
    engine = create_engine(url)
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    clock = MutableClock([NOW])
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    quota = QuotaService(engine, clock, calendar)
    requests = QuotaRequestService(engine, clock, calendar, quota, NoopOutboxEnqueuePort())
    seed_identity(engine)
    pending_id = seed_pending(engine, requests)
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.update()
            .where(identity_user_table.c.id == "u1")
            .values(lifecycle_status="pending_delete")
        )
    # 日志断言用直接挂在 app.usage.maintenance logger 上的 handler（不依赖 root/
    # caplog handler——alembic fileConfig 的 disable_existing_loggers=True 会禁用
    # 未在 alembic.ini 列名的 logger 并重配 root，破坏 caplog 捕获；此处显式
    # 恢复 disabled=False 并保存/恢复原状态）。
    import io

    logger = logging.getLogger("app.usage.maintenance")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    previous_level = logger.level
    previous_propagate = logger.propagate
    previous_disabled = logger.disabled
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.disabled = False
    try:
        maintenance_module.main([])  # 成功路径正常返回（不 raise）
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        logger.disabled = previous_disabled
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    log_text = stream.getvalue()
    assert secret not in log_text
    assert "completed=" in log_text  # stats 日志确实输出
    row = assert_row(engine, pending_id)
    assert row["status"] == "cancelled"
    assert row["cancel_reason"] == "applicant_inactive"


def test_run_usage_maintenance_once_production_requires_key(tmp_path) -> None:
    archive_dir = tmp_path / "user-deletion-archive"
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://s3.example.com",
            "RAG_OBJECT_STORAGE_BUCKET": "rag",
            "RAG_PROVIDER_NAME": "dashscope",
            "RAG_PROVIDER_API_KEY": "secret",
            "RAG_AUTH_SECRET_KEY": "secret-key-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.com",
            "RAG_AUTH_ADMIN_ROSTER": "root",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "USER_DELETION_ARCHIVE_DIR": str(archive_dir),
        }
    )
    with pytest.raises(ValueError, match="MAINTENANCE_KEY|maintenance key"):
        run_usage_maintenance_once(settings)
    # 配置了密钥后 production 设置可加载（密钥不进参数/日志）
    configured = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://s3.example.com",
            "RAG_OBJECT_STORAGE_BUCKET": "rag",
            "RAG_PROVIDER_NAME": "dashscope",
            "RAG_PROVIDER_API_KEY": "secret",
            "RAG_AUTH_SECRET_KEY": "secret-key-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.com",
            "RAG_AUTH_ADMIN_ROSTER": "root",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "USER_DELETION_ARCHIVE_DIR": str(archive_dir),
            "RAG_MAINTENANCE_KEY": "prod-maintenance-key",
        }
    )
    assert configured.maintenance_key is not None
    assert configured.maintenance_key.get_secret_value() == "prod-maintenance-key"


def _side_effect_counts(engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return (
            len(connection.execute(select(business_calendar_version_table)).all()),
            len(connection.execute(select(platform_lease_table)).all()),
            len(connection.execute(select(quota_request_table)).all()),
        )


def seed_raw_pending_without_calendar(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            quota_request_table.insert().values(
                quota_request_id="raw-pending",
                version=1,
                applicant_user_id="missing-user",
                applicant_role_snapshot="user",
                applicant_department_id_snapshot=None,
                quota_period="2026-08",
                business_calendar_version_id="missing-calendar",
                requested_pages=100,
                status="pending",
                approver_user_id=None,
                approver_role_snapshot=None,
                approved_pages=None,
                credit_entry_id=None,
                cancel_reason=None,
                idempotency_fingerprint="raw-fingerprint",
                created_at_utc=NOW,
                reviewed_at_utc=None,
                updated_at_utc=NOW,
            )
        )


@pytest.mark.parametrize(
    "owner",
    [None, True, 123, "", "   ", " worker-1", "worker-1 ", "x" * 129],
)
def test_worker_rejects_invalid_owner_before_calendar_lease_or_mutation(owner) -> None:
    engine, _requests, runtime, _times = make_env()
    seed_raw_pending_without_calendar(engine)
    before = _side_effect_counts(engine)

    with pytest.raises(ValueError) as exc_info:
        UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime)).run_once(
            owner=owner,
        )

    assert (
        str(exc_info.value)
        == "maintenance owner must be a non-empty string without surrounding whitespace"
    )
    assert _side_effect_counts(engine) == before
    assert assert_row(engine, "raw-pending")["status"] == "pending"


@pytest.mark.parametrize("limit", [None, True, False, -1, 1.0, "1"])
def test_worker_rejects_invalid_limit_before_calendar_lease_or_mutation(limit) -> None:
    engine, _requests, runtime, _times = make_env()
    seed_raw_pending_without_calendar(engine)
    before = _side_effect_counts(engine)

    with pytest.raises(ValueError) as exc_info:
        UsageMaintenanceWorker(create_worker_runtime(runtime.settings, runtime=runtime)).run_once(
            owner="worker-1",
            limit=limit,
        )

    assert str(exc_info.value) == "maintenance limit must be a non-negative integer"
    assert _side_effect_counts(engine) == before
    assert assert_row(engine, "raw-pending")["status"] == "pending"


def test_revoke_all_reuses_input_validation_before_runtime_construction(monkeypatch) -> None:
    _engine, _requests, runtime, _times = make_env()
    settings = runtime.settings
    build_calls = 0

    def fail_if_runtime_is_built(_settings):
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("runtime construction must not happen for invalid input")

    monkeypatch.setattr(maintenance_module, "_build_usage_runtime", fail_if_runtime_is_built)

    with pytest.raises(ValueError) as exc_info:
        run_usage_maintenance_once(settings, owner=" invalid-owner ", revoke_all=True)

    assert (
        str(exc_info.value)
        == "maintenance owner must be a non-empty string without surrounding whitespace"
    )
    assert build_calls == 0


@pytest.mark.parametrize("limit", [-1, True])
def test_revoke_all_rejects_invalid_limit_before_calendar_lease_or_mutation(limit) -> None:
    engine, _requests, runtime, _times = make_env()
    seed_raw_pending_without_calendar(engine)
    before = _side_effect_counts(engine)

    with pytest.raises(ValueError) as exc_info:
        run_usage_maintenance_once(
            runtime.settings,
            runtime=runtime,
            owner="worker-1",
            revoke_all=True,
            limit=limit,
        )

    assert str(exc_info.value) == "maintenance limit must be a non-negative integer"
    assert _side_effect_counts(engine) == before
    assert assert_row(engine, "raw-pending")["status"] == "pending"


def test_revoke_all_limit_zero_and_max_length_owner_have_no_side_effects() -> None:
    engine, _requests, runtime, _times = make_env()
    seed_raw_pending_without_calendar(engine)
    before = _side_effect_counts(engine)

    stats = run_usage_maintenance_once(
        runtime.settings,
        runtime=runtime,
        owner="x" * 128,
        revoke_all=True,
        limit=0,
    )

    assert stats == maintenance_module.MaintenanceStats(completed=0, deferred=0)
    assert _side_effect_counts(engine) == before
    assert assert_row(engine, "raw-pending")["status"] == "pending"


def test_default_owner_is_bounded_even_for_an_unusually_long_hostname(monkeypatch) -> None:
    monkeypatch.setattr(maintenance_module.socket, "gethostname", lambda: "h" * 500)

    owner = maintenance_module._default_owner()

    assert len(owner) <= 128
    assert owner == owner.strip()
    assert owner.startswith("usage-maintenance:")


@pytest.mark.parametrize("maintenance_key", [None, "", " \t "])
def test_direct_maintenance_call_rejects_blank_key_without_side_effects(
    maintenance_key: str | None,
) -> None:
    engine, _requests, runtime, _times = make_env()
    settings = runtime.settings.model_copy(
        update={
            "maintenance_key": (None if maintenance_key is None else SecretStr(maintenance_key))
        }
    )
    before = _side_effect_counts(engine)

    with pytest.raises(ValueError) as exc_info:
        run_usage_maintenance_once(settings, runtime=runtime, owner="worker-1", limit=0)

    assert str(exc_info.value) == "RAG_MAINTENANCE_KEY is required"
    assert _side_effect_counts(engine) == before


def test_limit_zero_processes_no_candidates() -> None:
    engine, requests, runtime, _times = make_env()
    seed_identity(engine, lifecycle_status="pending_delete")
    pending_id = seed_pending(engine, requests)
    before = _side_effect_counts(engine)

    stats = UsageMaintenanceWorker(
        create_worker_runtime(runtime.settings, runtime=runtime)
    ).run_once(owner="worker-1", limit=0)

    assert stats == maintenance_module.MaintenanceStats(completed=0, deferred=0)
    assert _side_effect_counts(engine) == before
    assert assert_row(engine, pending_id)["status"] == "pending"


def test_cli_configuration_error_has_fixed_nonleaking_output(monkeypatch, capsys, caplog) -> None:
    sentinels = (
        "sentinel-provider-credential",
        "sentinel-maintenance-key",
        "sentinel-owner",
        "sentinel-argv",
        "SecretStr('sentinel-secret-repr')",
    )

    def fail_with_sensitive_configuration_error():
        raise ValueError("invalid configuration: " + " ".join(sentinels))

    monkeypatch.setattr(
        maintenance_module,
        "load_platform_settings",
        fail_with_sensitive_configuration_error,
    )

    with pytest.raises(SystemExit) as exit_info:
        maintenance_module.main([])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ragqs-usage-maintenance: configuration error\n"
    assert str(exit_info.value) == "2"
    assert exit_info.value.__cause__ is None
    visible_text = captured.out + captured.err + caplog.text + str(exit_info.value)
    for sentinel in sentinels:
        assert sentinel not in visible_text


def test_cli_real_invalid_environment_does_not_leak_credentials(
    monkeypatch, capsys, caplog
) -> None:
    access_key = "sentinel-unpaired-storage-credential"
    provider_key = "sentinel-provider-key"
    maintenance_key = "sentinel-maintenance-key"
    monkeypatch.delenv("RAG_OBJECT_STORAGE_SECRET_KEY", raising=False)
    for key, value in {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_OBJECT_STORAGE_ACCESS_KEY": access_key,
        "RAG_PROVIDER_NAME": "fake",
        "RAG_PROVIDER_API_KEY": provider_key,
        "RAG_MAINTENANCE_KEY": maintenance_key,
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit) as exit_info:
        maintenance_module.main([])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ragqs-usage-maintenance: configuration error\n"
    assert str(exit_info.value) == "2"
    assert exit_info.value.__cause__ is None
    visible_text = captured.out + captured.err + caplog.text + str(exit_info.value)
    for sentinel in (access_key, provider_key, maintenance_key):
        assert sentinel not in visible_text


def test_cli_argument_error_does_not_echo_argv_maintenance_key(monkeypatch, capsys) -> None:
    sentinel_argv = "sentinel-argv-maintenance-key-do-not-echo"

    with pytest.raises(SystemExit) as exit_info:
        maintenance_module.main(["--maintenance-key", sentinel_argv])

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert sentinel_argv not in captured.out
    assert sentinel_argv not in captured.err


def test_standalone_maintenance_fails_without_usage_migration(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pre-usage.sqlite3'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0002_identity_access")
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": database_url,
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_MAINTENANCE_KEY": "standalone-maintenance-key",
        }
    )
    cancel_calls: list[bool] = []

    def record_cancel(*_args, **_kwargs) -> int:
        cancel_calls.append(True)
        return 1

    monkeypatch.setattr(QuotaRequestService, "_cancel_transition", record_cancel)

    with pytest.raises(OperationalError, match="business_calendar_version"):
        run_usage_maintenance_once(settings, owner="worker-1", revoke_all=True)

    assert cancel_calls == []
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "business_calendar_version" not in tables
        assert "quota_request" not in tables
        with engine.connect() as connection:
            assert connection.execute(select(platform_lease_table)).all() == []
    finally:
        engine.dispose()
