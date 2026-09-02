"""Usage and quota domain schema: metering ledger, page-quota ledger, calendar, price catalog."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)

usage_metadata = MetaData()

# 1) 业务日历：单行表（固定主键 'instance'，随机版本号在 version_id 列；不可 UPDATE/DELETE）
business_calendar_version_table = Table(
    "business_calendar_version",
    usage_metadata,
    Column("id", String(32), primary_key=True),
    Column("version_id", String(64), nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("effective_from_utc", DateTime(timezone=True), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 'instance'", name="ck_calendar_singleton_id"),
)

# 1.5) 价格 scope 基础行：每 (provider, model, operation) 一行，lock_version 作为数据库
# 写锁（register/close 先 upsert 该行，再原子 UPDATE lock_version+1 并持有至事务结束，
# PG 行锁 / SQLite 写锁），使首次注册、successor、close 按 scope 串行，消除 phantom /
# 旧 predecessor 窗口。不设不可变 trigger（允许 upsert 维护行本身）。
price_catalog_scope_table = Table(
    "price_catalog_scope",
    usage_metadata,
    Column("id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("lock_version", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider", "model", "operation", name="uq_price_catalog_scope_identity"),
)

# 2) 价格版本头：open-interval partial unique index 保证同一 scope 最多一个 open 版本
price_catalog_table = Table(
    "price_catalog",
    usage_metadata,
    Column("id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("currency_code", String(8), nullable=False),
    Column("effective_from_utc", DateTime(timezone=True), nullable=False),
    Column("effective_to_utc", DateTime(timezone=True), nullable=True),
    Column("supersedes_version_id", String(64), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    # 注意：price_catalog 的 append-only 语义（不可 DELETE；UPDATE 仅允许一次性
    # close 且其余字段逐列不变，含 id/supersedes_version_id 的 null-safe 比较）
    # 由 0011_usage_quota 迁移的 DB trigger 强制，metadata 无法表达 trigger 行为，此处不虚构。
    CheckConstraint(
        "effective_to_utc IS NULL OR effective_to_utc > effective_from_utc",
        name="ck_price_catalog_interval",
    ),
    UniqueConstraint(
        "provider",
        "model",
        "operation",
        "effective_from_utc",
        name="uq_price_catalog_scope",
    ),
)

price_catalog_line_table = Table(
    "price_catalog_line",
    usage_metadata,
    Column("id", String(64), primary_key=True),
    Column("price_version_id", String(64), nullable=False),
    Column("meter", String(32), nullable=False),
    Column("unit", String(32), nullable=False),
    Column("rate", Numeric(30, 10), nullable=False),
    Column("billing_granularity", Integer, nullable=False),
    Column("minimum_billable_quantity", Integer, nullable=False),
    Column("rounding_rule", String(16), nullable=False),
    UniqueConstraint("price_version_id", "meter", name="uq_price_line_meter"),
    CheckConstraint("rate >= 0", name="ck_price_line_rate_nonnegative"),
    CheckConstraint("rate = rate + 0", name="ck_price_line_rate_numeric"),
    CheckConstraint("COALESCE(rate - rate = 0, FALSE)", name="ck_price_line_rate_finite"),
    CheckConstraint("rate <= 99999999999999999999.9999999999", name="ck_price_line_rate_max"),
    CheckConstraint("billing_granularity >= 1", name="ck_price_line_granularity_positive"),
    CheckConstraint(
        "billing_granularity = CAST(billing_granularity AS INTEGER)",
        name="ck_price_line_granularity_integer",
    ),
    CheckConstraint(
        "billing_granularity <= 2147483647",
        name="ck_price_line_granularity_int32",
    ),
    CheckConstraint("minimum_billable_quantity >= 0", name="ck_price_line_minimum_nonnegative"),
    CheckConstraint(
        "minimum_billable_quantity = CAST(minimum_billable_quantity AS INTEGER)",
        name="ck_price_line_minimum_integer",
    ),
    CheckConstraint(
        "minimum_billable_quantity <= 2147483647",
        name="ck_price_line_minimum_int32",
    ),
    CheckConstraint(
        "rounding_rule IN ('floor','ceil','half_up')",
        name="ck_price_line_rounding_rule",
    ),
)

price_open_interval_index = Index(
    "uq_price_open_interval",
    price_catalog_table.c.provider,
    price_catalog_table.c.model,
    price_catalog_table.c.operation,
    unique=True,
    sqlite_where=price_catalog_table.c.effective_to_utc.is_(None),
    postgresql_where=price_catalog_table.c.effective_to_utc.is_(None),
)

# 3) provider_call：5 态状态机（CheckConstraint）
provider_call_table = Table(
    "provider_call",
    usage_metadata,
    Column("provider_call_id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("execution_kind", String(32), nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column("attempt_id", String(128), nullable=True),
    Column("generation_id", String(128), nullable=True),
    Column("resource_id", String(256), nullable=True),
    Column("replay_generation", Integer, nullable=False, server_default="0"),
    Column("request_fingerprint", String(128), nullable=False),
    Column("deadline_utc", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("prepared_at_utc", DateTime(timezone=True), nullable=False),
    Column("dispatching_at_utc", DateTime(timezone=True), nullable=True),
    Column("started_at_utc", DateTime(timezone=True), nullable=True),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("not_sent_at_utc", DateTime(timezone=True), nullable=True),
    Column("unknown_at_utc", DateTime(timezone=True), nullable=True),
    Column("last_reconcile_attempt_at_utc", DateTime(timezone=True), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "attempt_id IS NOT NULL OR generation_id IS NOT NULL",
        name="ck_provider_call_attempt_or_generation",
    ),
    CheckConstraint(
        "status IN ('prepared','dispatching','completed','not_sent','unknown')",
        name="ck_provider_call_status",
    ),
    # 状态 ↔ 对应时间：prepared 无任何流转时间；dispatching/completed/not_sent/unknown
    # 必须有各自时间戳。unknown → completed 恢复合法（completed 行允许保留 unknown_at）。
    # started_at_utc：即将物理发送的生命周期入口（mark_dispatching）持久化的 actual
    # send time；dispatching/completed/unknown 都经 dispatch，必须存在；prepared/
    # not_sent（从未发送）不得有 started。last_reconcile_attempt_at_utc 仅对账调度
    # 字段，prepared 不得有。
    CheckConstraint(
        "NOT (status = 'prepared' AND (dispatching_at_utc IS NOT NULL OR completed_at_utc IS NOT NULL"
        " OR not_sent_at_utc IS NOT NULL OR unknown_at_utc IS NOT NULL"
        " OR started_at_utc IS NOT NULL OR last_reconcile_attempt_at_utc IS NOT NULL))",
        name="ck_provider_call_prepared_clean",
    ),
    CheckConstraint(
        "NOT (status = 'dispatching' AND (dispatching_at_utc IS NULL OR started_at_utc IS NULL"
        " OR completed_at_utc IS NOT NULL OR not_sent_at_utc IS NOT NULL"
        " OR unknown_at_utc IS NOT NULL))",
        name="ck_provider_call_dispatching_at",
    ),
    CheckConstraint(
        "NOT (status = 'completed' AND (dispatching_at_utc IS NULL OR started_at_utc IS NULL"
        " OR completed_at_utc IS NULL OR not_sent_at_utc IS NOT NULL))",
        name="ck_provider_call_completed_at",
    ),
    CheckConstraint(
        "NOT (status = 'not_sent' AND (not_sent_at_utc IS NULL OR completed_at_utc IS NOT NULL"
        " OR unknown_at_utc IS NOT NULL OR started_at_utc IS NOT NULL))",
        name="ck_provider_call_not_sent_at",
    ),
    CheckConstraint(
        "NOT (status = 'unknown' AND (dispatching_at_utc IS NULL OR started_at_utc IS NULL"
        " OR unknown_at_utc IS NULL OR completed_at_utc IS NOT NULL"
        " OR not_sent_at_utc IS NOT NULL))",
        name="ck_provider_call_unknown_at",
    ),
    # reconcile/ledger 的 stale 扫描按 (status, dispatching_at_utc) 过滤。
    Index("ix_provider_call_reconcile", "status", "dispatching_at_utc"),
)

# 4) usage_event：不可变账本事实（触发器拒绝 UPDATE/DELETE）
usage_event_table = Table(
    "usage_event",
    usage_metadata,
    Column("usage_event_id", String(64), primary_key=True),
    Column("event_kind", String(32), nullable=False),
    # provider_usage 字段
    Column("provider_call_id", String(64), nullable=True),
    Column("provider", String(64), nullable=True),
    Column("model", String(128), nullable=True),
    Column("operation", String(64), nullable=True),
    Column("provider_request_id", String(256), nullable=True),
    Column("price_version_id", String(64), nullable=True),
    Column("currency_code", String(8), nullable=True),
    Column("estimated_cost_amount", Numeric(30, 10), nullable=True),
    Column("estimated_cost_status", String(16), nullable=True),
    Column("input_tokens", BigInteger, nullable=True),
    Column("prompt_cache_hit_tokens", BigInteger, nullable=True),
    Column("prompt_cache_miss_tokens", BigInteger, nullable=True),
    Column("output_tokens", BigInteger, nullable=True),
    Column("reasoning_tokens", BigInteger, nullable=True),
    Column("image_count", BigInteger, nullable=True),
    Column("visual_input_tokens", BigInteger, nullable=True),
    Column("embedding_input_tokens", BigInteger, nullable=True),
    Column("vector_count", BigInteger, nullable=True),
    # 执行/业务资源标识（M6：显式列，非仅 ownership 内嵌）
    Column("execution_kind", String(32), nullable=True),
    Column("execution_id", String(128), nullable=True),
    Column("attempt_id", String(128), nullable=True),
    Column("generation_id", String(128), nullable=True),
    Column("resource_id", String(256), nullable=True),
    Column("replay_generation", Integer, nullable=False, server_default="0"),
    Column("cost_center_key", String(128), nullable=True),
    # local_usage 字段
    Column("stage", String(64), nullable=True),
    Column("resource_kind", String(32), nullable=True),
    Column("item_count", BigInteger, nullable=True),
    Column("page_count", BigInteger, nullable=True),
    Column("input_bytes", BigInteger, nullable=True),
    Column("gpu_milliseconds", BigInteger, nullable=True),
    Column("cpu_milliseconds", BigInteger, nullable=True),
    Column("peak_vram_bytes", BigInteger, nullable=True),
    # adjustment 字段
    Column("adjustment_source_namespace", String(64), nullable=True),
    Column("adjustment_source_id", String(128), nullable=True),
    Column("adjustment_allocation_key", String(64), nullable=True),
    Column("referenced_usage_event_id", String(64), nullable=True),
    # 通用事实字段
    Column("result", String(32), nullable=False),
    Column("measurement_sources", JSON, nullable=True),
    Column("event_fingerprint", String(128), nullable=False),
    Column("ownership_json", JSON, nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_calendar_version_id", String(64), nullable=False),
    Column("effective_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_period", String(7), nullable=False),
    Column("recorded_calendar_version_id", String(64), nullable=False),
    Column("recorded_at_utc", DateTime(timezone=True), nullable=False),
    Column("recorded_period", String(7), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider_call_id", name="uq_usage_provider_call"),
    UniqueConstraint(
        "execution_kind",
        "execution_id",
        "stage",
        "resource_kind",
        name="uq_usage_local_scope",
    ),
    UniqueConstraint(
        "event_kind",
        "adjustment_source_namespace",
        "adjustment_source_id",
        "adjustment_allocation_key",
        name="uq_usage_adjustment_source",
    ),
    CheckConstraint(
        "event_kind IN ('provider_usage','local_usage','usage_adjustment','cost_adjustment')",
        name="ck_usage_event_kind",
    ),
    CheckConstraint(
        "estimated_cost_status IS NULL OR estimated_cost_status IN "
        "('complete','partial','unavailable')",
        name="ck_usage_cost_status",
    ),
    CheckConstraint(
        "execution_kind IS NOT NULL AND execution_id IS NOT NULL",
        name="ck_usage_event_execution_identity",
    ),
    # 按 event_kind 的 row-shape（review #4）：provider_usage 必须有归属/价格/成本状态；
    # local_usage 四元组非空；adjustment 必须有 source + referenced；cost_center_key 全类型必填。
    # measurement_sources 的 JSON value 白名单无法跨方言稳健约束 → 服务边界责任（Task 5 测试）。
    CheckConstraint(
        "NOT (event_kind = 'provider_usage' AND (provider_call_id IS NULL OR provider IS NULL"
        " OR model IS NULL OR operation IS NULL OR price_version_id IS NULL OR currency_code IS NULL"
        " OR estimated_cost_status IS NULL OR execution_kind IS NULL OR execution_id IS NULL))",
        name="ck_usage_provider_shape",
    ),
    CheckConstraint(
        "NOT (event_kind = 'local_usage' AND (execution_kind IS NULL OR execution_id IS NULL"
        " OR stage IS NULL OR resource_kind IS NULL))",
        name="ck_usage_local_shape",
    ),
    CheckConstraint(
        "NOT (event_kind IN ('usage_adjustment','cost_adjustment')"
        " AND (adjustment_source_namespace IS NULL OR adjustment_source_id IS NULL"
        " OR adjustment_allocation_key IS NULL OR referenced_usage_event_id IS NULL))",
        name="ck_usage_adjustment_shape",
    ),
    CheckConstraint("cost_center_key IS NOT NULL", name="ck_usage_event_cost_center"),
)

# 5) quota_debit：四类 entry_kind + 符号/来源约束（触发器拒绝 UPDATE/DELETE）
quota_debit_table = Table(
    "quota_debit",
    usage_metadata,
    Column("quota_debit_id", String(64), primary_key=True),
    Column("entry_kind", String(16), nullable=False),
    Column("page_delta", Integer, nullable=False),
    Column("entry_fingerprint", String(128), nullable=False),
    Column("quota_operation_id", String(128), nullable=True),
    Column("publication_id", String(128), nullable=True),
    Column("quota_subject_user_id", String(64), nullable=True),
    Column("quota_period", String(7), nullable=True),
    Column("quota_exempt_reason", String(64), nullable=True),
    Column("referenced_debit_id", String(64), nullable=True),
    Column("adjustment_source_namespace", String(64), nullable=True),
    Column("adjustment_source_id", String(128), nullable=True),
    Column("adjustment_allocation_key", String(64), nullable=True),
    Column("cost_center_key", String(128), nullable=False),
    Column("ownership_json", JSON, nullable=False),
    Column("effective_calendar_version_id", String(64), nullable=False),
    Column("effective_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_period", String(7), nullable=False),
    Column("recorded_calendar_version_id", String(64), nullable=False),
    Column("recorded_at_utc", DateTime(timezone=True), nullable=False),
    Column("recorded_period", String(7), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("quota_operation_id", name="uq_quota_debit_operation"),
    UniqueConstraint(
        "entry_kind",
        "adjustment_source_namespace",
        "adjustment_source_id",
        name="uq_quota_debit_adjustment",
    ),
    CheckConstraint(
        "entry_kind IN ('debit','reversal','supplement','credit')",
        name="ck_quota_debit_kind",
    ),
    CheckConstraint("page_delta != 0", name="ck_quota_debit_delta_nonzero"),
    # 所有投影分录（四类 entry_kind）必须带 subject/period/cost_center（review #5）
    CheckConstraint(
        "quota_subject_user_id IS NOT NULL AND quota_period IS NOT NULL",
        name="ck_quota_debit_projection_shape",
    ),
    CheckConstraint(
        "NOT (entry_kind = 'debit' AND (page_delta <= 0 OR quota_operation_id IS NULL"
        " OR publication_id IS NULL))",
        name="ck_quota_debit_debit_shape",
    ),
    CheckConstraint(
        "NOT (entry_kind = 'reversal' AND (page_delta >= 0 OR referenced_debit_id IS NULL"
        " OR adjustment_source_namespace IS NULL OR adjustment_source_id IS NULL))",
        name="ck_quota_debit_reversal_shape",
    ),
    CheckConstraint(
        "NOT (entry_kind = 'supplement' AND (page_delta <= 0 OR referenced_debit_id IS NULL"
        " OR adjustment_source_namespace IS NULL OR adjustment_source_id IS NULL))",
        name="ck_quota_debit_supplement_shape",
    ),
    CheckConstraint(
        "NOT (entry_kind = 'credit' AND (page_delta >= 0 OR adjustment_source_namespace IS NULL"
        " OR adjustment_source_namespace != 'quota_request' OR adjustment_source_id IS NULL))",
        name="ck_quota_debit_credit_shape",
    ),
    # rebuild 按 (subject, period) 全量重放；reversal/supplement 按 (entry_kind,
    # referenced_debit_id) 累计——追加式账本缺二级索引会随数据线性全表扫描。
    Index("ix_quota_debit_subject_period", "quota_subject_user_id", "quota_period"),
    Index("ix_quota_debit_kind_reference", "entry_kind", "referenced_debit_id"),
)

# 6) 投影（派生，可重建）
quota_projection_table = Table(
    "quota_projection",
    usage_metadata,
    Column("quota_subject_user_id", String(64), primary_key=True),
    Column("quota_period", String(7), primary_key=True),
    Column("base_limit", BigInteger, nullable=False),
    Column("extra_granted", BigInteger, nullable=False),
    Column("used", BigInteger, nullable=False),
    Column("last_debit_id", String(64), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
)

# 7) quota_request：同申请人同月最多一条 pending（partial unique index）
quota_request_table = Table(
    "quota_request",
    usage_metadata,
    Column("quota_request_id", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("applicant_user_id", String(64), nullable=False),
    Column("applicant_role_snapshot", String(32), nullable=False),
    Column("applicant_department_id_snapshot", String(64), nullable=True),
    Column("quota_period", String(7), nullable=False),
    Column("business_calendar_version_id", String(64), nullable=False),
    Column("requested_pages", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("approver_user_id", String(64), nullable=True),
    Column("approver_role_snapshot", String(32), nullable=True),
    # A58：审批事务冻结审核者当时部门快照（与 role snapshot 同源：AuthPrincipal）。
    Column("approver_department_id", String(64), nullable=True),
    Column("approved_pages", Integer, nullable=True),
    Column("credit_entry_id", String(64), nullable=True),
    Column("cancel_reason", String(64), nullable=True),
    Column("idempotency_fingerprint", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("reviewed_at_utc", DateTime(timezone=True), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('pending','approved','rejected','cancelled')",
        name="ck_quota_request_status",
    ),
    CheckConstraint("requested_pages BETWEEN 1 AND 500", name="ck_quota_request_pages"),
)

quota_request_pending_unique = Index(
    "uq_quota_request_pending",
    quota_request_table.c.applicant_user_id,
    quota_request_table.c.quota_period,
    unique=True,
    sqlite_where=quota_request_table.c.status == "pending",
    postgresql_where=quota_request_table.c.status == "pending",
)

# 7.5) 本地昂贵阶段 meter：可恢复的工作状态；最终事实仍只落在 usage_event。
local_usage_meter_table = Table(
    "local_usage_meter",
    usage_metadata,
    Column("meter_id", String(64), primary_key=True),
    Column("execution_kind", String(32), nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column("stage", String(64), nullable=False),
    Column("resource_kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("ownership_json", JSON, nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("abandoned_at_utc", DateTime(timezone=True), nullable=True),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("checkpoint_sequence", Integer, nullable=False, server_default="0"),
    Column("item_count", BigInteger, nullable=True),
    Column("page_count", BigInteger, nullable=True),
    Column("input_bytes", BigInteger, nullable=True),
    Column("gpu_milliseconds", BigInteger, nullable=True),
    Column("cpu_milliseconds", BigInteger, nullable=True),
    Column("peak_vram_bytes", BigInteger, nullable=True),
    Column("measurement_sources", JSON, nullable=True),
    Column("tail_estimated", Integer, nullable=False, server_default="0"),
    Column("result", String(32), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("usage_event_id", String(64), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "execution_kind", "execution_id", "stage", "resource_kind", name="uq_local_meter_scope"
    ),
    UniqueConstraint("usage_event_id", name="uq_local_meter_usage_event"),
    CheckConstraint("status IN ('running','completed','abandoned')", name="ck_local_meter_status"),
    CheckConstraint(
        "NOT (status = 'running' AND (completed_at_utc IS NOT NULL OR abandoned_at_utc IS NOT NULL"
        " OR usage_event_id IS NOT NULL))",
        name="ck_local_meter_running_clean",
    ),
    CheckConstraint(
        "NOT (status = 'completed' AND (completed_at_utc IS NULL OR usage_event_id IS NULL"
        " OR abandoned_at_utc IS NOT NULL))",
        name="ck_local_meter_completed_shape",
    ),
    CheckConstraint(
        "NOT (status = 'abandoned' AND (abandoned_at_utc IS NULL OR usage_event_id IS NULL"
        " OR completed_at_utc IS NOT NULL))",
        name="ck_local_meter_abandoned_shape",
    ),
    CheckConstraint("checkpoint_sequence >= 0", name="ck_local_meter_checkpoint_sequence"),
    CheckConstraint("tail_estimated IN (0, 1)", name="ck_local_meter_tail_estimated"),
)

# 7.5.1) 本地 usage 投影：可重建派生表；与 meter finalize 同事务更新。
local_usage_projection_table = Table(
    "local_usage_projection",
    usage_metadata,
    Column("local_usage_projection_id", String(64), primary_key=True),
    Column("usage_event_id", String(64), nullable=False),
    Column("execution_kind", String(32), nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column("stage", String(64), nullable=False),
    Column("resource_kind", String(32), nullable=False),
    Column("item_count", BigInteger, nullable=True),
    Column("page_count", BigInteger, nullable=True),
    Column("input_bytes", BigInteger, nullable=True),
    Column("gpu_milliseconds", BigInteger, nullable=True),
    Column("cpu_milliseconds", BigInteger, nullable=True),
    Column("peak_vram_bytes", BigInteger, nullable=True),
    Column("result", String(32), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("tail_estimated", Integer, nullable=False),
    Column("measurement_sources", JSON, nullable=True),
    Column("ownership_json", JSON, nullable=False),
    Column("effective_calendar_version_id", String(64), nullable=False),
    Column("effective_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_period", String(7), nullable=False),
    Column("recorded_calendar_version_id", String(64), nullable=False),
    Column("recorded_at_utc", DateTime(timezone=True), nullable=False),
    Column("recorded_period", String(7), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("usage_event_id", name="uq_local_projection_event"),
    UniqueConstraint(
        "execution_kind", "execution_id", "stage", "resource_kind", name="uq_local_projection_scope"
    ),
)

# 7.6) provider 账单源记录与月级对账分组。源记录不可变；分组/分摊也是可审计事实。
provider_billing_source_record_table = Table(
    "provider_billing_source_record",
    usage_metadata,
    Column("provider_billing_source_record_id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("provider_account_id", String(128), nullable=False),
    Column("billing_source_record_id", String(128), nullable=False),
    Column("model", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("provider_request_id", String(256), nullable=True),
    Column("service_start_utc", DateTime(timezone=True), nullable=True),
    Column("service_end_utc", DateTime(timezone=True), nullable=True),
    Column("service_month", String(7), nullable=True),
    Column("measurements", JSON, nullable=False),
    Column("amount", Numeric(30, 10), nullable=False),
    Column("currency_code", String(8), nullable=False),
    Column("source_status", String(32), nullable=False),
    Column("source_metadata", JSON, nullable=False),
    Column("content_fingerprint", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "provider",
        "provider_account_id",
        "billing_source_record_id",
        name="uq_provider_billing_source_identity",
    ),
    CheckConstraint(
        "service_start_utc IS NOT NULL OR service_end_utc IS NOT NULL OR service_month IS NOT NULL",
        name="ck_provider_billing_service_period",
    ),
    CheckConstraint("amount >= 0", name="ck_provider_billing_amount_nonnegative"),
)

provider_billing_reconciliation_group_table = Table(
    "provider_billing_reconciliation_group",
    usage_metadata,
    Column("provider_billing_reconciliation_group_id", String(64), primary_key=True),
    Column("group_key", String(512), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("provider_account_id", String(128), nullable=False),
    Column("model", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("service_start_utc", DateTime(timezone=True), nullable=True),
    Column("service_end_utc", DateTime(timezone=True), nullable=True),
    Column("service_month", String(7), nullable=True),
    Column("currency_code", String(8), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("group_key", name="uq_billing_group_key"),
    Index("ix_billing_group_scope", "provider", "provider_account_id", "model", "operation"),
)

provider_billing_source_group_table = Table(
    "provider_billing_source_group",
    usage_metadata,
    Column("provider_billing_source_group_id", String(64), primary_key=True),
    Column("provider_billing_source_record_id", String(64), nullable=False),
    Column("provider_billing_reconciliation_group_id", String(64), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "provider_billing_source_record_id",
        "provider_billing_reconciliation_group_id",
        name="uq_billing_source_group_pair",
    ),
    Index("ix_billing_source_group_group", "provider_billing_reconciliation_group_id"),
)

provider_billing_cost_adjustment_table = Table(
    "provider_billing_cost_adjustment",
    usage_metadata,
    Column("provider_billing_cost_adjustment_id", String(64), primary_key=True),
    Column("provider_billing_reconciliation_group_id", String(64), nullable=False),
    Column("event_kind", String(32), nullable=False),
    Column("adjustment_source_namespace", String(64), nullable=False),
    Column("adjustment_source_id", String(128), nullable=False),
    Column("adjustment_allocation_key", String(64), nullable=False),
    Column("amount_delta", Numeric(30, 10), nullable=False),
    Column("currency_code", String(8), nullable=False),
    Column("effective_calendar_version_id", String(64), nullable=False),
    Column("effective_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_period", String(7), nullable=False),
    Column("recorded_calendar_version_id", String(64), nullable=False),
    Column("recorded_at_utc", DateTime(timezone=True), nullable=False),
    Column("recorded_period", String(7), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "event_kind",
        "adjustment_source_namespace",
        "adjustment_source_id",
        "adjustment_allocation_key",
        name="uq_provider_billing_cost_adjustment_source",
    ),
    CheckConstraint("event_kind = 'cost_adjustment'", name="ck_provider_billing_event_kind"),
)

# 7.6.1) 成本投影：可重建派生表；对账在同一事务内同步目标 event/group 投影。
usage_cost_projection_table = Table(
    "usage_cost_projection",
    usage_metadata,
    Column("usage_cost_projection_id", String(64), primary_key=True),
    Column("target_kind", String(32), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("provider_billing_source_record_id", String(64), nullable=True),
    Column("currency_code", String(8), nullable=True),
    Column("estimated_amount", Numeric(30, 10), nullable=True),
    Column("adjustment_amount", Numeric(30, 10), nullable=True),
    Column("projected_amount", Numeric(30, 10), nullable=True),
    Column("cost_status", String(32), nullable=False),
    Column("effective_calendar_version_id", String(64), nullable=False),
    Column("effective_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_period", String(7), nullable=False),
    Column("recorded_calendar_version_id", String(64), nullable=False),
    Column("recorded_at_utc", DateTime(timezone=True), nullable=False),
    Column("recorded_period", String(7), nullable=False),
    Column("rebuilt_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "target_kind",
        "target_id",
        "effective_period",
        "provider_billing_source_record_id",
        name="uq_usage_cost_projection_target",
    ),
    CheckConstraint(
        "target_kind IN ('provider_event','local_usage','reconciliation_group')",
        name="ck_usage_cost_projection_target",
    ),
    CheckConstraint(
        "cost_status IN ('provisional','reconciled','billing_period_unallocated')",
        name="ck_usage_cost_projection_status",
    ),
)

# 7.7) generation 预算 meter 与 reservation：出网前在同一行锁内保守预留。
generation_budget_meter_table = Table(
    "generation_budget_meter",
    usage_metadata,
    Column("generation_budget_meter_id", String(64), primary_key=True),
    Column("generation_id", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("requested_effort_level", String(16), nullable=False),
    Column("effort_level", String(16), nullable=False),
    Column("price_version_id", String(64), nullable=False),
    Column("currency_code", String(8), nullable=False),
    Column("deadline_at_utc", DateTime(timezone=True), nullable=False),
    Column("max_rag_calls", Integer, nullable=False),
    Column("max_wall_seconds", Integer, nullable=False),
    Column("max_total_tokens", BigInteger, nullable=False),
    Column("max_estimated_cost_amount", Numeric(30, 10), nullable=False),
    Column("candidate_document_limit", Integer, nullable=False),
    Column("rag_calls_used", Integer, nullable=False, server_default="0"),
    Column("total_tokens_used", BigInteger, nullable=False, server_default="0"),
    Column("reserved_tokens", BigInteger, nullable=False, server_default="0"),
    Column("settled_cost_amount", Numeric(30, 10), nullable=False, server_default="0"),
    Column("reserved_cost_amount", Numeric(30, 10), nullable=False, server_default="0"),
    Column("upgrade_count", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("exhausted_reason", String(32), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("generation_id", name="uq_generation_budget_generation"),
    CheckConstraint(
        "requested_effort_level IN ('quick','think','deep')"
        " AND effort_level IN ('quick','think','deep')",
        name="ck_generation_budget_effort",
    ),
    CheckConstraint("status IN ('active','exhausted')", name="ck_generation_budget_status"),
    CheckConstraint(
        "status = 'active' OR exhausted_reason IS NOT NULL",
        name="ck_generation_budget_exhausted_reason",
    ),
)

generation_budget_reservation_table = Table(
    "generation_budget_reservation",
    usage_metadata,
    Column("reservation_id", String(128), primary_key=True),
    Column("generation_id", String(128), nullable=False),
    Column("operation_kind", String(64), nullable=False),
    Column("request_fingerprint", String(128), nullable=False),
    Column("estimated_tokens", BigInteger, nullable=False),
    Column("estimated_cost_amount", Numeric(30, 10), nullable=False),
    Column("is_rag", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("actual_tokens", BigInteger, nullable=True),
    Column("actual_cost_amount", Numeric(30, 10), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("settled_at_utc", DateTime(timezone=True), nullable=True),
    UniqueConstraint("generation_id", "request_fingerprint", name="uq_budget_reservation_replay"),
    CheckConstraint("is_rag IN (0, 1)", name="ck_budget_reservation_is_rag"),
    CheckConstraint(
        "status IN ('reserved','settled','released')", name="ck_budget_reservation_status"
    ),
)

# 8) 对账分组（H3：仅金额/未知项的对账事实，不伪造 usage）
usage_reconciliation_table = Table(
    "usage_reconciliation",
    usage_metadata,
    Column("id", String(64), primary_key=True),
    Column("provider_call_id", String(64), nullable=True),
    Column("provider", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("execution_kind", String(32), nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column("attempt_id", String(128), nullable=True),
    Column("reconciliation_kind", String(32), nullable=False),
    Column("amount_only_json", JSON, nullable=True),
    Column("ownership_json", JSON, nullable=False),
    Column("effective_calendar_version_id", String(64), nullable=False),
    Column("effective_at_utc", DateTime(timezone=True), nullable=False),
    Column("effective_period", String(7), nullable=False),
    Column("recorded_calendar_version_id", String(64), nullable=False),
    Column("recorded_at_utc", DateTime(timezone=True), nullable=False),
    Column("recorded_period", String(7), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "reconciliation_kind IN ('amount_only','unknown_pending')",
        name="ck_reconciliation_kind",
    ),
)

USAGE_TABLE_NAMES = frozenset(usage_metadata.tables)
