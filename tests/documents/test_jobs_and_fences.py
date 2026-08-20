from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.documents.schema import (
    document_versions_table,
    ingestion_attempts_table,
    ingestion_jobs_table,
    publications_table,
)
from app.indexing import ContentProcessor
from app.platform.errors import PlatformError

from .test_commands import _accept, _upload


class _IndexingHandoff:
    def __init__(self, publish_result=None) -> None:
        self.published = []
        self.discarded = []
        self.publish_result = publish_result or {"state": "published"}

    def publish(self, request, *, connection, receipt=None):
        del connection, receipt
        self.published.append(request)
        return self.publish_result

    def discard(self, request, *, connection) -> None:
        del connection
        self.discarded.append(request)

    def cleanup_resource(self, resource, *, connection) -> None:
        del resource, connection


class _Calendar:
    def lock_or_verify(self, connection):
        del connection
        return object()


class _Quota:
    def __init__(self) -> None:
        self.calendar = _Calendar()
        self.checked = []
        self.recorded = []

    def check(self, connection, **values) -> None:
        del connection
        self.checked.append(values)

    def record(self, connection, **values) -> str:
        del connection
        self.recorded.append(values)
        return "debit_1"


class _RejectingQuota:
    calendar = _Calendar()

    def check(self, connection, **values) -> None:
        del connection, values
        raise PlatformError("quota_exceeded", "Quota is exhausted", {}, 409)


class _IngestionNotifications:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish_ingestion_events(
        self,
        *,
        job_id: str,
        document_id: str,
        document_version_id: str,
        publication_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: object,
        ocr_low_confidence: bool,
        ocr_low_confidence_fact: object,
        connection: object,
    ) -> tuple[str, ...]:
        del occurred_at, ocr_low_confidence, ocr_low_confidence_fact, connection
        self.calls.append(
            {
                "job_id": job_id,
                "document_id": document_id,
                "document_version_id": document_version_id,
                "publication_id": publication_id,
                "transition_version": transition_version,
                "recipient_user_id": recipient_user_id,
            }
        )
        return ("evt_ingestion_completed",)


def _receipt_contract_fields() -> dict[str, object]:
    return {
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "processing_summary": {
            "chunk_count": 1,
            "page_count": 1,
            "image_count": 0,
            "table_count": 0,
            "ocr": {},
            "tree": {},
            "cr": {},
        },
        "locator_snippet_integrity": {"locators_valid": True, "snippets_valid": True},
        "index_component_results": {"dense": {"state": "succeeded"}},
        "content_manifest_id": "manifest-test",
        "content_manifest_hash": "manifest-hash-test",
        "failure": None,
        "degradations": [],
    }


def _receipt_request_echoes(service, attempt_id: str) -> dict[str, object]:
    with service._engine.connect() as connection:
        request = connection.execute(
            ingestion_attempts_table.select()
            .with_only_columns(ingestion_attempts_table.c.staging_request_json)
            .where(ingestion_attempts_table.c.id == attempt_id)
        ).scalar_one()
    return {
        "input_manifest_hash": request["input_manifest_hash"],
        "processing_profile_version": request["processing_profile_version"],
        "stage_resources": [
            {
                "backend_kind": "index_chunk",
                "resource_id": f"{attempt_id}:{request['publication_id']}:chunk_1",
                "attempt_id": attempt_id,
                "publication_id": request["publication_id"],
                "fencing_token": request["fencing_token"],
                "document_id": request["document_id"],
                "document_version_id": request["document_version_id"],
                "generation_id": request["expected_generation_id"],
            }
        ],
        "stage_resource_ids": [f"{attempt_id}:{request['publication_id']}:chunk_1"],
        "space_id": request["space_id"],
        "operation": request["operation"],
        "base_active_version_id": request["base_active_version_id"],
        "index_revision_at_start": request["index_revision_at_start"],
        "object_manifest_ref": request["object_manifest_ref"],
        "processing_config_snapshot": request["processing_config_snapshot"],
    }


def _accepted(service, principal):
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)
    return item


