"""Create usage-quota domain tables with immutability triggers.

Revision ID: 0011_usage_quota
Revises: 0010_compacted_fields_check
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_usage_quota"
down_revision: str | None = "0010_compacted_fields_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    # 1) business_calendar_version（单行表：id 固定 'instance'，不可 UPDATE/DELETE）
    op.create_table(
        "business_calendar_version",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("effective_from_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 'instance'", name="ck_calendar_singleton_id"),
    )
    # 2) price_catalog_scope（scope 串行化基础行；无不可变 trigger，允许 upsert）
    op.create_table(
        "price_catalog_scope",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "model",
            "operation",
            name="uq_price_catalog_scope_identity",
        ),
    )
    # 3) price_catalog + price_catalog_line
    op.create_table(
        "price_catalog",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("currency_code", sa.String(length=8), nullable=False),
        sa.Column("effective_from_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_version_id", sa.String(length=64), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "effective_to_utc IS NULL OR effective_to_utc > effective_from_utc",
            name="ck_price_catalog_interval",
        ),
        sa.UniqueConstraint(
            "provider",
            "model",
            "operation",
            "effective_from_utc",
            name="uq_price_catalog_scope",
        ),
    )
    op.create_table(
        "price_catalog_line",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("price_version_id", sa.String(length=64), nullable=False),
        sa.Column("meter", sa.String(length=32), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("rate", sa.Numeric(30, 10), nullable=False),
        sa.Column("billing_granularity", sa.Integer(), nullable=False),
        sa.Column("minimum_billable_quantity", sa.Integer(), nullable=False),
        sa.Column("rounding_rule", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("price_version_id", "meter", name="uq_price_line_meter"),
        sa.CheckConstraint("rate >= 0", name="ck_price_line_rate_nonnegative"),
        sa.CheckConstraint("rate = rate + 0", name="ck_price_line_rate_numeric"),
        sa.CheckConstraint("COALESCE(rate - rate = 0, FALSE)", name="ck_price_line_rate_finite"),
        sa.CheckConstraint(
            "rate <= 99999999999999999999.9999999999",
            name="ck_price_line_rate_max",
        ),
        sa.CheckConstraint("billing_granularity >= 1", name="ck_price_line_granularity_positive"),
        sa.CheckConstraint(
            "billing_granularity = CAST(billing_granularity AS INTEGER)",
            name="ck_price_line_granularity_integer",
        ),
        sa.CheckConstraint(
            "billing_granularity <= 2147483647",
            name="ck_price_line_granularity_int32",
        ),
        sa.CheckConstraint(
            "minimum_billable_quantity >= 0",
            name="ck_price_line_minimum_nonnegative",
        ),
        sa.CheckConstraint(
            "minimum_billable_quantity = CAST(minimum_billable_quantity AS INTEGER)",
            name="ck_price_line_minimum_integer",
        ),
        sa.CheckConstraint(
            "minimum_billable_quantity <= 2147483647",
            name="ck_price_line_minimum_int32",
        ),
        sa.CheckConstraint(
            "rounding_rule IN ('floor','ceil','half_up')",
            name="ck_price_line_rounding_rule",
        ),
    )
    # open-interval partial unique index（SQLite 与 PostgreSQL 均支持 WHERE 子句）
    op.execute(
        "CREATE UNIQUE INDEX uq_price_open_interval ON price_catalog "
        "(provider, model, operation) WHERE effective_to_utc IS NULL"
    )
    # 3) provider_call
    op.create_table(
        "provider_call",
        sa.Column("provider_call_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("execution_kind", sa.String(length=32), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_id", sa.String(length=128), nullable=True),
        sa.Column("generation_id", sa.String(length=128), nullable=True),
        sa.Column("resource_id", sa.String(length=256), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("deadline_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("prepared_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatching_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_sent_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unknown_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconcile_attempt_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_call_id"),
        sa.CheckConstraint(
            "attempt_id IS NOT NULL OR generation_id IS NOT NULL",
            name="ck_provider_call_attempt_or_generation",
        ),
        sa.CheckConstraint(
            "status IN ('prepared','dispatching','completed','not_sent','unknown')",
            name="ck_provider_call_status",
        ),
        sa.CheckConstraint(
            "NOT (status = 'prepared' AND (dispatching_at_utc IS NOT NULL OR completed_at_utc IS NOT NULL"
            " OR not_sent_at_utc IS NOT NULL OR unknown_at_utc IS NOT NULL"
            " OR started_at_utc IS NOT NULL OR last_reconcile_attempt_at_utc IS NOT NULL))",
            name="ck_provider_call_prepared_clean",
        ),
        sa.CheckConstraint(
            "NOT (status = 'dispatching' AND (dispatching_at_utc IS NULL OR started_at_utc IS NULL"
            " OR completed_at_utc IS NOT NULL OR not_sent_at_utc IS NOT NULL"
            " OR unknown_at_utc IS NOT NULL))",
            name="ck_provider_call_dispatching_at",
        ),
        sa.CheckConstraint(
            "NOT (status = 'completed' AND (dispatching_at_utc IS NULL OR started_at_utc IS NULL"
            " OR completed_at_utc IS NULL OR not_sent_at_utc IS NOT NULL))",
            name="ck_provider_call_completed_at",
        ),
        sa.CheckConstraint(
            "NOT (status = 'not_sent' AND (not_sent_at_utc IS NULL OR completed_at_utc IS NOT NULL"
            " OR unknown_at_utc IS NOT NULL OR started_at_utc IS NOT NULL))",
            name="ck_provider_call_not_sent_at",
        ),
        sa.CheckConstraint(
            "NOT (status = 'unknown' AND (dispatching_at_utc IS NULL OR started_at_utc IS NULL"
            " OR unknown_at_utc IS NULL OR completed_at_utc IS NOT NULL"
            " OR not_sent_at_utc IS NOT NULL))",
            name="ck_provider_call_unknown_at",
        ),
    )
    # 4) usage_event
    op.create_table(
        "usage_event",
        sa.Column("usage_event_id", sa.String(length=64), nullable=False),
        sa.Column("event_kind", sa.String(length=32), nullable=False),
        sa.Column("provider_call_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=True),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("price_version_id", sa.String(length=64), nullable=True),
        sa.Column("currency_code", sa.String(length=8), nullable=True),
        sa.Column("estimated_cost_amount", sa.Numeric(30, 10), nullable=True),
        sa.Column("estimated_cost_status", sa.String(length=16), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("prompt_cache_hit_tokens", sa.BigInteger(), nullable=True),
        sa.Column("prompt_cache_miss_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("image_count", sa.BigInteger(), nullable=True),
        sa.Column("visual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("embedding_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("vector_count", sa.BigInteger(), nullable=True),
        sa.Column("execution_kind", sa.String(length=32), nullable=True),
        sa.Column("execution_id", sa.String(length=128), nullable=True),
        sa.Column("attempt_id", sa.String(length=128), nullable=True),
        sa.Column("generation_id", sa.String(length=128), nullable=True),
        sa.Column("resource_id", sa.String(length=256), nullable=True),
        sa.Column("cost_center_key", sa.String(length=128), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("resource_kind", sa.String(length=32), nullable=True),
        sa.Column("item_count", sa.BigInteger(), nullable=True),
        sa.Column("page_count", sa.BigInteger(), nullable=True),
        sa.Column("input_bytes", sa.BigInteger(), nullable=True),
        sa.Column("gpu_milliseconds", sa.BigInteger(), nullable=True),
        sa.Column("cpu_milliseconds", sa.BigInteger(), nullable=True),
        sa.Column("peak_vram_bytes", sa.BigInteger(), nullable=True),
        sa.Column("adjustment_source_namespace", sa.String(length=64), nullable=True),
        sa.Column("adjustment_source_id", sa.String(length=128), nullable=True),
        sa.Column("adjustment_allocation_key", sa.String(length=64), nullable=True),
        sa.Column("referenced_usage_event_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("measurement_sources", sa.JSON(), nullable=True),
        sa.Column("event_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("ownership_json", sa.JSON(), nullable=False),
        sa.Column("started_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("effective_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_period", sa.String(length=7), nullable=False),
        sa.Column("recorded_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_period", sa.String(length=7), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("usage_event_id"),
        sa.UniqueConstraint("provider_call_id", name="uq_usage_provider_call"),
        sa.UniqueConstraint(
            "execution_kind",
            "execution_id",
            "stage",
            "resource_kind",
            name="uq_usage_local_scope",
        ),
        sa.UniqueConstraint(
            "event_kind",
            "adjustment_source_namespace",
            "adjustment_source_id",
            "adjustment_allocation_key",
            name="uq_usage_adjustment_source",
        ),
        sa.CheckConstraint(
            "event_kind IN ('provider_usage','local_usage','usage_adjustment','cost_adjustment')",
            name="ck_usage_event_kind",
        ),
        sa.CheckConstraint(
            "estimated_cost_status IS NULL OR estimated_cost_status IN "
            "('complete','partial','unavailable')",
            name="ck_usage_cost_status",
        ),
        sa.CheckConstraint(
            "execution_kind IS NOT NULL AND execution_id IS NOT NULL",
            name="ck_usage_event_execution_identity",
        ),
        sa.CheckConstraint(
            "NOT (event_kind = 'provider_usage' AND (provider_call_id IS NULL OR provider IS NULL"
            " OR model IS NULL OR operation IS NULL OR price_version_id IS NULL OR currency_code IS NULL"
            " OR estimated_cost_status IS NULL OR execution_kind IS NULL OR execution_id IS NULL))",
            name="ck_usage_provider_shape",
        ),
        sa.CheckConstraint(
            "NOT (event_kind = 'local_usage' AND (execution_kind IS NULL OR execution_id IS NULL"
            " OR stage IS NULL OR resource_kind IS NULL))",
            name="ck_usage_local_shape",
        ),
        sa.CheckConstraint(
            "NOT (event_kind IN ('usage_adjustment','cost_adjustment')"
            " AND (adjustment_source_namespace IS NULL OR adjustment_source_id IS NULL"
            " OR adjustment_allocation_key IS NULL OR referenced_usage_event_id IS NULL))",
            name="ck_usage_adjustment_shape",
        ),
        sa.CheckConstraint("cost_center_key IS NOT NULL", name="ck_usage_event_cost_center"),
    )
    # 5) quota_debit
    op.create_table(
        "quota_debit",
        sa.Column("quota_debit_id", sa.String(length=64), nullable=False),
        sa.Column("entry_kind", sa.String(length=16), nullable=False),
        sa.Column("page_delta", sa.Integer(), nullable=False),
        sa.Column("entry_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("quota_operation_id", sa.String(length=128), nullable=True),
        sa.Column("publication_id", sa.String(length=128), nullable=True),
        sa.Column("quota_subject_user_id", sa.String(length=64), nullable=True),
        sa.Column("quota_period", sa.String(length=7), nullable=True),
        sa.Column("quota_exempt_reason", sa.String(length=64), nullable=True),
        sa.Column("referenced_debit_id", sa.String(length=64), nullable=True),
        sa.Column("adjustment_source_namespace", sa.String(length=64), nullable=True),
        sa.Column("adjustment_source_id", sa.String(length=128), nullable=True),
        sa.Column("adjustment_allocation_key", sa.String(length=64), nullable=True),
        sa.Column("cost_center_key", sa.String(length=128), nullable=False),
        sa.Column("ownership_json", sa.JSON(), nullable=False),
        sa.Column("effective_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("effective_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_period", sa.String(length=7), nullable=False),
        sa.Column("recorded_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_period", sa.String(length=7), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("quota_debit_id"),
        sa.UniqueConstraint("quota_operation_id", name="uq_quota_debit_operation"),
        sa.UniqueConstraint(
            "entry_kind",
            "adjustment_source_namespace",
            "adjustment_source_id",
            name="uq_quota_debit_adjustment",
        ),
        sa.CheckConstraint(
            "entry_kind IN ('debit','reversal','supplement','credit')",
            name="ck_quota_debit_kind",
        ),
        sa.CheckConstraint("page_delta != 0", name="ck_quota_debit_delta_nonzero"),
        sa.CheckConstraint(
            "quota_subject_user_id IS NOT NULL AND quota_period IS NOT NULL",
            name="ck_quota_debit_projection_shape",
        ),
        sa.CheckConstraint(
            "NOT (entry_kind = 'debit' AND (page_delta <= 0 OR quota_operation_id IS NULL"
            " OR publication_id IS NULL))",
            name="ck_quota_debit_debit_shape",
        ),
        sa.CheckConstraint(
            "NOT (entry_kind = 'reversal' AND (page_delta >= 0 OR referenced_debit_id IS NULL"
            " OR adjustment_source_namespace IS NULL OR adjustment_source_id IS NULL))",
            name="ck_quota_debit_reversal_shape",
        ),
        sa.CheckConstraint(
            "NOT (entry_kind = 'supplement' AND (page_delta <= 0 OR referenced_debit_id IS NULL"
            " OR adjustment_source_namespace IS NULL OR adjustment_source_id IS NULL))",
            name="ck_quota_debit_supplement_shape",
        ),
        sa.CheckConstraint(
            "NOT (entry_kind = 'credit' AND (page_delta >= 0 OR adjustment_source_namespace IS NULL"
            " OR adjustment_source_namespace != 'quota_request' OR adjustment_source_id IS NULL))",
            name="ck_quota_debit_credit_shape",
        ),
    )
    # 6) quota_projection
    op.create_table(
        "quota_projection",
        sa.Column("quota_subject_user_id", sa.String(length=64), nullable=False),
        sa.Column("quota_period", sa.String(length=7), nullable=False),
        sa.Column("base_limit", sa.Integer(), nullable=False),
        sa.Column("extra_granted", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("last_debit_id", sa.String(length=64), nullable=True),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("quota_subject_user_id", "quota_period"),
    )
    # 7) quota_request + partial unique index
    op.create_table(
        "quota_request",
        sa.Column("quota_request_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("applicant_user_id", sa.String(length=64), nullable=False),
        sa.Column("applicant_role_snapshot", sa.String(length=32), nullable=False),
        sa.Column("applicant_department_id_snapshot", sa.String(length=64), nullable=True),
        sa.Column("quota_period", sa.String(length=7), nullable=False),
        sa.Column("business_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("requested_pages", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approver_user_id", sa.String(length=64), nullable=True),
        sa.Column("approver_role_snapshot", sa.String(length=32), nullable=True),
        sa.Column("approved_pages", sa.Integer(), nullable=True),
        sa.Column("credit_entry_id", sa.String(length=64), nullable=True),
        sa.Column("cancel_reason", sa.String(length=64), nullable=True),
        sa.Column("idempotency_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("quota_request_id"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','cancelled')",
            name="ck_quota_request_status",
        ),
        sa.CheckConstraint("requested_pages BETWEEN 1 AND 500", name="ck_quota_request_pages"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_quota_request_pending ON quota_request "
        "(applicant_user_id, quota_period) WHERE status = 'pending'"
    )
    # 8) usage_reconciliation
    op.create_table(
        "usage_reconciliation",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider_call_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("execution_kind", sa.String(length=32), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_id", sa.String(length=128), nullable=True),
        sa.Column("reconciliation_kind", sa.String(length=32), nullable=False),
        sa.Column("amount_only_json", sa.JSON(), nullable=True),
        sa.Column("ownership_json", sa.JSON(), nullable=False),
        sa.Column("effective_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("effective_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_period", sa.String(length=7), nullable=False),
        sa.Column("recorded_calendar_version_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_period", sa.String(length=7), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "reconciliation_kind IN ('amount_only','unknown_pending')",
            name="ck_reconciliation_kind",
        ),
    )
    # 9) 不可变/append-only 触发器（C1）：
    #   - 账本行 usage_event / quota_debit：不可 UPDATE/DELETE
    #   - business_calendar_version（singleton）：不可 UPDATE/DELETE；不提供运行时轮换/切换
    #   - price_catalog_line：不可 UPDATE/DELETE
    #   - price_catalog：不可 DELETE；UPDATE 仅允许一次性 close
    #     （effective_to_utc NULL→非 NULL 且其余字段完全不变），防历史重写
    if _dialect() == "postgresql":
        op.execute(
            "CREATE FUNCTION prevent_usage_event_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'usage_event is immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_usage_event_no_update BEFORE UPDATE ON usage_event "
            "FOR EACH ROW EXECUTE FUNCTION prevent_usage_event_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_usage_event_no_delete BEFORE DELETE ON usage_event "
            "FOR EACH ROW EXECUTE FUNCTION prevent_usage_event_mutation()"
        )
        op.execute(
            "CREATE FUNCTION validate_usage_event_measurement_sources() RETURNS trigger AS $$ "
            "BEGIN "
            "IF NEW.measurement_sources IS NOT NULL THEN "
            "IF jsonb_typeof(NEW.measurement_sources::jsonb) <> 'object' THEN "
            "RAISE EXCEPTION 'usage_event measurement_sources must be a JSON object'; "
            "END IF; "
            "IF EXISTS (SELECT 1 FROM jsonb_each(NEW.measurement_sources::jsonb) je "
            "WHERE je.value::text NOT IN "
            "('\"provider_reported\"', '\"client_measured\"', '\"estimated\"')) THEN "
            "RAISE EXCEPTION 'usage_event measurement_sources has invalid value'; "
            "END IF; "
            "END IF; "
            "RETURN NEW; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_usage_event_measurement_sources BEFORE INSERT ON usage_event "
            "FOR EACH ROW EXECUTE FUNCTION validate_usage_event_measurement_sources()"
        )
        op.execute(
            "CREATE FUNCTION prevent_quota_debit_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'quota_debit is immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_quota_debit_no_update BEFORE UPDATE ON quota_debit "
            "FOR EACH ROW EXECUTE FUNCTION prevent_quota_debit_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_quota_debit_no_delete BEFORE DELETE ON quota_debit "
            "FOR EACH ROW EXECUTE FUNCTION prevent_quota_debit_mutation()"
        )
        op.execute(
            "CREATE FUNCTION prevent_business_calendar_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'business_calendar_version is immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_calendar_version_no_update BEFORE UPDATE ON business_calendar_version "
            "FOR EACH ROW EXECUTE FUNCTION prevent_business_calendar_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_calendar_version_no_delete BEFORE DELETE ON business_calendar_version "
            "FOR EACH ROW EXECUTE FUNCTION prevent_business_calendar_mutation()"
        )
        op.execute(
            "CREATE FUNCTION prevent_price_catalog_line_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'price_catalog_line is immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_price_line_no_update BEFORE UPDATE ON price_catalog_line "
            "FOR EACH ROW EXECUTE FUNCTION prevent_price_catalog_line_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_price_line_no_delete BEFORE DELETE ON price_catalog_line "
            "FOR EACH ROW EXECUTE FUNCTION prevent_price_catalog_line_mutation()"
        )
        op.execute(
            "CREATE FUNCTION prevent_price_catalog_delete() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'price_catalog is append-only'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_price_catalog_no_delete BEFORE DELETE ON price_catalog "
            "FOR EACH ROW EXECUTE FUNCTION prevent_price_catalog_delete()"
        )
        op.execute(
            "CREATE FUNCTION prevent_price_catalog_history_rewrite() RETURNS trigger AS $$ "
            "BEGIN "
            "IF NOT (OLD.effective_to_utc IS NULL AND NEW.effective_to_utc IS NOT NULL "
            "AND NEW.effective_to_utc > NEW.effective_from_utc "
            "AND NEW.id = OLD.id "
            "AND NEW.provider = OLD.provider AND NEW.model = OLD.model "
            "AND NEW.operation = OLD.operation AND NEW.currency_code = OLD.currency_code "
            "AND NEW.effective_from_utc = OLD.effective_from_utc "
            "AND NEW.supersedes_version_id IS NOT DISTINCT FROM OLD.supersedes_version_id "
            "AND NEW.created_at_utc = OLD.created_at_utc) THEN "
            "RAISE EXCEPTION 'price_catalog update is append-only'; "
            "END IF; "
            "RETURN NEW; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_price_catalog_no_update BEFORE UPDATE ON price_catalog "
            "FOR EACH ROW EXECUTE FUNCTION prevent_price_catalog_history_rewrite()"
        )
    else:
        op.execute(
            "CREATE TRIGGER trg_usage_event_no_update BEFORE UPDATE ON usage_event "
            "BEGIN SELECT RAISE(ABORT, 'usage_event is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_usage_event_no_delete BEFORE DELETE ON usage_event "
            "BEGIN SELECT RAISE(ABORT, 'usage_event is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_usage_event_measurement_sources BEFORE INSERT ON usage_event "
            "FOR EACH ROW WHEN (NEW.measurement_sources IS NOT NULL) BEGIN "
            "SELECT CASE WHEN json_type(NEW.measurement_sources) IS NOT 'object' "
            "THEN RAISE(ABORT, 'usage_event measurement_sources must be a JSON object') END; "
            "SELECT CASE WHEN (SELECT COUNT(*) FROM json_each(NEW.measurement_sources) AS je "
            "WHERE NOT (je.type = 'text' AND je.value IN "
            "('provider_reported', 'client_measured', 'estimated'))) > 0 "
            "THEN RAISE(ABORT, 'usage_event measurement_sources has invalid value') END; END"
        )
        op.execute(
            "CREATE TRIGGER trg_quota_debit_no_update BEFORE UPDATE ON quota_debit "
            "BEGIN SELECT RAISE(ABORT, 'quota_debit is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_quota_debit_no_delete BEFORE DELETE ON quota_debit "
            "BEGIN SELECT RAISE(ABORT, 'quota_debit is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_calendar_version_no_update BEFORE UPDATE ON business_calendar_version "
            "BEGIN SELECT RAISE(ABORT, 'business_calendar_version is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_calendar_version_no_delete BEFORE DELETE ON business_calendar_version "
            "BEGIN SELECT RAISE(ABORT, 'business_calendar_version is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_price_line_no_update BEFORE UPDATE ON price_catalog_line "
            "BEGIN SELECT RAISE(ABORT, 'price_catalog_line is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_price_line_no_delete BEFORE DELETE ON price_catalog_line "
            "BEGIN SELECT RAISE(ABORT, 'price_catalog_line is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_price_catalog_no_delete BEFORE DELETE ON price_catalog "
            "BEGIN SELECT RAISE(ABORT, 'price_catalog is append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_price_catalog_no_update BEFORE UPDATE ON price_catalog "
            "FOR EACH ROW BEGIN "
            "SELECT CASE WHEN NOT (OLD.effective_to_utc IS NULL AND NEW.effective_to_utc IS NOT NULL "
            "AND NEW.effective_to_utc > NEW.effective_from_utc "
            "AND NEW.id = OLD.id "
            "AND NEW.provider = OLD.provider AND NEW.model = OLD.model "
            "AND NEW.operation = OLD.operation AND NEW.currency_code = OLD.currency_code "
            "AND NEW.effective_from_utc = OLD.effective_from_utc "
            "AND NEW.supersedes_version_id IS OLD.supersedes_version_id "
            "AND NEW.created_at_utc = OLD.created_at_utc) "
            "THEN RAISE(ABORT, 'price_catalog update is append-only') END; END"
        )


def downgrade() -> None:
    # 1) 先删全部触发器（所有方言），再删 PG 函数
    trigger_table_map = {
        "trg_usage_event_no_update": "usage_event",
        "trg_usage_event_no_delete": "usage_event",
        "trg_usage_event_measurement_sources": "usage_event",
        "trg_quota_debit_no_update": "quota_debit",
        "trg_quota_debit_no_delete": "quota_debit",
        "trg_calendar_version_no_update": "business_calendar_version",
        "trg_calendar_version_no_delete": "business_calendar_version",
        "trg_price_line_no_update": "price_catalog_line",
        "trg_price_line_no_delete": "price_catalog_line",
        "trg_price_catalog_no_update": "price_catalog",
        "trg_price_catalog_no_delete": "price_catalog",
    }
    if _dialect() == "postgresql":
        for name, table in trigger_table_map.items():
            op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        op.execute("DROP FUNCTION IF EXISTS prevent_usage_event_mutation()")
        op.execute("DROP FUNCTION IF EXISTS validate_usage_event_measurement_sources()")
        op.execute("DROP FUNCTION IF EXISTS prevent_quota_debit_mutation()")
        op.execute("DROP FUNCTION IF EXISTS prevent_business_calendar_mutation()")
        op.execute("DROP FUNCTION IF EXISTS prevent_price_catalog_line_mutation()")
        op.execute("DROP FUNCTION IF EXISTS prevent_price_catalog_delete()")
        op.execute("DROP FUNCTION IF EXISTS prevent_price_catalog_history_rewrite()")
    else:
        for name in trigger_table_map:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    # 2) 再删 partial unique index（触发器/函数不依赖索引，但保持逆序稳定）
    op.drop_index("uq_quota_request_pending", table_name="quota_request")
    op.drop_index("uq_price_open_interval", table_name="price_catalog")
    # 3) 最后逆序删表
    for table in (
        "usage_reconciliation",
        "quota_request",
        "quota_projection",
        "quota_debit",
        "usage_event",
        "provider_call",
        "price_catalog_line",
        "price_catalog",
        "price_catalog_scope",
        "business_calendar_version",
    ):
        op.drop_table(table)
