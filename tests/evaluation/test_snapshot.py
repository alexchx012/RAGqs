"""Read-only chat-fact snapshots for shadow evaluation."""

from __future__ import annotations

from datetime import timedelta

from app.chat.schema import (
    chat_ab_pair_table,
    chat_ab_vote_table,
    chat_conversation_table,
    chat_generation_table,
    chat_message_feedback_table,
    chat_message_table,
)
from app.evaluation.snapshot import SqlAlchemyChatFactsSnapshot

from .conftest import NOW, build_test_env


def test_snapshot_uses_completed_assistant_facts_for_weak_signals() -> None:
    env = build_test_env()
    with env["engine"].begin() as connection:
        connection.execute(
            chat_conversation_table.insert().values(
                id="conversation_1",
                owner_user_id="owner_1",
                title="Scoped conversation",
                pinned=False,
                effort_level="quick",
                scope_json={"space_ids": ["space_1"]},
                last_active_at_utc=NOW,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id="user_message_1",
                conversation_id="conversation_1",
                owner_user_id="owner_1",
                role="user",
                content="Which source supports the answer?",
                created_at_utc=NOW,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id="assistant_message_1",
                conversation_id="conversation_1",
                owner_user_id="owner_1",
                role="assistant",
                content="The completed answer.",
                generation_id="generation_1",
                status="completed",
                citations_json=[{"document_id": "doc_1"}],
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            chat_generation_table.insert().values(
                id="generation_1",
                conversation_id="conversation_1",
                owner_user_id="owner_1",
                user_message_id="user_message_1",
                message_id="assistant_message_1",
                root_generation_id="generation_1",
                attempt_number=1,
                status="completed",
                requested_effort_level="quick",
                effective_effort_level="quick",
                retrieval_profile_id="profile_1",
                retrieval_profile_version="v1",
                rag_budget_policy_version="budget_1",
                absolute_deadline_at_utc=NOW + timedelta(hours=1),
                auth_session_id="session_1",
                control_version=1,
                request_content="Which source supports the answer?",
                request_scope_json={},
                version=1,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            chat_message_feedback_table.insert().values(
                message_id="assistant_message_1",
                voter_user_id="voter_up",
                vote="up",
                down_reason=None,
                created_at_utc=NOW,
            )
        )
        connection.execute(
            chat_message_feedback_table.insert().values(
                message_id="assistant_message_1",
                voter_user_id="voter_down",
                vote="down",
                down_reason="wrong_citation",
                created_at_utc=NOW,
            )
        )
        connection.execute(
            chat_ab_pair_table.insert().values(
                pair_id="pair_1",
                generation_id="generation_1",
                message_id="assistant_message_1",
                window_id=None,
                owner_user_id="owner_1",
                status="voted",
                voted=True,
                choice="0",
                voted_at_utc=NOW,
                version=1,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        for voter_user_id in ("voter_1", "voter_2"):
            connection.execute(
                chat_ab_vote_table.insert().values(
                    pair_id="pair_1",
                    voter_user_id=voter_user_id,
                    choice="0",
                    created_at_utc=NOW,
                )
            )

    with env["engine"].connect() as connection:
        samples = SqlAlchemyChatFactsSnapshot(env["engine"]).collect_samples(
            connection,
            space_id="space_1",
            limit=10,
        )

    assert len(samples) == 1
    assert samples[0]["source_ref"] == "user_message_1"
    assert samples[0]["weak_signals"] == {
        "weak_has_citation": True,
        "weak_feedback_up": 1,
        "weak_feedback_down": 1,
        "weak_ab_vote_count": 2,
    }
