from app.ingestion import IndexingJob, IndexingJobStatus
from app.ingestion.queue import InMemoryIndexingQueue
from app.ingestion.worker import (
    BackgroundIndexingWorker,
    get_background_indexing_worker,
    reset_background_indexing_worker,
)


class RecordingIndexService:
    def __init__(self, fail_job_ids: set[str] | None = None):
        self.fail_job_ids = fail_job_ids or set()
        self.ran_job_ids = []

    def run_indexing_job(self, job_id: str):
        self.ran_job_ids.append(job_id)
        if job_id in self.fail_job_ids:
            raise RuntimeError(f"failed {job_id}")


class RecoverableIndexService(RecordingIndexService):
    def __init__(self, pending_jobs: list[IndexingJob]):
        super().__init__()
        self.job_store = RecoverableJobStore(pending_jobs)


class RecoverableJobStore:
    def __init__(self, pending_jobs: list[IndexingJob]):
        self.pending_jobs = pending_jobs
        self.list_calls = []

    def list(self, *, status=None, **filters):
        self.list_calls.append((status, filters))
        status_value = status.value if isinstance(status, IndexingJobStatus) else status
        return [job for job in self.pending_jobs if job.status.value == status_value]


def test_background_indexing_worker_processes_one_queued_job():
    index_service = RecordingIndexService()
    worker = BackgroundIndexingWorker(index_service=index_service)

    worker.enqueue("job-1")

    assert worker.run_once(timeout_seconds=0) is True
    assert index_service.ran_job_ids == ["job-1"]
    assert worker.stats.processed_count == 1
    assert worker.stats.failed_count == 0


def test_background_indexing_worker_records_job_failures_without_stopping():
    index_service = RecordingIndexService(fail_job_ids={"job-1"})
    worker = BackgroundIndexingWorker(index_service=index_service)

    worker.enqueue("job-1")
    worker.enqueue("job-2")

    assert worker.run_once(timeout_seconds=0) is True
    assert worker.run_once(timeout_seconds=0) is True
    assert index_service.ran_job_ids == ["job-1", "job-2"]
    assert worker.stats.processed_count == 1
    assert worker.stats.failed_count == 1
    assert worker.stats.last_error == "failed job-1"


def test_background_indexing_worker_starts_processes_and_stops_gracefully():
    index_service = RecordingIndexService()
    worker = BackgroundIndexingWorker(
        index_service=index_service,
        poll_interval_seconds=0.01,
    )

    worker.enqueue("job-1")
    worker.start()

    assert worker.wait_until_idle(timeout_seconds=1) is True
    worker.stop(timeout_seconds=1)

    assert index_service.ran_job_ids == ["job-1"]
    assert worker.is_running is False


def test_background_indexing_worker_recovers_persisted_pending_jobs_before_processing():
    pending = [
        IndexingJob.create(document_id="doc-1", source_path="/docs/a.md", job_id="job-1"),
        IndexingJob.create(document_id="doc-2", source_path="/docs/b.md", job_id="job-2"),
    ]
    index_service = RecoverableIndexService(pending)
    indexing_queue = InMemoryIndexingQueue()
    worker = BackgroundIndexingWorker(
        index_service=index_service,
        indexing_queue=indexing_queue,
        recover_pending_jobs_on_start=True,
    )

    assert worker.recover_pending_jobs() == 2
    assert index_service.job_store.list_calls == [
        (IndexingJobStatus.PENDING, {}),
    ]
    assert indexing_queue.unfinished_count == 2
    assert worker.run_once(timeout_seconds=0) is True
    assert worker.run_once(timeout_seconds=0) is True
    assert index_service.ran_job_ids == ["job-1", "job-2"]


def test_background_indexing_worker_start_recovers_persisted_pending_jobs():
    pending = [
        IndexingJob.create(document_id="doc-1", source_path="/docs/a.md", job_id="job-1"),
    ]
    index_service = RecoverableIndexService(pending)
    worker = BackgroundIndexingWorker(
        index_service=index_service,
        poll_interval_seconds=0.01,
        recover_pending_jobs_on_start=True,
    )

    worker.start()

    assert worker.wait_until_idle(timeout_seconds=1) is True
    worker.stop(timeout_seconds=1)
    assert index_service.ran_job_ids == ["job-1"]


def test_default_background_indexing_worker_prefers_grouped_storage_settings():
    from types import SimpleNamespace

    reset_background_indexing_worker()
    try:
        settings = SimpleNamespace(
            storage=SimpleNamespace(
                indexing_queue_provider="memory",
                indexing_worker_poll_interval_seconds=0.05,
                indexing_worker_recover_pending_jobs=False,
            )
        )
        worker = get_background_indexing_worker(
            index_service=RecordingIndexService(),
            settings=settings,
        )

        assert isinstance(worker.indexing_queue, InMemoryIndexingQueue)
        assert worker.poll_interval_seconds == 0.05
        assert worker.recover_pending_jobs_on_start is False
    finally:
        reset_background_indexing_worker()
