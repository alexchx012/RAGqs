from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.documents.schema import ingestion_attempts_table, ingestion_jobs_table, publications_table
from app.platform.errors import PlatformError

from .test_commands import _upload


def test_claim_creates_attempt_and_fencing_token(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    job_id = created["items"][0]["job_id"]
    lease = service.claim_job(worker_id="worker_1", job_id=job_id)
    assert lease.job_id == job_id
    assert lease.attempt_number == 1
    assert lease.fencing_token == 1


def test_claim_reclaims_expired_running_attempt(service, principal) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    service._now = lambda: clock[0]
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    job_id = created["items"][0]["job_id"]
    first = service.claim_job(worker_id="worker_1", job_id=job_id)
    clock[0] += timedelta(minutes=6)

    with pytest.raises(PlatformError) as error:
        service.claim_job(worker_id="worker_2", job_id=job_id)
    assert error.value.code == "job_unavailable"

    with service._engine.connect() as connection:
        attempt = (
            connection.execute(
                select(ingestion_attempts_table).where(
                    ingestion_attempts_table.c.id == first.attempt_id
                )
            )
            .mappings()
            .one()
        )
        job = (
            connection.execute(
                select(ingestion_jobs_table).where(ingestion_jobs_table.c.id == job_id)
            )
            .mappings()
            .one()
        )
        publication = (
            connection.execute(
                select(publications_table).where(publications_table.c.id == first.publication_id)
            )
            .mappings()
            .one()
        )
    assert attempt["state"] == "expired"
    assert job["state"] == "retry_wait"
    assert publication["status"] == "discarded"

    clock[0] = job["next_attempt_at_utc"] + timedelta(seconds=1)
    second = service.claim_job(worker_id="worker_2", job_id=job_id)
    assert second.attempt_number == 2
    assert second.fencing_token == 2
    assert second.publication_id != first.publication_id
