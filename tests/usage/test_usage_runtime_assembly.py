"""Task 12：build_runtime 默认 usage 服务注册与 ensure_business_calendar_locked（H3/M4）。

- 默认 `build_runtime` 注册 business_calendar / price_catalog / usage_ledger /
  quota_service / 事务化 quota outbox adapter / quota_request_service；注入的 adapters 优先。
- `ensure_business_calendar_locked(runtime)` 在 engine.begin() 内 lock_or_verify；
  已锁定时区冲突 → PlatformError 503 calendar_timezone_conflict；缺组装 → RuntimeError。
- app_factory lifespan 与 usage maintenance 必须共享同一个 helper（避免启动与 CLI
  的 calendar lock 逻辑漂移）。
"""

from __future__ import annotations

import gc
import weakref
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import StaticPool

from app.identity.schema import identity_metadata, identity_user_table
from app.identity.service import AuthPrincipal, IdentityAccessService
from app.outbox.schema import (
    outbox_delivery_table,
    outbox_event_table,
    outbox_metadata,
    outbox_recipient_table,
)
from app.platform import app_factory as app_factory_module
from app.platform import runtime as platform_runtime_module
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime, build_runtime, ensure_business_calendar_locked
from app.usage import maintenance as maintenance_module
from app.usage.calendar import BusinessCalendarService
from app.usage.ports import UnavailableOutboxEnqueuePort
from app.usage.requests import QuotaRequestService
from app.usage.schema import (
    business_calendar_version_table,
    quota_debit_table,
    quota_request_table,
    usage_metadata,
)


def make_settings(
    timezone: str = "Asia/Shanghai",
    *,
    database_url: str = "sqlite+pysqlite:///:memory:",
    maintenance_key: str | None = None,
    admin_roster: str | None = None,
):
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": database_url,
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_BUSINESS_TIMEZONE": timezone,
    }
    if maintenance_key is not None:
        values["RAG_MAINTENANCE_KEY"] = maintenance_key
    if admin_roster is not None:
        values["RAG_AUTH_ADMIN_ROSTER"] = admin_roster
    return load_platform_settings(values)


def make_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class _NullObjectStore:
    def exists(self, key: str) -> bool:
        return False


class _FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def now_utc(self, connection=None) -> datetime:
        del connection
        return self.now


class _DisposeTrackingEngine:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.dispose_calls = 0

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def dispose(self) -> None:
        self.dispose_calls += 1
        self.engine.dispose()


def make_runtime(settings, engine, **extra_adapters):
    return build_runtime(
        settings,
        adapters={"database_engine": engine, "object_store": _NullObjectStore(), **extra_adapters},
    )


def assert_usage_service_graph(runtime: PlatformRuntime) -> None:
    calendar = runtime.resolve("business_calendar")
    prices = runtime.resolve("price_catalog")
    ledger = runtime.resolve("usage_ledger")
    quota = runtime.resolve("quota_service")
    outbox = runtime.resolve("outbox_enqueue_port")
    requests = runtime.resolve("quota_request_service")

    assert ledger.calendar is calendar
    assert ledger.prices is prices
    assert ledger.clock is runtime.resolve("database_clock")
    assert quota.calendar is calendar
    assert requests.calendar is calendar
    assert requests._quota is quota
    assert requests._outbox is outbox


def test_build_runtime_registers_all_usage_services() -> None:
    settings = make_settings()
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    runtime = make_runtime(settings, engine)
    try:
        assert runtime.resolve("business_calendar") is not None
        assert runtime.resolve("price_catalog") is not None
        assert runtime.resolve("usage_ledger") is not None
        assert runtime.resolve("quota_service") is not None
        assert isinstance(runtime.resolve("quota_request_service"), QuotaRequestService)
        assert not isinstance(runtime.resolve("outbox_enqueue_port"), UnavailableOutboxEnqueuePort)
        assert_usage_service_graph(runtime)
    finally:
        runtime.close()


