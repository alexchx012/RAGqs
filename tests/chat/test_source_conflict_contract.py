from __future__ import annotations

from app.chat.ports import ChatProviderRequest


def test_chat_provider_request_carries_non_adjudicating_source_conflict_contract() -> None:
    request = ChatProviderRequest(
        generation_id="generation_1",
        owner_user_id="user_1",
        content="compare the policies",
        effort_level="quick",
        candidate=None,
        context_items=(
            {
                "library": "vector",
                "space_id": "space_1",
                "publication_id": "publication_1",
                "document_version_id": "version_1",
                "chunk_id": "chunk_1",
                "locator": {"page": 1},
            },
        ),
        source_conflict_contract={
            "required_fields": ("library", "space_id", "publication/version", "locator"),
            "conflict_policy": "present_each_claim_and_citation_no_system_adjudication",
        },
    )
    assert request.source_conflict_contract == {
        "required_fields": ("library", "space_id", "publication/version", "locator"),
        "conflict_policy": "present_each_claim_and_citation_no_system_adjudication",
    }
