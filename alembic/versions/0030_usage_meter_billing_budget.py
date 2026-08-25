"""Add local metering, provider billing, and generation budget tables.

Revision ID: 0030_usage_meter_billing_budget
Revises: 0029_account_deletion_archive
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.usage.schema import (
    generation_budget_meter_table,
    generation_budget_reservation_table,
    local_usage_meter_table,
    local_usage_projection_table,
    provider_billing_cost_adjustment_table,
    provider_billing_reconciliation_group_table,
    provider_billing_source_group_table,
    provider_billing_source_record_table,
    usage_cost_projection_table,
    usage_metadata,
)

revision: str = "0030_usage_meter_budget"
down_revision: str | None = "0029_account_deletion_archive"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    local_usage_meter_table,
    local_usage_projection_table,
    provider_billing_source_record_table,
    provider_billing_reconciliation_group_table,
    provider_billing_source_group_table,
    provider_billing_cost_adjustment_table,
    usage_cost_projection_table,
    generation_budget_meter_table,
    generation_budget_reservation_table,
)


def upgrade() -> None:
    usage_metadata.create_all(bind=op.get_bind(), tables=list(_TABLES))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION prevent_provider_billing_source_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'provider_billing_source_record is immutable'; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_source_no_update BEFORE UPDATE "
            "ON provider_billing_source_record FOR EACH ROW "
            "EXECUTE FUNCTION prevent_provider_billing_source_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_source_no_delete BEFORE DELETE "
            "ON provider_billing_source_record FOR EACH ROW "
            "EXECUTE FUNCTION prevent_provider_billing_source_mutation()"
        )
        op.execute(
            "CREATE FUNCTION prevent_provider_billing_adjustment_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'provider_billing_cost_adjustment is immutable'; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE FUNCTION prevent_provider_billing_group_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'provider billing reconciliation facts are immutable'; "
            "END; $$ LANGUAGE plpgsql"
        )
        for table in ("provider_billing_reconciliation_group", "provider_billing_source_group"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION prevent_provider_billing_group_mutation()"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION prevent_provider_billing_group_mutation()"
            )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_adjustment_no_update BEFORE UPDATE "
            "ON provider_billing_cost_adjustment FOR EACH ROW "
            "EXECUTE FUNCTION prevent_provider_billing_adjustment_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_adjustment_no_delete BEFORE DELETE "
            "ON provider_billing_cost_adjustment FOR EACH ROW "
            "EXECUTE FUNCTION prevent_provider_billing_adjustment_mutation()"
        )
    else:
        op.execute(
            "CREATE TRIGGER trg_provider_billing_source_no_update BEFORE UPDATE "
            "ON provider_billing_source_record "
            "BEGIN SELECT RAISE(ABORT, 'provider_billing_source_record is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_source_no_delete BEFORE DELETE "
            "ON provider_billing_source_record "
            "BEGIN SELECT RAISE(ABORT, 'provider_billing_source_record is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_adjustment_no_update BEFORE UPDATE "
            "ON provider_billing_cost_adjustment "
            "BEGIN SELECT RAISE(ABORT, 'provider_billing_cost_adjustment is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_provider_billing_adjustment_no_delete BEFORE DELETE "
            "ON provider_billing_cost_adjustment "
            "BEGIN SELECT RAISE(ABORT, 'provider_billing_cost_adjustment is immutable'); END"
        )
        for table in ("provider_billing_reconciliation_group", "provider_billing_source_group"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'provider billing reconciliation facts are immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'provider billing reconciliation facts are immutable'); END"
            )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table.name)