def test_standalone_maintenance_reuses_default_usage_service_graph() -> None:
    settings = make_settings(maintenance_key="maintenance-test-key")
    runtime = maintenance_module._build_usage_runtime(settings)
    try:
        assert not isinstance(runtime.resolve("outbox_enqueue_port"), UnavailableOutboxEnqueuePort)
        assert_usage_service_graph(runtime)
    finally:
        runtime.close()


def test_same_engine_runtimes_use_their_own_injected_calendar_clock() -> None:
    settings = make_settings()
    engine = make_engine()
    usage_metadata.create_all(engine)
    early = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    late = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    first_runtime = make_runtime(settings, engine, database_clock=_FixedClock(early))
    second_runtime = make_runtime(settings, engine, database_clock=_FixedClock(late))
    try:
        assert first_runtime.resolve("business_calendar") is not second_runtime.resolve(
            "business_calendar"
        )
        lock = ensure_business_calendar_locked(second_runtime)
        assert lock.effective_from_utc == late
    finally:
        second_runtime.close()
        first_runtime.close()


def test_closed_runtime_calendar_has_no_process_cache_strong_reference() -> None:
    def build_close_and_release():
        runtime = make_runtime(make_settings(), make_engine())
        calendar_ref = weakref.ref(runtime.resolve("business_calendar"))
        runtime.close()
        return calendar_ref

    calendar_ref = build_close_and_release()
    gc.collect()

    assert calendar_ref() is None


def test_ensure_business_calendar_locked_locks_once_and_is_stable() -> None:
    settings = make_settings()
    engine = make_engine()
    usage_metadata.create_all(engine)
    runtime = make_runtime(settings, engine)
    try:
        first = ensure_business_calendar_locked(runtime)
        second = ensure_business_calendar_locked(runtime)
        assert first == second
    finally:
        runtime.close()


def test_default_approval_transaction_publishes_outbox_event_for_applicant() -> None:
    settings = make_settings()
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    clock = _FixedClock()
    with engine.begin() as connection:
        for user_id, username, role in (
            ("u1", "alice", "user"),
            ("ops1", "operator", "ops"),
        ):
            connection.execute(
                identity_user_table.insert().values(
                    id=user_id,
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
                    created_at_utc=clock.now,
                    updated_at_utc=clock.now,
                )
            )
    runtime = make_runtime(settings, engine, database_clock=clock)
    try:
        requests = runtime.resolve("quota_request_service")
        applicant = AuthPrincipal(
            user_id="u1",
            auth_session_id="session-user",
            username="alice",
            role="user",
            department_id=None,
        )
        approver = AuthPrincipal(
            user_id="ops1",
            auth_session_id="session-ops",
            username="operator",
            role="ops",
            department_id=None,
        )
        pending = requests.create(
            actor=applicant,
            requested_pages=100,
            idempotency_key="create-default-outbox",
        )
        approved = requests.approve(
            actor=approver,
            request_id=pending["id"],
            expected_version=1,
            approved_pages=80,
            idempotency_key="approve-default-outbox",
        )

        assert approved["status"] == "approved"
        with engine.connect() as connection:
            request_row = (
                connection.execute(
                    select(quota_request_table).where(
                        quota_request_table.c.quota_request_id == pending["id"]
                    )
                )
                .mappings()
                .one()
            )
            events = connection.execute(select(outbox_event_table)).mappings().all()
            recipients = connection.execute(select(outbox_recipient_table)).mappings().all()
            deliveries = connection.execute(select(outbox_delivery_table)).mappings().all()
            credits = (
                connection.execute(
                    select(quota_debit_table).where(quota_debit_table.c.entry_kind == "credit")
                )
                .mappings()
                .all()
            )
        assert request_row["status"] == "approved"
        assert len(credits) == 1
        assert len(events) == 1
        assert events[0]["event_type"] == "quota_approved"
        assert events[0]["aggregate_id"] == pending["id"]
        assert events[0]["transition_version"] == 2
        assert events[0]["payload_json"] == {"request_id": pending["id"]}
        assert len(recipients) == 1
        assert recipients[0]["recipient_user_id"] == "u1"
        assert recipients[0]["recipient_kind"] == "identity"
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "pending"
    finally:
        runtime.close()


