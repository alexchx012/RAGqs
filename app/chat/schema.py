"""chat-generation domain tables (SQLAlchemy Core; greenfield, no legacy chat schema)."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

chat_metadata = MetaData()


def _timestamps():
    return (
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
        Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    )


chat_conversation_group_table = Table(
    "chat_conversation_group",
    chat_metadata,
    Column("id", String(64), primary_key=True),
    Column("owner_user_id", String(64), nullable=False),
    Column("name", String(256), nullable=False),
    *_timestamps(),
    Index("ix_chat_conversation_group_owner", "owner_user_id"),
)


chat_conversation_table = Table(
    "chat_conversation",
    chat_metadata,
    Column("id", String(64), primary_key=True),
    Column("owner_user_id", String(64), nullable=False),
    Column("title", String(512), nullable=False),
    Column("pinned", Boolean, nullable=False),
    Column(
        "group_id",
        String(64),
        ForeignKey("chat_conversation_group.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("effort_level", String(16), nullable=False),
    Column("scope_json", JSON, nullable=False),
    Column("last_active_at_utc", DateTime(timezone=True), nullable=False),
    *_timestamps(),
    CheckConstraint("effort_level IN ('quick','think','deep')", name="ck_chat_conversation_effort"),
    Index("ix_chat_conversation_owner", "owner_user_id"),
)


chat_message_table = Table(
    "chat_message",
    chat_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "conversation_id",
        String(64),
        ForeignKey("chat_conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("owner_user_id", String(64), nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("answer_mode", String(16), nullable=True),
    Column("effort_level", String(16), nullable=True),
    Column("generation_id", String(64), nullable=True),
    Column("root_generation_id", String(64), nullable=True),
    Column("retry_of_generation_id", String(64), nullable=True),
    Column("attempt_number", Integer, nullable=True),
    Column("status", String(16), nullable=True),
    Column("stop_reason", String(32), nullable=True),
    Column("notices_json", JSON, nullable=True),
    Column("citations_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint("role IN ('user','assistant')", name="ck_chat_message_role"),
    CheckConstraint(
        "status IS NULL OR status IN ('generating','completed','failed','stopped')",
        name="ck_chat_message_status",
    ),
    CheckConstraint(
        "stop_reason IS NULL OR stop_reason IN "
        "('manual_request','client_disconnected','authorization_revoked')",
        name="ck_chat_message_stop_reason",
    ),
    Index("ix_chat_message_conversation", "conversation_id", "created_at_utc"),
    Index("ix_chat_message_generation", "generation_id", unique=True),
)


chat_generation_table = Table(
    "chat_generation",
    chat_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "conversation_id",
        String(64),
        ForeignKey("chat_conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("owner_user_id", String(64), nullable=False),
    Column("actor_role_snapshot", String(32), nullable=True),
    Column("actor_department_id_snapshot", String(64), nullable=True),
    Column("quota_subject_user_id", String(64), nullable=True),
    Column("cost_center_key", String(128), nullable=True),
    Column("source_space_ids_json", JSON, nullable=True),
    Column("user_message_id", String(64), nullable=False),
    Column(
        "message_id",
        String(64),
        ForeignKey("chat_message.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("root_generation_id", String(64), nullable=False),
    Column("retry_of_generation_id", String(64), nullable=True),
    Column("attempt_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("stop_reason", String(32), nullable=True),
    Column("requested_effort_level", String(16), nullable=False),
    Column("effective_effort_level", String(16), nullable=False),
    Column("upgraded_from", String(16), nullable=True),
    Column("retrieval_profile_id", String(64), nullable=False),
    Column("retrieval_profile_version", String(64), nullable=False),
    Column("rag_budget_policy_version", String(64), nullable=False),
    Column("absolute_deadline_at_utc", DateTime(timezone=True), nullable=False),
    Column("auth_session_id", String(96), nullable=False),
    Column("request_id", String(128), nullable=True),
    Column("control_version", Integer, nullable=False),
    Column("request_content", Text, nullable=False),
    Column("request_scope_json", JSON, nullable=False),
    Column("window_id", String(64), nullable=True),
    Column("window_policy_version", String(64), nullable=True),
    Column("window_sample_rate", String(32), nullable=True),
    Column("window_kind", String(32), nullable=True),
    Column("disconnect_deadline_at_utc", DateTime(timezone=True), nullable=True),
    Column("last_error_code", String(64), nullable=True),
    Column("version", Integer, nullable=False),
    *_timestamps(),
    CheckConstraint(
        "status IN ('running','stop_requested','completed','failed','stopped')",
        name="ck_chat_generation_status",
    ),
    CheckConstraint(
        "stop_reason IS NULL OR stop_reason IN "
        "('manual_request','client_disconnected','authorization_revoked')",
        name="ck_chat_generation_stop_reason",
    ),
    Index("ix_chat_generation_owner_status", "owner_user_id", "status"),
    Index("ix_chat_generation_conversation", "conversation_id"),
    Index("ix_chat_generation_retry_of", "retry_of_generation_id"),
)


chat_generation_event_table = Table(
    "chat_generation_event",
    chat_metadata,
    Column(
        "generation_id",
        String(64),
        ForeignKey("chat_generation.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("event_seq", Integer, primary_key=True),
    Column("event_type", String(32), nullable=False),
    Column("data_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("event_seq >= 1", name="ck_chat_generation_event_seq"),
)


chat_generation_execution_table = Table(
    "chat_generation_execution",
    chat_metadata,
    Column("execution_id", String(64), primary_key=True),
    Column(
        "generation_id",
        String(64),
        ForeignKey("chat_generation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("execution_attempt_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=True),
    Column("heartbeat_at_utc", DateTime(timezone=True), nullable=True),
    Column("fencing_token", Integer, nullable=False),
    Column("checkpoint_version", Integer, nullable=False),
    Column("checkpoint_json", JSON, nullable=True),
    Column("next_attempt_at_utc", DateTime(timezone=True), nullable=True),
    Column("last_error_classification", String(64), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "status IN ('queued','running','retry_wait','expired',"
        "'provider_reconciling','completed','failed','cancelled')",
        name="ck_chat_generation_execution_status",
    ),
    UniqueConstraint(
        "generation_id",
        "execution_attempt_number",
        name="uq_chat_generation_execution_attempt",
    ),
    Index("ix_chat_generation_execution_claim", "status", "next_attempt_at_utc"),
    Index("ix_chat_generation_execution_generation", "generation_id"),
)


chat_subscription_lease_table = Table(
    "chat_subscription_lease",
    chat_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "generation_id",
        String(64),
        ForeignKey("chat_generation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("auth_session_id", String(96), nullable=False),
    Column("lease_token", String(128), nullable=False),
    Column("expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("last_renewed_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("lease_token", name="uq_chat_subscription_lease_token"),
    Index("ix_chat_subscription_lease_generation", "generation_id"),
    Index("ix_chat_subscription_lease_session", "auth_session_id"),
)


chat_message_feedback_table = Table(
    "chat_message_feedback",
    chat_metadata,
    Column(
        "message_id",
        String(64),
        ForeignKey("chat_message.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("voter_user_id", String(64), primary_key=True),
    Column("vote", String(16), nullable=False),
    Column("down_reason", String(32), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("vote IN ('up','down')", name="ck_chat_feedback_vote"),
    CheckConstraint(
        "down_reason IS NULL OR down_reason IN ('no_grounding','wrong_citation')",
        name="ck_chat_feedback_down_reason",
    ),
)


chat_ab_pair_table = Table(
    "chat_ab_pair",
    chat_metadata,
    Column("pair_id", String(64), primary_key=True),
    Column(
        "generation_id",
        String(64),
        ForeignKey("chat_generation.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column(
        "message_id",
        String(64),
        ForeignKey("chat_message.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("window_id", String(64), nullable=True),
    Column("owner_user_id", String(64), nullable=False),
    Column("space_id", String(64), nullable=False, server_default=""),
    Column("status", String(16), nullable=False),
    Column("voted", Boolean, nullable=False),
    Column("choice", String(16), nullable=True),
    Column("voted_at_utc", DateTime(timezone=True), nullable=True),
    Column("expires_at_utc", DateTime(timezone=True), nullable=True),
    Column("close_deadline_at_utc", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    *_timestamps(),
    CheckConstraint(
        "status IN ('pending','open','voted','expired')", name="ck_chat_ab_pair_status"
    ),
    CheckConstraint(
        "choice IS NULL OR choice IN ('0','1','neither')", name="ck_chat_ab_pair_choice"
    ),
    Index("ix_chat_ab_pair_owner", "owner_user_id"),
    Index("ix_chat_ab_pair_space", "space_id"),
)


chat_ab_candidate_table = Table(
    "chat_ab_candidate",
    chat_metadata,
    Column(
        "pair_id",
        String(64),
        ForeignKey("chat_ab_pair.pair_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("candidate", Integer, primary_key=True),
    Column("status", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("citations_json", JSON, nullable=False),
    Column("answer_mode", String(16), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("candidate IN (0,1)", name="ck_chat_ab_candidate_number"),
    CheckConstraint(
        "status IN ('planned','published','discarded')", name="ck_chat_ab_candidate_status"
    ),
)


chat_ab_vote_table = Table(
    "chat_ab_vote",
    chat_metadata,
    Column(
        "pair_id",
        String(64),
        ForeignKey("chat_ab_pair.pair_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("voter_user_id", String(64), primary_key=True),
    Column("choice", String(16), nullable=False),
    Column("operation_kind", String(32), nullable=False, server_default="ab_vote"),
    Column("idempotency_key", String(256), nullable=False, server_default=""),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("choice IN ('0','1','neither')", name="ck_chat_ab_vote_choice"),
    UniqueConstraint(
        "voter_user_id",
        "operation_kind",
        "idempotency_key",
        name="uq_chat_ab_vote_idempotency",
    ),
)


chat_idempotency_table = Table(
    "chat_idempotency",
    chat_metadata,
    Column("user_id", String(64), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("target_id", String(64), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("request_hash", String(128), nullable=False),
    Column("response_target", String(64), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "kind IN ('ask','retry','feedback','ab_vote')", name="ck_chat_idempotency_kind"
    ),
)


chat_revocation_consumption_table = Table(
    "chat_revocation_consumption",
    chat_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("applied_at_utc", DateTime(timezone=True), nullable=False),
)


CHAT_TABLE_NAMES = frozenset(chat_metadata.tables)
