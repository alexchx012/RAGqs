from __future__ import annotations

from app.documents.domain import canonical_request_fingerprint


def test_request_fingerprint_is_order_stable_and_changes_for_semantic_input() -> None:
    first = canonical_request_fingerprint({"filename": "a.txt", "files": ["one", "two"]})
    equivalent = canonical_request_fingerprint({"files": ["one", "two"], "filename": "a.txt"})
    different = canonical_request_fingerprint({"filename": "a.txt", "files": ["two", "one"]})

    assert first == equivalent
    assert first != different
    assert len(first) == 64
