"""Provider-call usage recording for the shadow-evaluation judge lane.

Every real judge HTTP send (and each bounded short retry) creates an
independent ``provider_call_id`` and immutable usage event tied to the
evaluation run, attempt, provider/model/operation and the system evaluation
cost center. The evaluation lane performs no quota balance check and never
creates a ``quota_debit`` (A19).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.graph.usage import UsageLedgerSubmissionAdapter
from app.platform.errors import PlatformError
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement
from app.usage.ports import UsageSubmissionPort

EXECUTION_KIND = "shadow_evaluation"
OPERATION = "evaluation_judge"
COST_CENTER_KEY = "system:evaluation"


@dataclass(frozen=True, slots=True)
class EvaluationUsageRecorder:
    submission: UsageSubmissionPort
    provider: str
    model: str
    run_id: str
    attempt_id: str
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
        now = self.started_at()
        if now >= self.deadline_utc:
            # Pre-send deadline rejection must not fabricate usage (A19).
            raise PlatformError(
                "evaluation_judge_deadline_exceeded",
                "The judge call crossed its deadline before sending",
                {"retryable": False},
                408,
                False,
            )
        call_id = self.submission.prepare_provider_call(
            provider=self.provider,
            model=self.model,
            operation=OPERATION,
            execution_kind=EXECUTION_KIND,
            execution_id=self.run_id,
            provider_call_id=None,
            attempt_id=self.attempt_id,
            generation_id=None,
            resource_id=resource_id,
            deadline_utc=self.deadline_utc,
            request_fingerprint=request_fingerprint,
        )
        if not self.submission.mark_dispatching(call_id, started_at_provider=self.started_at):
            raise PlatformError(
                "evaluation_judge_unavailable",
                "The judge provider call could not be dispatched",
                {"retryable": True},
                503,
                True,
            )
        ownership = OwnershipSnapshot(
            actor_user_id=self.actor_user_id,
            actor_role_snapshot="ops",
            actor_department_id_snapshot=None,
            quota_subject_user_id=None,
            cost_center_key=COST_CENTER_KEY,
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
                "evaluation_judge_transport_error",
                "The judge provider call failed",
                {"retryable": True},
                503,
                True,
            ) from error
        self.submission.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement,
            ownership=ownership,
            result="succeeded",
        )
        return result


__all__ = [
    "COST_CENTER_KEY",
    "EXECUTION_KIND",
    "OPERATION",
    "EvaluationUsageRecorder",
    "UsageLedgerSubmissionAdapter",
]