def test_default_approval_rolls_back_business_and_outbox_when_delivery_insert_fails() -> None:
    settings = make_settings()
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    clock = _FixedClock()
    with engine.begin() as connection:
        for user_id, username, role in (
            ("u1", "alice", "user"),
            ("ops1", "operator", "ops"),
        ):
            connection.execute(
                identity_user_table.insert().values(
                    id=user_id,
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
                    created_at_utc=clock.now,
                    updated_at_utc=clock.now,
                )
            )
    runtime = make_runtime(settings, engine, database_clock=clock)
    try:
        requests = runtime.resolve("quota_request_service")
        applicant = AuthPrincipal(
            user_id="u1",
            auth_session_id="session-user",
            username="alice",
            role="user",
            department_id=None,
        )
        approver = AuthPrincipal(
            user_id="ops1",
            auth_session_id="session-ops",
            username="operator",
            role="ops",
            department_id=None,
        )
        pending = requests.create(
            actor=applicant,
            requested_pages=100,
            idempotency_key="create-outbox-rollback",
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TRIGGER fail_quota_delivery BEFORE INSERT ON outbox_delivery "
                    "BEGIN SELECT RAISE(ABORT, 'forced delivery failure'); END"
                )
            )

        with pytest.raises(IntegrityError, match="forced delivery failure"):
            requests.approve(
                actor=approver,
                request_id=pending["id"],
                expected_version=1,
                approved_pages=80,
                idempotency_key="approve-outbox-rollback",
            )

        with engine.connect() as connection:
            request_row = (
                connection.execute(
                    select(quota_request_table).where(
                        quota_request_table.c.quota_request_id == pending["id"]
                    )
                )
                .mappings()
                .one()
            )
            assert request_row["status"] == "pending"
            assert request_row["version"] == 1
            assert connection.execute(select(quota_debit_table)).all() == []
            assert connection.execute(select(outbox_event_table)).all() == []
            assert connection.execute(select(outbox_recipient_table)).all() == []
            assert connection.execute(select(outbox_delivery_table)).all() == []
    finally:
        runtime.close()


def test_ensure_business_calendar_locked_rejects_conflicting_timezone() -> None:
    """H3：已锁定时区与配置冲突 → 503 calendar_timezone_conflict，不悄悄重写。"""
    settings_asia = make_settings("Asia/Shanghai")
    engine = make_engine()
    usage_metadata.create_all(engine)
    first_runtime = make_runtime(settings_asia, engine)
    conflicting: PlatformRuntime | None = None
    try:
        locked = ensure_business_calendar_locked(first_runtime)
        # 第二个 runtime 同一 engine：时区不同 → lock_or_verify 冲突。注意 first_runtime
        # 不能先 close（close 会 dispose 共享的 in-memory engine，SQLite 内存库即销毁）。
        settings_utc = make_settings("UTC")
        conflicting = make_runtime(settings_utc, engine)
        with pytest.raises(PlatformError) as exc_info:
            ensure_business_calendar_locked(conflicting)
        assert exc_info.value.code == "calendar_timezone_conflict"
        assert exc_info.value.status_code == 503
        # 行未被修改：仍是首次锁定版本/时区。
        with engine.connect() as connection:
            from app.usage.schema import business_calendar_version_table

            row = connection.execute(business_calendar_version_table.select()).mappings().one()
            assert row["timezone"] == "Asia/Shanghai"
            assert row["version_id"] == locked.version_id
    finally:
        if conflicting is not None:
            conflicting.close()
        first_runtime.close()


def test_ensure_business_calendar_locked_requires_full_usage_assembly() -> None:
    settings = make_settings()
    engine = make_engine()
    bare = PlatformRuntime(settings, adapters={"database_engine": engine})
    with pytest.raises(RuntimeError, match="usage runtime is not fully assembled"):
        ensure_business_calendar_locked(bare)
    bare.close()


