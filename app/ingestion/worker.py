"""Background indexing worker for queued document ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

from loguru import logger

from app.ingestion.jobs import IndexingJobStatus
from app.ingestion.queue import IndexingQueue, InMemoryIndexingQueue


@dataclass
class BackgroundIndexingWorkerStats:
    processed_count: int = 0
    failed_count: int = 0
    last_error: str = ""


class BackgroundIndexingWorker:
    """Small in-process worker that executes persisted indexing jobs by id."""

    def __init__(
        self,
        *,
        index_service: Any,
        indexing_queue: IndexingQueue | None = None,
        poll_interval_seconds: float = 0.25,
        recover_pending_jobs_on_start: bool = False,
    ):
        self.index_service = index_service
        self.indexing_queue = indexing_queue or InMemoryIndexingQueue()
        self.poll_interval_seconds = poll_interval_seconds
        self.recover_pending_jobs_on_start = recover_pending_jobs_on_start
        self.stats = BackgroundIndexingWorkerStats()
        self._stop_requested = Event()
        self._thread: Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def enqueue(self, job_id: str) -> str:
        self.indexing_queue.enqueue(job_id)
        return job_id

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_requested.clear()
        self.recover_pending_jobs()
        self._thread = Thread(target=self._run_loop, name="ragqs-indexing-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        self._stop_requested.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout_seconds)
        return not self.is_running

    def wait_until_idle(self, *, timeout_seconds: float = 5.0) -> bool:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if self.indexing_queue.unfinished_count == 0:
                return True
            sleep(min(self.poll_interval_seconds, 0.05))
        return self.indexing_queue.unfinished_count == 0

    def run_once(self, *, timeout_seconds: float | None = None) -> bool:
        timeout = self.poll_interval_seconds if timeout_seconds is None else timeout_seconds
        job_id = self.indexing_queue.dequeue(timeout_seconds=timeout)
        if job_id is None:
            return False

        try:
            self.index_service.run_indexing_job(job_id)
            self.stats.processed_count += 1
        except Exception as exc:
            self.stats.failed_count += 1
            self.stats.last_error = str(exc)
            logger.error(f"后台索引任务失败: {job_id}, {exc}")
        finally:
            self.indexing_queue.task_done(job_id)
        return True

    def recover_pending_jobs(self) -> int:
        if not self.recover_pending_jobs_on_start:
            return 0
        job_store = getattr(self.index_service, "job_store", None)
        if job_store is None or not hasattr(job_store, "list"):
            return 0

        recovered_count = 0
        for job in job_store.list(status=IndexingJobStatus.PENDING):
            if self.indexing_queue.enqueue(job.job_id):
                recovered_count += 1
        return recovered_count

    def _run_loop(self) -> None:
        while not self._stop_requested.is_set() or self.indexing_queue.unfinished_count > 0:
            self.run_once(timeout_seconds=self.poll_interval_seconds)


_default_worker: BackgroundIndexingWorker | None = None


def get_background_indexing_worker(
    *,
    index_service: Any | None = None,
    settings: Any | None = None,
) -> BackgroundIndexingWorker:
    """Return the process-wide background indexing worker."""

    global _default_worker
    if _default_worker is None:
        if index_service is None:
            from app.services.vector_index_service import vector_index_service

            index_service = vector_index_service
        poll_interval = float(getattr(settings, "indexing_worker_poll_interval_seconds", 0.25))
        recover_pending_jobs = bool(
            getattr(settings, "indexing_worker_recover_pending_jobs", True)
        )
        _default_worker = BackgroundIndexingWorker(
            index_service=index_service,
            poll_interval_seconds=poll_interval,
            recover_pending_jobs_on_start=recover_pending_jobs,
        )
    return _default_worker


def reset_background_indexing_worker() -> None:
    """Reset the process-wide worker; intended for tests."""

    global _default_worker
    if _default_worker is not None:
        _default_worker.stop()
    _default_worker = None
