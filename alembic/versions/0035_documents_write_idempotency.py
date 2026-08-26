"""Hash document idempotency keys and bind dedup claims to versions."""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from sqlalchemy import JSON, CheckConstraint, Column, DateTime, MetaData, PrimaryKeyConstraint

from alembic import op

revision: str = "0035_documents_write_idempotency"
down_revision: str | None = "0034_backup_skipped_missed"
branch_labels = None
depends_on = None


def _columns(bind, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _idempotency_copy_table() -> sa.Table:
    metadata = MetaData()
    return sa.Table(
        "documents_idempotency",
        metadata,
        Column("actor_id", sa.String(64), nullable=False),
        Column("endpoint", sa.String(256), nullable=False),
        Column("target_id", sa.String(128), nullable=False),
        Column("idempotency_key", sa.String(256), nullable=False),
        Column("idempotency_key_hash", sa.String(64), nullable=True),
        Column("request_fingerprint", sa.String(128), nullable=False),
        Column("status", sa.String(32), nullable=False),
        Column("response_json", JSON, nullable=True),
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
        Column("completed_at_utc", DateTime(timezone=True), nullable=True),
        PrimaryKeyConstraint(
            "actor_id",
            "endpoint",
            "target_id",
            "idempotency_key",
            name="pk_documents_idempotency",
        ),
        CheckConstraint(
            "status IN ('reserved','completed')",
            name="ck_documents_idempotency_status",
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "documents_idempotency")
    if "idempotency_key_hash" not in columns:
        if "idempotency_key" not in columns:
            raise RuntimeError("documents_idempotency has neither key column nor hash column")

        with op.batch_alter_table("documents_idempotency", recreate="always") as batch:
            batch.add_column(sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True))

        rows = bind.execute(
            sa.text(
                "SELECT actor_id, endpoint, target_id, idempotency_key "
                "FROM documents_idempotency"
            )
        ).mappings().all()
        for row in rows:
            key_hash = hashlib.sha256(str(row["idempotency_key"]).encode("utf-8")).hexdigest()
            bind.execute(
                sa.text(
                    "UPDATE documents_idempotency SET idempotency_key_hash = :key_hash "
                    "WHERE actor_id = :actor_id AND endpoint = :endpoint "
                    "AND target_id = :target_id AND idempotency_key = :idempotency_key"
                ),
                {
                    "key_hash": key_hash,
                    "actor_id": row["actor_id"],
                    "endpoint": row["endpoint"],
                    "target_id": row["target_id"],
                    "idempotency_key": row["idempotency_key"],
                },
            )

        with op.batch_alter_table(
            "documents_idempotency",
            recreate="always",
            copy_from=_idempotency_copy_table(),
        ) as batch:
            batch.drop_column("idempotency_key")
            batch.alter_column(
                "idempotency_key_hash",
                existing_type=sa.String(length=64),
                nullable=False,
            )
            batch.create_primary_key(
                "pk_documents_idempotency",
                ["actor_id", "endpoint", "target_id", "idempotency_key_hash"],
            )

    claim_columns = _columns(bind, "upload_dedup_claims")
    if "document_version_id" not in claim_columns:
        with op.batch_alter_table("upload_dedup_claims", recreate="always") as batch:
            batch.add_column(
                sa.Column(
                    "document_version_id",
                    sa.String(length=128),
                    sa.ForeignKey("document_versions.id", name="fk_upload_dedup_claims_version"),
                    nullable=True,
                )
            )
        bind.execute(
            sa.text(
                "UPDATE upload_dedup_claims "
                "SET document_version_id = COALESCE("
                "(SELECT pending_version_id FROM documents WHERE documents.id = upload_dedup_claims.document_id), "
                "(SELECT active_version_id FROM documents WHERE documents.id = upload_dedup_claims.document_id)) "
                "WHERE document_version_id IS NULL"
            )
        )


def _idempotency_hash_copy_table() -> sa.Table:
    metadata = MetaData()
    return sa.Table(
        "documents_idempotency",
        metadata,
        Column("actor_id", sa.String(64), nullable=False),
        Column("endpoint", sa.String(256), nullable=False),
        Column("target_id", sa.String(128), nullable=False),
        Column("idempotency_key_hash", sa.String(64), nullable=False),
        Column("request_fingerprint", sa.String(128), nullable=False),
        Column("status", sa.String(32), nullable=False),
        Column("response_json", JSON, nullable=True),
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
        Column("completed_at_utc", DateTime(timezone=True), nullable=True),
        PrimaryKeyConstraint(
            "actor_id",
            "endpoint",
            "target_id",
            "idempotency_key_hash",
            name="pk_documents_idempotency",
        ),
        CheckConstraint(
            "status IN ('reserved','completed')",
            name="ck_documents_idempotency_status",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "idempotency_key_hash" in _columns(bind, "documents_idempotency"):
        remaining = bind.execute(
            sa.text("SELECT COUNT(*) FROM documents_idempotency")
        ).scalar_one()
        if remaining:
            # Hashed keys cannot be turned back into plaintext.
            raise RuntimeError("downgrade cannot recover plaintext idempotency keys")
        # Empty table: restore the pre-hash plaintext shape so a fresh
        # database can still roll all the way back to base.
        with op.batch_alter_table(
            "documents_idempotency",
            recreate="always",
            copy_from=_idempotency_hash_copy_table(),
        ) as batch:
            batch.drop_column("idempotency_key_hash")
            batch.add_column(
                sa.Column("idempotency_key", sa.String(length=256), nullable=False)
            )
            batch.create_primary_key(
                "pk_documents_idempotency",
                ["actor_id", "endpoint", "target_id", "idempotency_key"],
            )
