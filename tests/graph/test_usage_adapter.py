"""UsageLedgerSubmissionAdapter 端口完整性：submit_local_usage 委托转发。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.graph.usage import UsageLedgerSubmissionAdapter
from app.usage.ledger import LocalMeasurement, OwnershipSnapshot


class _RecordingLedger:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = "local_usage_1"

    def submit_local_usage(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.result


def test_submit_local_usage_delegates_to_ledger() -> None:
    ledger = _RecordingLedger()
    adapter = UsageLedgerSubmissionAdapter(ledger)
    measurement = LocalMeasurement(
        item_count=3,
        page_count=12,
        input_bytes=None,
        gpu_milliseconds=None,
        cpu_milliseconds=None,
        peak_vram_bytes=None,
        measurement_sources={"indexing": "test"},
    )
    ownership = OwnershipSnapshot(
        actor_user_id="user_1",
        actor_role_snapshot="member",
        actor_department_id_snapshot=None,
        quota_subject_user_id="user_1",
        cost_center_key="space:s1",
    )
    started = datetime(2026, 9, 4, tzinfo=UTC)

    outcome = adapter.submit_local_usage(
        execution_kind="document_ingestion",
        execution_id="job_1",
        stage="contextual_retrieval",
        resource_kind="document_version",
        measurement=measurement,
        ownership=ownership,
        result="succeeded",
        started_at_utc=started,
        replay_generation=2,
    )

    assert outcome == "local_usage_1"
    assert ledger.calls == [
        {
            "execution_kind": "document_ingestion",
            "execution_id": "job_1",
            "stage": "contextual_retrieval",
            "resource_kind": "document_version",
            "measurement": measurement,
            "ownership": ownership,
            "result": "succeeded",
            "started_at_utc": started,
            "replay_generation": 2,
        }
    ]


def test_submit_local_usage_defaults_replay_generation() -> None:
    ledger = _RecordingLedger()
    adapter = UsageLedgerSubmissionAdapter(ledger)
    measurement = LocalMeasurement(
        item_count=None,
        page_count=None,
        input_bytes=None,
        gpu_milliseconds=None,
        cpu_milliseconds=None,
        peak_vram_bytes=None,
        measurement_sources={},
    )
    ownership = OwnershipSnapshot(
        actor_user_id="user_1",
        actor_role_snapshot="ops",
        actor_department_id_snapshot=None,
        quota_subject_user_id=None,
        cost_center_key="system:graph",
    )

    adapter.submit_local_usage(
        execution_kind="graph_build",
        execution_id="build_1",
        stage="extraction",
        resource_kind="graph_build",
        measurement=measurement,
        ownership=ownership,
        result="failed",
        started_at_utc=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert ledger.calls[0]["replay_generation"] == 0
