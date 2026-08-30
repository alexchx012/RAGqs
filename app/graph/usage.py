"""Provider-call usage recording through the usage/quota public submission port.

Every real graph provider HTTP send and its bounded short retries create an
independent provider_call_id and immutable usage event tied to the
graph_build_id, attempt, provider/model/operation and the public cost center.
The graph never performs a quota balance check and never creates a
quota_debit; actual usage only aggregates from these raw events.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.platform.errors import PlatformError
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement
from app.usage.ports import UsageSubmissionPort


@dataclass(frozen=True, slots=True)
class GraphUsageRecorder:
    submission: UsageSubmissionPort
    provider: str
    model: str
    operation: str
    execution_id: str
    attempt_id: str
    generation_id: str | None
    actor_user_id: str
    deadline_utc: datetime
    started_at: Callable[[], datetime]

    def record_call(
        self,
        *,
        resource_id: str | None,
        request_fingerprint: str,
        send: Callable[[], Any],
    ) -> Any:
        call_id = self.submission.prepare_provider_call(
            provider=self.provider,
            model=self.model,
            operation=self.operation,
            execution_kind="graph_build",
            execution_id=self.execution_id,
            provider_call_id=None,
            attempt_id=self.attempt_id,
            generation_id=self.generation_id,
            resource_id=resource_id,
            deadline_utc=self.deadline_utc,
            request_fingerprint=request_fingerprint,
        )
        if not self.submission.mark_dispatching(call_id, started_at_provider=self.started_at):
            raise PlatformError(
                "graph_provider_dispatch_failed",
                "The graph provider call could not be dispatched",
                {},
                503,
            )
        ownership = OwnershipSnapshot(
            actor_user_id=self.actor_user_id,
            actor_role_snapshot="ops",
            actor_department_id_snapshot=None,
            quota_subject_user_id=None,
            cost_center_key="system:graph",
        )
        measurement = ProviderMeasurement(
            input_tokens=None,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            image_count=None,
            visual_input_tokens=None,
            embedding_input_tokens=None,
            vector_count=None,
            measurement_sources={},
        )
        try:
            result = send()
        except Exception as error:
            self.submission.complete_provider_call(
                provider_call_id=call_id,
                measurement=measurement,
                ownership=ownership,
                result="failed",
            )
            raise PlatformError(
                "graph_provider_call_failed",
                f"The graph provider call failed: {error}",
                {},
                502,
            ) from error
        self.submission.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement,
            ownership=ownership,
            result="succeeded",
        )
        return result


class UsageLedgerSubmissionAdapter:
    """Adapts the production usage ledger to the public usage submission port."""

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger

    def prepare_provider_call(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        execution_kind: str,
        execution_id: str,
        provider_call_id: str | None = None,
        attempt_id: str | None = None,
        generation_id: str | None = None,
        resource_id: str | None = None,
        deadline_utc: datetime,
        request_fingerprint: str,
        replay_generation: int = 0,
    ) -> str:
        call_id, created = self._ledger.prepare_provider_call_with_status(
            provider=provider,
            model=model,
            operation=operation,
            execution_kind=execution_kind,
            execution_id=execution_id,
            provider_call_id=provider_call_id,
            attempt_id=attempt_id,
            generation_id=generation_id,
            resource_id=resource_id,
            deadline_utc=deadline_utc,
            request_fingerprint=request_fingerprint,
            replay_generation=replay_generation,
        )
        if not created:
            raise PlatformError(
                "idempotency_key_conflict",
                "The provider call identity already exists",
                {},
                409,
            )
        return call_id

    def mark_dispatching(
        self, provider_call_id: str, *, started_at_provider: Callable[[], datetime] | datetime
    ) -> bool:
        return self._ledger.mark_dispatching(
            provider_call_id, started_at_provider=started_at_provider
        )

    def complete_provider_call(
        self,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: datetime | None = None,
    ) -> str:
        return self._ledger.complete_provider_call(
            provider_call_id=provider_call_id,
            measurement=measurement,
            ownership=ownership,
            result=result,
            provider_request_id=provider_request_id,
            started_at_utc=started_at_utc,
        )

    def complete_provider_call_in_transaction(
        self,
        connection: Any,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: object | None = None,
    ) -> str:
        return self._ledger.complete_provider_call_in_transaction(
            connection,
            provider_call_id=provider_call_id,
            measurement=measurement,
            ownership=ownership,
            result=result,
            provider_request_id=provider_request_id,
            started_at_utc=started_at_utc,
        )

    def mark_not_sent(self, provider_call_id: str) -> None:
        self._ledger.mark_not_sent(provider_call_id)

    def mark_unknown(self, provider_call_id: str) -> None:
        self._ledger.mark_unknown(provider_call_id)


__all__ = ["GraphUsageRecorder", "UsageLedgerSubmissionAdapter"]
