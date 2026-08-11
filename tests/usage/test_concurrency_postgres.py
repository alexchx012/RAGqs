"""Task 13: PostgreSQL 真实并发 / 迁移 parity / 维护租约-fence 验收（H6/H9 + 0011 parity）。

环境门槛（**全部**满足才连接/建 schema；缺任一条件整组 ``pytest.skip``，报告必须显式
记为 PostgreSQL mandatory acceptance ``NOT RUN/BLOCKED``，不得因 skip 视为通过）：
1. ``RAGQS_TEST_POSTGRES_URL`` —— 指向专用测试 PostgreSQL（允许 CREATE SCHEMA /
   DROP SCHEMA ... CASCADE），且 URL backend 必须为 ``postgresql``（SQLAlchemy
   ``make_url().get_backend_name()``，非 PG URL 明确 skip/BLOCKED）；
2. ``RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS=1`` —— 显式 destructive 测试 opt-in
   （本文件会在临时 schema 上执行真实 Alembic 迁移并 DROP SCHEMA CASCADE）；
3. URL 数据库名必须包含 ``test``（大小写不敏感）——**无旁路**（不提供
   ``ALLOW_ANY_DB`` 类开关），防止误指共享/生产库。

skip reason 全部为静态字符串，绝不含 URL/DSN/凭据（由 gate 单元测试钉住）。

隔离与清理：每次 fixture 在 URL 数据库内创建随机 ``usage_test_<uuid4 hex>`` schema，
search_path 通过 URL 的 ``options=-csearch_path=<schema>`` 注入（admin/alembic/worker
引擎一致），**绝不对 public schema 做 DDL**；schema 名由随机 hex 生成、可安全双引号
引用，绝无用户输入拼接。setup 失败也尝试 cleanup（schema 一旦创建即尽力
``DROP SCHEMA ... CASCADE``），不污染 public；DROP 失败**不得静默吞掉**——作为
pytest teardown error 抛出（主体失败时链式呈现），drop 引擎无论成功失败都 dispose。

Alembic URL：合法密码可能含 ``%``（含 URL 编码如 ``%40``），ConfigParser
BasicInterpolation 会把裸 ``%`` 当作插值语法；``_alembic_config`` 先 ``%``→``%%``
escape（``set_main_option``/``get_main_option`` 读回时还原），由 sentinel 密码纯
config 单元测试证明不解析失败、不泄露。

并发测试的临界点：不把 barrier 放在服务调用前（可能退化为串行），而是包装
``QuotaService._update_projection_locked`` / ``QuotaRequestService._require_approvable``，
使两个独立事务都到达"即将执行 ``SELECT ... FOR UPDATE``"的点后才放行（barrier 每线程
一次 + timeout + 异常收集）；离线单测证明若某线程未到临界点测试必然失败。

迁移 parity：fixture 执行真实 Alembic ``upgrade head``（0011 为 head），不是
``metadata.create_all``；``alembic_version`` 与全部对象（表/触发器/函数/partial
index/约束）落在临时 schema。对象存在性经 pg_catalog 验证；partial index 的
predicate 用 ``pg_index.indpred`` + ``pg_get_expr`` 取回并经规范化 helper 与
``effective_to_utc IS NULL`` / ``status = 'pending'`` 严格等价比较（不比较脆弱
``pg_indexes.indexdef`` 原始 substring）。0011 的每个 migration-only trigger 以
实际非法 mutation 逐一验证（独立事务，失败无残留）。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import DataError, IntegrityError

from alembic import command
from alembic.config import Config
from app.identity.schema import identity_user_table
from app.identity.service import AuthPrincipal
from app.platform.config import load_platform_settings
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    platform_audit_table,
    platform_idempotency_table,
)
from app.platform.errors import PlatformError
from app.platform.persistence import LeaseUnavailable
from app.platform.provider import CircuitBreakerRegistry, ProviderCallContext, RetryPolicy
from app.platform.runtime import build_runtime
from app.platform.worker import create_worker_runtime
from app.usage._fingerprint import ledger_fingerprint
from app.usage._sql import _insert_do_nothing as _usage_insert_do_nothing
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger
from app.usage.maintenance import MaintenanceStats, UsageMaintenanceWorker
from app.usage.price import PriceCatalogService, PriceVersion
from app.usage.provider_integration import UsageLedgerLifecycle, run_provider_call_with_usage
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import (
    price_catalog_line_table,
    price_catalog_table,
    provider_call_table,
    quota_debit_table,
    quota_projection_table,
    quota_request_table,
    usage_event_table,
)

pytestmark = pytest.mark.integration

_PG_URL_ENV = "RAGQS_TEST_POSTGRES_URL"
_DESTRUCTIVE_OPTIN_ENV = "RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS"

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
# Asia/Shanghai（UTC+8，无 DST）的精确月界：2026-08-31 15:59:59Z = 8 月末最后一刻；
# 2026-08-31 16:00:00Z = 2026-09-01 00:00 +08（业务月界）。
AUG_LAST_INSTANT = datetime(2026, 8, 31, 15, 59, 59, tzinfo=UTC)
SEP_FIRST_INSTANT = datetime(2026, 8, 31, 16, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@dataclass(frozen=True, slots=True)
class PgEnv:
    engine: Engine
    schema: str
    schema_url: str
    quota: QuotaService
    calendar: BusinessCalendarService
    requests: QuotaRequestService


class _ArrivalBarrier:
    """并发测试的临界点 barrier：带 timeout；broken 状态显式记录。

    若任一参与者未到达临界点（异常早退/串行退化），其余参与者的 ``wait()`` 超时抛
    ``BrokenBarrierError`` 并被线程错误收集——测试失败而不是静默退化为串行。
    """

    def __init__(self, parties: int, timeout: float = 30.0) -> None:
        self._barrier = threading.Barrier(parties)
        self.timeout = timeout
        self.broken: list[BaseException] = []

    def wait(self) -> None:
        try:
            self._barrier.wait(timeout=self.timeout)
        except BaseException as exc:  # noqa: BLE001 - BrokenBarrierError/超时都记录
            self.broken.append(exc)
            raise


class _LeaseOrderCoordinator:
    """确定性编排两个真实 ``SqlAlchemyLeaseStore.acquire``（不替代真实 acquire）。

    - ``begin_attempt``：两个 worker 都到达 acquire 入口后才放行（缺参与者 →
      超时 ``BrokenBarrierError`` 被 ``broken`` 记录并抛出 → 测试失败而非串行）；
      返回 ``'first'``/``'second'``（锁内确定性判定）。
    - ``mark_first_done``：first 完成真实 acquire 后由 wrapper 调用（此时真实
      lease 已持有、未释放）。
    - ``wait_for_first``：second 等待 first 真实 acquire 完成；超时抛
      ``RuntimeError``（first 从未真实 acquire → 测试失败）。
    - ``loser_owner``：second 的真实 acquire 抛 ``LeaseUnavailable`` 时由 wrapper
      记录（证明 loser 确实因 LeaseUnavailable 被 deferred，而非任意 PlatformError）。
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._arrival = threading.Barrier(2)
        self.timeout = timeout
        self._order_lock = threading.Lock()
        self._first_seen = False
        self.first_done = threading.Event()
        self.first_owner: str | None = None
        self.loser_owner: str | None = None
        self.broken: list[BaseException] = []

    def begin_attempt(self) -> str:
        try:
            self._arrival.wait(timeout=self.timeout)
        except BaseException as exc:  # noqa: BLE001 - BrokenBarrierError/超时都记录
            self.broken.append(exc)
            raise
        with self._order_lock:
            if not self._first_seen:
                self._first_seen = True
                return "first"
            return "second"

    def mark_first_done(self, owner: str) -> None:
        self.first_owner = owner
        self.first_done.set()

    def wait_for_first(self) -> None:
        if not self.first_done.wait(timeout=self.timeout):
            exc = RuntimeError("first worker never acquired the lease (timeout)")
            self.broken.append(exc)
            raise exc


def _wrap_lease_acquire(store, coordinator) -> Callable:
    """包装单个 lease store 实例的 ``acquire``：真实 acquire 前后加协调点。

    实际 acquire（``SqlAlchemyLeaseStore.acquire``）与 ``run_task``/``run_once``
    全部走生产实现；本包装只控制"两个 worker 都到达"与"first 已持有租约后才放行
    second"的时序，并记录 loser 因 ``LeaseUnavailable`` 被拒绝的事实。
    """
    real_acquire = store.acquire

    def acquire(resource: str, owner: str, ttl: timedelta):
        role = coordinator.begin_attempt()
        if role == "first":
            try:
                lease = real_acquire(resource, owner, ttl)
            finally:
                coordinator.mark_first_done(owner)
            return lease
        coordinator.wait_for_first()
        try:
            return real_acquire(resource, owner, ttl)
        except LeaseUnavailable:
            coordinator.loser_owner = owner
            raise

    return acquire


def _wrap_candidate_rendezvous(requests, candidate_barrier, request_id: str) -> Callable:
    """包装共享 ``QuotaRequestService.list_cancel_candidates``：两个 worker 都读到
    同一 pending candidate 后才放行（真实 run_once 候选发现，仅包装时序）。

    签名必须与生产调用一致：``run_once`` 以关键字 ``connection``/``calendar_lock``/
    ``now`` 调用（maintenance.py:104）。wrapper 经实例属性 monkeypatch 安装（不绑定、
    不重命名关键字），参数名错误会在真实 PG 测试抛 ``TypeError: unexpected keyword
    argument``——由离线 signature 回归测试钉住。
    """
    original_list = requests.list_cancel_candidates

    def both_saw_candidate(connection, *, calendar_lock, now):
        result = original_list(connection, calendar_lock=calendar_lock, now=now)
        assert any(
            c["quota_request_id"] == request_id for c in result
        ), "candidate must be visible to both workers"
        candidate_barrier.wait()
        return result

    return both_saw_candidate


_outbox_meta = MetaData()
tests_outbox_enqueued = Table(
    "tests_outbox_enqueued",
    _outbox_meta,
    Column("id", Integer, primary_key=True),
    Column("event_type", String(64), nullable=False),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", String(128), nullable=False),
    Column("transition_version", Integer, nullable=False),
    Column("payload_fingerprint", String(128), nullable=False),
    Column("payload_json", JSON, nullable=False),
)


class RecordingOutboxPort:
    """真正事务型 outbox 端口：同 connection 写入外部表（回滚即消失），跨线程安全。

    port 自身无共享可变状态（失败开关只读）；并发 approve 的每个事务用自己的
    connection 写行，赢家提交、输家回滚——与生产 transactional outbox 语义一致，
    不假装原子（Noop 内存列表跨线程不安全，且无事务语义）。
    """

    def __init__(self) -> None:
        self.fail = False

    def enqueue(
        self,
        *,
        connection,
        event_type: Literal["quota_approved", "quota_rejected"],
        aggregate_type: Literal["quota_request"],
        aggregate_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: datetime,
        payload_fingerprint: str,
        payload: dict,
    ) -> None:
        del recipient_user_id, occurred_at
        if self.fail:
            raise PlatformError(
                "quota_event_outbox_unavailable", "Outbox enqueue failed", {}, 503, True
            )
        connection.execute(
            tests_outbox_enqueued.insert().values(
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                transition_version=transition_version,
                payload_fingerprint=payload_fingerprint,
                payload_json=payload,
            )
        )


# ---------------------------------------------------------------------------
# 环境门槛：缺任一条件都不连接、不建 schema、不打印 DSN
# ---------------------------------------------------------------------------


