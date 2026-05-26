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
