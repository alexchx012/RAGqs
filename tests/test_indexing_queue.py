from types import SimpleNamespace

from app.ingestion.queue import InMemoryIndexingQueue


def test_in_memory_indexing_queue_dequeues_jobs_fifo_and_tracks_unfinished_count():
    queue = InMemoryIndexingQueue()

    queue.enqueue("job-1")
    queue.enqueue("job-2")

    assert queue.unfinished_count == 2
    assert queue.dequeue(timeout_seconds=0) == "job-1"
    queue.task_done("job-1")
    assert queue.unfinished_count == 1
    assert queue.dequeue(timeout_seconds=0) == "job-2"
    queue.task_done("job-2")
    assert queue.unfinished_count == 0
    assert queue.dequeue(timeout_seconds=0) is None


def test_in_memory_indexing_queue_deduplicates_queued_job_ids():
    queue = InMemoryIndexingQueue()

    queue.enqueue("job-1")
    queue.enqueue("job-1")

    assert queue.unfinished_count == 1
    assert queue.dequeue(timeout_seconds=0) == "job-1"
    queue.task_done("job-1")
    assert queue.dequeue(timeout_seconds=0) is None


def test_postgres_indexing_queue_claims_jobs_once_and_deletes_completed_items():
    import app.ingestion.queue as queue_module

    assert hasattr(queue_module, "PostgresIndexingQueue")
    database = FakePostgresQueueDatabase()
    queue = queue_module.PostgresIndexingQueue(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )

    assert database.connect_count == 0
    assert queue.enqueue("job-1") is True
    assert queue.enqueue("job-1") is False
    assert queue.enqueue("job-2") is True
    assert database.dsns == ["postgresql://rag:secret@db/ragqs"]

    assert queue.unfinished_count == 2
    assert queue.dequeue(timeout_seconds=0) == "job-1"
    assert queue.dequeue(timeout_seconds=0) == "job-2"
    assert queue.dequeue(timeout_seconds=0) is None

    queue.task_done("job-1")
    assert queue.unfinished_count == 1
    queue.task_done("job-2")
    assert queue.unfinished_count == 0
    assert queue.enqueue("job-1") is True


def test_build_indexing_queue_selects_postgres_provider_without_connecting():
    import app.ingestion.worker as worker_module

    assert hasattr(worker_module, "build_indexing_queue")
    queue = worker_module.build_indexing_queue(
        SimpleNamespace(
            indexing_queue_provider="postgres",
            indexing_queue_postgres_dsn="postgresql://rag:secret@db/ragqs",
        )
    )

    assert queue.__class__.__name__ == "PostgresIndexingQueue"
    assert queue.dsn == "postgresql://rag:secret@db/ragqs"


class FakePostgresQueueDatabase:
    def __init__(self):
        self.rows = []
        self.connect_count = 0
        self.dsns = []
        self.next_sequence = 1

    def connect(self, dsn: str):
        self.connect_count += 1
        if dsn not in self.dsns:
            self.dsns.append(dsn)
        return FakePostgresQueueConnection(self)


class FakePostgresQueueConnection:
    def __init__(self, database: FakePostgresQueueDatabase):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakePostgresQueueCursor(self.database)


class FakePostgresQueueCursor:
    def __init__(self, database: FakePostgresQueueDatabase):
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
        if normalized.startswith("insert into indexing_queue_jobs"):
            self._insert_job(params[0])
            return self
        if normalized.startswith("update indexing_queue_jobs set status = 'pending'"):
            self.results = []
            return self
        if normalized.startswith("with next_job as"):
            self._claim_next_job()
            return self
        if normalized.startswith("select count(*) as unfinished_count"):
            self.results = [
                {
                    "unfinished_count": sum(
                        1 for row in self.database.rows if row["status"] in {"pending", "running"}
                    )
                }
            ]
            return self
        if normalized.startswith("delete from indexing_queue_jobs where job_id"):
            job_id = params[0]
            self.database.rows = [row for row in self.database.rows if row["job_id"] != job_id]
            self.results = []
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self.results[0] if self.results else None

    def _insert_job(self, job_id: str) -> None:
        if any(row["job_id"] == job_id for row in self.database.rows):
            self.results = []
            return
        self.database.rows.append(
            {
                "sequence": self.database.next_sequence,
                "job_id": job_id,
                "status": "pending",
            }
        )
        self.database.next_sequence += 1
        self.results = [{"job_id": job_id}]

    def _claim_next_job(self) -> None:
        pending = [
            row for row in sorted(self.database.rows, key=lambda item: item["sequence"])
            if row["status"] == "pending"
        ]
        if not pending:
            self.results = []
            return
        pending[0]["status"] = "running"
        self.results = [{"job_id": pending[0]["job_id"]}]