def _pg_gate() -> str | None:
    """返回 skip reason（环境不足以运行 destructive PG 验收）；None 表示放行。

    纯环境/URL 解析逻辑，**绝不构造 engine、绝不连接**（由 gate 测试钉住）；
    reason 全为静态字符串，绝不含 URL/DSN/凭据。
    """
    url = os.environ.get(_PG_URL_ENV)
    if not url:
        return "PostgreSQL mandatory acceptance requires RAGQS_TEST_POSTGRES_URL (NOT RUN/BLOCKED)"
    try:
        parsed = make_url(url)
        backend = parsed.get_backend_name()
        database = parsed.database
    except Exception:  # noqa: BLE001 - malformed URL must skip, not error
        return "RAGQS_TEST_POSTGRES_URL is malformed (NOT RUN/BLOCKED)"
    if backend != "postgresql":
        return "RAGQS_TEST_POSTGRES_URL must use a postgresql backend (NOT RUN/BLOCKED)"
    if os.environ.get(_DESTRUCTIVE_OPTIN_ENV) != "1":
        return (
            "PostgreSQL destructive acceptance requires "
            "RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS=1 (NOT RUN/BLOCKED)"
        )
    if database is None or "test" not in database.lower():
        return (
            "RAGQS_TEST_POSTGRES_URL database name must contain 'test' "
            "(no bypass; NOT RUN/BLOCKED)"
        )
    return None


def _search_path_url(url: str, schema: str) -> str:
    """把 search_path 注入 URL 的 options 参数（保留已有 options，绝不打印 DSN）。"""
    parsed = make_url(url)
    query = dict(parsed.query)
    existing = query.get("options")
    combined = f"{existing} " if existing else ""
    query["options"] = combined + f"-csearch_path={schema}"
    return parsed.set(query=query).render_as_string(hide_password=False)


def _alembic_config(schema_url: str) -> Config:
    """alembic.ini Config，URL 经 main option 传入。

    ConfigParser BasicInterpolation 会把合法密码中的 ``%``（含 URL 编码如 ``%40``）
    当作插值语法（``set`` 时直接 ValueError）；先 ``%``→``%%`` escape，
    ``get_main_option`` 读回时还原为原值（env.py 的 ``_database_url`` 使用
    ``get_main_option``）。
    """
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", schema_url.replace("%", "%%"))
    return config


def _pg_settings(schema_url: str):
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": schema_url,
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
            # WorkerSettings.lease_seconds 下限 5s：stale-fence 测试用 ttl=5s + pg_sleep(5.2)
            "RAG_WORKER_LEASE_SECONDS": "5",
        }
    )


@pytest.fixture()
def pg_env():
    gate = _pg_gate()
    if gate is not None:
        pytest.skip(gate)
    url = os.environ[_PG_URL_ENV]
    schema = f"usage_test_{uuid.uuid4().hex[:12]}"
    schema_url = _search_path_url(url, schema)
    engines: list = []
    created_schema = False
    try:
        admin = create_engine(schema_url)
        engines.append(admin)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created_schema = True
        # 真实 Alembic migration：upgrade 到 head（0011 为 head），alembic_version
        # 与全部对象经 search_path 落在临时 schema，不是 metadata.create_all。
        config = _alembic_config(schema_url)
        command.upgrade(config, "head")
        engine = create_engine(schema_url)
        engines.append(engine)
        _outbox_meta.create_all(engine)
        clock = FixedClock(NOW)
        calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
        quota = QuotaService(engine, clock, calendar)
        requests = QuotaRequestService(engine, clock, calendar, quota, RecordingOutboxPort())
        yield PgEnv(
            engine=engine,
            schema=schema,
            schema_url=schema_url,
            quota=quota,
            calendar=calendar,
            requests=requests,
        )
    finally:
        # 先 dispose 全部已创建引擎（alembic 自建的 NullPool 引擎已自行 dispose）；
        # dispose 失败是 best-effort（吞掉，不掩盖主失败）。schema 一旦创建，无论
        # setup 是否成功都尽力 DROP；DROP 失败**不得静默吞掉**——作为 pytest
        # teardown error 抛出（若测试主体也已失败，pytest 会链式呈现两者），
        # drop 引擎无论成功失败都 dispose。
        for eng in reversed(engines):
            try:
                eng.dispose()
            except Exception:  # noqa: BLE001 - best-effort dispose
                pass
        if created_schema:
            drop = create_engine(schema_url)
            try:
                with drop.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                drop.dispose()


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def provider_measurement() -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=10,
        output_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={"input_tokens": "provider_reported"},
    )


def priced_ledger(env: PgEnv) -> tuple[UsageLedger, PriceCatalogService, PriceVersion]:
    clock = FixedClock(NOW)
    prices = PriceCatalogService(env.engine, clock)
    ledger = UsageLedger(env.engine, clock, env.calendar, prices)
    with env.engine.begin() as connection:
        version = prices.register(
            connection,
            provider="price-race-provider",
            model="price-race-model",
            operation="generate",
            currency_code="USD",
            lines=[
                {
                    "meter": "input_tokens",
                    "unit": "token",
                    "rate": Decimal("0.000020"),
                }
            ],
            effective_from_utc=NOW - timedelta(days=1),
        )
    return ledger, prices, version


def seed_identity(engine, *, lifecycle_status: str = "active", now: datetime = NOW) -> None:
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.insert().values(
                id="u1",
                username="alice",
                normalized_username="alice",
                password_hash="x",
                real_name="Alice",
                display_name="Alice",
                department_id=None,
                role="user",
                lifecycle_status=lifecycle_status,
                version=1,
                preferences_json={},
                transition_version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )


def applicant() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="u1", auth_session_id="s1", username="alice", role="user", department_id=None
    )


def approver() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="ops1", auth_session_id="s2", username="op", role="ops", department_id=None
    )


def create_pending(env: PgEnv, *, pages: int = 100, key: str = "create-pg-1") -> dict:
    return env.requests.create(actor=applicant(), requested_pages=pages, idempotency_key=key)


def _db_now(engine: Engine) -> datetime:
    """读一次真实 PostgreSQL database clock（SqlAlchemyDatabaseClock，DB 时间）。"""
    with engine.connect() as connection:
        return SqlAlchemyDatabaseClock(engine).now_utc(connection)


def create_pending_current_period(
    env: PgEnv, *, pages: int = 100, key: str, now: datetime | None = None
) -> tuple[dict, datetime]:
    """用真实 PostgreSQL database clock 创建"当前业务月"的 pending 申请。

    不用固定 2026-08 搭配真实 DB clock：若测试在任意真实日期运行，请求的
    quota_period 都等于当前业务月（Asia/Shanghai），维护 worker 读到
    period_for(real now) == 请求月 → 不判 period_closed，稳定断言
    applicant_inactive。

    ``now`` 提供时（推荐）：由调用方一次性读取 DB clock 并同时用于 identity seed
    与 create（**单次 DB-clock 读取**，无二次读取 race）；缺省时内部读取一次。
    创建后防御性断言创建月 == ``period_for(lock, now)``（同一 now）——跨月界瞬间
    或日历不一致时大声失败，而非静默给出错误 reason。跨 create 与 run_once 之间的
    月界切换由测试自身的 ``cancel_reason == "applicant_inactive"`` 断言大声失败
    （period_closed 时 reason 不符），绝不静默通过。
    """
    engine = env.engine
    captured = now if now is not None else _db_now(engine)
    clock = FixedClock(captured)
    requests = QuotaRequestService(engine, clock, env.calendar, env.quota, RecordingOutboxPort())
    created = requests.create(actor=applicant(), requested_pages=pages, idempotency_key=key)
    with engine.connect() as connection:
        lock = env.calendar.lock_or_verify(connection)
        current_period = env.calendar.period_for(lock, captured)
    assert created["quota_period"] == current_period, (
        created["quota_period"],
        current_period,
    )
    return created, captured


# ---------------------------------------------------------------------------
# 环境门槛单元测试（默认环境即可运行：证明 skip 先于任何 engine 构造/连接）
# ---------------------------------------------------------------------------


def test_pg_gate_skips_without_url(monkeypatch) -> None:
    monkeypatch.delenv(_PG_URL_ENV, raising=False)
    monkeypatch.delenv(_DESTRUCTIVE_OPTIN_ENV, raising=False)
    reason = _pg_gate()
    assert reason is not None
    assert "RAGQS_TEST_POSTGRES_URL" in reason
    assert "NOT RUN/BLOCKED" in reason


def test_pg_gate_skips_non_postgresql_backend(monkeypatch) -> None:
    monkeypatch.setenv(_PG_URL_ENV, "sqlite+pysqlite:///local.db")
    monkeypatch.setenv(_DESTRUCTIVE_OPTIN_ENV, "1")
    reason = _pg_gate()
    assert reason is not None
    assert "postgresql backend" in reason
    assert "NOT RUN/BLOCKED" in reason


