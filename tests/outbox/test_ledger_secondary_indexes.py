"""0023 ledger secondary indexes: PG query plans must use the new indexes."""

from __future__ import annotations

import pytest
from _helpers import alembic_config, pg_schema_context
from sqlalchemy import text

from alembic import command


def test_head_upgrade_secondary_indexes_are_used_by_query_plans_on_postgres() -> None:
    # 空表下 PostgreSQL 会退回 Seq Scan，因此关闭 enable_seqscan，断言
    # 0023 的四个索引可被规划器选中；每个 EXPLAIN 对应一条真实查询形状
    # （rebuild / reversal 累计 / reconcile stale 扫描 / compaction 回收）。
    context = pg_schema_context()
    if context is None:
        pytest.skip("PostgreSQL integration environment is not configured")
    try:
        with context.engine.connect() as connection:
            connection.execute(text("SET enable_seqscan = off"))
            subject_period_plan = str(
                connection.execute(
                    text(
                        """EXPLAIN (FORMAT JSON) SELECT * FROM quota_debit
                           WHERE quota_subject_user_id = 'u' AND quota_period = '2026-08'"""
                    )
                ).scalar()
            )
            kind_reference_plan = str(
                connection.execute(
                    text(
                        """EXPLAIN (FORMAT JSON) SELECT * FROM quota_debit
                           WHERE entry_kind = 'reversal' AND referenced_debit_id = 'd'"""
                    )
                ).scalar()
            )
            reconcile_plan = str(
                connection.execute(
                    text(
                        """EXPLAIN (FORMAT JSON) SELECT * FROM provider_call
                           WHERE status = 'dispatching'
                           AND dispatching_at_utc <= '2026-01-01'"""
                    )
                ).scalar()
            )
            attempt_event_plan = str(
                connection.execute(
                    text(
                        """EXPLAIN (FORMAT JSON) SELECT * FROM outbox_delivery_attempt
                           WHERE event_id = 'e'"""
                    )
                ).scalar()
            )
        assert "ix_quota_debit_subject_period" in subject_period_plan
        assert "ix_quota_debit_kind_reference" in kind_reference_plan
        assert "ix_provider_call_reconcile" in reconcile_plan
        assert "ix_outbox_delivery_attempt_event" in attempt_event_plan
        # 反向迁移在 PG 上移除 0023 的索引（0023 是纯索引迁移）。
        command.downgrade(
            alembic_config(context.engine.url.render_as_string(hide_password=False)),
            "0022_merge_graph_op_id",
        )
        with context.engine.connect() as connection:
            connection.execute(text("SET enable_seqscan = off"))
            subject_period_plan_after = str(
                connection.execute(
                    text(
                        """EXPLAIN (FORMAT JSON) SELECT * FROM quota_debit
                           WHERE quota_subject_user_id = 'u' AND quota_period = '2026-08'"""
                    )
                ).scalar()
            )
        assert "ix_quota_debit_subject_period" not in subject_period_plan_after
    finally:
        context.close()