def test_reindex_keeps_active_version_and_stages_new_publication(service, principal) -> None:
    item = _accepted(service, principal)
    response = service.reindex(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="reindex-1",
    )
    assert response["document_version_id"] == item["document_version_id"]
    assert response["job_id"]
    assert response["publication_id"] != item["publication_id"]
    assert (
        service.list_documents(principal=principal, space_id="space_1")["items"][0][
            "document_version_id"
        ]
        == item["document_version_id"]
    )


def test_cancel_discards_staged_publication(service, principal) -> None:
    item = _accepted(service, principal)
    job = service.reindex(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="reindex-1",
    )
    cancelled = service.cancel_job(principal=principal, job_id=job["job_id"])
    assert cancelled["state"] == "cancelled"
    with service._engine.connect() as connection:
        publication_state = (
            connection.execute(
                publications_table.select().where(publications_table.c.id == job["publication_id"])
            )
            .mappings()
            .one()["status"]
        )
        job_state = (
            connection.execute(
                ingestion_jobs_table.select().where(ingestion_jobs_table.c.id == job["job_id"])
            )
            .mappings()
            .one()["state"]
        )
    assert publication_state == "discarded"
    assert job_state == "cancelled"


def test_replay_is_ops_only_and_uses_new_publication(service, principal) -> None:
    item = _accepted(service, principal)
    job = service.reindex(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="reindex-1",
    )
    lease = service.claim_job(worker_id="worker_1", job_id=job["job_id"])
    service.fail_job(
        job_id=job["job_id"],
        reason="temporary",
        retryable=False,
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )
    with pytest.raises(PlatformError) as error:
        service.replay_job(principal=principal, job_id=job["job_id"], idempotency_key="replay-1")
    assert error.value.code == "forbidden"

    ops = principal.__class__(
        user_id=principal.user_id,
        auth_session_id=principal.auth_session_id,
        username=principal.username,
        role="ops",
        department_id=None,
    )
    replay = service.replay_job(principal=ops, job_id=job["job_id"], idempotency_key="replay-1")
    assert replay["state"] == "pending"
    assert replay["replay_generation"] == 1
    assert replay["publication_id"] != job["publication_id"]


