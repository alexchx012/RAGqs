from __future__ import annotations

import pytest

from app.documents.domain import (
    DocumentLifecycle,
    IngestionJobState,
    PublicationState,
    SubmissionState,
    canonical_request_fingerprint,
    require_document_lifecycle_transition,
    require_job_transition,
    require_publication_transition,
    require_submission_transition,
)


def test_document_lifecycle_only_allows_forward_delete_and_tombstone() -> None:
    assert require_document_lifecycle_transition(DocumentLifecycle.ACTIVE, "pending_delete")
    assert require_document_lifecycle_transition("pending_delete", DocumentLifecycle.DELETED)

    with pytest.raises(ValueError, match="document lifecycle transition"):
        require_document_lifecycle_transition(DocumentLifecycle.PENDING_DELETE, DocumentLifecycle.ACTIVE)


def test_publication_and_job_transitions_keep_staged_output_invisible() -> None:
    assert require_publication_transition(PublicationState.STAGED, PublicationState.ACTIVE)
    assert require_publication_transition(PublicationState.STAGED, PublicationState.DISCARDED)
    assert require_job_transition(IngestionJobState.PENDING, IngestionJobState.RUNNING)
    assert require_job_transition(IngestionJobState.RUNNING, IngestionJobState.RETRY_WAIT)
    assert require_job_transition(IngestionJobState.RETRY_WAIT, IngestionJobState.DEAD_LETTER)

    with pytest.raises(ValueError, match="publication transition"):
        require_publication_transition(PublicationState.DISCARDED, PublicationState.ACTIVE)


def test_submission_terminal_states_are_irreversible() -> None:
    assert require_submission_transition(SubmissionState.PENDING, SubmissionState.APPROVED)
    assert require_submission_transition(SubmissionState.PENDING, SubmissionState.INVALIDATED)

    with pytest.raises(ValueError, match="submission transition"):
        require_submission_transition(SubmissionState.REJECTED, SubmissionState.PENDING)


def test_request_fingerprint_is_order_stable_and_changes_for_semantic_input() -> None:
    first = canonical_request_fingerprint({"filename": "a.txt", "files": ["one", "two"]})
    equivalent = canonical_request_fingerprint({"files": ["one", "two"], "filename": "a.txt"})
    different = canonical_request_fingerprint({"filename": "a.txt", "files": ["two", "one"]})

    assert first == equivalent
    assert first != different
    assert len(first) == 64
