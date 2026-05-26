"""Background indexing worker for queued document ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

from loguru import logger


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
        poll_interval_seconds: float = 0.25,
    ):
        self.index_service = index_service
        self.poll_interval_seconds = poll_interval_seconds
        self.stats = BackgroundIndexingWorkerStats()
        self._queue: Queue[str] = Queue()
        self._stop_requested = Event()
        self._thread: Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def enqueue(self, job_id: str) -> str:
        self._queue.put(job_id)
        return job_id

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_requested.clear()
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
            if self._queue.unfinished_tasks == 0:
                return True
            sleep(min(self.poll_interval_seconds, 0.05))
        return self._queue.unfinished_tasks == 0

    def run_once(self, *, timeout_seconds: float | None = None) -> bool:
        timeout = self.poll_interval_seconds if timeout_seconds is None else timeout_seconds
        try:
            job_id = self._queue.get(timeout=timeout)
        except Empty:
            return False

        try:
            self.index_service.run_indexing_job(job_id)
            self.stats.processed_count += 1
        except Exception as exc:
            self.stats.failed_count += 1
            self.stats.last_error = str(exc)
            logger.error(f"后台索引任务失败: {job_id}, {exc}")
        finally:
            self._queue.task_done()
        return True

    def _run_loop(self) -> None:
        while not self._stop_requested.is_set() or self._queue.unfinished_tasks > 0:
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
        _default_worker = BackgroundIndexingWorker(
            index_service=index_service,
            poll_interval_seconds=poll_interval,
        )
    return _default_worker


def reset_background_indexing_worker() -> None:
    """Reset the process-wide worker; intended for tests."""

    global _default_worker
    if _default_worker is not None:
        _default_worker.stop()
    _default_worker = None
