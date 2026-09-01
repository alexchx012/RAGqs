from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update

from app.platform.errors import PlatformError

from .domain import DocumentVersionState, IngestionJobState, PublicationState
from .schema import (
    document_version_restore_holds_table,
    document_versions_table,
    documents_table,
    ingestion_attempts_table,
    ingestion_jobs_table,
    publications_table,
    upload_batch_items_table,
)


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(15)}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    attempt_id: str
    attempt_number: int
    fencing_token: int
    lease_owner: str
    lease_expires_at: datetime
    publication_id: str
    expected_generation_id: str
    authorization_fence: Mapping[str, Any]


class DocumentsJobCoordinator:
    def __init__(self, service: Any, *, lease_ttl: timedelta = timedelta(minutes=5)) -> None:
        self._service = service
        self._lease_ttl = lease_ttl

    def claim(self, *, worker_id: str, job_id: str | None = None) -> JobLease:
        if not worker_id.strip():
            raise PlatformError("validation_error", "worker_id is required", {}, 422)
        with self._service._engine.begin() as connection:
            now = self._service._current_time()
            self._reclaim_expired_attempts(connection, now)
        with self._service._engine.begin() as connection:
            now = self._service._current_time()
            conditions = [
                ingestion_jobs_table.c.state.in_(
                    [
                        IngestionJobState.PENDING.value,
                        IngestionJobState.RETRY_WAIT.value,
                    ]
                ),
                (ingestion_jobs_table.c.next_attempt_at_utc.is_(None))
                | (ingestion_jobs_table.c.next_attempt_at_utc <= now),
            ]
            if job_id is not None:
                conditions.append(ingestion_jobs_table.c.id == job_id)
            candidate = (
                connection.execute(
                    select(ingestion_jobs_table.c.id, ingestion_jobs_table.c.document_id)
                    .where(and_(*conditions))
                    .order_by(ingestion_jobs_table.c.created_at_utc)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None:
                raise PlatformError(
                    "job_unavailable", "No runnable ingestion job was found", {}, 409
                )
            document = self._service._locked_document(connection, str(candidate["document_id"]))
            if document is None:
                raise PlatformError(
                    "job_unavailable", "No runnable ingestion job was found", {}, 409
                )
            job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(
                        and_(
                            ingestion_jobs_table.c.id == candidate["id"],
                            *conditions,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                raise PlatformError(
                    "job_unavailable", "No runnable ingestion job was found", {}, 409
                )
            attempt_number = (
                int(
                    connection.execute(
                        select(func.max(ingestion_attempts_table.c.attempt_number)).where(
                            ingestion_attempts_table.c.job_id == job["id"]
                        )
                    ).scalar_one()
                    or 0
                )
                + 1
            )
            fencing_token = (
                int(
                    connection.execute(
                        select(func.max(ingestion_attempts_table.c.fencing_token)).where(
                            ingestion_attempts_table.c.job_id == job["id"]
                        )
                    ).scalar_one()
                    or 0
                )
                + 1
            )
            publication_id = str(job["active_publication_id"])
            publication = (
                connection.execute(
                    select(publications_table).where(publications_table.c.id == publication_id)
                )
                .mappings()
                .one_or_none()
            )
            if publication is None or publication["status"] != "staged":
                publication_id = _id("publication")
                connection.execute(
                    publications_table.insert().values(
                        id=publication_id,
                        document_id=job["document_id"],
                        document_version_id=job["document_version_id"],
                        job_id=job["id"],
                        attempt_id="pending",
                        generation_id=self._service._current_index_generation(connection),
                        status="staged",
                        resource_manifest_json={},
                        created_at_utc=now,
                        activated_at_utc=None,
                        superseded_at_utc=None,
                        discarded_at_utc=None,
                    )
                )
                connection.execute(
                    update(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == job["id"])
                    .values(active_publication_id=publication_id)
                )
                publication = (
                    connection.execute(
                        select(publications_table).where(publications_table.c.id == publication_id)
                    )
                    .mappings()
                    .one()
                )
            active_generation_id = self._service._current_index_generation(connection)
            if publication["generation_id"] != active_generation_id:
                connection.execute(
                    update(publications_table)
                    .where(publications_table.c.id == publication_id)
                    .values(generation_id=active_generation_id)
                )
                publication = dict(publication)
                publication["generation_id"] = active_generation_id
            version = None
            if job["document_version_id"]:
                version = (
                    connection.execute(
                        select(document_versions_table).where(
                            document_versions_table.c.id == job["document_version_id"]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            index_revision = self._service._current_index_revision(connection)
            authorization_fence = self._service._worker_authorization_fence(
                connection, job=job, document=document
            )
            cycle_attempt_number = (
                int(
                    connection.execute(
                        select(func.count())
                        .select_from(ingestion_attempts_table)
                        .where(
                            and_(
                                ingestion_attempts_table.c.job_id == job["id"],
                                ingestion_attempts_table.c.replay_generation
                                == job["replay_generation"],
                            )
                        )
                    ).scalar_one()
                )
                + 1
            )
            attempt_id = _id("attempt")
            expires = now + self._lease_ttl
            # 初始执行序列的执行/额度主体是原上传者；人工重放序列切换为
            # 重放操作者（created_by_user_id 不改写，主体切换见设计 2.3）。
            subject_user_id = (
                str(job["replayed_by_user_id"])
                if int(job["replay_generation"]) > 0 and job["replayed_by_user_id"]
                else str(job["created_by_user_id"])
            )
            space_id = str(document["space_id"])
            cost_center_key, space_kind, space_owner_user_id = (
                self._service._publication_space_ownership(
                    space_id=space_id,
                    subject_user_id=subject_user_id,
                )
            )
            # §2.3 L129：人工重放序列沿用重放事务固化的处理配置快照，同代
            # attempt 共用；初始序列仍由处理端按媒体画像解析（快照留空）。
            replay_snapshot: Mapping[str, Any] = {}
            if int(job["replay_generation"]) > 0 and isinstance(
                job["replay_config_snapshot_json"], Mapping
            ):
                replay_snapshot = dict(job["replay_config_snapshot_json"])
            processing_profile_version = (
                str(replay_snapshot["config_version"])
                if isinstance(replay_snapshot.get("config_version"), str)
                and replay_snapshot["config_version"].strip()
                else "default"
            )
            staging_request = {
                "job_id": str(job["id"]),
                "attempt_id": attempt_id,
                "fencing_token": fencing_token,
                "publication_id": publication_id,
                "document_id": str(job["document_id"]),
                "document_version_id": str(job["document_version_id"] or ""),
                "space_id": space_id,
                "operation": str(job["operation"]),
                "base_active_version_id": job["base_active_version_id"],
                "expected_generation_id": str(publication["generation_id"]),
                "index_revision_at_start": index_revision,
                "object_manifest_ref": (
                    str((version["object_manifest_json"] or {}).get("object_key", ""))
                    if version is not None
                    else ""
                ),
                "processing_config_snapshot": replay_snapshot,
                "authorization_fence": authorization_fence,
                "input_manifest_hash": (
                    str(version["content_hash_sha256"]) if version is not None else None
                ),
                "processing_profile_version": processing_profile_version,
                "usage_ownership": {
                    "actor_user_id": subject_user_id,
                    "actor_role_snapshot": str(job["quota_role_snapshot"]),
                    "actor_department_id_snapshot": job["quota_department_id_snapshot"],
                    "quota_subject_user_id": subject_user_id,
                    "cost_center_key": cost_center_key,
                    "space_id": space_id,
                    "space_kind": space_kind,
                    "space_owner_user_id": space_owner_user_id,
                    "authorization_version": None,
                    "fence_token": fencing_token,
                    "source_space_ids": [space_id],
                },
                "usage_deadline_at_utc": expires.isoformat(),
                "usage_replay_generation": int(job["replay_generation"]),
            }
            connection.execute(
                ingestion_attempts_table.insert().values(
                    id=attempt_id,
                    job_id=job["id"],
                    attempt_number=attempt_number,
                    cycle_attempt_number=cycle_attempt_number,
                    replay_generation=job["replay_generation"],
                    state="running",
                    lease_owner=worker_id,
                    lease_expires_at_utc=expires,
                    fencing_token=fencing_token,
                    publication_id=publication_id,
                    staging_request_json=staging_request,
                    processing_receipt_json=None,
                    failure_class=None,
                    failure_reason=None,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(ingestion_jobs_table)
                .where(
                    and_(
                        ingestion_jobs_table.c.id == job["id"],
                        ingestion_jobs_table.c.state.in_(
                            [
                                IngestionJobState.PENDING.value,
                                IngestionJobState.RETRY_WAIT.value,
                            ]
                        ),
                    )
                )
                .values(
                    state=IngestionJobState.RUNNING.value,
                    stage="parsing",
                    active_attempt_id=attempt_id,
                    # retry_wait 被认领转 running 后清空调度时刻：读模型仅对
                    # retry_wait 暴露 next_attempt_at，running 不再携带旧值。
                    next_attempt_at_utc=None,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(publications_table)
                .where(publications_table.c.id == publication_id)
                .values(attempt_id=attempt_id)
            )
            if job["upload_batch_id"]:
                connection.execute(
                    update(upload_batch_items_table)
                    .where(upload_batch_items_table.c.job_id == job["id"])
                    .values(result_state="running", updated_at_utc=now)
                )
                self._service._refresh_upload_batch(connection, job["upload_batch_id"], now)
            restore_is_pending = bool(
                connection.execute(
                    select(func.count())
                    .select_from(document_version_restore_holds_table)
                    .where(document_version_restore_holds_table.c.job_id == job["id"])
                ).scalar_one()
            )
        lease = JobLease(
            job_id=str(job["id"]),
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            fencing_token=fencing_token,
            lease_owner=worker_id,
            lease_expires_at=expires,
            publication_id=publication_id,
            expected_generation_id=str(publication["generation_id"]),
            authorization_fence=authorization_fence,
        )
        if restore_is_pending:
            # 恢复 job 的 worker 侧复制步骤：领取后、内容读取前执行（设计
            # §2.3.2）。复制/校验失败由 copy_restore_source 走既有失败事务并
            # 保留持有引用，租约随 job 终态失效。
            self._service.copy_restore_source(
                job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                fencing_token=lease.fencing_token,
            )
        return lease

    def renew(
        self,
        lease: JobLease,
        *,
        worker_id: str | None = None,
        lease_ttl: timedelta | None = None,
    ) -> JobLease | None:
        """Extend a running attempt only while its owner and fencing token match."""

        owner = lease.lease_owner if worker_id is None else worker_id
        if not isinstance(owner, str) or not owner.strip():
            raise PlatformError("validation_error", "worker_id is required", {}, 422)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        if ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl must be positive")
        with self._service._engine.begin() as connection:
            now = self._service._current_time()
            attempt = (
                connection.execute(
                    select(ingestion_attempts_table)
                    .where(
                        and_(
                            ingestion_attempts_table.c.id == lease.attempt_id,
                            ingestion_attempts_table.c.job_id == lease.job_id,
                            ingestion_attempts_table.c.state == "running",
                            ingestion_attempts_table.c.lease_owner == owner,
                            ingestion_attempts_table.c.fencing_token == lease.fencing_token,
                            ingestion_attempts_table.c.lease_expires_at_utc > now,
                            ingestion_jobs_table.c.id == lease.job_id,
                            ingestion_jobs_table.c.state == IngestionJobState.RUNNING.value,
                            ingestion_jobs_table.c.active_attempt_id == lease.attempt_id,
                        )
                    )
                    .select_from(
                        ingestion_attempts_table.join(
                            ingestion_jobs_table,
                            ingestion_jobs_table.c.id == ingestion_attempts_table.c.job_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                return None
            expires = now + ttl
            updated = connection.execute(
                update(ingestion_attempts_table)
                .where(
                    and_(
                        ingestion_attempts_table.c.id == lease.attempt_id,
                        ingestion_attempts_table.c.job_id == lease.job_id,
                        ingestion_attempts_table.c.state == "running",
                        ingestion_attempts_table.c.lease_owner == owner,
                        ingestion_attempts_table.c.fencing_token == lease.fencing_token,
                        ingestion_attempts_table.c.lease_expires_at_utc > now,
                    )
                )
                .values(lease_expires_at_utc=expires, updated_at_utc=now)
            ).rowcount
            if updated != 1:
                return None
            return JobLease(
                job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                attempt_number=lease.attempt_number,
                fencing_token=lease.fencing_token,
                lease_owner=owner,
                lease_expires_at=expires,
                publication_id=lease.publication_id,
                expected_generation_id=lease.expected_generation_id,
                authorization_fence=lease.authorization_fence,
            )

    def _reclaim_expired_attempts(self, connection, now: datetime) -> None:
        running_jobs = (
            connection.execute(
                select(ingestion_jobs_table.c.id, ingestion_jobs_table.c.document_id).where(
                    ingestion_jobs_table.c.state == IngestionJobState.RUNNING.value
                )
            )
            .mappings()
            .all()
        )
        for expired_job in running_jobs:
            job_document_id = expired_job["document_id"]
            if job_document_id is None:
                continue
            document = self._service._locked_document(connection, str(job_document_id))
            if document is None:
                continue
            job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == expired_job["id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if job["state"] != IngestionJobState.RUNNING.value or job["active_attempt_id"] is None:
                continue
            attempt = (
                connection.execute(
                    select(ingestion_attempts_table)
                    .where(ingestion_attempts_table.c.id == job["active_attempt_id"])
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                attempt is None
                or attempt["state"] != "running"
                or attempt["lease_expires_at_utc"] is None
                or _as_utc(attempt["lease_expires_at_utc"]) > now
            ):
                continue

            attempt_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(ingestion_attempts_table)
                    .where(
                        and_(
                            ingestion_attempts_table.c.job_id == expired_job["id"],
                            ingestion_attempts_table.c.replay_generation
                            == job["replay_generation"],
                        )
                    )
                ).scalar_one()
            )
            will_retry = attempt_count < 4
            retry_delay = None
            if will_retry:
                base_minutes = (1, 5, 30)[attempt_count - 1]
                retry_delay = timedelta(
                    seconds=base_minutes * 60 * (80 + secrets.randbelow(41)) / 100
                )
            next_state = (
                IngestionJobState.RETRY_WAIT.value
                if will_retry
                else IngestionJobState.DEAD_LETTER.value
            )

            self._service._discard_indexing_attempt(connection, str(attempt["id"]))
            connection.execute(
                update(ingestion_attempts_table)
                .where(
                    and_(
                        ingestion_attempts_table.c.id == attempt["id"],
                        ingestion_attempts_table.c.state == "running",
                        ingestion_attempts_table.c.fencing_token == attempt["fencing_token"],
                    )
                )
                .values(
                    state="expired",
                    lease_expires_at_utc=None,
                    failure_class="lease_expired",
                    failure_reason="lease_expired",
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(publications_table)
                .where(
                    and_(
                        publications_table.c.job_id == expired_job["id"],
                        publications_table.c.status == PublicationState.STAGED.value,
                    )
                )
                .values(status=PublicationState.DISCARDED.value, discarded_at_utc=now)
            )
            connection.execute(
                update(ingestion_jobs_table)
                .where(
                    and_(
                        ingestion_jobs_table.c.id == expired_job["id"],
                        ingestion_jobs_table.c.state == IngestionJobState.RUNNING.value,
                        ingestion_jobs_table.c.active_attempt_id == attempt["id"],
                    )
                )
                .values(
                    state=next_state,
                    stage=None,
                    active_attempt_id=None,
                    failure_reason="lease_expired",
                    next_attempt_at_utc=now + retry_delay if retry_delay is not None else None,
                    updated_at_utc=now,
                )
            )
            if not will_retry:
                if job["operation"] in {"initial", "replace"} and job["document_version_id"]:
                    connection.execute(
                        update(document_versions_table)
                        .where(document_versions_table.c.id == job["document_version_id"])
                        .values(
                            status=DocumentVersionState.FAILED.value,
                            terminal_at_utc=now,
                            purge_after_at_utc=now
                            + timedelta(days=self._service._version_retention_days),
                            updated_at_utc=now,
                        )
                    )
                    connection.execute(
                        update(documents_table)
                        .where(
                            and_(
                                documents_table.c.id == job["document_id"],
                                documents_table.c.active_operation_job_id == expired_job["id"],
                            )
                        )
                        .values(
                            pending_version_id=None,
                            active_operation_job_id=None,
                            version=int(document["version"]) + 1,
                            updated_at_utc=now,
                        )
                    )
                else:
                    connection.execute(
                        update(documents_table)
                        .where(
                            and_(
                                documents_table.c.id == job["document_id"],
                                documents_table.c.active_operation_job_id == expired_job["id"],
                            )
                        )
                        .values(
                            active_operation_job_id=None,
                            version=int(document["version"]) + 1,
                            updated_at_utc=now,
                        )
                    )
            if job["upload_batch_id"]:
                connection.execute(
                    update(upload_batch_items_table)
                    .where(upload_batch_items_table.c.job_id == expired_job["id"])
                    .values(
                        result_state="retry_wait" if will_retry else next_state,
                        updated_at_utc=now,
                    )
                )
                self._service._refresh_upload_batch(connection, job["upload_batch_id"], now)