def test_replay_eligibility_trusts_stored_state_and_ignores_object_body(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-replay-content-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-replay-content", job_id=created["job_id"])
    service.fail_job(
        job_id=created["job_id"],
        reason="deterministic failure",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )
    with service._engine.connect() as connection:
        object_key = connection.execute(
            document_versions_table.select()
            .with_only_columns(document_versions_table.c.original_object_key)
            .where(document_versions_table.c.id == created["document_version_id"])
        ).scalar_one()
    service._object_store.delete(str(object_key))
    service._identity_access = None
    ops = principal.__class__(
        user_id="ops_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )

    # Listing decides from durable DB state only; the missing object body does not
    # hide the replay action, and replay eligibility no longer re-reads the object.
    get_calls = []

    class _CountingStore:
        def __init__(self, store) -> None:
            self._store = store

        def __getattr__(self, name):
            return getattr(self._store, name)

        def get(self, key):
            get_calls.append(key)
            return self._store.get(key)

    service._object_store = _CountingStore(service._object_store)
    listed = service.list_jobs(principal=ops, space_id="space_1")
    assert listed["items"][0]["allowed_actions"] == ["replay"]
    replayed = service.replay_job(
        principal=ops,
        job_id=created["job_id"],
        idempotency_key="replay-content-missing",
    )
    assert replayed["state"] == "pending"
    assert get_calls == []


def test_direct_replay_uses_ops_as_execution_quota_and_notification_subject(
    service, principal
) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-replay-actor-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-replay-actor", job_id=created["job_id"])
    service.fail_job(
        job_id=created["job_id"],
        reason="deterministic failure",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )
    quota = _Quota()
    notifications = _IngestionNotifications()
    service._quota_service = quota
    service._ingestion_notification_port = notifications
    service._identity_access = None
    ops = principal.__class__(
        user_id="ops_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )

    replay = service.replay_job(
        principal=ops,
        job_id=created["job_id"],
        idempotency_key="replay-direct-actor",
    )
    _accept(
        service,
        ops,
        {
            **created,
            "job_id": replay["job_id"],
            "publication_id": replay["publication_id"],
        },
    )

    assert quota.recorded[-1]["quota_subject_user_id"] == "ops_1"
    assert notifications.calls[-1]["recipient_user_id"] == "ops_1"


def test_list_jobs_projects_replay_inputs_and_hides_nonterminal_failure_reason(
    service, principal
) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-list-projection-1",
    )["items"][0]

    pending = service.list_jobs(principal=principal, space_id="space_1")["items"][0]
    assert pending["base_active_version_id"] is None
    assert pending["upload_batch_id"] is not None
    assert pending["failure_reason"] is None
    lease = service.claim_job(worker_id="worker-list-projection", job_id=created["job_id"])
    service.fail_job(
        job_id=created["job_id"],
        reason="transient failure",
        retryable=True,
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )

    retry_wait = service.list_jobs(principal=principal, space_id="space_1")["items"][0]
    assert retry_wait["state"] == "retry_wait"
    assert retry_wait["stage"] is None
    assert retry_wait["failure_reason"] is None


def test_receipt_requires_current_lease_and_generation(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=item["job_id"],
            receipt={
                "job_id": item["job_id"],
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "publication_id": item["publication_id"],
            },
        )
    assert error.value.code == "fence_conflict"

    lease = service.claim_job(worker_id="worker_1", job_id=item["job_id"])
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=item["job_id"],
            receipt={
                "job_id": item["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": "stale-generation",
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "stage_resources": [],
                "processing_config_version": "v1",
                "authorization_fence": dict(lease.authorization_fence),
                **_receipt_request_echoes(service, lease.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "generation_conflict"


def test_receipt_requires_complete_contract_before_publication(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    lease = service.claim_job(worker_id="worker_1", job_id=item["job_id"])

    service._indexing_handoff_port = None
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=item["job_id"],
            receipt={
                "job_id": item["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": lease.expected_generation_id,
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
            },
        )
    assert error.value.code == "validation_error"

    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=item["job_id"],
            receipt={
                "job_id": item["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": lease.expected_generation_id,
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "stage_resources": [],
                "processing_config_version": "v1",
                "authorization_fence": dict(lease.authorization_fence),
                **_receipt_request_echoes(service, lease.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "indexing_handoff_unavailable"


def test_receipt_discards_when_direct_acl_is_revoked(service, principal) -> None:
    handoff = _IndexingHandoff()
    service._indexing_handoff_port = handoff
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-acl-fence-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-acl", job_id=created["job_id"])

    class _RevokedIdentity:
        def authorize_space(self, *, principal, space_id, action):
            del principal, space_id, action
            raise PlatformError("space_action_forbidden", "Access was revoked", {}, 403)

    service._identity_access = _RevokedIdentity()
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=created["job_id"],
            receipt={
                "job_id": created["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": lease.expected_generation_id,
                "document_id": created["document_id"],
                "document_version_id": created["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "stage_resources": [],
                "processing_config_version": "v1",
                "authorization_fence": dict(lease.authorization_fence),
                **_receipt_request_echoes(service, lease.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "authorization_changed"
    assert [request.attempt_id for request in handoff.discarded] == [lease.attempt_id]


def test_publication_requires_a_successful_index_handoff(service, principal) -> None:
    handoff = _IndexingHandoff(publish_result={"state": "discarded"})
    service._indexing_handoff_port = handoff
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-publish-fence-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-publish", job_id=created["job_id"])
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=created["job_id"],
            receipt={
                "job_id": created["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": lease.expected_generation_id,
                "document_id": created["document_id"],
                "document_version_id": created["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "stage_resources": [],
                "processing_config_version": "v1",
                "authorization_fence": dict(lease.authorization_fence),
                **_receipt_request_echoes(service, lease.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "indexing_publish_failed"
    assert [request.attempt_id for request in handoff.discarded] == [lease.attempt_id]


def test_malformed_receipt_discards_the_current_attempt(service, principal) -> None:
    handoff = _IndexingHandoff()
    service._indexing_handoff_port = handoff
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-malformed-receipt-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-malformed", job_id=created["job_id"])

    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=created["job_id"],
            receipt={
                "job_id": created["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
            },
        )
    assert error.value.code == "validation_error"
    assert [request.attempt_id for request in handoff.discarded] == [lease.attempt_id]


def test_late_receipt_does_not_discard_expired_current_attempt_staging(service, principal) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    service._now = lambda: clock[0]
    handoff = _IndexingHandoff()
    service._indexing_handoff_port = handoff
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-late-receipt-1",
    )["items"][0]
    first = service.claim_job(worker_id="worker-first", job_id=item["job_id"])

    clock[0] += timedelta(minutes=6)
    with pytest.raises(PlatformError) as error:
        service.claim_job(worker_id="worker-reclaim", job_id=item["job_id"])
    assert error.value.code == "job_unavailable"
    with service._engine.connect() as connection:
        retry_at = connection.execute(
            select(ingestion_jobs_table.c.next_attempt_at_utc).where(
                ingestion_jobs_table.c.id == item["job_id"]
            )
        ).scalar_one()
    clock[0] = retry_at + timedelta(seconds=1)
    second = service.claim_job(worker_id="worker-second", job_id=item["job_id"])
    clock[0] += timedelta(minutes=6)

    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=item["job_id"],
            receipt={
                "job_id": item["job_id"],
                "attempt_id": first.attempt_id,
                "fencing_token": first.fencing_token,
                "publication_id": first.publication_id,
                "generation_id": first.expected_generation_id,
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "processing_config_version": "v1",
                "authorization_fence": dict(first.authorization_fence),
                **_receipt_request_echoes(service, first.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "fence_conflict"
    assert [request.attempt_id for request in handoff.discarded] == [first.attempt_id]
    assert second.attempt_id not in [request.attempt_id for request in handoff.discarded]


def test_malformed_late_receipt_does_not_discard_current_attempt_staging(
    service, principal
) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    service._now = lambda: clock[0]
    handoff = _IndexingHandoff()
    service._indexing_handoff_port = handoff
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-malformed-late-receipt-1",
    )["items"][0]
    first = service.claim_job(worker_id="worker-first", job_id=item["job_id"])

    clock[0] += timedelta(minutes=6)
    with pytest.raises(PlatformError) as error:
        service.claim_job(worker_id="worker-reclaim", job_id=item["job_id"])
    assert error.value.code == "job_unavailable"
    with service._engine.connect() as connection:
        retry_at = connection.execute(
            select(ingestion_jobs_table.c.next_attempt_at_utc).where(
                ingestion_jobs_table.c.id == item["job_id"]
            )
        ).scalar_one()
    clock[0] = retry_at + timedelta(seconds=1)
    second = service.claim_job(worker_id="worker-second", job_id=item["job_id"])

    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=item["job_id"],
            receipt={"attempt_id": first.attempt_id},
        )

    assert error.value.code == "fence_conflict"
    assert [request.attempt_id for request in handoff.discarded] == [first.attempt_id]
    assert second.attempt_id not in [request.attempt_id for request in handoff.discarded]


def test_receipt_revalidates_direct_acl_after_handoff_publish(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-final-acl-fence-1",
    )["items"][0]

    class _RevokingHandoff(_IndexingHandoff):
        def publish(self, request, *, connection, receipt=None):
            del receipt
            result = super().publish(request, connection=connection)

            class _RevokedIdentity:
                def authorize_space(self, *, principal, space_id, action):
                    del principal, space_id, action
                    raise PlatformError("space_action_forbidden", "Access was revoked", {}, 403)

            service._identity_access = _RevokedIdentity()
            return result

    handoff = _RevokingHandoff()
    service._indexing_handoff_port = handoff
    lease = service.claim_job(worker_id="worker-final-acl", job_id=created["job_id"])
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=created["job_id"],
            receipt={
                "job_id": created["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": lease.expected_generation_id,
                "document_id": created["document_id"],
                "document_version_id": created["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "stage_resources": [],
                "processing_config_version": "v1",
                "authorization_fence": dict(lease.authorization_fence),
                **_receipt_request_echoes(service, lease.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "authorization_changed"
    assert [request.attempt_id for request in handoff.discarded] == [lease.attempt_id]


def test_terminal_job_cannot_hide_active_document(service, principal) -> None:
    item = _accepted(service, principal)
    with pytest.raises(PlatformError) as error:
        service.fail_job(
            job_id=item["job_id"],
            reason="late failure",
            attempt_id="attempt_late",
            fencing_token=1,
        )
    assert error.value.code == "job_not_failable"
    assert service.list_documents(principal=principal, space_id="space_1")["total"] == 1


def test_handoff_and_quota_are_part_of_publication_transaction(service, principal) -> None:
    handoff = _IndexingHandoff()
    quota = _Quota()
    service._indexing_handoff_port = handoff
    service._quota_service = quota
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)
    assert len(handoff.published) == 1
    assert quota.checked == [{"quota_subject_user_id": "user_1", "pages": 1, "role": "user"}]
    assert quota.recorded[0]["quota_operation_id"] == item["job_id"]
    assert quota.recorded[0]["publication_id"] == item["publication_id"]

    reindex = service.reindex(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="reindex-1",
    )
    service.claim_job(worker_id="worker_1", job_id=reindex["job_id"])
    service.cancel_job(principal=principal, job_id=reindex["job_id"])
    assert quota.checked[-1] == {"quota_subject_user_id": "user_1", "pages": 1, "role": "user"}
    assert len(handoff.discarded) == 1


def test_text_processor_receipt_records_one_page_of_quota(service, principal) -> None:
    handoff = _IndexingHandoff()
    quota = _Quota()
    service._indexing_handoff_port = handoff
    service._quota_service = quota
    content = b"A plain text document without page metadata."
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(content=content)],
        idempotency_key="upload-text-processor-quota-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-text-processor", job_id=item["job_id"])
    with service._engine.connect() as connection:
        attempt = (
            connection.execute(
                select(ingestion_attempts_table).where(
                    ingestion_attempts_table.c.id == lease.attempt_id
                )
            )
            .mappings()
            .one()
        )
    request = service._index_request_from_attempt(attempt)
    assert request is not None
    output = ContentProcessor().process(
        request,
        content,
        media_kind="text/plain",
        content_manifest_id="manifest-text-processor-quota",
        content_manifest_hash=hashlib.sha256(content).hexdigest(),
    )

    service.accept_processing_receipt(
        principal=principal,
        job_id=item["job_id"],
        receipt=output.receipt,
    )

    assert output.receipt.processing_summary["page_count"] == 1
    assert quota.recorded[-1]["pages"] == 1


def test_successful_publication_records_creator_notification_event_ids(service, principal) -> None:
    notifications = _IngestionNotifications()
    service._ingestion_notification_port = notifications
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-notifications-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)

    assert notifications.calls == [
        {
            "job_id": item["job_id"],
            "document_id": item["document_id"],
            "document_version_id": item["document_version_id"],
            "publication_id": item["publication_id"],
            "transition_version": 1,
            "recipient_user_id": principal.user_id,
        }
    ]
    jobs = service.list_jobs(principal=principal)
    assert jobs["items"][0]["notification_event_ids"] == ["evt_ingestion_completed"]


def test_same_content_replace_deduplicates_before_quota_admission(service, principal) -> None:
    item = _accepted(service, principal)
    service._quota_service = _RejectingQuota()

    response = service.replace_version(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        file=_upload(),
        idempotency_key="same-content-replace",
    )

    assert response == {
        "document_id": item["document_id"],
        "document_version_id": item["document_version_id"],
        "job_id": None,
        "version": 1,
        "deduplicated": True,
        "status": "active",
    }


def test_replay_rejects_a_stale_replacement_after_a_newer_version_is_active(
    service, principal
) -> None:
    first = _accepted(service, principal)
    failed = service.replace_version(
        principal=principal,
        document_id=first["document_id"],
        expected_version=1,
        file=_upload(content=b"failed replacement"),
        idempotency_key="replace-failed",
    )
    lease = service.claim_job(worker_id="worker-failed", job_id=failed["job_id"])
    service.fail_job(
        job_id=failed["job_id"],
        reason="deterministic failure",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )
    newer = service.replace_version(
        principal=principal,
        document_id=first["document_id"],
        expected_version=2,
        file=_upload(content=b"new active version"),
        idempotency_key="replace-newer",
    )
    _accept(service, principal, newer)
    ops = principal.__class__(
        user_id="user_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )

    with pytest.raises(PlatformError) as error:
        service.replay_job(principal=ops, job_id=failed["job_id"], idempotency_key="replay-stale")
    assert error.value.code == "document_version_changed"
