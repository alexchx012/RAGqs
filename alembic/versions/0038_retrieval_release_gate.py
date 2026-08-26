"""Create the immutable retrieval release gate tables and release gate columns.

- ``retrieval_release_gates`` / ``retrieval_release_gate_metrics``：与
  price_catalog 同模式的不可变版本化配置（append-only；UPDATE 仅允许一次性
  close 且其余字段逐列不变，DB trigger 强制，SQLite/PG 双方言）。
- ``retrieval_releases`` 新增 ``gate_version_id`` / ``gate_judgment_json``
  可空列；历史 release 行保持原样（NULL 即沿用内嵌 suite 判定路径）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.indexing.schema import (
    indexing_metadata,
    retrieval_release_gate_metric_table,
    retrieval_release_gate_table,
)

revision: str = "0038_retrieval_release_gate"
down_revision: str | None = "0037_document_read_lease_token"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None

_RELEASES = "retrieval_releases"
_GATE = "retrieval_release_gates"
_GATE_METRIC = "retrieval_release_gate_metrics"


def _gate_columns_present(bind: sa.engine.Connection) -> bool:
    columns = {column["name"] for column in sa.inspect(bind).get_columns(_RELEASES)}
    return {"gate_version_id", "gate_judgment_json"} <= columns


def upgrade() -> None:
    bind = op.get_bind()
    indexing_metadata.create_all(
        bind, tables=[retrieval_release_gate_table, retrieval_release_gate_metric_table]
    )
    if not _gate_columns_present(bind):
        with op.batch_alter_table(_RELEASES) as batch:
            batch.add_column(sa.Column("gate_version_id", sa.String(length=128), nullable=True))
            batch.add_column(sa.Column("gate_judgment_json", sa.JSON(), nullable=True))
    # 不可变/append-only 触发器：gate 版本发布后不得原地修改（新要求发新版本）。
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION prevent_retrieval_release_gate_delete() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'retrieval_release_gate is append-only'; END; "
            "$$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_retrieval_release_gate_no_delete "
            f"BEFORE DELETE ON {_GATE} "
            "FOR EACH ROW EXECUTE FUNCTION prevent_retrieval_release_gate_delete()"
        )
        op.execute(
            "CREATE FUNCTION prevent_retrieval_gate_metric_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'retrieval_release_gate_metric is immutable'; END; "
            "$$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_retrieval_gate_metric_no_update "
            f"BEFORE UPDATE ON {_GATE_METRIC} "
            "FOR EACH ROW EXECUTE FUNCTION prevent_retrieval_gate_metric_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_retrieval_gate_metric_no_delete "
            f"BEFORE DELETE ON {_GATE_METRIC} "
            "FOR EACH ROW EXECUTE FUNCTION prevent_retrieval_gate_metric_mutation()"
        )
        op.execute(
            "CREATE FUNCTION prevent_retrieval_release_gate_history_rewrite() "
            "RETURNS trigger AS $$ "
            "BEGIN "
            "IF NOT (OLD.effective_to_utc IS NULL AND NEW.effective_to_utc IS NOT NULL "
            "AND NEW.effective_to_utc > NEW.effective_from_utc "
            "AND NEW.id = OLD.id AND NEW.version = OLD.version "
            "AND NEW.hardware_profile_json = OLD.hardware_profile_json "
            "AND NEW.concurrency = OLD.concurrency "
            "AND NEW.effective_from_utc = OLD.effective_from_utc "
            "AND NEW.supersedes_version_id IS NOT DISTINCT FROM "
            "OLD.supersedes_version_id "
            "AND NEW.created_at_utc = OLD.created_at_utc) THEN "
            "RAISE EXCEPTION 'retrieval_release_gate update is append-only'; "
            "END IF; "
            "RETURN NEW; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_retrieval_release_gate_no_update "
            f"BEFORE UPDATE ON {_GATE} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "prevent_retrieval_release_gate_history_rewrite()"
        )
    else:
        op.execute(
            f"CREATE TRIGGER trg_retrieval_release_gate_no_delete BEFORE DELETE ON {_GATE} "
            "BEGIN SELECT RAISE(ABORT, 'retrieval_release_gate is append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_retrieval_gate_metric_no_update "
            f"BEFORE UPDATE ON {_GATE_METRIC} "
            "BEGIN SELECT RAISE(ABORT, 'retrieval_release_gate_metric is immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_retrieval_gate_metric_no_delete "
            f"BEFORE DELETE ON {_GATE_METRIC} "
            "BEGIN SELECT RAISE(ABORT, 'retrieval_release_gate_metric is immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_retrieval_release_gate_no_update BEFORE UPDATE ON {_GATE} "
            "FOR EACH ROW BEGIN "
            "SELECT CASE WHEN NOT ("
            "OLD.effective_to_utc IS NULL AND NEW.effective_to_utc IS NOT NULL "
            "AND NEW.effective_to_utc > NEW.effective_from_utc "
            "AND NEW.id = OLD.id AND NEW.version = OLD.version "
            "AND NEW.hardware_profile_json = OLD.hardware_profile_json "
            "AND NEW.concurrency = OLD.concurrency "
            "AND NEW.effective_from_utc = OLD.effective_from_utc "
            "AND NEW.supersedes_version_id IS OLD.supersedes_version_id "
            "AND NEW.created_at_utc = OLD.created_at_utc) "
            "THEN RAISE(ABORT, 'retrieval_release_gate update is append-only') END; END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_release_gate_no_update ON " + _GATE)
        op.execute("DROP FUNCTION IF EXISTS prevent_retrieval_release_gate_history_rewrite()")
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_gate_metric_no_delete ON " + _GATE_METRIC)
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_gate_metric_no_update ON " + _GATE_METRIC)
        op.execute("DROP FUNCTION IF EXISTS prevent_retrieval_gate_metric_mutation()")
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_release_gate_no_delete ON " + _GATE)
        op.execute("DROP FUNCTION IF EXISTS prevent_retrieval_release_gate_delete()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_release_gate_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_gate_metric_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_gate_metric_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_retrieval_release_gate_no_delete")
    if _gate_columns_present(bind):
        with op.batch_alter_table(_RELEASES) as batch:
            batch.drop_column("gate_judgment_json")
            batch.drop_column("gate_version_id")
    indexing_metadata.drop_all(
        bind, tables=[retrieval_release_gate_metric_table, retrieval_release_gate_table]
    )
