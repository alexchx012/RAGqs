"""C7-restore：worker 侧源对象复制（claim 后、内容处理前）端到端行为。"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.documents.schema import (
    document_version_restore_holds_table,
    document_versions_table,
    ingestion_jobs_table,
)
from app.documents.service import DocumentUpload
from app.platform.errors import PlatformError
from app.platform.storage import ObjectMetadata


def _upload(name: str = "guide.txt", content: bytes = b"hello") -> DocumentUpload:
    return DocumentUpload(filename=name, content=content, media_kind="text/plain")


def _version_facts(service, document_version_id: str) -> dict[str, object]:
    with service._engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    document_versions_table.c.original_object_key,
                    document_versions_table.c.content_hash_sha256,
                ).where(document_versions_table.c.id == document_version_id)
            )
            .mappings()
            .one()
        )
    return {
        "original_object_key": str(row["original_object_key"]),
        "content_hash_sha256": str(row["content_hash_sha256"]),
    }


def _job_state(service, job_id: str) -> str:
    with service._engine.connect() as connection:
        return str(
            connection.execute(
                select(ingestion_jobs_table.c.state).where(ingestion_jobs_table.c.id == job_id)
            ).scalar_one()
        )


def _holds(service, job_id: str) -> int:
    with service._engine.connect() as connection:
        return int(
            connection.execute(
                select(document_version_restore_holds_table.c.id).where(
                    document_version_restore_holds_table.c.job_id == job_id
                )
            ).scalar_one_or_none()
            is not None
        )


def _superseded_source(service, principal) -> dict[str, object]:
    """create → publish → replace → publish，返回首个被替换的源版本。"""
    from tests.documents.test_commands import _accept

    first = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="restore-copy-first-1",
    )["items"][0]
    _accept(service, principal, first)
    replacement = service.replace_version(
        principal=principal,
        document_id=str(first["document_id"]),
        expected_version=2,
        file=_upload(content=b"second edition"),
        idempotency_key="restore-copy-replace-1",
    )
    _accept(service, principal, replacement)
    facts = _version_facts(service, str(first["document_version_id"]))
    assert service._object_store.exists(facts["original_object_key"])
    return {
        "document_id": first["document_id"],
        "document_version_id": first["document_version_id"],
        **facts,
    }


def _restore_job(service, principal, source: dict[str, object]) -> dict[str, object]:
    restore = service.restore_version(
        principal=principal,
        document_id=str(source["document_id"]),
        document_version_id=str(source["document_version_id"]),
        expected_version=4,
        idempotency_key="restore-copy-accept-1",
    )
    facts = _version_facts(service, str(restore["document_version_id"]))
    return {
        "job_id": restore["job_id"],
        "document_version_id": restore["document_version_id"],
        **facts,
    }


def test_claim_copies_restore_source_and_releases_hold(service, principal) -> None:
    source = _superseded_source(service, principal)
    restored = _restore_job(service, principal, source)
    assert not service._object_store.exists(str(restored["original_object_key"]))
    assert _holds(service, str(restored["job_id"])) == 1

    service.claim_job(worker_id="worker-restore-copy", job_id=str(restored["job_id"]))

    content, _metadata = service._object_store.get(str(restored["original_object_key"]))
    assert content == b"hello"
    assert hashlib.sha256(content).hexdigest() == source["content_hash_sha256"]
    assert _holds(service, str(restored["job_id"])) == 0
    assert _job_state(service, str(restored["job_id"])) == "running"


def test_copy_mismatch_fails_job_and_keeps_hold(service, principal) -> None:
    source = _superseded_source(service, principal)
    restored = _restore_job(service, principal, source)
    source_key = str(source["original_object_key"])
    tampered = b"tampered payload"
    service._object_store.put(
        source_key,
        tampered,
        ObjectMetadata(
            content_type="text/plain",
            size_bytes=len(tampered),
            checksum_sha256=hashlib.sha256(tampered).hexdigest(),
        ),
    )

    with pytest.raises(PlatformError) as error:
        service.claim_job(worker_id="worker-restore-bad", job_id=str(restored["job_id"]))
    assert error.value.code == "restore_copy_mismatch"

    assert _job_state(service, str(restored["job_id"])) == "failed"
    assert _holds(service, str(restored["job_id"])) == 1


def test_cancelled_restore_job_keeps_hold_and_skips_copy(service, principal) -> None:
    source = _superseded_source(service, principal)
    restored = _restore_job(service, principal, source)
    service.cancel_job(principal=principal, job_id=str(restored["job_id"]))

    with pytest.raises(PlatformError) as error:
        service.claim_job(worker_id="worker-restore-cancel", job_id=str(restored["job_id"]))
    assert error.value.code == "job_unavailable"

    assert _holds(service, str(restored["job_id"])) == 1
    assert not service._object_store.exists(str(restored["original_object_key"]))


def test_replay_reruns_restore_copy(service, principal) -> None:
    source = _superseded_source(service, principal)
    restored = _restore_job(service, principal, source)
    service.cancel_job(principal=principal, job_id=str(restored["job_id"]))
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
        job_id=str(restored["job_id"]),
        idempotency_key="restore-copy-replay-1",
    )
    service.claim_job(worker_id="worker-restore-replay", job_id=str(replay["job_id"]))

    content, _metadata = service._object_store.get(str(restored["original_object_key"]))
    assert content == b"hello"
    assert _holds(service, str(replay["job_id"])) == 0
    assert _job_state(service, str(replay["job_id"])) == "running"
