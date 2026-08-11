"""`GET /v1/approvals/*`、`POST /v1/approvals/quota-requests/{id}/approve|reject`
路由契约测试（Task 10）。

遵循 tests/api_v1/test_quota_routes.py 的 API fixture 风格（手动构造 engine/adapters
注入 build_runtime + create_platform_app，本文件保持自包含复制）；路由使用项目当前
AuthPrincipal/request-state service DI 与 PlatformError 映射（
register_exception_handlers），Header Idempotency-Key，Pydantic 严格校验
（extra="forbid" + strict int ge/le——JSON true/false、字符串"1"、1.0、null 均标准
422 validation_error；expected_version 0/字符串拒绝；approved_pages 0/600/1.5
拒绝；reject 不接受自由文本 reason）。
用例（正式 spec §5）：ops 全流程 approve 200 精确形状 + reject 200；summary 仅
ops 真实计数（admin 为 0）；list 仅精确 ops（user/admin 均 403）、created_at
RFC3339、status Literal 422；approve 404 not_found、409 version_conflict /
already_processed / idempotency_key_conflict（同 key 同事实重放原 payload，异事实
冲突后同事实重放仍可用）、缺 Idempotency-Key 422；审计行 request_id 来自请求
context（req_ 前缀）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import AuthPrincipal, IdentityAccessService
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import (
    core_metadata,
    platform_audit_table,
    platform_idempotency_table,
)
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime
from app.usage.calendar import BusinessCalendarService
from app.usage.ports import NoopOutboxEnqueuePort
from app.usage.price import PriceCatalogService
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import (
    quota_debit_table,
    quota_projection_table,
    quota_request_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

_APPROVE_KEYS = {"id", "version", "status", "approved_pages", "credit_entry_id", "quota_period"}
_LIST_ITEM_KEYS = {
    "id",
    "version",
    "status",
    "applicant",
    "current_usage",
    "requested_pages",
    "approved_pages",
    "quota_period",
    "created_at",
    "reviewed_at",
}

_api_outbox_metadata = MetaData()
api_outbox_event_table = Table(
    "tests_api_outbox_event",
    _api_outbox_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", String(64), nullable=False),
    Column("aggregate_id", String(128), nullable=False),
    Column("transition_version", Integer, nullable=False),
    Column("payload_json", JSON, nullable=False),
)


class TransactionalOutboxPort:
    def __init__(self, *, fail_after_insert: bool = False) -> None:
        self.fail_after_insert = fail_after_insert

    def enqueue(
        self,
        *,
        connection,
        event_type,
        aggregate_type,
        aggregate_id,
        transition_version,
        recipient_user_id,
        occurred_at,
        payload_fingerprint,
        payload,
    ) -> None:
        del aggregate_type, recipient_user_id, occurred_at, payload_fingerprint
        connection.execute(
            api_outbox_event_table.insert().values(
                event_type=event_type,
                aggregate_id=aggregate_id,
                transition_version=transition_version,
                payload_json=payload,
            )
        )
        if self.fail_after_insert:
            raise PlatformError(
                "quota_event_outbox_unavailable",
                "Outbox enqueue failed after insert",
                {},
                503,
                True,
            )


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


class _NullObjectStore:
    def exists(self, key: str) -> bool:
        return False


def make_client(*, outbox_port=None):
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
    usage_metadata.create_all(engine)
    if outbox_port is not None:
        _api_outbox_metadata.create_all(engine)
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, clock, settings.business_timezone)
    prices = PriceCatalogService(engine, clock)
    quota = QuotaService(engine, clock, calendar)
    requests = QuotaRequestService(
        engine,
        clock,
        calendar,
        quota,
        outbox_port if outbox_port is not None else NoopOutboxEnqueuePort(),
    )
    identity_service = IdentityAccessService(
        engine, settings.auth, revocation_port=NoopGenerationRevocationPort()
    )
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "business_calendar": calendar,
            "price_catalog": prices,
            "quota_service": quota,
            "quota_request_service": requests,
            "identity_access": identity_service,
            "object_store": _NullObjectStore(),
        },
    )
    return TestClient(create_platform_app(settings, runtime=runtime)), runtime


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
    admin_principal = AuthPrincipal(
        user_id="root",
        auth_session_id="s-admin",
        username="root",
        role="admin",
        department_id=None,
    )
    department = identity.create_department(
        actor=admin_principal, name="Finance", idempotency_key="dept-minister-1"
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


def expected_period(runtime) -> str:
    calendar = runtime.resolve("business_calendar")
    engine = runtime.resolve("database_engine")
    with engine.connect() as connection:
        lock = calendar.lock_or_verify(connection)
        return calendar.period_for(lock, NOW)


def create_pending(client, token: str, pages: int = 100, key: str = "create-1") -> dict:
    created = client.post(
        "/v1/quota-requests",
        json={"requested_pages": pages},
        headers={"Authorization": token, "Idempotency-Key": key},
    )
    assert created.status_code == 201
    return created.json()


def test_ops_approve_full_shape_and_reject() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    pending = create_pending(client, user_token, 100, "create-approve")
    period = expected_period(runtime)
    summary = client.get("/v1/approvals/summary", headers={"Authorization": ops_token})
    assert summary.status_code == 200
    assert summary.json() == {"quota_pending": 1, "submission_pending": 0}
    approve = client.post(
        f"/v1/approvals/quota-requests/{pending['id']}/approve",
        json={"expected_version": 1, "approved_pages": 80},
        headers={"Authorization": ops_token, "Idempotency-Key": "approve-1"},
    )
    assert approve.status_code == 200
    body = approve.json()
    assert set(body) == _APPROVE_KEYS
    assert body == {
        "id": pending["id"],
        "version": 2,
        "status": "approved",
        "approved_pages": 80,
        "credit_entry_id": body["credit_entry_id"],
        "quota_period": period,
    }
    assert isinstance(body["credit_entry_id"], str) and body["credit_entry_id"]
    # 审计行 request_id 来自请求 context（middleware 安装的 RequestContext）
    engine = runtime.resolve("database_engine")
    with engine.connect() as connection:
        audits = connection.execute(select(platform_audit_table)).mappings().all()
        assert len(audits) == 1
        assert audits[0]["resource_type"] == "quota_request"
        assert audits[0]["result"] == "quota_request_approved"
        assert audits[0]["request_id"].startswith("req_")
    # 同用户可再申请（已 approved 不占 pending 唯一索引）→ ops reject 200 形状
    pending2 = create_pending(client, user_token, 50, "create-reject")
    reject = client.post(
        f"/v1/approvals/quota-requests/{pending2['id']}/reject",
        json={"expected_version": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "reject-1"},
    )
    assert reject.status_code == 200
    assert reject.json() == {"id": pending2["id"], "version": 2, "status": "rejected"}


def test_list_scoping_summary_admin_zero_and_errors() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    admin_token = seed_user(runtime, "admin", "root")
    create_pending(client, user_token, 100, "create-list")
    period = expected_period(runtime)
    # user/admin 的 summary quota_pending 恒 0（正式 spec L50），仍可读（200）
    user_summary = client.get("/v1/approvals/summary", headers={"Authorization": user_token})
    assert user_summary.status_code == 200
    assert user_summary.json() == {"quota_pending": 0, "submission_pending": 0}
    admin_summary = client.get("/v1/approvals/summary", headers={"Authorization": admin_token})
    assert admin_summary.status_code == 200
    assert admin_summary.json() == {"quota_pending": 0, "submission_pending": 0}
    # list 仅精确 ops：user/admin 均 403
    for token in (user_token, admin_token):
        assert_error_shape(
            client.get("/v1/approvals/quota-requests", headers={"Authorization": token}),
            403,
            "forbidden_target",
        )
    listing = client.get(
        "/v1/approvals/quota-requests?status=pending", headers={"Authorization": ops_token}
    )
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert set(items[0]) == _LIST_ITEM_KEYS
    assert items[0]["applicant"] == {"id": items[0]["applicant"]["id"], "display_name": "alice"}
    assert items[0]["current_usage"] == {"used": 0, "effective_limit": 500}
    assert items[0]["quota_period"] == period
    assert items[0]["approved_pages"] is None
    assert items[0]["reviewed_at"] is None
    assert items[0]["created_at"].endswith("+00:00")  # RFC3339
    # status 只允许 4 个固定值
    assert_error_shape(
        client.get(
            "/v1/approvals/quota-requests?status=bogus", headers={"Authorization": ops_token}
        ),
        422,
        "validation_error",
    )
    # approve 未找到 → 404 not_found
    assert_error_shape(
        client.post(
            "/v1/approvals/quota-requests/qr_missing/approve",
            json={"expected_version": 1},
            headers={"Authorization": ops_token, "Idempotency-Key": "approve-missing"},
        ),
        404,
        "not_found",
    )
    # user token approve → 403
    assert_error_shape(
        client.post(
            f"/v1/approvals/quota-requests/{items[0]['id']}/approve",
            json={"expected_version": 1},
            headers={"Authorization": user_token, "Idempotency-Key": "approve-user"},
        ),
        403,
        "forbidden_target",
    )


def test_approve_idempotent_replay_and_409_conflicts() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    pending = create_pending(client, user_token, 100, "create-replay")
    url = f"/v1/approvals/quota-requests/{pending['id']}/approve"
    headers = {"Authorization": ops_token, "Idempotency-Key": "approve-replay"}
    first = client.post(url, json={"expected_version": 1, "approved_pages": 80}, headers=headers)
    assert first.status_code == 200
    # 同 key 同事实 → 完整 payload 重放（200）
    replay = client.post(url, json={"expected_version": 1, "approved_pages": 80}, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # 同 key 异事实（approved_pages 不同）→ 409 idempotency_key_conflict
    assert_error_shape(
        client.post(url, json={"expected_version": 1, "approved_pages": 90}, headers=headers),
        409,
        "idempotency_key_conflict",
    )
    # 冲突重放尝试（回滚）后，同事实重放仍可用
    again = client.post(url, json={"expected_version": 1, "approved_pages": 80}, headers=headers)
    assert again.status_code == 200
    assert again.json() == first.json()
    # 新 key 处理已 approved 请求 → 409 already_processed
    assert_error_shape(
        client.post(
            url,
            json={"expected_version": 2, "approved_pages": 80},
            headers={"Authorization": ops_token, "Idempotency-Key": "approve-again"},
        ),
        409,
        "already_processed",
    )
    # 新 pending + 错误 expected_version → 409 version_conflict
    pending2 = create_pending(client, user_token, 60, "create-ver")
    assert_error_shape(
        client.post(
            f"/v1/approvals/quota-requests/{pending2['id']}/approve",
            json={"expected_version": 5},
            headers={"Authorization": ops_token, "Idempotency-Key": "approve-ver"},
        ),
        409,
        "version_conflict",
    )
    # 缺 Idempotency-Key → 422 validation_error
    assert_error_shape(
        client.post(
            f"/v1/approvals/quota-requests/{pending2['id']}/approve",
            json={"expected_version": 1},
            headers={"Authorization": ops_token},
        ),
        422,
        "validation_error",
    )


@pytest.mark.parametrize(
    ("action", "request_body"),
    [
        ("approve", {"expected_version": 1, "approved_pages": 80}),
        ("reject", {"expected_version": 1}),
    ],
)
def test_completed_approval_replay_rechecks_current_ops_role(
    action: str, request_body: dict
) -> None:
    """完成后的幂等 replay 仍先按当前身份授权，且 403 不产生任何副作用。"""
    idempotency_key = f"{action}-role-replay"
    client, runtime = make_client(outbox_port=TransactionalOutboxPort())
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    seed_user(runtime, "admin", "root")
    identity = runtime.resolve("identity_access")
    ops_login = identity.login(username="op", password="Password1")
    admin_login = identity.login(username="root", password="Password1")
    pending = create_pending(client, user_token, 100, f"create-{action}-role-replay")
    url = f"/v1/approvals/quota-requests/{pending['id']}/{action}"
    first = client.post(
        url,
        json=request_body,
        headers={"Authorization": ops_token, "Idempotency-Key": idempotency_key},
    )
    assert first.status_code == 200

    identity.update_managed_user(
        actor=AuthPrincipal(
            user_id=str(admin_login.user["id"]),
            auth_session_id=admin_login.session_id,
            username="root",
            role="admin",
            department_id=None,
        ),
        user_id=str(ops_login.user["id"]),
        expected_version=1,
        role="user",
        department_id=None,
        department_provided=False,
        idempotency_key=f"demote-{action}-role-replay",
    )
    demoted_login = identity.login(username="op", password="Password1")
    demoted_token = f"Bearer {demoted_login.access_token}"
    engine = runtime.resolve("database_engine")

    def side_effect_snapshot() -> tuple[dict, int, int, int, int, int]:
        with engine.connect() as connection:
            request_row = dict(
                connection.execute(
                    select(quota_request_table).where(
                        quota_request_table.c.quota_request_id == pending["id"]
                    )
                )
                .mappings()
                .one()
            )
            return (
                request_row,
                int(
                    connection.execute(
                        select(func.count())
                        .select_from(quota_debit_table)
                        .where(quota_debit_table.c.adjustment_source_id == pending["id"])
                    ).scalar_one()
                ),
                int(
                    connection.execute(
                        select(func.count()).select_from(quota_projection_table)
                    ).scalar_one()
                ),
                int(
                    connection.execute(
                        select(func.count())
                        .select_from(platform_audit_table)
                        .where(platform_audit_table.c.resource_id == pending["id"])
                    ).scalar_one()
                ),
                int(
                    connection.execute(
                        select(func.count())
                        .select_from(platform_idempotency_table)
                        .where(platform_idempotency_table.c.idempotency_key == idempotency_key)
                    ).scalar_one()
                ),
                int(
                    connection.execute(
                        select(func.count())
                        .select_from(api_outbox_event_table)
                        .where(api_outbox_event_table.c.aggregate_id == pending["id"])
                    ).scalar_one()
                ),
            )

    before = side_effect_snapshot()
    replay = client.post(
        url,
        json=request_body,
        headers={"Authorization": demoted_token, "Idempotency-Key": idempotency_key},
    )
    assert_error_shape(replay, 403, "forbidden_target")
    assert side_effect_snapshot() == before


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_approval_request_id_path_is_bounded(action: str) -> None:
    client, runtime = make_client()
    ops_token = seed_user(runtime, "ops", "op")
    response = client.post(
        f"/v1/approvals/quota-requests/{'q' * 65}/{action}",
        json={"expected_version": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": f"bounded-{action}"},
    )
    assert_error_shape(response, 422, "validation_error")


def test_strict_body_validation_and_extra_forbid() -> None:
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    pending = create_pending(client, user_token, 100, "create-strict")
    url = f"/v1/approvals/quota-requests/{pending['id']}/approve"
    # expected_version：strict int，拒绝字符串/布尔/0/小数
    for bad in ("1", True, 0, 1.0):
        assert_error_shape(
            client.post(
                url,
                json={"expected_version": bad},
                headers={"Authorization": ops_token, "Idempotency-Key": f"k-ver-{bad!r}"},
            ),
            422,
            "validation_error",
        )
    # approved_pages：strict int ge=1 le=500（缺省由服务取申请量）
    for bad in (0, 600, 1.5, True):
        assert_error_shape(
            client.post(
                url,
                json={"expected_version": 1, "approved_pages": bad},
                headers={"Authorization": ops_token, "Idempotency-Key": f"k-pages-{bad!r}"},
            ),
            422,
            "validation_error",
        )
    # reject 不接受自由文本 reason：extra="forbid" → 422
    assert_error_shape(
        client.post(
            f"/v1/approvals/quota-requests/{pending['id']}/reject",
            json={"expected_version": 1, "reason": "free text"},
            headers={"Authorization": ops_token, "Idempotency-Key": "reject-reason"},
        ),
        422,
        "validation_error",
    )
    # approve body 多余字段同样 extra="forbid" → 422
    assert_error_shape(
        client.post(
            url,
            json={"expected_version": 1, "surprise": 1},
            headers={"Authorization": ops_token, "Idempotency-Key": "approve-extra"},
        ),
        422,
        "validation_error",
    )
    # 合法缺省（approved_pages 缺省 → 100）
    ok = client.post(
        url,
        json={"expected_version": 1},
        headers={"Authorization": ops_token, "Idempotency-Key": "approve-default"},
    )
    assert ok.status_code == 200
    assert ok.json()["approved_pages"] == 100


def test_approved_pages_dynamic_upper_bound_422() -> None:
    """review：approved_pages 动态上限（requested=100、approved=101）→ 422，无残留
    副作用（同 key 重放仍完整可用，不因 failed transaction 损坏）。"""
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    pending = create_pending(client, user_token, 100, "create-dyn")
    url = f"/v1/approvals/quota-requests/{pending['id']}/approve"
    over = client.post(
        url,
        json={"expected_version": 1, "approved_pages": 101},
        headers={"Authorization": ops_token, "Idempotency-Key": "approve-dyn-over"},
    )
    assert_error_shape(over, 422, "validation_error")
    # 无残留：me pending 仍在，approve 幂等 reserve 已回滚 → 同 key 同事实可重放
    me = client.get("/v1/quota/me", headers={"Authorization": user_token})
    assert me.status_code == 200
    assert me.json()["pending_request"] is not None
    ok = client.post(
        url,
        json={"expected_version": 1, "approved_pages": 101},
        headers={"Authorization": ops_token, "Idempotency-Key": "approve-dyn-over"},
    )
    assert_error_shape(ok, 422, "validation_error")
    # 合法值成功
    good = client.post(
        url,
        json={"expected_version": 1, "approved_pages": 100},
        headers={"Authorization": ops_token, "Idempotency-Key": "approve-dyn-ok"},
    )
    assert good.status_code == 200
    assert good.json()["approved_pages"] == 100


def test_approve_http_replay_no_duplicate_side_effects() -> None:
    """review：HTTP 层同 key 同事实 replay 后 credit/projection/audit/outbox/request
    版本均无重复变化；summary 对 minister 为 0/0。"""
    client, runtime = make_client()
    user_token = seed_user(runtime, "user", "alice")
    ops_token = seed_user(runtime, "ops", "op")
    minister_token = seed_minister(runtime)
    pending = create_pending(client, user_token, 100, "create-replay2")
    url = f"/v1/approvals/quota-requests/{pending['id']}/approve"
    headers = {"Authorization": ops_token, "Idempotency-Key": "approve-replay2"}
    first = client.post(url, json={"expected_version": 1, "approved_pages": 80}, headers=headers)
    assert first.status_code == 200
    replay = client.post(url, json={"expected_version": 1, "approved_pages": 80}, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    engine = runtime.resolve("database_engine")

    from app.platform.database import platform_audit_table
    from app.usage.schema import quota_debit_table, quota_projection_table

    with engine.connect() as connection:
        assert (
            int(
                connection.execute(select(func.count()).select_from(quota_debit_table)).scalar_one()
            )
            == 1
        )
        # identity 的 provision 也会写 audit：按 resource_type 过滤审批审计
        assert (
            int(
                connection.execute(
                    select(func.count())
                    .select_from(platform_audit_table)
                    .where(platform_audit_table.c.resource_type == "quota_request")
                ).scalar_one()
            )
            == 1
        )
        # 投影行 key 为 identity 生成的随机 user id：断言总量与 extra_granted
        projs = connection.execute(select(quota_projection_table)).mappings().all()
        assert len(projs) == 1
        assert projs[0]["extra_granted"] == 80
    # minister summary 0/0（正式 spec L50）
    minister_summary = client.get(
        "/v1/approvals/summary", headers={"Authorization": minister_token}
    )
    assert minister_summary.status_code == 200
    assert minister_summary.json() == {"quota_pending": 0, "submission_pending": 0}


@pytest.mark.parametrize(
    ("action", "request_body", "idempotency_key", "final_status"),
    [
        (
            "approve",
            {"expected_version": 1, "approved_pages": 80},
            "approve-failclosed",
            "approved",
        ),
        ("reject", {"expected_version": 1}, "reject-failclosed", "rejected"),
    ],
)
def test_injected_outbox_failure_rolls_back_each_approval_and_same_key_retries(
    action: str,
    request_body: dict,
    idempotency_key: str,
    final_status: str,
) -> None:
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
    usage_metadata.create_all(engine)
    _api_outbox_metadata.create_all(engine)
    clock = FixedClock(NOW)
    identity_service = IdentityAccessService(engine, settings.auth)
    default_runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "identity_access": identity_service,
            "object_store": _NullObjectStore(),
        },
    )
    failing_runtime = None
    available_runtime = None
    try:
        with TestClient(create_platform_app(settings, runtime=default_runtime)) as client:
            user_token = seed_user(default_runtime, "user", f"alice-{action}")
            ops_token = seed_user(default_runtime, "ops", f"op-{action}")
            pending = create_pending(client, user_token, 100, f"create-failclosed-{action}")
            url = f"/v1/approvals/quota-requests/{pending['id']}/{action}"
            headers = {
                "Authorization": ops_token,
                "Idempotency-Key": idempotency_key,
            }
            with engine.connect() as connection:
                request_before = dict(
                    connection.execute(
                        select(quota_request_table).where(
                            quota_request_table.c.quota_request_id == pending["id"]
                        )
                    )
                    .mappings()
                    .one()
                )

            def assert_no_approval_side_effects() -> None:
                with engine.connect() as connection:
                    request_after = dict(
                        connection.execute(
                            select(quota_request_table).where(
                                quota_request_table.c.quota_request_id == pending["id"]
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert request_after == request_before
                    assert (
                        connection.execute(
                            select(func.count())
                            .select_from(platform_idempotency_table)
                            .where(platform_idempotency_table.c.idempotency_key == idempotency_key)
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        connection.execute(
                            select(func.count())
                            .select_from(quota_debit_table)
                            .where(quota_debit_table.c.adjustment_source_id == pending["id"])
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        connection.execute(
                            select(func.count()).select_from(quota_projection_table)
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        connection.execute(
                            select(func.count())
                            .select_from(platform_audit_table)
                            .where(platform_audit_table.c.resource_id == pending["id"])
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        connection.execute(
                            select(func.count()).select_from(api_outbox_event_table)
                        ).scalar_one()
                        == 0
                    )

        failing_runtime = build_runtime(
            settings,
            adapters={
                "database_engine": engine,
                "database_clock": clock,
                "identity_access": identity_service,
                "object_store": _NullObjectStore(),
                "outbox_enqueue_port": TransactionalOutboxPort(fail_after_insert=True),
            },
        )
        with TestClient(create_platform_app(settings, runtime=failing_runtime)) as client:
            failed_after_insert = client.post(url, json=request_body, headers=headers)
            assert_error_shape(failed_after_insert, 503, "quota_event_outbox_unavailable")
        assert_no_approval_side_effects()

        available_runtime = build_runtime(
            settings,
            adapters={
                "database_engine": engine,
                "database_clock": clock,
                "identity_access": identity_service,
                "object_store": _NullObjectStore(),
                "outbox_enqueue_port": TransactionalOutboxPort(),
            },
        )
        with TestClient(create_platform_app(settings, runtime=available_runtime)) as client:
            retried = client.post(url, json=request_body, headers=headers)
            assert retried.status_code == 200
            assert retried.json()["status"] == final_status

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
            assert request_row["status"] == final_status
            assert request_row["version"] == 2
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(platform_idempotency_table)
                    .where(
                        platform_idempotency_table.c.idempotency_key == idempotency_key,
                        platform_idempotency_table.c.status == "completed",
                    )
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(platform_audit_table)
                    .where(platform_audit_table.c.resource_id == pending["id"])
                ).scalar_one()
                == 1
            )
            outbox_row = (
                connection.execute(
                    select(api_outbox_event_table).where(
                        api_outbox_event_table.c.aggregate_id == pending["id"]
                    )
                )
                .mappings()
                .one()
            )
            assert outbox_row["event_type"] == f"quota_{final_status}"
            assert outbox_row["transition_version"] == 2
            credit_count = connection.execute(
                select(func.count())
                .select_from(quota_debit_table)
                .where(quota_debit_table.c.adjustment_source_id == pending["id"])
            ).scalar_one()
            projection_count = connection.execute(
                select(func.count()).select_from(quota_projection_table)
            ).scalar_one()
            if action == "approve":
                assert credit_count == 1
                assert projection_count == 1
                assert request_row["approved_pages"] == 80
                assert request_row["credit_entry_id"] is not None
            else:
                assert credit_count == 0
                assert projection_count == 0
                assert request_row["approved_pages"] is None
                assert request_row["credit_entry_id"] is None
    finally:
        if available_runtime is not None:
            available_runtime.close()
        if failing_runtime is not None:
            failing_runtime.close()
        default_runtime.close()
