"""PG 集成：同一 index generation 下重复失败回收幂等（uq_publications_version_generation_status）。

语义由常规套件（SQLite 同样强制唯一约束）钉死；本文件验证 PostgreSQL 方言下的
真实约束场景：连续失败/过期回收后回收写点不再撞唯一键，worker 仍能认领后续 job。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, make_url

from app.documents.schema import documents_metadata, publications_table
from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.platform.database import core_metadata
from app.platform.storage import MemoryObjectStore

pytestmark = pytest.mark.integration

_PG_URL_ENV = "RAGQS_TEST_POSTGRES_URL"


class _Identity:
    def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
        del principal, space_id, action, connection
        return "manage"


def _postgres_test_url() -> URL:
    url = os.environ.get(_PG_URL_ENV)
    if not url:
        pytest.skip(f"requires {_PG_URL_ENV} (NOT RUN/BLOCKED)")
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - an invalid external test URL must skip
        pytest.skip(f"{_PG_URL_ENV} is malformed (NOT RUN/BLOCKED)")
    if parsed.get_backend_name() != "postgresql":
        pytest.skip(f"{_PG_URL_ENV} must use a postgresql backend (NOT RUN/BLOCKED)")
    if parsed.database is None or "test" not in parsed.database.lower():
        pytest.skip(f"{_PG_URL_ENV} database name must contain 'test' (NOT RUN/BLOCKED)")
    return parsed


def _schema_url(url: URL, schema: str) -> URL:
    query = dict(url.query)
    existing_options = str(query.get("options", "")).strip()
    query["options"] = f"{existing_options} -csearch_path={schema}".strip()
    return url.set(query=query)


@pytest.fixture()
def pg_service():
    base_url = _postgres_test_url()
    schema = f"documents_reclaim_{uuid4().hex[:12]}"
    base_engine = create_engine(base_url)
    scoped_engine = None
    try:
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_engine(_schema_url(base_url, schema))
        core_metadata.create_all(scoped_engine)
        documents_metadata.create_all(scoped_engine)
        yield DocumentsService(
            scoped_engine,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            object_store=MemoryObjectStore(),
            identity_access=_Identity(),
        )
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


def test_repeated_failures_same_generation_stay_idempotent_on_postgres(pg_service) -> None:
    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}

    def advancing_now():
        clock["now"] += timedelta(minutes=45)
        return clock["now"]

    pg_service._now = advancing_now
    created = pg_service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[DocumentUpload("guide.txt", b"poison", "text/plain")],
        idempotency_key="pg-reclaim-1",
    )["items"][0]
    job_id = created["job_id"]

    for expected_state in ("retry_wait", "retry_wait", "retry_wait", "dead_letter"):
        lease = pg_service.claim_job(worker_id="worker-pg-poison", job_id=job_id)
        failed = pg_service.fail_job(
            job_id=job_id,
            reason="transient outage",
            retryable=True,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
        )
        assert failed["state"] == expected_state
        with pg_service._engine.connect() as connection:
            remaining = connection.execute(
                select(func.count())
                .select_from(publications_table)
                .where(publications_table.c.job_id == job_id)
            ).scalar_one()
        assert remaining == 0

    # 毒化循环结束后 worker 仍能认领后续 job（claim 循环不再被约束异常打断）。
    followup = pg_service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[DocumentUpload("next.txt", b"next", "text/plain")],
        idempotency_key="pg-reclaim-2",
    )["items"][0]
    lease = pg_service.claim_job(worker_id="worker-pg-poison", job_id=followup["job_id"])
    assert lease.job_id == followup["job_id"]