def test_pg_gate_requires_destructive_optin_before_any_engine_construction(monkeypatch) -> None:
    """缺少 opt-in 时 gate 与 fixture 都不得构造 engine（boom 证明零连接）。"""
    monkeypatch.setenv(_PG_URL_ENV, "postgresql+psycopg://u:p@localhost/rag_test")
    monkeypatch.delenv(_DESTRUCTIVE_OPTIN_ENV, raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("create_engine must not be called without destructive opt-in")

    monkeypatch.setattr(sys.modules[__name__], "create_engine", boom)

    reason = _pg_gate()
    assert reason is not None
    assert _DESTRUCTIVE_OPTIN_ENV in reason
    # fixture 本体在 gate 失败时也必须以 skip 终止，且不构造 engine
    with pytest.raises(pytest.skip.Exception):
        next(pg_env.__wrapped__())


def test_pg_gate_rejects_non_test_database_name_without_bypass(monkeypatch) -> None:
    monkeypatch.setenv(_DESTRUCTIVE_OPTIN_ENV, "1")
    monkeypatch.setenv(_PG_URL_ENV, "postgresql+psycopg://u:p@localhost/prod_rag")
    reason = _pg_gate()
    assert reason is not None
    assert "database name must contain 'test'" in reason
    assert "no bypass" in reason
    # 数据库名含 test 时放行（大小写不敏感）
    monkeypatch.setenv(_PG_URL_ENV, "postgresql+psycopg://u:p@localhost/rag_test")
    assert _pg_gate() is None
    monkeypatch.setenv(_PG_URL_ENV, "postgresql+psycopg://u:p@localhost/RAG_TEST")
    assert _pg_gate() is None


def test_pg_gate_reasons_never_leak_url_or_credentials(monkeypatch) -> None:
    url = "postgresql+psycopg://svc:hunter2secret%40x@db.example.com:5432/prod_rag"
    monkeypatch.setenv(_DESTRUCTIVE_OPTIN_ENV, "1")
    monkeypatch.setenv(_PG_URL_ENV, url)
    for secret in (
        "hunter2secret",
        "db.example.com",
        "prod_rag",
        "postgresql+psycopg://",
        "svc:",
    ):
        assert secret not in _pg_gate()
    # 非 PG backend 与缺 URL 的 reason 同样无泄露
    monkeypatch.setenv(_PG_URL_ENV, "sqlite:///secret-local.db")
    assert "secret-local.db" not in _pg_gate()
    monkeypatch.delenv(_PG_URL_ENV, raising=False)
    assert "secret" not in _pg_gate()


# ---------------------------------------------------------------------------
# Alembic URL 传递：合法密码中的 % / URL encoding 不破坏 ConfigParser
# ---------------------------------------------------------------------------


def test_alembic_config_roundtrips_sentinel_percent_url() -> None:
    """sentinel 特殊字符密码经 _alembic_config 设置后 get_main_option 原样还原。"""
    sentinel_password = "p%40ss%2Fword%25%21%40%23%24"
    url = f"postgresql+psycopg://svc:{sentinel_password}@db.example.com:5432/rag_test"
    config = _alembic_config(url)
    assert config.get_main_option("sqlalchemy.url") == url
    assert "p%40ss%2Fword%25%21%40%23%24" in config.get_main_option("sqlalchemy.url")


def test_alembic_config_unescaped_percent_is_rejected_by_configparser() -> None:
    """证明 escape 是必要的：未 escape 的 URL 在 set 时即被 ConfigParser 拒绝。"""
    url = "postgresql+psycopg://svc:p%40ss@db.example.com:5432/rag_test"
    naive = Config("alembic.ini")
    with pytest.raises(ValueError):
        naive.set_main_option("sqlalchemy.url", url)


def test_search_path_url_injects_options_and_preserves_credentials() -> None:
    url = "postgresql+psycopg://svc:hunter2%40secret@host:5432/rag_test"
    rendered = _search_path_url(url, "usage_test_abc")
    parsed = make_url(rendered)
    assert parsed.database == "rag_test"
    assert parsed.password == "hunter2@secret"
    assert "-csearch_path=usage_test_abc" in parsed.query["options"]
    # 已有 options 保留
    with_options = _search_path_url(url + "?application_name=x", "usage_test_abc")
    parsed2 = make_url(with_options)
    assert parsed2.query["application_name"] == "x"
    assert "-csearch_path=usage_test_abc" in parsed2.query["options"]


# ---------------------------------------------------------------------------
# partial-index predicate 规范化 helper（纯函数，离线单测覆盖 PG deparser 输出）
# ---------------------------------------------------------------------------

_CAST_RE = re.compile(
    r"::[a-z_][a-z0-9_]*(\s+(?!and\b|or\b|not\b|is\b|null\b|in\b)[a-z_][a-z0-9_]*)?(\s*\[\s*\])?"
)
_QUALIFIED_RE = re.compile(r"\b[a-z_][a-z0-9_]*\.")


def _normalize_predicate(sql: str) -> str:
    """把 pg_get_expr/pg_indexes 输出的谓词规范化成规范字符串。

    容忍：schema qualification（``usage_test_x.col`` / ``"schema"."col"``）、显式
    cast（``::text``）、冗余外层括号、大小写与空白差异。**不做脆弱 substring 比较**。
    """
    text = sql.strip().lower()
    text = _CAST_RE.sub("", text)
    text = text.replace('"', "")
    text = _QUALIFIED_RE.sub("", text)
    text = text.replace("(", " ( ").replace(")", " ) ")
    text = " ".join(text.split())
    # 比较/赋值运算符两侧空白归一（deparser 输出 `=` 两侧空白不固定）
    text = re.sub(r"\s*(<>|!=|<=|>=|=|<|>)\s*", r" \1 ", text)
    text = " ".join(text.split())
    # 剥离单个标识符操作数的冗余括号（PG deparser 常见 ``((status))`` → ``status``）
    text = re.sub(r"\( ([a-z_][a-z0-9_]*) \)", r"\1", text)
    # 剥离冗余外层括号（整个表达式被一对括号包住时），直至不再包裹
    while text.startswith("( ") and text.endswith(" )"):
        text = text[2:-2].strip()
    return text


def _assert_predicate_equivalent(actual: str | None, expected: str) -> None:
    assert actual is not None
    assert _normalize_predicate(actual) == _normalize_predicate(expected), (actual, expected)


def test_normalize_predicate_tolerates_deparser_outputs() -> None:
    """覆盖常见 PostgreSQL deparser 输出（括号/cast/schema qualification/空白）。"""
    cases = [
        ("(effective_to_utc IS NULL)", "effective_to_utc IS NULL"),
        ("((status)::text = 'pending'::text)", "status = 'pending'"),
        ("usage_test_abc.effective_to_utc IS NULL", "effective_to_utc IS NULL"),
        ("(status = 'pending')", "status='pending'"),
        ('"usage_test_x"."status" = \'pending\'::text', "status = 'pending'"),
        (
            "(provider = 'dashscope'::text AND model = 'qwen-max'::text"
            " AND operation = 'chat'::text)",
            "provider='dashscope' AND model='qwen-max' AND operation='chat'",
        ),
        ("status = 'pending'::character varying", "status = 'pending'"),
    ]
    for actual, expected in cases:
        assert _normalize_predicate(actual) == _normalize_predicate(expected), actual
    # 语义不等价的谓词必须判不等（防规范化过度）
    assert _normalize_predicate("effective_to_utc IS NULL") != _normalize_predicate(
        "status = 'pending'"
    )
    assert _normalize_predicate("effective_to_utc IS NOT NULL") != _normalize_predicate(
        "effective_to_utc IS NULL"
    )


# ---------------------------------------------------------------------------
# 临界点/协调 helper：离线证明缺参与者必然失败（不退化串行）
# ---------------------------------------------------------------------------


def test_rendezvous_coordination_fails_when_participant_missing() -> None:
    """离线证明：若某线程未到达临界点（candidate rendezvous / acquire 入口共用
    ``_ArrivalBarrier`` 机制），其余参与者的 wait 超时抛 BrokenBarrierError，
    错误被收集 → 测试失败，而不是静默退化为串行。"""
    barrier = _ArrivalBarrier(2, timeout=1.0)
    errors: list[BaseException] = []

    def crashed_worker() -> None:
        try:
            raise RuntimeError("worker crashed before reaching the critical point")
        except BaseException as exc:  # noqa: BLE001 - collect
            errors.append(exc)

    thread = threading.Thread(target=crashed_worker)
    thread.start()
    thread.join()
    # 主线程作为第二个参与者到达临界点 → 超时 → BrokenBarrierError
    with pytest.raises(threading.BrokenBarrierError):
        barrier.wait()
    assert barrier.broken
    assert len(errors) == 1


def test_lease_coordinator_fails_when_second_worker_never_arrives() -> None:
    """只有 first 到达 acquire 入口 → begin_attempt 超时 BrokenBarrierError（记录）。"""
    coordinator = _LeaseOrderCoordinator(timeout=1.0)
    with pytest.raises(threading.BrokenBarrierError):
        coordinator.begin_attempt()
    assert coordinator.broken


def test_lease_coordinator_fails_when_first_never_acquires() -> None:
    """first 到达（真实场景：两个线程都到 acquire 入口）但从未真实 acquire
    （first_done 不 set）→ second 等待超时 RuntimeError（记录）——不会悄悄让
    second 直接串行 acquire。"""
    coordinator = _LeaseOrderCoordinator(timeout=1.0)
    roles: dict[str, str] = {}

    def arrive(name: str) -> None:
        roles[name] = coordinator.begin_attempt()

    threads = [
        threading.Thread(target=arrive, args=("a",)),
        threading.Thread(target=arrive, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert set(roles.values()) == {"first", "second"}  # 谁先到由调度决定，但必有 first/second
    with pytest.raises(RuntimeError, match="never acquired"):
        coordinator.wait_for_first()
    assert coordinator.broken


def test_lease_coordinator_releases_second_after_first_acquires() -> None:
    """正常路径：两个线程都到 acquire 入口，first 完成真实 acquire 后 second
    放行；first_owner 记录。"""
    coordinator = _LeaseOrderCoordinator(timeout=1.0)
    roles: dict[str, str] = {}

    def arrive(name: str) -> None:
        roles[name] = coordinator.begin_attempt()

    threads = [
        threading.Thread(target=arrive, args=("a",)),
        threading.Thread(target=arrive, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert set(roles.values()) == {"first", "second"}
    coordinator.mark_first_done("worker-a")
    coordinator.wait_for_first()  # 不抛错
    assert coordinator.first_owner == "worker-a"
    assert coordinator.broken == []


def test_candidate_wrapper_signature_matches_production_call() -> None:
    """离线 signature 回归：`_wrap_candidate_rendezvous` 生成的 wrapper 必须能接受
    生产 `UsageMaintenanceWorker.run_once` 的调用方式——关键字 `connection`/
    `calendar_lock`/`now`（maintenance.py:104 实际调用）。实例属性 monkeypatch 不
    绑定、不重命名关键字；若 wrapper 参数名写成 `now_` 之类，这里会抛
    `TypeError: unexpected keyword argument 'now'` → RED。

    用假 requests（记录调用）与 stub barrier 纯离线验证，不连接 PG。
    """
    calls: list[dict] = []

    class FakeRequests:
        def list_cancel_candidates(self, connection, *, calendar_lock, now):
            calls.append({"connection": connection, "calendar_lock": calendar_lock, "now": now})
            return [{"quota_request_id": "qr_target"}]

    class StubBarrier:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self) -> None:
            self.waits += 1

    barrier = StubBarrier()
    wrapper = _wrap_candidate_rendezvous(FakeRequests(), barrier, "qr_target")

    # 生产调用形状：run_once 用关键字 now= 调用
    result = wrapper("conn-x", calendar_lock="lock-y", now=object())
    assert result == [{"quota_request_id": "qr_target"}]
    assert barrier.waits == 1
    assert calls == [{"connection": "conn-x", "calendar_lock": "lock-y", "now": calls[0]["now"]}]


def test_pg_insert_do_nothing_reports_insert_and_conflict_with_real_results(pg_env) -> None:
    """真实 PostgreSQL 上首插必须为 True，ON CONFLICT 无行必须为 False。

    The old implementation exposed psycopg3's ``rowcount == -1`` for both cases;
    this assertion is intentionally at the helper boundary and uses the temporary
    Alembic schema rather than a mocked result.
    """
    values = {
        "scope": "pg-helper-scope",
        "idempotency_key": "pg-helper-key",
        "request_hash": "pg-helper-hash",
        "status": "reserved",
        "response_json": None,
        "created_at_utc": NOW,
        "completed_at_utc": None,
    }
    with pg_env.engine.begin() as connection:
        assert _usage_insert_do_nothing(
            connection, platform_idempotency_table, values, ["scope", "idempotency_key"]
        )
        assert not _usage_insert_do_nothing(
            connection,
            platform_idempotency_table,
            values,
            ["scope", "idempotency_key"],
        )


def test_pg_quota_request_idempotency_first_replay_and_conflict(pg_env) -> None:
    """The real create path must reserve, commit, replay, and reject changed facts."""
    first = pg_env.requests.create(
        actor=applicant(), requested_pages=100, idempotency_key="pg-request-idempotency"
    )
    replay = pg_env.requests.create(
        actor=applicant(), requested_pages=100, idempotency_key="pg-request-idempotency"
    )
    assert replay == first
    with pytest.raises(PlatformError) as conflict:
        pg_env.requests.create(
            actor=applicant(), requested_pages=200, idempotency_key="pg-request-idempotency"
        )
    assert conflict.value.code == "idempotency_key_conflict"
    assert conflict.value.status_code == 409


def test_pg_projection_insert_then_update_uses_real_insert_result(pg_env) -> None:
    """A missing projection row is created and then receives the atomic increment."""
    with pg_env.engine.begin() as connection:
        pg_env.quota._update_projection_locked(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            debit_delta=10,
            updated_at_utc=NOW,
        )
        pg_env.quota._update_projection_locked(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            debit_delta=20,
            updated_at_utc=NOW,
        )
    with pg_env.engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_projection_table).where(
                    and_(
                        quota_projection_table.c.quota_subject_user_id == "u1",
                        quota_projection_table.c.quota_period == "2026-08",
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["used"] == 30


# ---------------------------------------------------------------------------
# H9 barrier：并发 debit 无丢失更新（临界点 = 投影行 SELECT ... FOR UPDATE）
# ---------------------------------------------------------------------------


def test_pg_concurrent_debits_do_not_lose_updates(pg_env, monkeypatch) -> None:
    """H9：两个独立事务同时 append_debit；临界点 barrier 包在
    ``_update_projection_locked``（真实锁点：投影行 FOR UPDATE）前。

    预建投影行，两个 worker 直接竞争投影行锁（真正 row-lock update 路径）；
    断言只基于**提交后**的独立连接读取：quota_debit 恰 2 行、sum=600、
    projection.used=600（无 lost update），并覆盖并发后的 limit gate 最终一致性。
    """
    engine = pg_env.engine
    quota = pg_env.quota
    with engine.begin() as connection:
        quota.calendar.lock_or_verify(connection)
        # 预建投影行：两个 worker 都走 SELECT ... FOR UPDATE 行锁，不竞争首行 INSERT。
        connection.execute(
            quota_projection_table.insert().values(
                quota_subject_user_id="u1",
                quota_period="2026-08",
                base_limit=500,
                extra_granted=0,
                used=0,
                last_debit_id=None,
                updated_at_utc=NOW,
            )
        )
    barrier = _ArrivalBarrier(2, timeout=30)
    errors: list[BaseException] = []
    original_update = quota._update_projection_locked

    def critical_point(connection, **kwargs):
        # 两个事务都已打开并到达投影行锁之前；放行后由 DB 行锁串行化。
        barrier.wait()
        return original_update(connection, **kwargs)

    monkeypatch.setattr(quota, "_update_projection_locked", critical_point)

    def worker(operation_id: str, pages: int) -> None:
        try:
            with engine.begin() as connection:
                lock2 = quota.calendar.lock_or_verify(connection)
                quota.append_debit(
                    connection,
                    quota_operation_id=operation_id,
                    publication_id=f"pub_{operation_id}",
                    quota_subject_user_id="u1",
                    pages=pages,
                    ownership=ownership(),
                    calendar_lock=lock2,
                    role="user",
                    effective_at_utc=NOW,
                )
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("job_a", 300)),
        threading.Thread(target=worker, args=("job_b", 300)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not t.is_alive() for t in threads)
    assert barrier.broken == []
    assert errors == [], errors
    # 提交后状态：全新连接（独立事务）验证
    with engine.connect() as connection:
        debit_count = connection.execute(
            select(func.count()).select_from(quota_debit_table)
        ).scalar_one()
        assert debit_count == 2
        total = connection.execute(select(func.sum(quota_debit_table.c.page_delta))).scalar_one()
        assert total == 600
        row = (
            connection.execute(
                select(quota_projection_table).where(
                    and_(
                        quota_projection_table.c.quota_subject_user_id == "u1",
                        quota_projection_table.c.quota_period == "2026-08",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["used"] == 600  # 无 lost update
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 600
        # 并发 limit gate 最终一致性：提交后新请求被拒
        with pytest.raises(PlatformError) as gate:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=1, role="user"
            )
        assert gate.value.code == "quota_exceeded"


def test_pg_concurrent_same_reversal_replays_persisted_id(pg_env, monkeypatch) -> None:
    """同来源 reversal 并发重放在 debit 行锁后重新查重，只应用一次投影。"""
    engine = pg_env.engine
    quota = pg_env.quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_reversal_replay",
            publication_id="pub_reversal_replay",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert quota.read_snapshot(connection, quota_subject_user_id="u1", role="user").used == 120

    barrier = _ArrivalBarrier(2, timeout=30)
    results: list[str] = []
    errors: list[BaseException] = []
    original_require = quota._require_referenced_debit

    def critical_point(connection, referenced_debit_id, *, lock):
        assert lock is True
        barrier.wait()
        return original_require(connection, referenced_debit_id, lock=lock)

    monkeypatch.setattr(quota, "_require_referenced_debit", critical_point)

    def worker() -> None:
        try:
            with engine.begin() as connection:
                calendar_lock = quota.calendar.lock_or_verify(connection)
                results.append(
                    quota.append_reversal(
                        connection,
                        referenced_debit_id=debit_id,
                        pages=120,
                        adjustment_source_namespace="billing",
                        adjustment_source_id="same-reversal",
                        ownership=ownership(),
                        calendar_lock=calendar_lock,
                        now=NOW,
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert all(not thread.is_alive() for thread in threads)
    assert barrier.broken == []
    assert errors == [], errors
    assert len(results) == 2
    assert results[0] == results[1]
    with engine.connect() as connection:
        reversal_count = connection.execute(
            select(func.count())
            .select_from(quota_debit_table)
            .where(quota_debit_table.c.entry_kind == "reversal")
        ).scalar_one()
        assert reversal_count == 1
        assert quota.read_snapshot(connection, quota_subject_user_id="u1", role="user").used == 0


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_pg_approval_service_rejects_overlong_request_id_before_reserve(pg_env, action) -> None:
    request_id = "q" * 65
    idempotency_key = f"bounded-{action}"
    with pytest.raises(PlatformError) as failure:
        if action == "approve":
            pg_env.requests.approve(
                actor=approver(),
                request_id=request_id,
                expected_version=1,
                approved_pages=None,
                idempotency_key=idempotency_key,
            )
        else:
            pg_env.requests.reject(
                actor=approver(),
                request_id=request_id,
                expected_version=1,
                idempotency_key=idempotency_key,
            )
    assert failure.value.status_code == 422
    assert failure.value.code == "validation_error"
    with pg_env.engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(platform_idempotency_table)
                .where(platform_idempotency_table.c.idempotency_key == idempotency_key)
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("close_mode", ["standalone", "supersede"])
@pytest.mark.parametrize("pending_status", ["dispatching", "unknown"])
def test_pg_pending_provider_call_blocks_price_close(
    pg_env, close_mode: str, pending_status: str
) -> None:
    """dispatch-first：未终态 provider call 的 started 事实阻止覆盖它的价格关闭。"""
    ledger, prices, version = priced_ledger(pg_env)
    call_id = ledger.prepare_provider_call(
        provider="price-race-provider",
        model="price-race-model",
        operation="generate",
        execution_kind="generation",
        attempt_id="attempt-price-race",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id=f"dispatch-first-{close_mode}-{pending_status}",
        request_fingerprint=f"fp-{close_mode}-{pending_status}",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    if pending_status == "unknown":
        ledger.mark_unknown(call_id)

    with pytest.raises(PlatformError) as failure:
        with pg_env.engine.begin() as connection:
            if close_mode == "standalone":
                prices.close_version(connection, version.id, NOW)
            else:
                prices.register(
                    connection,
                    provider="price-race-provider",
                    model="price-race-model",
                    operation="generate",
                    currency_code="USD",
                    lines=[
                        {
                            "meter": "input_tokens",
                            "unit": "token",
                            "rate": Decimal("0.000030"),
                        }
                    ],
                    effective_from_utc=NOW,
                    supersedes_version_id=version.id,
                )
    assert failure.value.code == "price_close_conflict"
    assert failure.value.status_code == 409
    with pg_env.engine.connect() as connection:
        assert (
            connection.execute(
                select(price_catalog_table.c.effective_to_utc).where(
                    price_catalog_table.c.id == version.id
                )
            ).scalar_one()
            is None
        )
        assert (
            connection.execute(select(func.count()).select_from(price_catalog_table)).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                select(provider_call_table.c.status).where(
                    provider_call_table.c.provider_call_id == call_id
                )
            ).scalar_one()
            == pending_status
        )


@pytest.mark.parametrize("close_mode", ["standalone", "supersede"])
def test_pg_price_close_first_revalidates_dispatch_before_send(pg_env, close_mode: str) -> None:
    """close-first：无覆盖价格则发送前失败；有 successor 则绑定 successor。"""
    ledger, prices, version = priced_ledger(pg_env)
    call_id = ledger.prepare_provider_call(
        provider="price-race-provider",
        model="price-race-model",
        operation="generate",
        execution_kind="generation",
        attempt_id="attempt-price-race",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id=f"close-first-{close_mode}",
        request_fingerprint=f"fp-close-first-{close_mode}",
    )
    successor = None
    with pg_env.engine.begin() as connection:
        if close_mode == "standalone":
            prices.close_version(connection, version.id, NOW)
        else:
            successor = prices.register(
                connection,
                provider="price-race-provider",
                model="price-race-model",
                operation="generate",
                currency_code="USD",
                lines=[
                    {
                        "meter": "input_tokens",
                        "unit": "token",
                        "rate": Decimal("0.000030"),
                    }
                ],
                effective_from_utc=NOW,
                supersedes_version_id=version.id,
            )

    if close_mode == "standalone":
        with pytest.raises(PlatformError) as failure:
            ledger.mark_dispatching(call_id, started_at_provider=NOW)
        assert failure.value.code == "price_not_found"
        with pg_env.engine.connect() as connection:
            assert (
                connection.execute(
                    select(provider_call_table.c.status).where(
                        provider_call_table.c.provider_call_id == call_id
                    )
                ).scalar_one()
                == "prepared"
            )
        return

    assert successor is not None
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    event_id = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=provider_measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with pg_env.engine.connect() as connection:
        assert (
            connection.execute(
                select(usage_event_table.c.price_version_id).where(
                    usage_event_table.c.usage_event_id == event_id
                )
            ).scalar_one()
            == successor.id
        )


def test_pg_dispatch_price_lock_serializes_concurrent_close(pg_env, monkeypatch) -> None:
    """dispatch 持有 price FOR UPDATE 时 close 等待；提交后 close 看到 pending 并拒绝。"""
    ledger, prices, version = priced_ledger(pg_env)
    call_id = ledger.prepare_provider_call(
        provider="price-race-provider",
        model="price-race-model",
        operation="generate",
        execution_kind="generation",
        attempt_id="attempt-price-race",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id="concurrent-dispatch-first",
        request_fingerprint="fp-concurrent-dispatch-first",
    )
    price_locked = threading.Event()
    release_dispatch = threading.Event()
    close_started = threading.Event()
    dispatch_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    original_locked_price = ledger._locked_price

    def hold_price_lock(connection, **kwargs):
        price = original_locked_price(connection, **kwargs)
        price_locked.set()
        if not release_dispatch.wait(30):
            raise TimeoutError("dispatch price lock release timed out")
        return price

    monkeypatch.setattr(ledger, "_locked_price", hold_price_lock)

    def dispatch_worker() -> None:
        try:
            ledger.mark_dispatching(call_id, started_at_provider=NOW)
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            dispatch_errors.append(exc)

    def close_worker() -> None:
        try:
            with pg_env.engine.begin() as connection:
                close_started.set()
                prices.close_version(connection, version.id, NOW)
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            close_errors.append(exc)

    dispatch_thread = threading.Thread(target=dispatch_worker)
    close_thread = threading.Thread(target=close_worker)
    dispatch_thread.start()
    assert price_locked.wait(30)
    close_thread.start()
    assert close_started.wait(30)
    assert dispatch_thread.is_alive()
    assert close_thread.is_alive()
    release_dispatch.set()
    dispatch_thread.join(timeout=60)
    close_thread.join(timeout=60)

    assert not dispatch_thread.is_alive()
    assert not close_thread.is_alive()
    assert dispatch_errors == []
    assert len(close_errors) == 1
    assert isinstance(close_errors[0], PlatformError)
    assert close_errors[0].code == "price_close_conflict"


@pytest.mark.parametrize("close_mode", ["standalone", "supersede"])
def test_pg_concurrent_close_first_revalidates_waiting_dispatch(
    pg_env, monkeypatch, close_mode: str
) -> None:
    """close 事务先持有 price 锁；等待中的 dispatch 醒来后重验覆盖区间。"""
    ledger, prices, version = priced_ledger(pg_env)
    call_id = ledger.prepare_provider_call(
        provider="price-race-provider",
        model="price-race-model",
        operation="generate",
        execution_kind="generation",
        attempt_id="attempt-price-race",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id=f"concurrent-close-first-{close_mode}",
        request_fingerprint=f"fp-concurrent-close-first-{close_mode}",
    )
    selected_old_price = threading.Event()
    dispatch_errors: list[BaseException] = []
    original_select_for = prices.select_for

    def signal_selection(connection, **kwargs):
        selected = original_select_for(connection, **kwargs)
        selected_old_price.set()
        return selected

    monkeypatch.setattr(prices, "select_for", signal_selection)

    def dispatch_worker() -> None:
        try:
            ledger.mark_dispatching(call_id, started_at_provider=NOW)
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            dispatch_errors.append(exc)

    successor = None
    close_connection = pg_env.engine.connect()
    close_transaction = close_connection.begin()
    dispatch_thread: threading.Thread | None = None
    try:
        if close_mode == "standalone":
            prices.close_version(close_connection, version.id, NOW)
        else:
            successor = prices.register(
                close_connection,
                provider="price-race-provider",
                model="price-race-model",
                operation="generate",
                currency_code="USD",
                lines=[
                    {
                        "meter": "input_tokens",
                        "unit": "token",
                        "rate": Decimal("0.000030"),
                    }
                ],
                effective_from_utc=NOW,
                supersedes_version_id=version.id,
            )
        dispatch_thread = threading.Thread(target=dispatch_worker)
        dispatch_thread.start()
        assert selected_old_price.wait(30)
        assert dispatch_thread.is_alive()
        close_transaction.commit()
    finally:
        if close_transaction.is_active:
            close_transaction.rollback()
        close_connection.close()
        if dispatch_thread is not None:
            dispatch_thread.join(timeout=60)

    assert dispatch_thread is not None
    assert not dispatch_thread.is_alive()
    if close_mode == "standalone":
        assert len(dispatch_errors) == 1
        assert isinstance(dispatch_errors[0], PlatformError)
        assert dispatch_errors[0].code == "price_not_found"
        with pg_env.engine.connect() as connection:
            assert (
                connection.execute(
                    select(provider_call_table.c.status).where(
                        provider_call_table.c.provider_call_id == call_id
                    )
                ).scalar_one()
                == "prepared"
            )
        return

    assert dispatch_errors == []
    assert successor is not None
    event_id = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=provider_measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with pg_env.engine.connect() as connection:
        assert (
            connection.execute(
                select(usage_event_table.c.price_version_id).where(
                    usage_event_table.c.usage_event_id == event_id
                )
            ).scalar_one()
            == successor.id
        )


def test_pg_max_root_id_uses_bounded_physical_provider_call_id(pg_env) -> None:
    ledger, _prices, _version = priced_ledger(pg_env)
    operation_calls: list[str] = []

    result = run_provider_call_with_usage(
        operation=lambda context, request: operation_calls.append(context.provider_call_id)
        or "sent",
        context=ProviderCallContext(
            provider="price-race-provider",
            operation="generate",
            provider_call_id="r" * 64,
            attempt_id="attempt-max-root",
            deadline_utc=NOW + timedelta(minutes=5),
            resource_id="resource-max-root",
        ),
        model="price-race-model",
        lifecycle=UsageLedgerLifecycle(ledger),
        measurement_extractor=lambda value, context, failure: provider_measurement(),
        ownership_provider=lambda context: ownership(),
        execution_kind="generation",
        execution_id="gen-max-root",
        request_fingerprint="fp-max-root",
        policy=RetryPolicy(synchronous_attempts=1),
        circuits=CircuitBreakerRegistry(),
        now=lambda: NOW,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.state == "succeeded"
    assert len(operation_calls) == 1
    assert operation_calls[0].startswith("pc_")
    assert len(operation_calls[0]) == 64
    with pg_env.engine.connect() as connection:
        persisted_id = connection.execute(
            select(provider_call_table.c.provider_call_id)
        ).scalar_one()
    assert persisted_id == operation_calls[0]


def test_pg_price_lock_delay_past_deadline_never_sends(pg_env, monkeypatch) -> None:
    """真实 price 行锁跨过绝对 deadline 后，call 必须 not_sent 且绝不物理发送。"""
    clock = MutableClock(NOW)
    calendar = BusinessCalendarService(
        pg_env.engine, SqlAlchemyDatabaseClock(pg_env.engine), "Asia/Shanghai"
    )
    prices = PriceCatalogService(pg_env.engine, clock)
    ledger = UsageLedger(pg_env.engine, clock, calendar, prices)
    with pg_env.engine.begin() as connection:
        calendar.lock_or_verify(connection)
        version = prices.register(
            connection,
            provider="deadline-provider",
            model="deadline-model",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=NOW,
        )

    deadline = NOW + timedelta(seconds=1)
    selected_before_lock = threading.Event()
    operation_calls: list[str] = []
    results = []
    errors: list[BaseException] = []
    original_select = prices.select_for

    def signal_selected(connection, **kwargs):
        selected = original_select(connection, **kwargs)
        selected_before_lock.set()
        return selected

    monkeypatch.setattr(prices, "select_for", signal_selected)

    def worker() -> None:
        try:
            results.append(
                run_provider_call_with_usage(
                    operation=lambda context, request: operation_calls.append(
                        context.provider_call_id
                    )
                    or "sent",
                    context=ProviderCallContext(
                        provider="deadline-provider",
                        operation="generate",
                        provider_call_id="pc-deadline-lock",
                        attempt_id="attempt-deadline-lock",
                        deadline_utc=deadline,
                        resource_id="resource-deadline-lock",
                    ),
                    model="deadline-model",
                    lifecycle=UsageLedgerLifecycle(ledger),
                    measurement_extractor=lambda value, context, failure: provider_measurement(),
                    ownership_provider=lambda context: ownership(),
                    execution_kind="generation",
                    execution_id="gen-deadline-lock",
                    request_fingerprint="fp-deadline-lock",
                    policy=RetryPolicy(synchronous_attempts=1),
                    circuits=CircuitBreakerRegistry(),
                    now=lambda: clock.now,
                    sleep=lambda _delay: None,
                    jitter=lambda _delay: 0,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            errors.append(exc)

    lock_connection = pg_env.engine.connect()
    lock_transaction = lock_connection.begin()
    thread: threading.Thread | None = None
    try:
        lock_connection.execute(
            select(price_catalog_table.c.id)
            .where(price_catalog_table.c.id == version.id)
            .with_for_update()
        ).one()
        thread = threading.Thread(target=worker)
        thread.start()
        assert selected_before_lock.wait(30)
        assert thread.is_alive()  # 已 select，真实阻塞在 price FOR UPDATE
        clock.now = deadline
        lock_transaction.commit()
    finally:
        if lock_transaction.is_active:
            lock_transaction.rollback()
        lock_connection.close()
        if thread is not None:
            thread.join(timeout=60)

    assert thread is not None
    assert not thread.is_alive()
    assert errors == []
    assert operation_calls == []
    assert len(results) == 1
    assert (results[0].state, results[0].error_class, results[0].attempts) == (
        "not_sent",
        "deadline_exceeded",
        0,
    )
    with pg_env.engine.connect() as connection:
        call = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).all()
    assert call["status"] == "not_sent"
    assert call["started_at_utc"] is None
    assert usage_rows == []


def test_pg_fixed_started_price_lock_crossing_deadline_returns_not_sent(
    pg_env, monkeypatch
) -> None:
    """固定 started fact 不重采样，但 price 锁后 current clock 过期必须 not_sent。"""
    clock = MutableClock(NOW)
    calendar = BusinessCalendarService(
        pg_env.engine, SqlAlchemyDatabaseClock(pg_env.engine), "Asia/Shanghai"
    )
    prices = PriceCatalogService(pg_env.engine, clock)
    ledger = UsageLedger(pg_env.engine, clock, calendar, prices)
    with pg_env.engine.begin() as connection:
        calendar.lock_or_verify(connection)
        version = prices.register(
            connection,
            provider="fixed-deadline-provider",
            model="fixed-deadline-model",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=NOW,
        )
    deadline = NOW + timedelta(seconds=1)
    call_id = ledger.prepare_provider_call(
        provider="fixed-deadline-provider",
        model="fixed-deadline-model",
        operation="generate",
        execution_kind="generation",
        execution_id="gen-fixed-deadline-lock",
        attempt_id="attempt-fixed-deadline-lock",
        deadline_utc=deadline,
        request_fingerprint="fp-fixed-deadline-lock",
    )

    selected_before_lock = threading.Event()
    errors: list[BaseException] = []
    results: list[bool] = []
    original_select = prices.select_for

    def signal_selected(connection, **kwargs):
        selected = original_select(connection, **kwargs)
        selected_before_lock.set()
        return selected

    monkeypatch.setattr(prices, "select_for", signal_selected)

    def worker() -> None:
        try:
            results.append(ledger.mark_dispatching(call_id, started_at_provider=NOW))
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            errors.append(exc)

    lock_connection = pg_env.engine.connect()
    lock_transaction = lock_connection.begin()
    thread: threading.Thread | None = None
    try:
        lock_connection.execute(
            select(price_catalog_table.c.id)
            .where(price_catalog_table.c.id == version.id)
            .with_for_update()
        ).one()
        thread = threading.Thread(target=worker)
        thread.start()
        assert selected_before_lock.wait(30)
        assert thread.is_alive()
        clock.now = deadline
        lock_transaction.commit()
    finally:
        if lock_transaction.is_active:
            lock_transaction.rollback()
        lock_connection.close()
        if thread is not None:
            thread.join(timeout=60)

    assert thread is not None
    assert not thread.is_alive()
    assert errors == []
    assert results == [False]
    with pg_env.engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert call["status"] == "not_sent"
    assert call["started_at_utc"] is None


def test_pg_final_price_relock_crossing_deadline_returns_not_sent(pg_env, monkeypatch) -> None:
    """第二次时刻切换价格版本时，最终 price 锁等待跨 deadline 也必须原子 not_sent。"""
    clock = MutableClock(NOW + timedelta(seconds=1))
    calendar = BusinessCalendarService(
        pg_env.engine, SqlAlchemyDatabaseClock(pg_env.engine), "Asia/Shanghai"
    )
    prices = PriceCatalogService(pg_env.engine, clock)
    ledger = UsageLedger(pg_env.engine, clock, calendar, prices)
    with pg_env.engine.begin() as connection:
        calendar.lock_or_verify(connection)
        first = prices.register(
            connection,
            provider="deadline-relock-provider",
            model="deadline-relock-model",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=NOW,
        )
        second = prices.register(
            connection,
            provider="deadline-relock-provider",
            model="deadline-relock-model",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000030")}],
            effective_from_utc=NOW + timedelta(seconds=1),
            supersedes_version_id=first.id,
        )
    deadline = NOW + timedelta(seconds=2)
    call_id = ledger.prepare_provider_call(
        provider="deadline-relock-provider",
        model="deadline-relock-model",
        operation="generate",
        execution_kind="generation",
        execution_id="gen-deadline-relock",
        attempt_id="attempt-deadline-relock",
        deadline_utc=deadline,
        request_fingerprint="fp-deadline-relock",
    )

    selected_second = threading.Event()
    errors: list[BaseException] = []
    results: list[bool] = []
    sample_count = 0
    original_select = prices.select_for

    def signal_second_selection(connection, **kwargs):
        selected = original_select(connection, **kwargs)
        if selected.id == second.id:
            selected_second.set()
        return selected

    def sample_send_time() -> datetime:
        nonlocal sample_count
        sample_count += 1
        return NOW if sample_count == 1 else clock.now

    monkeypatch.setattr(prices, "select_for", signal_second_selection)

    def worker() -> None:
        try:
            results.append(ledger.mark_dispatching(call_id, started_at_provider=sample_send_time))
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            errors.append(exc)

    lock_connection = pg_env.engine.connect()
    lock_transaction = lock_connection.begin()
    thread: threading.Thread | None = None
    try:
        lock_connection.execute(
            select(price_catalog_table.c.id)
            .where(price_catalog_table.c.id == second.id)
            .with_for_update()
        ).one()
        thread = threading.Thread(target=worker)
        thread.start()
        assert selected_second.wait(30)
        assert thread.is_alive()
        clock.now = deadline
        lock_transaction.commit()
    finally:
        if lock_transaction.is_active:
            lock_transaction.rollback()
        lock_connection.close()
        if thread is not None:
            thread.join(timeout=60)

    assert thread is not None
    assert not thread.is_alive()
    assert errors == []
    assert results == [False]
    # Initial + post-first-lock dynamic samples selected two price versions;
    # authoritative current-clock expiry after the final lock short-circuits a third.
    assert sample_count == 2
    with pg_env.engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert call["status"] == "not_sent"
    assert call["started_at_utc"] is None


# ---------------------------------------------------------------------------
# H9 barrier：并发 approve 单赢家（临界点 = request 行 SELECT ... FOR UPDATE）
# ---------------------------------------------------------------------------


def test_pg_concurrent_approve_single_winner(pg_env, monkeypatch) -> None:
    """H9：两个独立事务竞争 approve 同一 pending request。

    临界点 barrier 包在 ``_require_approvable``（真实锁点：request 行 FOR UPDATE）前，
    两个事务均已 reserve 完成。行锁串行化 → 恰一个成功 + 一个 409
    already_processed（确定性）。join 后新连接验证 request/credit/projection/audit/
    outbox 唯一性与 credit_entry_id 一致。
    """
    engine = pg_env.engine
    requests = pg_env.requests
    seed_identity(engine)
    created = create_pending(pg_env, pages=100, key="create-pg-1")
    barrier = _ArrivalBarrier(2, timeout=30)
    outcomes: list[str] = []
    ok_results: list[dict] = []
    original_approvable = requests._require_approvable

    def critical_point(tx, *, request_id, expected_version, actor):
        # 两个事务都已打开、即将执行 request 行 SELECT ... FOR UPDATE。
        barrier.wait()
        return original_approvable(
            tx, request_id=request_id, expected_version=expected_version, actor=actor
        )

    monkeypatch.setattr(requests, "_require_approvable", critical_point)

    def worker(key: str) -> None:
        try:
            ok_results.append(
                requests.approve(
                    actor=approver(),
                    request_id=created["id"],
                    expected_version=1,
                    approved_pages=80,
                    idempotency_key=key,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 收集失败码（赢家外的确定性 already_processed）
            outcomes.append(str(getattr(exc, "code", type(exc).__name__)))

    threads = [threading.Thread(target=worker, args=(f"k-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not t.is_alive() for t in threads)
    assert barrier.broken == []
    # 行锁串行化 → 恰好一个成功 + 一个 already_processed（真实 service contract）
    assert sorted(outcomes) == ["already_processed"]
    assert len(ok_results) == 1
    result = ok_results[0]
    assert result["id"] == created["id"]
    assert result["version"] == 2
    assert result["status"] == "approved"
    assert result["approved_pages"] == 80
    assert result["quota_period"] == "2026-08"
    credit_id = result["credit_entry_id"]
    assert credit_id is not None
    # 提交后状态：全新连接验证
    with engine.connect() as connection:
        request_row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert request_row["status"] == "approved"
        assert request_row["version"] == 2
        assert request_row["credit_entry_id"] == credit_id
        credits = (
            connection.execute(
                select(quota_debit_table).where(quota_debit_table.c.entry_kind == "credit")
            )
            .mappings()
            .all()
        )
        assert len(credits) == 1  # 恰好一条 credit
        assert credits[0]["quota_debit_id"] == credit_id
        assert credits[0]["page_delta"] == -80
        assert credits[0]["adjustment_source_namespace"] == "quota_request"
        assert credits[0]["adjustment_source_id"] == created["id"]
        projection = (
            connection.execute(
                select(quota_projection_table).where(
                    and_(
                        quota_projection_table.c.quota_subject_user_id == "u1",
                        quota_projection_table.c.quota_period == "2026-08",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert projection["extra_granted"] == 80  # 只增加一次
        audit_count = connection.execute(
            select(func.count()).select_from(platform_audit_table)
        ).scalar_one()
        assert audit_count == 1  # 恰一次
        events = connection.execute(select(tests_outbox_enqueued)).mappings().all()
        assert len(events) == 1  # 唯一 outbox 事件
        assert events[0]["event_type"] == "quota_approved"
        assert events[0]["aggregate_type"] == "quota_request"
        assert events[0]["aggregate_id"] == created["id"]
        assert events[0]["transition_version"] == 2
        assert events[0]["payload_json"] == {"request_id": created["id"]}
        expected_fp = ledger_fingerprint("quota_approved", {"request_id": created["id"]})
        assert events[0]["payload_fingerprint"] == expected_fp


# ---------------------------------------------------------------------------
# H6：Asia/Shanghai 精确月界
# ---------------------------------------------------------------------------


def test_pg_month_boundary_precise_instants(pg_env) -> None:
    """H6：精确月界——2026-08-31T15:59:59Z 归 2026-08；2026-08-31T16:00:00Z 归 2026-09。

    Asia/Shanghai（UTC+8，无 DST）：15:59:59Z = 8 月末最后一刻；16:00:00Z =
    2026-09-01 00:00 +08。同时覆盖同一请求时钟一致性：recorded_period 用同一业务
    now（FixedClock NOW=2026-08-05 → 2026-08），与 effective_period 解耦。
    """
    engine = pg_env.engine
    quota = pg_env.quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="job_aug",
            publication_id="pub_aug",
            quota_subject_user_id="u1",
            pages=10,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=AUG_LAST_INSTANT,
        )
        quota.append_debit(
            connection,
            quota_operation_id="job_sep",
            publication_id="pub_sep",
            quota_subject_user_id="u1",
            pages=20,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=SEP_FIRST_INSTANT,
        )
        rows = (
            connection.execute(
                select(
                    quota_debit_table.c.quota_operation_id,
                    quota_debit_table.c.effective_period,
                    quota_debit_table.c.recorded_period,
                ).order_by(quota_debit_table.c.effective_at_utc)
            )
            .mappings()
            .all()
        )
        projections = (
            connection.execute(
                select(quota_projection_table).order_by(quota_projection_table.c.quota_period)
            )
            .mappings()
            .all()
        )
    assert [(r["quota_operation_id"], r["effective_period"]) for r in rows] == [
        ("job_aug", "2026-08"),
        ("job_sep", "2026-09"),
    ]
    # 同一请求时钟一致性：recorded_period 恒为业务 now（2026-08），与 effective 解耦
    assert all(r["recorded_period"] == "2026-08" for r in rows)
    assert [(p["quota_period"], p["used"]) for p in projections] == [
        ("2026-08", 10),
        ("2026-09", 20),
    ]


# ---------------------------------------------------------------------------
# 0011 迁移 parity：对象存在性（pg_catalog）
# ---------------------------------------------------------------------------


def test_pg_migration_parity_0011_head_objects_in_temp_schema(pg_env) -> None:
    """0011 真实迁移产物：alembic_version=0011；触发器/函数/partial index/约束齐备。

    全部经 pg_catalog 在临时 schema 内验证（current_schema() == 随机 schema）；
    partial index 的 unique/columns/predicate 经 pg_index（indisunique/indkey/
    indpred + pg_get_expr）读取并用规范化 helper 严格等价比较。
    """
    engine = pg_env.engine
    with engine.connect() as connection:
        assert connection.execute(text("SELECT current_schema()")).scalar_one() == pg_env.schema
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0011_usage_quota"
        )
        triggers = set(
            connection.execute(
                text(
                    "SELECT t.tgname, c.relname FROM pg_trigger t"
                    " JOIN pg_class c ON c.oid = t.tgrelid"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = current_schema() AND NOT t.tgisinternal"
                )
            ).all()
        )
        for trigger, table in [
            ("trg_usage_event_no_update", "usage_event"),
            ("trg_usage_event_no_delete", "usage_event"),
            ("trg_usage_event_measurement_sources", "usage_event"),
            ("trg_quota_debit_no_update", "quota_debit"),
            ("trg_quota_debit_no_delete", "quota_debit"),
            ("trg_calendar_version_no_update", "business_calendar_version"),
            ("trg_calendar_version_no_delete", "business_calendar_version"),
            ("trg_price_line_no_update", "price_catalog_line"),
            ("trg_price_line_no_delete", "price_catalog_line"),
            ("trg_price_catalog_no_update", "price_catalog"),
            ("trg_price_catalog_no_delete", "price_catalog"),
        ]:
            assert (trigger, table) in triggers, (trigger, table)
        functions = set(
            connection.execute(
                text(
                    "SELECT p.proname FROM pg_proc p"
                    " JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE n.nspname = current_schema()"
                )
            ).scalars()
        )
        for function in [
            "prevent_usage_event_mutation",
            "validate_usage_event_measurement_sources",
            "prevent_quota_debit_mutation",
            "prevent_business_calendar_mutation",
            "prevent_price_catalog_line_mutation",
            "prevent_price_catalog_delete",
            "prevent_price_catalog_history_rewrite",
        ]:
            assert function in functions, function
        # partial index：pg_index.indisunique/indkey/indpred + pg_get_expr
        rows = (
            connection.execute(
                text(
                    "SELECT i.relname AS indexname, ix.indisunique,"
                    " pg_get_expr(ix.indpred, ix.indrelid) AS pred_expr,"
                    " (SELECT array_agg(a.attname ORDER BY k.ord)"
                    "    FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)"
                    "    JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.attnum"
                    "   WHERE a.attnum > 0) AS columns"
                    " FROM pg_index ix"
                    " JOIN pg_class i ON i.oid = ix.indexrelid"
                    " JOIN pg_class t ON t.oid = ix.indrelid"
                    " JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE n.nspname = current_schema()"
                    "   AND i.relname IN ('uq_price_open_interval', 'uq_quota_request_pending')"
                )
            )
            .mappings()
            .all()
        )
        indexes = {row["indexname"]: dict(row) for row in rows}
        assert set(indexes) == {"uq_price_open_interval", "uq_quota_request_pending"}
        open_interval = indexes["uq_price_open_interval"]
        assert open_interval["indisunique"] is True
        assert tuple(open_interval["columns"]) == ("provider", "model", "operation")
        _assert_predicate_equivalent(open_interval["pred_expr"], "effective_to_utc IS NULL")
        pending = indexes["uq_quota_request_pending"]
        assert pending["indisunique"] is True
        assert tuple(pending["columns"]) == ("applicant_user_id", "quota_period")
        _assert_predicate_equivalent(pending["pred_expr"], "status = 'pending'")
        constraints = set(
            connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c"
                    " JOIN pg_class cl ON cl.oid = c.conrelid"
                    " JOIN pg_namespace n ON n.oid = cl.relnamespace"
                    " WHERE n.nspname = current_schema()"
                )
            ).scalars()
        )
        for constraint in [
            "ck_provider_call_attempt_or_generation",
            "ck_usage_event_execution_identity",
            "ck_price_line_granularity_int32",
            "ck_price_line_granularity_integer",
            "ck_price_line_granularity_positive",
            "ck_price_line_minimum_int32",
            "ck_price_line_minimum_integer",
            "ck_price_line_minimum_nonnegative",
            "ck_price_line_rate_finite",
            "ck_price_line_rate_max",
            "ck_price_line_rate_nonnegative",
            "ck_price_line_rate_numeric",
            "ck_price_line_rounding_rule",
            "ck_quota_debit_credit_shape",
            "ck_quota_debit_debit_shape",
            "ck_quota_request_status",
            "uq_quota_debit_adjustment",
            "uq_quota_debit_operation",
            "uq_usage_provider_call",
        ]:
            assert constraint in constraints, constraint


@pytest.mark.parametrize(
    ("rate", "granularity", "minimum_quantity", "rounding_rule", "constraint_name"),
    [
        ("-0.0000000001", 1, 0, "floor", "ck_price_line_rate_nonnegative"),
        ("NaN", 1, 0, "floor", "ck_price_line_rate_finite"),
        ("Infinity", 1, 0, "floor", "ck_price_line_rate_finite"),
        ("0", 0, 0, "floor", "ck_price_line_granularity_positive"),
        ("0", 1, -1, "floor", "ck_price_line_minimum_nonnegative"),
        ("0", 1, 0, "round_half_up", "ck_price_line_rounding_rule"),
    ],
)
def test_pg_price_line_rejects_invalid_raw_insert(
    pg_env,
    rate: str,
    granularity: int,
    minimum_quantity: int,
    rounding_rule: str,
    constraint_name: str,
) -> None:
    """真实 PG Alembic 表必须以命名 CHECK 拒绝四类非法 price line。"""
    with pytest.raises(Exception) as failure:
        with pg_env.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog_line (id, price_version_id, meter, unit, rate,"
                    " billing_granularity, minimum_billable_quantity, rounding_rule) VALUES"
                    " (:id, 'price-raw', 'input_tokens', 'token', :rate, :granularity,"
                    " :minimum_quantity, :rounding_rule)"
                ),
                {
                    "id": f"line-{constraint_name}",
                    "rate": rate,
                    "granularity": granularity,
                    "minimum_quantity": minimum_quantity,
                    "rounding_rule": rounding_rule,
                },
            )
    assert constraint_name in str(failure.value)


@pytest.mark.parametrize(
    ("case", "rate", "granularity", "minimum_quantity"),
    [
        ("rate", "100000000000000000000.0000000000", 1, 0),
        ("granularity", "0", 2147483648, 0),
        ("minimum", "0", 1, 2147483648),
    ],
)
def test_pg_price_line_rejects_storage_boundary_overflow(
    pg_env,
    case: str,
    rate: str,
    granularity: int,
    minimum_quantity: int,
) -> None:
    """PG typmod/int32 storage boundary rejects the first value beyond each legal maximum."""
    with pytest.raises(DataError):
        with pg_env.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog_line (id, price_version_id, meter, unit, rate,"
                    " billing_granularity, minimum_billable_quantity, rounding_rule) VALUES"
                    " (:id, 'price-raw', 'input_tokens', 'token', :rate, :granularity,"
                    " :minimum_quantity, 'floor')"
                ),
                {
                    "id": f"line-overflow-{case}",
                    "rate": rate,
                    "granularity": granularity,
                    "minimum_quantity": minimum_quantity,
                },
            )


def test_pg_price_line_accepts_exact_storage_maxima(pg_env) -> None:
    """PG 精确接受 Numeric(30,10) 与 int32 的最后一个合法值且不发生舍入。"""
    exact_rate = Decimal("99999999999999999999.9999999999")
    with pg_env.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO price_catalog_line (id, price_version_id, meter, unit, rate,"
                " billing_granularity, minimum_billable_quantity, rounding_rule) VALUES"
                " ('line-exact-max', 'price-raw', 'input_tokens', 'token', :rate,"
                " 2147483647, 2147483647, 'half_up')"
            ),
            {"rate": exact_rate},
        )
    with pg_env.engine.connect() as connection:
        row = connection.execute(
            select(
                price_catalog_line_table.c.rate,
                price_catalog_line_table.c.billing_granularity,
                price_catalog_line_table.c.minimum_billable_quantity,
            ).where(price_catalog_line_table.c.id == "line-exact-max")
        ).one()
    assert row == (exact_rate, 2147483647, 2147483647)


def test_pg_provider_call_requires_attempt_or_generation_identity(pg_env) -> None:
    constraint_name = "ck_provider_call_attempt_or_generation"
    with pytest.raises(Exception) as failure:
        with pg_env.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
                    " execution_kind, execution_id, request_fingerprint, deadline_utc, status,"
                    " prepared_at_utc, created_at_utc) VALUES"
                    " ('pc-no-identity', 'p', 'm', 'o', 'generation', 'gen-1', 'fp-1',"
                    " :deadline, 'prepared', :now, :now)"
                ),
                {"deadline": NOW + timedelta(minutes=5), "now": NOW},
            )
    assert constraint_name in str(failure.value)


@pytest.mark.parametrize("event_kind", ["usage_adjustment", "cost_adjustment"])
def test_pg_usage_adjustment_requires_execution_identity(pg_env, event_kind: str) -> None:
    constraint_name = "ck_usage_event_execution_identity"
    with pytest.raises(Exception) as failure:
        with pg_env.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO usage_event (usage_event_id, event_kind, result,"
                    " event_fingerprint, ownership_json, cost_center_key, started_at_utc,"
                    " completed_at_utc, effective_calendar_version_id, effective_at_utc,"
                    " effective_period, recorded_calendar_version_id, recorded_at_utc,"
                    " recorded_period, created_at_utc, adjustment_source_namespace,"
                    " adjustment_source_id, adjustment_allocation_key,"
                    " referenced_usage_event_id) VALUES"
                    " (:event_id, :event_kind, 'adjusted', 'fp', '{}', 'user:u1', :now,"
                    " :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
                    " 'meter_recheck', 'recheck-identity', 'allocation-1', 'ue-ref')"
                ),
                {
                    "event_id": f"ue-{event_kind}-no-execution",
                    "event_kind": event_kind,
                    "now": NOW,
                },
            )
    assert constraint_name in str(failure.value)


# ---------------------------------------------------------------------------
# 0011 迁移 parity：每个 migration-only trigger 的实际 mutation 验证
# ---------------------------------------------------------------------------


def _seed_parity_rows(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO business_calendar_version (id, version_id, timezone,"
                " effective_from_utc, created_at_utc) VALUES"
                " ('instance', 'cal_1', 'Asia/Shanghai', :now, :now)"
            ),
            {"now": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
                " effective_from_utc, created_at_utc) VALUES"
                " ('p1', 'dashscope', 'qwen-max', 'chat', 'CNY', :now, :now)"
            ),
            {"now": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO price_catalog_line (id, price_version_id, meter, unit, rate,"
                " billing_granularity, minimum_billable_quantity, rounding_rule) VALUES"
                " ('pl1', 'p1', 'input_tokens', 'token', 0.001, 1, 0, 'half_up')"
            ),
        )
        connection.execute(
            text(
                "INSERT INTO quota_debit (quota_debit_id, entry_kind, page_delta,"
                " entry_fingerprint, quota_operation_id, publication_id,"
                " quota_subject_user_id, quota_period, cost_center_key, ownership_json,"
                " effective_calendar_version_id, effective_at_utc, effective_period,"
                " recorded_calendar_version_id, recorded_at_utc, recorded_period,"
                " created_at_utc) VALUES"
                " ('qd_1', 'debit', 10, 'fp', 'op_1', 'pub_1', 'u1', '2026-08',"
                " 'user:u1', '{}', 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now)"
            ),
            {"now": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO usage_event (usage_event_id, event_kind, result,"
                " event_fingerprint, ownership_json, cost_center_key, started_at_utc,"
                " completed_at_utc, effective_calendar_version_id, effective_at_utc,"
                " effective_period, recorded_calendar_version_id, recorded_at_utc,"
                " recorded_period, created_at_utc, execution_kind, execution_id, stage,"
                " resource_kind) VALUES"
                " ('ue_local', 'local_usage', 'succeeded', 'fp', '{}', 'user:u1',"
                " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
                " 'ocr', 'exec1', 'extract', 'pdf')"
            ),
            {"now": NOW},
        )


def _expect_db_error(engine, sql: str, message_fragment: str, params: dict | None = None) -> None:
    """每个预期 DB error 用独立事务（engine.begin），避免 aborted transaction 污染。"""
    bind = dict(params or {})
    with pytest.raises(Exception) as exc:
        with engine.begin() as connection:
            connection.execute(text(sql), bind)
    assert message_fragment in str(exc.value)


def test_pg_parity_usage_event_and_quota_debit_immutable(pg_env) -> None:
    engine = pg_env.engine
    _seed_parity_rows(engine)
    _expect_db_error(
        engine,
        "UPDATE usage_event SET result = 'failed' WHERE usage_event_id = 'ue_local'",
        "usage_event is immutable",
    )
    _expect_db_error(
        engine,
        "DELETE FROM usage_event WHERE usage_event_id = 'ue_local'",
        "usage_event is immutable",
    )
    _expect_db_error(
        engine,
        "UPDATE quota_debit SET page_delta = 99 WHERE quota_debit_id = 'qd_1'",
        "quota_debit is immutable",
    )
    _expect_db_error(
        engine,
        "DELETE FROM quota_debit WHERE quota_debit_id = 'qd_1'",
        "quota_debit is immutable",
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM usage_event")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM quota_debit")).scalar_one() == 1


def test_pg_parity_calendar_singleton_immutable(pg_env) -> None:
    engine = pg_env.engine
    _seed_parity_rows(engine)
    _expect_db_error(
        engine,
        "UPDATE business_calendar_version SET version_id = 'cal_2' WHERE id = 'instance'",
        "business_calendar_version is immutable",
    )
    _expect_db_error(
        engine,
        "DELETE FROM business_calendar_version WHERE id = 'instance'",
        "business_calendar_version is immutable",
    )
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM business_calendar_version")).scalar_one()
            == 1
        )


def test_pg_parity_price_catalog_line_immutable(pg_env) -> None:
    engine = pg_env.engine
    _seed_parity_rows(engine)
    _expect_db_error(
        engine,
        "UPDATE price_catalog_line SET rate = 0.5 WHERE id = 'pl1'",
        "price_catalog_line is immutable",
    )
    _expect_db_error(
        engine,
        "DELETE FROM price_catalog_line WHERE id = 'pl1'",
        "price_catalog_line is immutable",
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM price_catalog_line")).scalar_one() == 1


def test_pg_parity_price_catalog_delete_and_close_rules(pg_env) -> None:
    """price_catalog：不可 DELETE；UPDATE 只允许一次性 close（其余重写全部拒绝）。"""
    engine = pg_env.engine
    _seed_parity_rows(engine)
    # DELETE → append-only 拒绝
    _expect_db_error(
        engine,
        "DELETE FROM price_catalog WHERE id = 'p1'",
        "price_catalog is append-only",
    )
    # 非 close 的 UPDATE 重写（currency）→ 拒绝
    _expect_db_error(
        engine,
        "UPDATE price_catalog SET currency_code = 'USD' WHERE id = 'p1'",
        "price_catalog update is append-only",
    )
    # close 同时改 id → 拒绝
    _expect_db_error(
        engine,
        "UPDATE price_catalog SET id = 'p1_rewritten', effective_to_utc = :to WHERE id = 'p1'",
        "price_catalog update is append-only",
        {"to": NOW + timedelta(days=1)},
    )
    # close 同时改 supersedes_version_id（NULL→非 NULL）→ 拒绝（null-safe 比较）
    _expect_db_error(
        engine,
        "UPDATE price_catalog SET supersedes_version_id = 'p0', effective_to_utc = :to"
        " WHERE id = 'p1'",
        "price_catalog update is append-only",
        {"to": NOW + timedelta(days=1)},
    )
    # 合法 close 一次（effective_to_utc NULL→非 NULL，其余字段不变）
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE price_catalog SET effective_to_utc = :to WHERE id = 'p1'"),
            {"to": NOW + timedelta(days=1)},
        )
    # 第二次 close → 拒绝（一次性）
    _expect_db_error(
        engine,
        "UPDATE price_catalog SET effective_to_utc = :to WHERE id = 'p1'",
        "price_catalog update is append-only",
        {"to": NOW + timedelta(days=2)},
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM price_catalog")).scalar_one() == 1
        effective_to = connection.execute(
            text("SELECT effective_to_utc FROM price_catalog WHERE id = 'p1'")
        ).scalar_one()
        assert effective_to == NOW + timedelta(days=1)


def test_pg_parity_measurement_sources_trigger(pg_env) -> None:
    """usage_event 的 measurement_sources INSERT 触发器：非法值/非 object 拒绝，合法通过。"""
    engine = pg_env.engine
    _seed_parity_rows(engine)
    base = (
        "INSERT INTO usage_event (usage_event_id, event_kind, result,"
        " event_fingerprint, ownership_json, cost_center_key, started_at_utc,"
        " completed_at_utc, effective_calendar_version_id, effective_at_utc,"
        " effective_period, recorded_calendar_version_id, recorded_at_utc,"
        " recorded_period, created_at_utc, execution_kind, execution_id, stage,"
        " resource_kind, measurement_sources) VALUES"
        " ('ue_ms_%s', 'local_usage', 'succeeded', 'fp', '{}', 'user:u1',"
        " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
        " 'ocr', 'exec_ms_%s', 'extract', 'pdf', %s)"
    )

    def measurement_sql(marker: str, payload_json: str) -> str:
        return base % (marker, marker, "'" + payload_json.replace("'", "''") + "'")

    _expect_db_error(
        engine,
        measurement_sql("bad", '{"tokens": "provider_sniffed"}'),
        "invalid value",
        params={"now": NOW},
    )
    _expect_db_error(
        engine,
        measurement_sql("obj", '{"tokens": {"nested": "estimated"}}'),
        "invalid value",
        params={"now": NOW},
    )
    _expect_db_error(
        engine,
        measurement_sql("str", '"provider_reported"'),
        "must be a JSON object",
        params={"now": NOW},
    )
    with engine.begin() as connection:
        connection.execute(
            text(measurement_sql("ok", '{"tokens": "provider_reported"}')),
            {"now": NOW},
        )
        connection.execute(text(measurement_sql("empty", "{}")), {"now": NOW})
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM usage_event")).scalar_one() == 3


def test_pg_parity_partial_indexes_and_credit_shape_behavioral(pg_env) -> None:
    """partial unique index（open price interval / pending request）与 credit 约束行为。"""
    engine = pg_env.engine
    _seed_parity_rows(engine)
    # 同 scope 第二个 open 价格版本被拒（uq_price_open_interval）
    with pytest.raises(IntegrityError, match="uq_price_open_interval"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
                    " effective_from_utc, created_at_utc) VALUES"
                    " ('p2', 'dashscope', 'qwen-max', 'chat', 'CNY', :now, :now)"
                ),
                {"now": NOW + timedelta(days=1)},
            )
    # 第一个 pending 成功；同申请人同月第二个 pending 被拒（uq_quota_request_pending）
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO quota_request (quota_request_id, version, applicant_user_id,"
                " applicant_role_snapshot, quota_period, business_calendar_version_id,"
                " requested_pages, status, idempotency_fingerprint, created_at_utc,"
                " updated_at_utc) VALUES"
                " ('qr_1', 1, 'u1', 'user', '2026-08', 'cal_1', 100, 'pending',"
                " 'fp1', :now, :now)"
            ),
            {"now": NOW},
        )
    with pytest.raises(IntegrityError, match="uq_quota_request_pending"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO quota_request (quota_request_id, version, applicant_user_id,"
                    " applicant_role_snapshot, quota_period, business_calendar_version_id,"
                    " requested_pages, status, idempotency_fingerprint, created_at_utc,"
                    " updated_at_utc) VALUES"
                    " ('qr_2', 1, 'u1', 'user', '2026-08', 'cal_1', 100, 'pending',"
                    " 'fp2', :now, :now)"
                ),
                {"now": NOW},
            )
    # credit 非 quota_request namespace 被拒（ck_quota_debit_credit_shape）
    with pytest.raises(IntegrityError, match="ck_quota_debit_credit_shape"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO quota_debit (quota_debit_id, entry_kind, page_delta,"
                    " entry_fingerprint, quota_subject_user_id, quota_period,"
                    " adjustment_source_namespace, adjustment_source_id, cost_center_key,"
                    " ownership_json, effective_calendar_version_id, effective_at_utc,"
                    " effective_period, recorded_calendar_version_id, recorded_at_utc,"
                    " recorded_period, created_at_utc) VALUES"
                    " ('qd_bad', 'credit', -20, 'fp', 'u1', '2026-08', 'billing', 'adj-1',"
                    " 'user:u1', '{}', 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now)"
                ),
                {"now": NOW},
            )
    # 全部失败后无残留
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM price_catalog")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM quota_request")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM quota_debit")).scalar_one() == 1


# ---------------------------------------------------------------------------
# 真实 PG 维护租约/fence 验收（真实 WorkerRuntime / SqlAlchemyLeaseStore / DB clock）
# ---------------------------------------------------------------------------


def _maintenance_worker(env: PgEnv):
    settings = _pg_settings(env.schema_url)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": env.engine,
            "database_clock": SqlAlchemyDatabaseClock(env.engine),
            "business_calendar": env.calendar,
            "quota_service": env.quota,
            "quota_request_service": env.requests,
        },
    )
    return create_worker_runtime(settings, runtime=runtime)


def test_pg_maintenance_double_worker_lease_competition_defers(pg_env, monkeypatch) -> None:
    """真实 PG 租约竞争：两个独立 WorkerRuntime + 真实 UsageMaintenanceWorker 并发
    ``run_once()``，**确定性**地在同一 ``usage-maintenance:cancel:{qr_id}`` 租约上竞争。

    保证（全部走生产实现，仅测试层包装时序）：
    1) candidate rendezvous：包装共享 ``QuotaRequestService.list_cancel_candidates``，
       两个 worker 都在各自事务里读到同一 pending candidate 后才放行；
    2) lease ordering：包装两个真实 ``SqlAlchemyLeaseStore.acquire``——first 用真实
       acquire 成功取得租约并持有，second 在持有期间用真实 acquire 尝试 →
       ``LeaseUnavailable``（wrapper 记录 loser owner）→ ``run_once`` 记 deferred；
    3) 不用手工 ``leases.acquire`` 替代，不 mock lease store；candidate 用真实
       database clock 创建（当前业务月 + 申请人 inactive → 稳定 applicant_inactive）；
    4) 所有等待有 timeout，线程异常向主线程收集；loser 被 deferred 的原因必须是
       ``LeaseUnavailable``（``coordinator.loser_owner`` 断言）。
    """
    engine = pg_env.engine
    now = _db_now(engine)
    seed_identity(engine, lifecycle_status="pending_delete", now=now)
    created, captured_now = create_pending_current_period(pg_env, key="create-lease-1", now=now)
    assert captured_now == now  # 单次 DB-clock 读取：seed 与 create 同一 now
    request_id = created["id"]

    runtime_a = _maintenance_worker(pg_env)
    runtime_b = _maintenance_worker(pg_env)
    worker_a = UsageMaintenanceWorker(runtime_a)
    worker_b = UsageMaintenanceWorker(runtime_b)

    # 1) candidate rendezvous：两个 worker 都读到同一 pending candidate 后才返回。
    candidate_barrier = _ArrivalBarrier(2, timeout=30)
    monkeypatch.setattr(
        pg_env.requests,
        "list_cancel_candidates",
        _wrap_candidate_rendezvous(pg_env.requests, candidate_barrier, request_id),
    )

    # 2) lease ordering：真实 acquire 前后加协调点（不替代真实 acquire）。
    coordinator = _LeaseOrderCoordinator(timeout=30)
    runtime_a.leases.acquire = _wrap_lease_acquire(runtime_a.leases, coordinator)
    runtime_b.leases.acquire = _wrap_lease_acquire(runtime_b.leases, coordinator)

    errors: list[BaseException] = []
    stats: list[MaintenanceStats] = []

    def run(worker: UsageMaintenanceWorker, owner: str) -> None:
        try:
            stats.append(worker.run_once(owner=owner))
        except BaseException as exc:  # noqa: BLE001 - 显式收集线程异常
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(worker_a, "worker-a")),
        threading.Thread(target=run, args=(worker_b, "worker-b")),
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert all(not t.is_alive() for t in threads)
        assert candidate_barrier.broken == []
        assert coordinator.broken == []
        assert errors == [], errors
        assert len(stats) == 2
        completed = sum(s.completed for s in stats)
        deferred = sum(s.deferred for s in stats)
        assert (completed, deferred) == (1, 1)  # 同一任务仅一个 worker 完成
        # loser 确实因 LeaseUnavailable 被 deferred（真实 acquire 抛出并记录），
        # 且 winner/loser 正好是两个不同 worker。
        assert coordinator.first_owner is not None
        assert coordinator.loser_owner is not None
        assert coordinator.first_owner != coordinator.loser_owner
        assert {coordinator.first_owner, coordinator.loser_owner} == {"worker-a", "worker-b"}
        with engine.connect() as connection:
            row = (
                connection.execute(
                    select(quota_request_table).where(
                        quota_request_table.c.quota_request_id == request_id
                    )
                )
                .mappings()
                .one()
            )
            # request 只 transition 一次：version 2、cancelled、reason 为 applicant_inactive，
            # reviewed_at == updated_at（同一业务 now）。
            assert row["status"] == "cancelled"
            assert row["version"] == 2
            assert row["cancel_reason"] == "applicant_inactive"
            assert row["reviewed_at_utc"] is not None
            assert row["reviewed_at_utc"] == row["updated_at_utc"]
    finally:
        # 测试后明确 close 两个 worker runtime 与其 PlatformRuntime（dispose 共享
        # engine；fixture 兜底再 dispose/drop schema）。
        runtime_a.runtime.close()
        runtime_b.runtime.close()


def test_pg_maintenance_stale_fence_rolls_back_cancel(pg_env, monkeypatch) -> None:
    """真实 PG fence：真实 ``run_once()`` 候选路径内，取消写入后租约过期 → 提交前
    fence 检查失败 → 事务回滚（无半取消）。

    不使用客户端 ``time.sleep``：callback 内在 fenced transaction 里执行服务端
    ``pg_sleep(5.2)``（settings ttl=5s，WorkerSettings.lease_seconds 下限 5s），让
    DB 时钟确定越过租约到期点；随后 ``fenced_transaction`` 的提交前条件 UPDATE
    失败（FenceViolation）→ ``run_once`` 记 deferred=1。wrapper 内断言真实取消写入
    已发生（affected=1），rollback 后请求仍 pending。
    """
    engine = pg_env.engine
    now = _db_now(engine)
    seed_identity(engine, lifecycle_status="pending_delete", now=now)
    created, _captured_now = create_pending_current_period(pg_env, key="create-fence-1", now=now)
    worker_runtime = _maintenance_worker(pg_env)
    worker = UsageMaintenanceWorker(worker_runtime)
    original_cancel = worker._cancel

    def cancel_then_expire_lease(context, connection, *, quota_request_id, reason):
        result = original_cancel(
            context, connection, quota_request_id=quota_request_id, reason=reason
        )
        assert result == {"quota_request_id": quota_request_id, "affected": 1}
        # 服务端 sleep：事务保持打开，提交前租约必然过期（确定性，非客户端 sleep）
        connection.execute(text("SELECT pg_sleep(5.2)"))
        return result

    monkeypatch.setattr(worker, "_cancel", cancel_then_expire_lease)
    try:
        stats = worker.run_once(owner="worker-1")
        assert stats.completed == 0
        assert stats.deferred == 1
        with engine.connect() as connection:
            row = (
                connection.execute(
                    select(quota_request_table).where(
                        quota_request_table.c.quota_request_id == created["id"]
                    )
                )
                .mappings()
                .one()
            )
            assert row["status"] == "pending"  # rollback：无半取消
            assert row["version"] == 1
            assert row["cancel_reason"] is None
            assert row["reviewed_at_utc"] is None
    finally:
        # 测试后明确 close worker runtime 与其 PlatformRuntime（dispose 共享 engine；
        # fixture 兜底再 dispose/drop schema）。
        worker_runtime.runtime.close()