def test_lifespan_and_maintenance_call_shared_calendar_lock_helper(monkeypatch) -> None:
    settings = make_settings(maintenance_key="maintenance-test-key")
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    runtime = make_runtime(settings, engine, database_clock=_FixedClock())
    calls: list[PlatformRuntime] = []
    original = platform_runtime_module.ensure_business_calendar_locked

    def record_call(active_runtime: PlatformRuntime):
        calls.append(active_runtime)
        return original(active_runtime)

    monkeypatch.setattr(
        platform_runtime_module,
        "ensure_business_calendar_locked",
        record_call,
    )
    try:
        with TestClient(create_platform_app(settings, runtime=runtime)) as client:
            assert client.get("/v1/health").status_code == 200
        maintenance_module.run_usage_maintenance_once(
            settings,
            runtime=runtime,
            owner="worker-1",
            limit=1,
        )
        assert calls == [runtime, runtime]
    finally:
        runtime.close()


def _lifespan_app(settings, engine):
    """构造 create_platform_app：注入 engine/identity/object-store，usage 服务默认组装。"""
    identity = IdentityAccessService(engine, settings.auth)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": _FixedClock(),
            "identity_access": identity,
            "object_store": _NullObjectStore(),
        },
    )
    return TestClient(create_platform_app(settings, runtime=runtime)), runtime


def test_lifespan_locks_calendar_on_first_startup_and_matching_restart_succeeds() -> None:
    """H3：首次启动（空 usage schema）lifespan 锁定日历；同配置重启（匹配时区）成功。

    两个 runtime 共享同一 in-memory engine：任一 runtime.close() 都会 dispose 共享
    engine（SQLite 内存库即销毁），因此两个 runtime 都在结束时才 close。
    """
    settings = make_settings("Asia/Shanghai")
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    first_client, first_runtime = _lifespan_app(settings, engine)
    second_client, second_runtime = _lifespan_app(settings, engine)
    try:
        with first_client as client:
            health = client.get("/v1/health")
            assert health.status_code == 200
        with engine.connect() as connection:
            from app.usage.schema import business_calendar_version_table

            row = connection.execute(business_calendar_version_table.select()).mappings().one()
            assert row["timezone"] == "Asia/Shanghai"
            locked_version = row["version_id"]
        # 匹配时区重启（第二个 runtime，同一 engine）→ 成功
        with second_client as client:
            assert client.get("/v1/health").status_code == 200
        with engine.connect() as connection:
            from app.usage.schema import business_calendar_version_table

            row = connection.execute(business_calendar_version_table.select()).mappings().one()
            assert row["version_id"] == locked_version  # 未被重写
    finally:
        first_runtime.close()
        second_runtime.close()


def test_lifespan_rejects_startup_when_timezone_conflicts_with_locked_calendar() -> None:
    """外部注入 runtime 的启动失败由调用方管理，且锁定行不被改写。"""
    settings_asia = make_settings("Asia/Shanghai")
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    first_client, first_runtime = _lifespan_app(settings_asia, engine)
    settings_utc = make_settings("UTC")
    conflicting_client, conflicting_runtime = _lifespan_app(settings_utc, engine)
    try:
        with first_client as client:
            assert client.get("/v1/health").status_code == 200
        with pytest.raises(PlatformError) as exc_info:
            with conflicting_client:
                pass
        assert exc_info.value.code == "calendar_timezone_conflict"
        assert exc_info.value.status_code == 503
        assert conflicting_runtime.closed is False
        with engine.connect() as connection:
            row = connection.execute(business_calendar_version_table.select()).mappings().one()
            assert row["timezone"] == "Asia/Shanghai"
    finally:
        first_runtime.close()
        conflicting_runtime.close()


def _database_snapshot(engine) -> tuple[tuple[str, ...], dict[str, int]]:
    tables = tuple(sorted(inspect(engine).get_table_names()))
    with engine.connect() as connection:
        counts = {
            table_name: int(
                connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()
            )
            for table_name in tables
        }
    return tables, counts


