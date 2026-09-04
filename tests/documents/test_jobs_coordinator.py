from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

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
            .one_or_none()
        )
    assert attempt["state"] == "expired"
    assert job["state"] == "retry_wait"
    # 过期回收删除 staged publication（幂等回收，不再产出同键第二行 discarded）。
    assert publication is None

    clock[0] = job["next_attempt_at_utc"] + timedelta(seconds=1)
    second = service.claim_job(worker_id="worker_2", job_id=job_id)
    assert second.attempt_number == 2
    assert second.fencing_token == 2
    assert second.publication_id != first.publication_id


def test_repeated_failures_with_stable_generation_stay_idempotent(service, principal) -> None:
    """同一 index generation 下多次 attempt 失败：回收幂等，不撞 publications 唯一约束。"""

    service._identity_access = None
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}

    def advancing_now():
        clock["now"] += timedelta(minutes=45)
        return clock["now"]

    service._now = advancing_now
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-poison-fail-1",
    )
    job_id = created["items"][0]["job_id"]

    for expected_state in ("retry_wait", "retry_wait", "retry_wait", "dead_letter"):
        lease = service.claim_job(worker_id="worker-poison", job_id=job_id)
        failed = service.fail_job(
            job_id=job_id,
            reason="transient outage",
            retryable=True,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
        )
        assert failed["state"] == expected_state
        with service._engine.connect() as connection:
            remaining = connection.execute(
                select(func.count())
                .select_from(publications_table)
                .where(publications_table.c.job_id == job_id)
            ).scalar_one()
        # 失败即回收：staged 行删除，不产出同键第二行 discarded。
        assert remaining == 0


def test_reclaim_cycles_with_stable_generation_stay_idempotent(service, principal) -> None:
    """同一 generation 下连续租约过期回收不撞唯一约束，job 仍按预算重试。"""

    service._identity_access = None
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    service._now = lambda: clock["now"]  # noqa: E731
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-poison-reclaim-1",
    )
    job_id = created["items"][0]["job_id"]

    for _round in range(2):
        service.claim_job(worker_id="worker-poison", job_id=job_id, lease_ttl=timedelta(seconds=1))
        clock["now"] += timedelta(minutes=2)
        # 过期回收发生在下一次 claim 的预回收事务里；回收后未到重试时刻。
        with pytest.raises(PlatformError) as error:
            service.claim_job(worker_id="worker-poison", job_id=job_id)
        assert error.value.code == "job_unavailable"
        with service._engine.connect() as connection:
            next_attempt = connection.execute(
                select(ingestion_jobs_table.c.next_attempt_at_utc).where(
                    ingestion_jobs_table.c.id == job_id
                )
            ).scalar_one()
        clock["now"] = next_attempt + timedelta(seconds=1)

    with service._engine.connect() as connection:
        state = connection.execute(
            select(ingestion_jobs_table.c.state).where(ingestion_jobs_table.c.id == job_id)
        ).scalar_one()
        staged = connection.execute(
            select(func.count())
            .select_from(publications_table)
            .where(
                publications_table.c.job_id == job_id,
                publications_table.c.status == "staged",
            )
        ).scalar_one()
        attempts = (
            connection.execute(
                select(ingestion_attempts_table.c.state).where(
                    ingestion_attempts_table.c.job_id == job_id
                )
            )
            .scalars()
            .all()
        )
    assert state == "retry_wait"
    assert staged == 0
    assert attempts == ["expired", "expired"]
