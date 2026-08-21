"""Contract fixes for retrieval A/B evaluation: metric separation, golden-set
auto shadow trigger and calibration claim determinism (A4/A5/A6)."""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.evaluation.schema import evaluation_run_command_table, shadow_evaluation_run_table

from .conftest import NOW, build_test_env, provision_and_login
from .test_worker_flow import AttributionJudge, _insert_run, _sample, _worker


# ------------------------------------------------------- metric split (A4)


class SplitRetrieval:
    """Replay fake whose pre-rerank candidate pool differs from the final set."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def replay(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "session_id": kwargs["session_id"],
            "candidate_config_version": kwargs["candidate_config_version"],
            "hits": (
                {"document_id": "s9", "snippet": "x"},
                {"document_id": "s8", "snippet": "y"},
            ),
            "candidate_hits": (
                {"document_id": "s9", "snippet": "x"},
                {"document_id": "s1", "snippet": "y"},
            ),
            "degradations": (),
        }


def test_hit_at_k_candidate_and_final_use_distinct_candidate_sets() -> None:
    env = build_test_env()
    question = "split question"
    question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
    service = env["runtime"].resolve("evaluation_service")
    service.publish_golden_set(
        space_id="space_1",
        version="gv1",
        items=(
            {
                "question_text": question,
                "expected_sources": ["s1"],
                "expects_refusal": False,
            },
        ),
    )
    _insert_run(
        env,
        sample_items=(_sample(question=question, question_hash=question_hash),),
        golden_version="gv1",
    )
    worker = _worker(env, AttributionJudge(), retrieval=SplitRetrieval())
    worker.run_once()
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].connect() as connection:
        results = repo.list_results(connection, run_id="run_1")
    metrics = results[0]["metrics_json"]
    # The candidate pool contains the expected source at k=5, the final
    # ranking does not: the two metrics are no longer the same number fed
    # from the same list (A4).
    assert metrics["hit_at_k_candidate"] == 1.0
    assert metrics["hit_at_k_final"] == 0.0
    assert metrics["mrr"] == 0.0


# ------------------------------------------- golden-set auto trigger (A5)


def test_publishing_default_golden_set_auto_triggers_shadow_run() -> None:
    env = build_test_env()
    token, _, user_id = provision_and_login(env["identity"], "ops1", role="ops")
    space_id = f"personal:{user_id}"
    service = env["runtime"].resolve("evaluation_service")
    published = service.publish_golden_set(
        space_id=space_id,
        version="gv1",
        items=(
            {
                "question_text": "auto trigger question",
                "expected_sources": ["s1"],
                "expects_refusal": False,
            },
        ),
    )
    assert published == "gv1"
    with env["engine"].connect() as connection:
        rows = connection.execute(select(shadow_evaluation_run_table)).mappings().all()
        commands = connection.execute(select(evaluation_run_command_table)).mappings().all()
    assert len(rows) == 1
    run = rows[0]
    assert str(run["state"]) == "queued"
    assert len(commands) == 1
    assert str(commands[0]["operator_user_id"]) == "system:evaluation"
    assert str(commands[0]["idempotency_key"]) == f"golden-auto:{space_id}:gv1"
    # The auto trigger keeps operator-created runs working afterwards.
    response = env["client"].post(
        "/v1/admin/evaluations/shadow-runs",
        json={"space_id": space_id},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "manual-1"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "shadow_evaluation_in_progress"


def test_auto_trigger_skips_silently_when_not_eligible() -> None:
    env = build_test_env()
    service = env["runtime"].resolve("evaluation_service")
    # Unknown space: the trigger must swallow the eligibility error instead of
    # failing the golden publication (A5).
    published = service.publish_golden_set(
        space_id="space_missing",
        version="gv1",
        items=(
            {
                "question_text": "q1",
                "expected_sources": ["s1"],
                "expects_refusal": False,
            },
        ),
    )
    assert published == "gv1"
    with env["engine"].connect() as connection:
        assert connection.execute(select(shadow_evaluation_run_table)).all() == []


# -------------------------------------------------- calibration claims (A6)


def test_claims_do_not_repeat_samples_and_break_ties_stably() -> None:
    env = build_test_env()
    # Two queued runs sharing the exact same created_at/next_attempt time.
    _insert_run(
        env,
        run_id="run_a",
        sample_items=(_sample(),),
        golden_version=None,
        snapshot_id="snap_a",
        idempotency_key="key_a",
    )
    _insert_run(
        env,
        run_id="run_b",
        sample_items=(_sample(),),
        golden_version=None,
        snapshot_id="snap_b",
        idempotency_key="key_b",
    )
    with env["engine"].begin() as connection:
        connection.execute(
            shadow_evaluation_run_table.update().values(
                created_at_utc=NOW, next_attempt_at_utc=NOW
            )
        )
    repo = env["runtime"].resolve("evaluation_repository")
    claimed: list[str] = []
    with env["engine"].begin() as connection:
        for owner in ("worker_1", "worker_2"):
            record = repo.claim_next(
                connection, owner=owner, lease_ttl_seconds=60, now=NOW
            )
            if record is not None:
                claimed.append(record.run_id)
    # Identical timestamps: the run_id tie-break fixes the order and each
    # run is claimed exactly once (A6).
    assert claimed == ["run_a", "run_b"]
    assert len(set(claimed)) == len(claimed)
