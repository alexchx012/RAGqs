"""`GET /v1/quota/me`、`POST /v1/quota-requests` 路由契约测试（Task 9 + review 修订）。

遵循 tests/identity（test_space_routes.py）的 API fixture 风格：手动构造 engine/
adapters 注入 build_runtime + create_platform_app（Task 12 会改为默认构造，本文件
保持手动注入不被覆盖）。路由使用项目当前 AuthPrincipal/request-state service DI 与
PlatformError 映射（register_exception_handlers），Header Idempotency-Key，Pydantic
严格校验（extra="forbid" + requested_pages strict int ge=1 le=500——JSON
true/false、字符串"1"、1.0、null 均标准 422 validation_error，1 与 500 成功），
201/200。错误断言钉住平台完整 error shape（error 对象精确 4 键：
code/message/details/request_id，request_id 以 req_ 开头）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.schema import identity_metadata
from app.identity.service import AuthPrincipal, IdentityAccessService
from app.outbox.publisher import SqlAlchemyQuotaOutboxEnqueueAdapter
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.usage.calendar import BusinessCalendarService
from app.usage.ports import NoopOutboxEnqueuePort
from app.usage.price import PriceCatalogService
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import usage_metadata

# 注入 FixedClock 使业务月/重置时点可预期：路由断言从注入 clock + calendar 派生
# 预期 period/reset_at，不依赖真实当前月份（避免硬编码 2026-08 在真实时钟漂移时失效）。
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

_ME_TOP_LEVEL_KEYS = {
    "used",
    "base_limit",
    "extra_granted",
    "effective_limit",
    "unlimited",
    "reset_at",
    "business_timezone",
    "quota_period",
    "business_calendar_version_id",
    "pending_request",
}


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


class _NullObjectStore:
    def exists(self, key: str) -> bool:
        return False


def make_client():
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
        }
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, clock, settings.business_timezone)
    prices = PriceCatalogService(engine, clock)
    quota = QuotaService(engine, clock, calendar)
    requests = QuotaRequestService(engine, clock, calendar, quota, NoopOutboxEnqueuePort())
    identity = IdentityAccessService(engine, settings.auth)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "business_calendar": calendar,
            "price_catalog": prices,
            "quota_service": quota,
            "quota_request_service": requests,
            "identity_access": identity,
            "object_store": _NullObjectStore(),
        },
    )
    return TestClient(create_platform_app(settings, runtime=runtime)), runtime


def make_default_client():
    """Task 12 默认构造：只注入 engine/clock/identity/object_store，usage 域服务
    （calendar/price/ledger/quota/quota_request/outbox）由 build_runtime 自动组装；
    quota 审批事件缺省接入事务化 SqlAlchemyQuotaOutboxEnqueueAdapter。"""
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
        }
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    clock = FixedClock(NOW)
    identity = IdentityAccessService(engine, settings.auth)
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "identity_access": identity,
            "object_store": _NullObjectStore(),
        },
    )
    return TestClient(create_platform_app(settings, runtime=runtime)), runtime


def test_default_construction_resolves_usage_services_and_get_me_works() -> None:
    """Task 12：默认构造自动组装 quota 服务及事务化 quota outbox adapter。"""
    client, runtime = make_default_client()
    try:
        assert isinstance(runtime.resolve("quota_request_service"), QuotaRequestService)
        assert isinstance(
            runtime.resolve("outbox_enqueue_port"), SqlAlchemyQuotaOutboxEnqueueAdapter
        )
        user_token = seed_user(runtime, "user", "alice")
        me = client.get("/v1/quota/me", headers={"Authorization": user_token})
        assert me.status_code == 200
        body = me.json()
        assert set(body) == _ME_TOP_LEVEL_KEYS
        assert body["business_timezone"] == "Asia/Shanghai"
        created = client.post(
            "/v1/quota-requests",
            json={"requested_pages": 50},
            headers={"Authorization": user_token, "Idempotency-Key": "idem-default"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "pending"
    finally:
        runtime.close()


def seed_user(runtime, role: str, username: str) -> str:
    identity = runtime.resolve("identity_access")
    identity.provision_user(
        username=username,
        password="Password1",
        real_name=username,
        display_name=username,
        role=role,
        department_id=None,
    )
    login = identity.login(username=username, password="Password1")
    return f"Bearer {login.access_token}"


def seed_minister(runtime, username: str = "minister") -> str:
    """minister 必须属于有效部门：先用 admin actor 建部门，再 provision minister。"""
    identity = runtime.resolve("identity_access")
    admin = AuthPrincipal(
        user_id="root",
        auth_session_id="s-admin",
        username="root",
        role="admin",
        department_id=None,
    )
    department = identity.create_department(
        actor=admin, name="Finance", idempotency_key="dept-minister-1"
    )
    identity.provision_user(
        username=username,
        password="Password1",
        real_name="Minister",
        display_name="Minister",
        role="minister",
        department_id=department["id"],
    )
    login = identity.login(username=username, password="Password1")
    return f"Bearer {login.access_token}"


def assert_error_shape(response, status: int, code: str) -> dict:
    """钉住平台完整 error shape：error 对象精确 4 键 + request_id 前缀。"""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    assert isinstance(error["request_id"], str) and error["request_id"].startswith("req_")
    return error


def expected_business_context(runtime) -> tuple[str, str]:
    """从注入的 FixedClock + calendar 派生预期业务月与 reset_at（RFC3339）。

    与服务的计算路径同源（lock_or_verify → period_for / next_month_start_utc），
    使断言不依赖真实当前月份。
    """
    calendar = runtime.resolve("business_calendar")
    engine = runtime.resolve("database_engine")
    with engine.connect() as connection:
        lock = calendar.lock_or_verify(connection)
        period = calendar.period_for(lock, NOW)
        reset_at = calendar.next_month_start_utc(lock, NOW).isoformat()
    return period, reset_at


def test_quota_me_exact_shape_and_unlimited_variant() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    me = client.get("/v1/quota/me", headers={"Authorization": user_token})
    assert me.status_code == 200
    body = me.json()
    assert set(body) == _ME_TOP_LEVEL_KEYS
    assert body["used"] == 0
    assert body["base_limit"] == 500
    assert body["extra_granted"] == 0
    assert body["effective_limit"] == 500
    assert body["unlimited"] is False
    assert body["business_timezone"] == "Asia/Shanghai"
    assert body["pending_request"] is None
    expected_period, expected_reset_at = expected_business_context(runtime)
    assert body["quota_period"] == expected_period
    assert body["reset_at"] == expected_reset_at
    assert body["reset_at"].endswith("+00:00")
    ops_token = seed_user(runtime, "ops", "op")
    ops_me = client.get("/v1/quota/me", headers={"Authorization": ops_token})
    ops_body = ops_me.json()
    assert ops_me.status_code == 200
    assert set(ops_body) == _ME_TOP_LEVEL_KEYS
    assert ops_body["unlimited"] is True
    assert ops_body["pending_request"] is None


def test_create_boundaries_and_strict_type_negatives() -> None:
    client, runtime = make_client()
    # 同月每用户最多一条 pending：1/500/256 键用不同用户避免互相干扰
    low_token = seed_user(runtime, "user", "alice")
    high_token = seed_user(runtime, "user", "bob")
    long_ok_token = seed_user(runtime, "user", "carol")
    low = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 1},
        headers={"Authorization": low_token, "Idempotency-Key": "idem-1"},
    )
    assert low.status_code == 201
    assert low.json()["requested_pages"] == 1
    high = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 500},
        headers={"Authorization": high_token, "Idempotency-Key": "idem-500"},
    )
    assert high.status_code == 201
    assert high.json()["requested_pages"] == 500
    # 256 字符键接受：与 platform_idempotency.idempotency_key String(256) 对齐
    # （spec 无 255 产品上限）。
    long_ok = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 10},
        headers={"Authorization": long_ok_token, "Idempotency-Key": "x" * 256},
    )
    assert long_ok.status_code == 201
    # 257 字符键拒绝：键长度校验先于任何写入，同用户无 pending 冲突。
    overlong = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 10},
        headers={"Authorization": long_ok_token, "Idempotency-Key": "x" * 257},
    )
    assert_error_shape(overlong, 422, "validation_error")


@pytest.mark.parametrize(
    "pages",
    [True, False, "1", "100", 1.0, 1.5, None, 0, 501],
)
def test_create_strict_type_and_range_negatives(pages: object) -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    response = client.post(
        "/v1/quota-requests",
        json={"requested_pages": pages},
        headers={"Authorization": user_token, "Idempotency-Key": "idem-neg"},
    )
    assert_error_shape(response, 422, "validation_error")


def test_create_replay_full_payload_and_conflicts() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    created = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 100},
        headers={"Authorization": user_token, "Idempotency-Key": "idem-1"},
    )
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "version", "status", "requested_pages", "quota_period", "created_at"}
    assert body["status"] == "pending"
    assert body["version"] == 1
    expected_period, _ = expected_business_context(runtime)
    assert body["quota_period"] == expected_period
    assert body["created_at"].endswith("+00:00")
    replay = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 100},
        headers={"Authorization": user_token, "Idempotency-Key": "idem-1"},
    )
    assert replay.status_code == 201
    assert replay.json() == body  # 完整 payload 重放
    conflict = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 200},
        headers={"Authorization": user_token, "Idempotency-Key": "idem-1"},
    )
    assert_error_shape(conflict, 409, "idempotency_key_conflict")
    pending = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 300},
        headers={"Authorization": user_token, "Idempotency-Key": "idem-2"},
    )
    assert_error_shape(pending, 409, "pending_request_exists")


def test_create_role_forbidden_and_minister_success() -> None:
    client, runtime = make_client()
    ops_token = seed_user(runtime, "ops", "op")
    admin_token = seed_user(runtime, "admin", "root")
    minister_token = seed_minister(runtime)
    forbidden_ops = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 100},
        headers={"Authorization": ops_token, "Idempotency-Key": "idem-ops"},
    )
    assert_error_shape(forbidden_ops, 403, "forbidden_target")
    forbidden_admin = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 100},
        headers={"Authorization": admin_token, "Idempotency-Key": "idem-admin"},
    )
    assert_error_shape(forbidden_admin, 403, "forbidden_target")
    minister = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 100},
        headers={"Authorization": minister_token, "Idempotency-Key": "idem-minister"},
    )
    assert minister.status_code == 201
    assert minister.json()["status"] == "pending"


def test_missing_key_validation_error_shape() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    response = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 100},
        headers={"Authorization": user_token},
    )
    assert_error_shape(response, 422, "validation_error")


def test_quota_me_pending_request_after_create() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    created = client.post(
        "/v1/quota-requests",
        json={"requested_pages": 80},
        headers={"Authorization": user_token, "Idempotency-Key": "idem-1"},
    )
    assert created.status_code == 201
    me = client.get("/v1/quota/me", headers={"Authorization": user_token})
    assert me.status_code == 200
    body = me.json()
    assert set(body) == _ME_TOP_LEVEL_KEYS
    expected_period, _ = expected_business_context(runtime)
    assert body["pending_request"] == {
        "id": created.json()["id"],
        "version": 1,
        "requested_pages": 80,
        "quota_period": expected_period,
        "created_at": created.json()["created_at"],
    }
    assert body["pending_request"]["created_at"].endswith("+00:00")
    assert body["reset_at"].endswith("+00:00")


def test_health_smoke() -> None:
    client, _ = make_client()
    health = client.get("/v1/health")
    assert health.status_code == 200


def test_ready_probes_database_and_object_storage() -> None:
    client, _ = make_client()
    ready = client.get("/v1/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "service": "core-platform",
        "database": "ok",
        "object_storage": "ok",
    }


def test_ready_returns_retryable_503_when_a_dependency_fails() -> None:
    class BrokenObjectStore:
        def exists(self, key: str) -> bool:
            raise OSError("storage endpoint unreachable")

    client, runtime = make_client()
    runtime.adapters["object_store"] = BrokenObjectStore()
    ready = client.get("/v1/ready")
    error = assert_error_shape(ready, 503, "dependency_unavailable")
    assert error["message"] == "The object storage readiness check failed"


def test_ready_returns_retryable_503_when_the_database_fails() -> None:
    client, runtime = make_client()
    runtime.adapters["database_engine"] = None
    ready = client.get("/v1/ready")
    assert_error_shape(ready, 503, "dependency_unavailable")
