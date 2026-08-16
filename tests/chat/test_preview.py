from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, insert

from app.chat.preview import SqlAlchemyMessageCitationPreviewAdapter
from app.chat.schema import chat_conversation_table, chat_message_table, chat_metadata
from app.identity.service import AuthPrincipal


def test_owned_message_projects_only_selected_document_version_hits_in_order() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    chat_metadata.create_all(engine)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    with engine.begin() as connection:
        connection.execute(
            insert(chat_conversation_table).values(
                id="conversation_1",
                owner_user_id="user_1",
                title="Preview citations",
                pinned=False,
                group_id=None,
                effort_level="quick",
                scope_json={},
                last_active_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            insert(chat_message_table).values(
                id="message_1",
                conversation_id="conversation_1",
                owner_user_id="user_1",
                role="assistant",
                content="answer",
                answer_mode="grounded",
                effort_level="quick",
                generation_id="generation_1",
                root_generation_id="generation_1",
                retry_of_generation_id=None,
                attempt_number=1,
                status="completed",
                stop_reason=None,
                notices_json=[],
                citations_json=[
                    {
                        "document_id": "document_1",
                        "document_version_id": "version_1",
                        "locator": {"page": "2", "span": "4:9"},
                        "snippet": "First matching excerpt",
                    },
                    {
                        "document_id": "document_other",
                        "document_version_id": "version_1",
                        "locator": {"page": 1},
                        "snippet": "Wrong document",
                    },
                    {
                        "document_id": "document_1",
                        "document_version_id": "version_old",
                        "locator": {"section_path": [1, "Policy"], "paragraph": 2},
                        "snippet": "Wrong version",
                    },
                    {
                        "document_id": "document_1",
                        "document_version_id": "version_1",
                        "locator": {"section_path": "Chapter 2 / Attendance", "paragraph": 2},
                        "snippet": "Word section",
                    },
                    {
                        "document_id": "document_1",
                        "document_version_id": "version_1",
                        "locator": {"sheet": "Q1", "a1_range": "A2:C2"},
                        "snippet": "",
                    },
                ],
                created_at_utc=now,
                updated_at_utc=now,
            )
        )

    hits = SqlAlchemyMessageCitationPreviewAdapter(engine).get_hits(
        principal,
        "message_1",
        "document_1",
        "version_1",
    )

    assert [hit.index for hit in hits] == [1, 2, 3]
    assert hits[0].locator == {"page": 2, "span": {"start": 4, "end": 9}}
    assert hits[1].locator == {
        "section_path": ["Chapter 2", "Attendance"],
        "paragraph": 2,
    }
    assert hits[2].locator == {"sheet": "Q1", "a1_range": "A2:C2"}
    assert hits[0].snippet == "First matching excerpt"
    assert hits[1].snippet is None
    assert hits[2].snippet is None
    assert hits[1].summary == "Chapter 2 / Attendance"
    assert hits[2].summary == "Sheet Q1, range A2:C2"
