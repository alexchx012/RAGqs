"""Resident ingestion worker: claim, process, publish, and renew attempt leases."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, select

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation
from app.platform.runtime import PlatformRuntime
from app.platform.storage import StorageKeyError
from app.platform.worker import (
    WorkerRuntime,
    create_worker_runtime,
    install_stop_signal_handlers,
    restore_signal_handlers,
)

from .indexing import IndexProcessingReceipt
from .jobs import JobLease
from .schema import document_versions_table, ingestion_attempts_table

_logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 300
DEFAULT_HEARTBEAT_SECONDS = 20
DEFAULT_POLL_INTERVAL_SECONDS = 5
_OWNER_MAX_LENGTH = 128


def _default_owner() -> str:
    prefix = "ingestion:"
    suffix = f":{str(os.getpid())[-16:]}"
    hostname_limit = max(0, _OWNER_MAX_LENGTH - len(prefix) - len(suffix))
    hostname = str(socket.gethostname()).strip()[:hostname_limit]
    return f"{prefix}{hostname}{suffix}"


@dataclass(frozen=True, slots=True)
class IngestionWorkerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    deferred: int = 0

    @property
    def completed(self) -> int:
        return self.succeeded


class LeaseHeartbeat:
    """Renew one ingestion attempt from a daemon thread while processing runs."""

    def __init__(
        self,
        documents_service: Any,
        claim: JobLease,
        owner: str,
        *,
        lease_ttl: timedelta,
        interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._documents_service = documents_service
        self.claim = claim
        self._owner = owner
        self._lease_ttl = lease_ttl
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._beat_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.lost = False

    def beat(self) -> JobLease | None:
        """Renew the attempt; return ``None`` when its fence is no longer current."""

        with self._beat_lock:
            renewed = self._documents_service.renew_job_lease(
                self.claim,
                worker_id=self._owner,
                lease_ttl=self._lease_ttl,
            )
            if renewed is None:
                with self._state_lock:
                    self.lost = True
                self._stop_event.set()
                return None
            self.claim = renewed
            return renewed

    def raise_if_inactive(self) -> None:
        with self._state_lock:
            error = self._error
            lost = self.lost
        if error is not None:
            raise error
        if lost:
            raise FenceViolation(f"stale fence for {self.claim.attempt_id}")

    def start(self) -> None:
        self._stop_event.clear()
        with self._state_lock:
            self._error = None
            self.lost = False
        self._thread = threading.Thread(
            target=self._loop,
            name="ingestion-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        try:
            while not self._stop_event.wait(self.interval_seconds):
                if self.beat() is None:
                    return
        except BaseException as exc:
            with self._state_lock:
                if self._error is None:
                    self._error = exc
            self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("ingestion heartbeat thread did not stop")
            self._thread = None


class IngestionWorker:
    """Drive durable document ingestion attempts for one process owner."""

    def __init__(
        self,
        worker_runtime: WorkerRuntime,
        *,
        lease_ttl: timedelta | None = None,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self._worker_runtime = worker_runtime
        runtime = worker_runtime.runtime
        documents_service = runtime.resolve("documents_service")
        indexing_service = runtime.resolve("indexing_service")
        if documents_service is None:
            raise RuntimeError("documents service is not configured")
        if indexing_service is None:
            raise RuntimeError("indexing service is not configured")
        self._documents_service = documents_service
        self._indexing_service = indexing_service
        settings = runtime.settings.worker
        configured_lease_seconds = getattr(settings, "ingestion_lease_seconds", None)
        configured_heartbeat_seconds = getattr(settings, "ingestion_heartbeat_seconds", None)
        self._lease_ttl = lease_ttl or timedelta(
            seconds=configured_lease_seconds or DEFAULT_LEASE_SECONDS
        )
        self._heartbeat_interval_seconds = heartbeat_interval_seconds or (
            configured_heartbeat_seconds or DEFAULT_HEARTBEAT_SECONDS
        )
        if self._lease_ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl must be positive")

    @staticmethod
    def _validate_owner(owner: object) -> str:
        if (
            not isinstance(owner, str)
            or not owner
            or owner != owner.strip()
            or len(owner) > _OWNER_MAX_LENGTH
        ):
            raise ValueError("ingestion worker owner must be a non-empty string")
        return owner

    @staticmethod
    def _validate_limit(limit: object) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("ingestion worker limit must be a non-negative integer")
        return limit

    def _load_processing_input(self, claim: JobLease) -> tuple[Any, bytes, str, str, str]:
        engine = self._documents_service._engine
        with engine.connect() as connection:
            attempt = (
                connection.execute(
                    select(ingestion_attempts_table).where(
                        and_(
                            ingestion_attempts_table.c.id == claim.attempt_id,
                            ingestion_attempts_table.c.job_id == claim.job_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                raise PlatformError(
                    "fence_conflict",
                    "The processing attempt is no longer current",
                    {},
                    409,
                )
            request = self._documents_service._index_request_from_attempt(attempt)
            if request is None:
                raise PlatformError(
                    "processing_receipt_conflict",
                    "The processing attempt has no staging request",
                    {},
                    409,
                )
            version = (
                connection.execute(
                    select(document_versions_table).where(
                        document_versions_table.c.id == request.document_version_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if version is None:
            raise PlatformError(
                "document_version_not_found",
                "The document version was not found",
                {},
                404,
            )
        object_key = request.object_manifest_ref.strip()
        if not object_key:
            raise PlatformError(
                "document_version_purged",
                "Document version content was purged",
                {},
                410,
            )
        try:
            content, metadata = self._documents_service._object_store.get(object_key)
        except StorageKeyError:
            raise PlatformError(
                "document_object_unavailable",
                "The document object is unavailable",
                {},
                503,
                True,
            ) from None
        media_kind = str(version["media_kind"] or metadata.content_type or "").strip()
        manifest_hash = str(version["content_hash_sha256"] or request.input_manifest_hash).strip()
        manifest = version["object_manifest_json"] or {}
        manifest_id = str(manifest.get("content_manifest_id") or "").strip()
        if not manifest_id:
            manifest_id = f"document-version:{request.document_version_id}"
        if not media_kind or not manifest_hash:
            raise PlatformError(
                "document_version_purged",
                "Document version content metadata is incomplete",
                {},
                410,
            )
        return request, content, media_kind, manifest_id, manifest_hash

    def _process_and_publish(self, claim: JobLease, owner: str) -> dict[str, Any]:
        heartbeat = LeaseHeartbeat(
            self._documents_service,
            claim,
            owner,
            lease_ttl=self._lease_ttl,
            interval_seconds=self._heartbeat_interval_seconds,
        )
        heartbeat.start()
        heartbeat_stopped = False
        try:
            heartbeat.raise_if_inactive()
            request, content, media_kind, manifest_id, manifest_hash = self._load_processing_input(
                claim
            )
            output = self._indexing_service.process_and_stage(
                request,
                content,
                media_kind=media_kind,
                content_manifest_id=manifest_id,
                content_manifest_hash=manifest_hash,
            )
            receipt = getattr(output, "receipt", output)
            if isinstance(receipt, IndexProcessingReceipt):
                typed_receipt: Any = receipt
            elif hasattr(receipt, "to_mapping"):
                typed_receipt = receipt.to_mapping()
            else:
                typed_receipt = receipt
            heartbeat.raise_if_inactive()
            if heartbeat.beat() is None:
                raise FenceViolation(f"stale fence for {claim.attempt_id}")
            # SQLite's shared development connection cannot safely run the final
            # publication transaction alongside a heartbeat transaction. Other
            # backends keep renewing until the terminal publication commits.
            if self._documents_service._engine.dialect.name == "sqlite":
                heartbeat.stop()
                heartbeat_stopped = True
                heartbeat.raise_if_inactive()
            result = self._documents_service.accept_processing_receipt(
                job_id=claim.job_id,
                receipt=typed_receipt,
                internal_worker=True,
            )
            if not heartbeat_stopped:
                heartbeat.stop()
                heartbeat_stopped = True
            return result
        finally:
            if not heartbeat_stopped:
                heartbeat.stop()

    def run_once(self, *, owner: str, limit: int = 100) -> IngestionWorkerStats:
        normalized_owner = self._validate_owner(owner)
        validated_limit = self._validate_limit(limit)
        if validated_limit == 0:
            return IngestionWorkerStats()
        claimed = succeeded = failed = deferred = 0
        for _ in range(validated_limit):
            try:
                claim = self._documents_service.claim_job(
                    worker_id=normalized_owner,
                    lease_ttl=self._lease_ttl,
                )
            except PlatformError as exc:
                if exc.code == "job_unavailable":
                    break
                deferred += 1
                _logger.exception("ingestion claim failed code=%s", exc.code)
                break
            claimed += 1
            try:
                self._process_and_publish(claim, normalized_owner)
            except FenceViolation:
                deferred += 1
                _logger.warning("ingestion attempt lost its fence attempt_id=%s", claim.attempt_id)
            except PlatformError as exc:
                if exc.code == "fence_conflict":
                    deferred += 1
                    _logger.warning(
                        "ingestion receipt lost its fence attempt_id=%s", claim.attempt_id
                    )
                    continue
                retryable = exc.retryable
                reason = str(exc.message)[:256]
                try:
                    self._documents_service.fail_job(
                        job_id=claim.job_id,
                        reason=reason or "ingestion_processing_failed",
                        retryable=retryable,
                        attempt_id=claim.attempt_id,
                        fencing_token=claim.fencing_token,
                    )
                    failed += 1
                except PlatformError as fail_error:
                    deferred += 1
                    _logger.warning(
                        "ingestion failure could not be fenced attempt_id=%s code=%s",
                        claim.attempt_id,
                        fail_error.code,
                    )
            except Exception as exc:
                retryable = True
                reason = str(getattr(exc, "message", None) or exc)[:256]
                try:
                    self._documents_service.fail_job(
                        job_id=claim.job_id,
                        reason=reason or "ingestion_processing_failed",
                        retryable=retryable,
                        attempt_id=claim.attempt_id,
                        fencing_token=claim.fencing_token,
                    )
                    failed += 1
                except PlatformError as fail_error:
                    deferred += 1
                    _logger.warning(
                        "ingestion failure could not be fenced attempt_id=%s code=%s",
                        claim.attempt_id,
                        fail_error.code,
                    )
            else:
                succeeded += 1
        return IngestionWorkerStats(
            claimed=claimed,
            succeeded=succeeded,
            failed=failed,
            deferred=deferred,
        )

    def run_forever(
        self,
        *,
        owner: str,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        limit: int = 100,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        normalized_owner = self._validate_owner(owner)
        if interval_seconds < 0:
            raise ValueError("poll interval must be non-negative")
        self._validate_limit(limit)
        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(owner=normalized_owner, limit=limit)
            except Exception:
                _logger.exception("ingestion worker loop iteration failed")
            if stop is not None and stop():
                return
            time.sleep(interval_seconds)

    def close(self) -> None:
        self._worker_runtime.close()


def run_ingestion_worker_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    limit: int = 100,
) -> IngestionWorkerStats:
    worker_runtime = create_worker_runtime(settings, runtime=runtime)
    worker = IngestionWorker(worker_runtime)
    try:
        resolved_owner = owner or _default_owner()
        return worker.run_once(owner=resolved_owner, limit=limit)
    finally:
        worker.close()


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="ragqs-ingestion-worker")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--interval-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    settings = load_platform_settings()
    worker_runtime = create_worker_runtime(settings)
    worker = IngestionWorker(worker_runtime)
    stop_event, previous_handlers = install_stop_signal_handlers()
    owner = _default_owner()
    interval = args.interval_seconds
    if interval is None:
        interval = getattr(
            settings.worker,
            "ingestion_poll_interval_seconds",
            DEFAULT_POLL_INTERVAL_SECONDS,
        )
    try:
        _logger.info("ingestion worker resident loop starting owner=%s", owner)
        worker.run_forever(
            owner=owner,
            interval_seconds=interval,
            limit=args.limit,
            stop=stop_event.is_set,
        )
    finally:
        restore_signal_handlers(previous_handlers)
        worker.close()


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "IngestionWorker",
    "IngestionWorkerStats",
    "LeaseHeartbeat",
    "main",
    "run_ingestion_worker_once",
]
