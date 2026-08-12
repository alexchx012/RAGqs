"""Graph build worker: claim, extract, stage, release, terminalize.

The worker never activates anything itself: after staging it only persists
the indexing `ReleaseGraphComponent.state=activated` receipt before the run
can succeed and notify. Failed/cancelled runs are never auto-retried; lease
loss only invalidates the attempt and requeues the same run once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.documents.public_graph import PublicGraphSourceSnapshot
from app.platform.errors import PlatformError

from .extraction import DbGraphExtractionSession
from .models import GraphRunRecord
from .ports import GraphExtractorPort
from .service import GraphBuildService
from .usage import GraphUsageRecorder


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class GraphBuildWorkerStats:
    builds_processed: int = 0
    runs_requeued: int = 0
    runs_failed: int = 0


class GraphBuildWorker:
    def __init__(
        self,
        service: GraphBuildService,
        extractor: GraphExtractorPort,
        usage_submission: Any,
        *,
        owner: str = "graph-build-worker",
        now: Callable[[], datetime] = _now,
        deadline_seconds: int = 120,
    ) -> None:
        self._service = service
        self._extractor = extractor
        self._usage_submission = usage_submission
        self._owner = owner
        self._now = now
        self._deadline_seconds = deadline_seconds

    def run_once(self) -> GraphBuildWorkerStats:
        requeued = self._service.requeue_expired()
        run = self._service.claim_next(owner=self._owner)
        if run is None:
            return GraphBuildWorkerStats(runs_requeued=requeued)
        try:
            self._build(run)
            return GraphBuildWorkerStats(builds_processed=1, runs_requeued=requeued)
        except PlatformError as error:
            if error.code == "graph_build_lease_lost":
                return GraphBuildWorkerStats(runs_requeued=requeued)
            self._fail(run, _failure_class(error), error.message)
            return GraphBuildWorkerStats(builds_processed=1, runs_requeued=requeued, runs_failed=1)
        except Exception as error:  # noqa: BLE001 - terminalize unknown worker faults
            self._fail(run, "graph_provider_failed", str(error))
            return GraphBuildWorkerStats(builds_processed=1, runs_requeued=requeued, runs_failed=1)

    def _build(self, run: GraphRunRecord) -> None:
        if run.grant_expires_at <= self._now():
            raise PlatformError(
                "graph_stage_grant_expired",
                "The component stage grant expired",
                {},
                409,
            )
        config = run.config_snapshot
        recorder = GraphUsageRecorder(
            submission=self._usage_submission,
            provider=str(config.get("provider", "openai_compatible")),
            model=str(config.get("model", "public-graph-extraction-v1")),
            operation=str(config.get("operation", "graph_build")),
            execution_id=run.graph_build_id,
            attempt_id=f"{run.graph_build_id}:{run.current_attempt}",
            generation_id=run.target_generation_id,
            actor_user_id=run.initiator_identity_id,
            deadline_utc=self._now() + timedelta(seconds=self._deadline_seconds),
            started_at=self._now,
        )
        session = DbGraphExtractionSession(
            run=run,
            write_staging=lambda kind, resource_id, payload: self._service.write_staging_resource(
                run=run,
                resource_kind=kind,
                resource_id=resource_id,
                payload=payload,
            ),
            recorder=recorder,
            now=self._now,
            deadline_seconds=self._deadline_seconds,
        )
        snapshot = PublicGraphSourceSnapshot(
            schema_version=1,
            source_revision=run.source_revision,
            source_manifest_id=run.source_manifest_id,
            source_manifest_hash=run.source_manifest_hash,
            publications=run.publications,
        )
        self._extractor.extract(snapshot, session)
        self._service.record_usage(
            run=run,
            primary_model_calls=session.primary_calls,
            provider_calls=session.provider_calls,
        )
        # Synchronous head revalidation immediately before handoff; a stale
        # run must discard its staging and fail, never activate.
        self._service.validate_head(run=run)
        component_stage_id = self._service.stage_component(run=run)
        release_receipt = self._service.release_component(
            run=run, component_stage_id=component_stage_id
        )
        if str(release_receipt.state) != "activated":
            raise PlatformError(
                "graph_release_not_activated",
                f"The indexing release receipt state is {release_receipt.state}",
                {},
                409,
            )
        self._service.complete_succeeded(
            run=run, owner=self._owner, release_receipt=release_receipt
        )

    def _fail(self, run: GraphRunRecord, failure_class: str, reason: str) -> None:
        self._service.complete_failed(
            run=run,
            owner=self._owner,
            failure_class=failure_class,
            reason=reason,
        )
        self._service.discard_staging(run=run)


def _failure_class(error: PlatformError) -> str:
    if error.code == "graph_source_changed":
        return "graph_source_changed"
    if error.code == "graph_stage_grant_expired":
        return "graph_stage_grant_expired"
    if error.code in {"graph_component_not_found", "processing_receipt_conflict"}:
        return "graph_component_stage_failed"
    if error.code in {
        "graph_release_not_activated",
        "generation_conflict",
        "graph_build_lease_lost",
    }:
        return "graph_release_failed"
    if error.code in {"graph_provider_call_failed", "graph_provider_dispatch_failed"}:
        return "graph_provider_failed"
    return "graph_component_stage_failed"


__all__ = ["GraphBuildWorker", "GraphBuildWorkerStats"]
