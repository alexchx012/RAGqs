from datetime import UTC, datetime
from types import SimpleNamespace

from app.ingestion import IndexingJob, IndexingJobStatus, PostgresIndexingJobStore
from app.services.vector_index_service import _build_default_job_store


def test_postgres_indexing_job_store_saves_updates_gets_and_filters_jobs():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    database = FakePostgresIndexingDatabase()
    store = PostgresIndexingJobStore(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )
    succeeded = IndexingJob.create(
        document_id="doc-a",
        source_path="/docs/a.md",
        space_id="finance",
        job_id="job-a",
        created_at=now,
    )
    succeeded.start(now=now)
    store.save(succeeded)
    succeeded.complete(total_chunks=2, indexed_chunks=2, now=now)
    store.save(succeeded)
    failed = IndexingJob.create(
        document_id="doc-b",
        source_path="/docs/b.md",
        space_id="legal",
        job_id="job-b",
        created_at=now,
    )
    failed.start(now=now)
    failed.complete(total_chunks=1, indexed_chunks=0, errors=["split failed"], now=now)
    store.save(failed)

    assert database.dsns == ["postgresql://rag:secret@db/ragqs"]
    assert store.get("job-a").status is IndexingJobStatus.SUCCEEDED
    assert store.get("job-a").space_id == "finance"
    assert store.get("job-a").total_chunks == 2
    assert store.get("job-b").errors == ["split failed"]
    assert [job.job_id for job in store.list()] == ["job-a", "job-b"]
    assert [job.job_id for job in store.list(document_id="doc-a")] == ["job-a"]
    assert [job.job_id for job in store.list(source_path="/docs/b.md")] == ["job-b"]
    assert [job.job_id for job in store.list(status=IndexingJobStatus.FAILED)] == ["job-b"]


def test_postgres_indexing_job_store_defers_connection_until_first_operation():
    database = FakePostgresIndexingDatabase()

    store = PostgresIndexingJobStore(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )

    assert database.connect_count == 0

    store.list()

    assert database.connect_count == 1


def test_vector_index_service_can_select_postgres_indexing_job_store_by_config():
    store = _build_default_job_store(
        SimpleNamespace(
            indexing_job_store_provider="postgres",
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        )
    )

    assert isinstance(store, PostgresIndexingJobStore)
    assert store.dsn == "postgresql://rag:secret@db/ragqs"


class FakePostgresIndexingDatabase:
    def __init__(self):
        self.rows = []
        self.connect_count = 0
        self.dsns = []
        self.next_sequence = 1

    def connect(self, dsn: str):
        self.connect_count += 1
        if dsn not in self.dsns:
            self.dsns.append(dsn)
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakePostgresIndexingDatabase):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeCursor(self.database)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, database: FakePostgresIndexingDatabase):
        self.database = database
        self.results = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql: str, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create table") or normalized.startswith("create index"):
            self.results = []
            return self
        if normalized.startswith("insert into indexing_jobs"):
            self._upsert_job(params)
            self.results = []
            return self
        if normalized.startswith("select * from indexing_jobs where job_id"):
            job_id = params[0]
            self.results = [row for row in self.database.rows if row["job_id"] == job_id]
            return self
        if normalized.startswith("select * from indexing_jobs"):
            self.results = self._filter_jobs(normalized, params)
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self.results[0] if self.results else None

    def fetchall(self):
        return list(self.results)

    def _upsert_job(self, params):
        (
            job_id,
            document_id,
            source_path,
            space_id,
            status,
            total_chunks,
            indexed_chunks,
            errors_json,
            created_at,
            started_at,
            completed_at,
        ) = params
        existing = next((row for row in self.database.rows if row["job_id"] == job_id), None)
        row = {
            "job_id": job_id,
            "document_id": document_id,
            "source_path": source_path,
            "space_id": space_id,
            "status": status,
            "total_chunks": total_chunks,
            "indexed_chunks": indexed_chunks,
            "errors_json": errors_json,
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        if existing is None:
            row["sequence"] = self.database.next_sequence
            self.database.next_sequence += 1
            self.database.rows.append(row)
        else:
            row["sequence"] = existing["sequence"]
            existing.update(row)

    def _filter_jobs(self, normalized: str, params):
        rows = sorted(self.database.rows, key=lambda row: row["sequence"])
        if " where " not in normalized:
            return rows
        filtered = rows
        param_index = 0
        for field in ("document_id", "source_path", "status"):
            if f"{field} = %s" in normalized:
                expected = params[param_index]
                param_index += 1
                filtered = [row for row in filtered if row[field] == expected]
        return filtered
