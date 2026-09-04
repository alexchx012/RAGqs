"""Contextual provider usage submission must use ledger-whitelisted sources.

``_availability`` in the usage ledger rejects any non-null meter whose
measurement source is not one of ``provider_reported``/``client_measured``/
``estimated``. The contextual-retrieval submission used to send the
non-whitelisted value ``"provider"``, which would fail every token-reporting
CR call with ``validation_error``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.documents.indexing import IndexStagingRequest
from app.indexing.contextual import ContextualUsageFact
from app.indexing.processing import ContentProcessor


class _RecordingSubmission:
    def __init__(self) -> None:
        self.completions: list[dict[str, object]] = []

    def prepare_provider_call(self, **kwargs: object) -> str:
        return "call_cr_1"

    def mark_dispatching(self, provider_call_id: str, *, started_at_provider: object) -> bool:
        return True

    def complete_provider_call(self, **kwargs: object) -> str:
        self.completions.append(dict(kwargs))
        return str(kwargs["provider_call_id"])


def _staging_request() -> IndexStagingRequest:
    return IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=1,
        publication_id="pub_1",
        document_id="doc_1",
        document_version_id="docver_1",
        space_id="space_1",
        operation="index",
        base_active_version_id=None,
        expected_generation_id="gen_1",
        index_revision_at_start=1,
        object_manifest_ref="manifest_ref_1",
        processing_config_snapshot={},
        authorization_fence={"generation_id": "gen_1"},
        input_manifest_hash="hash_1",
        processing_profile_version="v1",
        usage_ownership={
            "actor_user_id": "user_1",
            "actor_role_snapshot": "user",
            "actor_department_id_snapshot": None,
            "quota_subject_user_id": "user_1",
            "cost_center_key": "user:user_1",
        },
        usage_deadline_at_utc=datetime.now(UTC),
    )


def test_contextual_usage_sources_are_whitelisted_for_non_null_meters() -> None:
    submission = _RecordingSubmission()
    processor = ContentProcessor(usage_submission=submission)
    fact = ContextualUsageFact(
        provider="dashscope",
        model="cr-model",
        operation="chat/contextual",
        chunk_id="chunk_1",
        unit_id="unit_1",
        attempt=1,
        result="completed",
        request_fingerprint="fp_1",
        cache_mode="none",
        warmup=False,
        input_tokens=11,
        output_tokens=7,
    )

    processor._submit_contextual_provider_usage(_staging_request(), fact)

    measurement = submission.completions[-1]["measurement"]
    assert measurement.measurement_sources == {
        "input_tokens": "provider_reported",
        "output_tokens": "provider_reported",
    }


def test_contextual_usage_without_tokens_keeps_empty_sources() -> None:
    submission = _RecordingSubmission()
    processor = ContentProcessor(usage_submission=submission)
    fact = ContextualUsageFact(
        provider="dashscope",
        model="cr-model",
        operation="chat/contextual",
        chunk_id="chunk_1",
        unit_id="unit_1",
        attempt=1,
        result="completed",
        request_fingerprint="fp_1",
        cache_mode="none",
        warmup=False,
    )

    processor._submit_contextual_provider_usage(_staging_request(), fact)

    measurement = submission.completions[-1]["measurement"]
    assert measurement.measurement_sources == {}
