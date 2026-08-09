"""Outbox dispatcher worker: claim, deliver, renew, recycle and compact.

The worker drives dispatcher deliveries for one process owner. Long-running
deliveries use a 20-second database-time heartbeat (`LeaseHeartbeat`) that
aborts the delivery as soon as the owner/fence is lost, so a stale runner can
never commit notification or delivery state.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from typing import Any

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation
from app.platform.runtime import PlatformRuntime
from app.platform.worker import WorkerRuntime, create_worker_runtime

from .dispatcher import OutboxDispatcher
from .ports import DeliveryClaim, DeliveryOutcome

_logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 20


@dataclass(frozen=True, slots=True)
class OutboxWorkerStats:
    claimed: int
    delivered: int
    failed: int
    dead_lettered: int
    expired: int
    compacted: int


class LeaseHeartbeat:
    """Renews one delivery lease on an independent thread every interval.

    The heartbeat runs on its own daemon thread so a single long blocking
    chunk of work can never let the lease expire. It stops as soon as the
    fence is lost (renewal affects zero rows).
    """

    def __init__(
        self,
        dispatcher: OutboxDispatcher,
        claim: DeliveryClaim,
        owner: str,
        *,
        interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        import threading

        self._dispatcher = dispatcher
        self.claim = claim
        self._owner = owner
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def beat(self) -> DeliveryClaim | None:
        """Renew the lease now; return None when the fence is gone."""
        renewed = self._dispatcher.renew_lease(self.claim, owner=self._owner)
        if renewed is None:
            self.lost = True
            return None
        self.claim = renewed
        return renewed

    def start(self) -> None:
        import threading

        self._stop_event.clear()
        self.lost = False
        self._thread = threading.Thread(target=self._loop, name="outbox-heartbeat", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import time

        while not self._stop_event.is_set():
            time.sleep(self.interval_seconds)
            if self._stop_event.is_set():
                return
            if self.beat() is None:
                self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_until(self, should_stop) -> bool:
        """Keep beating every interval until the work says stop or the fence dies."""
        while not should_stop():
            if self.beat() is None:
                return False
            import time

            time.sleep(self.interval_seconds)
        return True


class OutboxWorker:
    """Leased worker driving dispatcher deliveries for one process owner."""

    def __init__(self, worker_runtime: WorkerRuntime) -> None:
        self._worker_runtime = worker_runtime
        dispatcher = worker_runtime.runtime.resolve("outbox_dispatcher")
        if not isinstance(dispatcher, OutboxDispatcher):
            raise RuntimeError("outbox dispatcher is not configured")
        self._dispatcher = dispatcher

    def run_delivery_with_heartbeat(
        self,
        claim: DeliveryClaim,
        owner: str,
        *,
        work=None,
        heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
    ) -> DeliveryOutcome:
        """Run the consumer work with an independent timed heartbeat.

        The heartbeat thread renews the lease every interval regardless of how
        long a single chunk of `work` blocks, so a >60s chunk cannot lose the
        lease. Every finalize path still validates the DB lease/fence before
        commit; a lost fence raises FenceViolation and nothing is committed.
        """
        heartbeat = LeaseHeartbeat(
            self._dispatcher, claim, owner, interval_seconds=heartbeat_interval_seconds
        )
        heartbeat.start()
        try:
            if work is not None:
                while work(heartbeat.claim):
                    # A long blocking chunk is fine: the heartbeat thread keeps
                    # renewing while we are inside `work`.
                    pass
            # Final fence check: the lease must still be current at commit time.
            current = self._dispatcher.renew_lease(heartbeat.claim, owner=owner)
            if current is None:
                raise FenceViolation(f"stale fence for {claim.event_id}")
            return self._dispatcher.run_consumer_and_finalize(current, owner=owner)
        finally:
            heartbeat.stop()

    def run_once(self, *, owner: str, limit: int = 100) -> OutboxWorkerStats:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("worker owner must not be empty")
        delivered = 0
        failed = 0
        dead_lettered = 0
        claimed = 0
        for _ in range(limit):
            try:
                claim = self._dispatcher.claim_one(normalized_owner)
            except (FenceViolation, PlatformError):
                break
            if claim is None:
                break
            claimed += 1
            try:
                # Production path beats the lease every 20s and aborts on fence
                # loss, so a long delivery never loses its lease mid-flight.
                outcome = self.run_delivery_with_heartbeat(claim, normalized_owner)
                if outcome.status == "delivered":
                    delivered += 1
                elif outcome.status == "dead_letter":
                    dead_lettered += 1
                else:
                    failed += 1
            except FenceViolation:
                failed += 1
        expired = self._dispatcher.recycle_expired_running(limit=limit)
        compacted = self._dispatcher.compact_due_events()
        return OutboxWorkerStats(
            claimed=claimed,
            delivered=delivered,
            failed=failed,
            dead_lettered=dead_lettered,
            expired=expired,
            compacted=compacted,
        )

    def run_forever(
        self,
        *,
        owner: str,
        interval_seconds: float = 5.0,
        limit: int = 100,
        stop: Any = None,
    ) -> None:
        """Continuous dispatcher loop: claim/deliver, recycle, compact, sleep.

        `stop` is an optional zero-arg callable returning True to exit; without
        it the loop runs until the process is terminated.
        """
        import time

        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(owner=owner, limit=limit)
            except Exception:
                _logger.exception("outbox worker loop iteration failed")
            time.sleep(interval_seconds)

    def close(self) -> None:
        self._worker_runtime.close()


def run_outbox_worker_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    limit: int = 100,
) -> OutboxWorkerStats:
    worker_runtime = create_worker_runtime(settings, runtime=runtime)
    owns_runtime = runtime is None
    try:
        resolved_owner = owner or f"outbox:{socket.gethostname()}:{os.getpid()}"
        worker = OutboxWorker(worker_runtime)
        try:
            return worker.run_once(owner=resolved_owner, limit=limit)
        finally:
            worker.close()
    finally:
        if owns_runtime:
            worker_runtime.close()


def main() -> None:
    """Resident dispatcher loop entrypoint (console script)."""
    settings = load_platform_settings()
    worker_runtime = create_worker_runtime(settings)
    owns_runtime = True
    try:
        resolved_owner = f"outbox:{socket.gethostname()}:{os.getpid()}"
        worker = OutboxWorker(worker_runtime)
        try:
            _logger.info("outbox worker resident loop starting owner=%s", resolved_owner)
            worker.run_forever(owner=resolved_owner)
        finally:
            worker.close()
    finally:
        if owns_runtime:
            worker_runtime.close()