def test_lifespan_missing_usage_schema_is_exact_and_does_not_mutate_database() -> None:
    settings = make_settings("Asia/Shanghai")
    engine = make_engine()
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    before = _database_snapshot(engine)
    client, runtime = _lifespan_app(settings, engine)
    try:
        with pytest.raises(OperationalError, match="business_calendar_version"):
            with client:
                pass
        assert runtime.closed is False
        assert _database_snapshot(engine) == before
        assert "business_calendar_version" not in inspect(engine).get_table_names()
    finally:
        runtime.close()


def _owned_app(monkeypatch, settings, runtime: PlatformRuntime):
    monkeypatch.setattr(app_factory_module, "build_runtime", lambda _settings: runtime)
    return create_platform_app(settings)


def test_owned_runtime_closes_when_database_probe_fails(monkeypatch) -> None:
    class BrokenEngine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        def connect(self):
            raise OSError("database unavailable")

        def dispose(self) -> None:
            self.dispose_calls += 1

    settings = make_settings()
    engine = BrokenEngine()
    runtime = PlatformRuntime(settings, adapters={"database_engine": engine})
    app = _owned_app(monkeypatch, settings, runtime)

    with pytest.raises(OSError, match="database unavailable"):
        with TestClient(app):
            pass

    assert runtime.closed is True
    assert engine.dispose_calls == 1


def test_owned_runtime_closes_when_usage_schema_is_missing(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'missing-usage.db').as_posix()}"
    settings = make_settings(database_url=database_url)
    engine = create_engine(database_url)
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    before = _database_snapshot(engine)
    tracking_engine = _DisposeTrackingEngine(engine)
    runtime = make_runtime(settings, tracking_engine, database_clock=_FixedClock())
    app = _owned_app(monkeypatch, settings, runtime)

    with pytest.raises(OperationalError, match="business_calendar_version"):
        with TestClient(app):
            pass

    assert runtime.closed is True
    assert tracking_engine.dispose_calls == 1
    verification_engine = create_engine(database_url)
    try:
        assert _database_snapshot(verification_engine) == before
        assert "business_calendar_version" not in inspect(verification_engine).get_table_names()
    finally:
        verification_engine.dispose()


def test_owned_runtime_closes_on_calendar_timezone_conflict(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'timezone-conflict.db').as_posix()}"
    engine = create_engine(database_url)
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    with engine.begin() as connection:
        original = BusinessCalendarService(
            engine,
            _FixedClock(),
            "Asia/Shanghai",
        ).lock_or_verify(connection)
    settings = make_settings("UTC", database_url=database_url)
    tracking_engine = _DisposeTrackingEngine(engine)
    runtime = make_runtime(settings, tracking_engine, database_clock=_FixedClock())
    app = _owned_app(monkeypatch, settings, runtime)

    with pytest.raises(PlatformError) as exc_info:
        with TestClient(app):
            pass

    assert exc_info.value.code == "calendar_timezone_conflict"
    assert runtime.closed is True
    assert tracking_engine.dispose_calls == 1
    verification_engine = create_engine(database_url)
    try:
        with verification_engine.connect() as connection:
            row = connection.execute(select(business_calendar_version_table)).mappings().one()
        assert row["timezone"] == "Asia/Shanghai"
        assert row["version_id"] == original.version_id
    finally:
        verification_engine.dispose()


def test_owned_runtime_closes_when_admin_roster_reconcile_fails(tmp_path, monkeypatch) -> None:
    class FailingRosterIdentity(IdentityAccessService):
        def reconcile_admin_roster(self):
            raise RuntimeError("roster reconcile failed")

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'roster-failure.db').as_posix()}"
    settings = make_settings(database_url=database_url, admin_roster="root")
    engine = create_engine(database_url)
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    tracking_engine = _DisposeTrackingEngine(engine)
    identity = FailingRosterIdentity(tracking_engine, settings.auth)
    runtime = make_runtime(
        settings,
        tracking_engine,
        database_clock=_FixedClock(),
        identity_access=identity,
    )
    app = _owned_app(monkeypatch, settings, runtime)

    with pytest.raises(RuntimeError, match="roster reconcile failed"):
        with TestClient(app):
            pass

    assert runtime.closed is True
    assert tracking_engine.dispose_calls == 1
