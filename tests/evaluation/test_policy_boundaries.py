"""Evaluation policy configuration boundary tests (后端设计 §8.2.1/§8.5).

Pins the exact boundary semantics that previously had no test coverage:
- a top-2 composite gap exactly equal to ``calibration_open_score_gap`` still
  opens a suggestion (only ``> gap`` skips),
- a sample rate of 0 never samples an A/B pair (even at the smallest draw),
- ``min_real_queries`` is an exact floor: N-1 real questions are rejected and
  N are accepted.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from sqlalchemy import func, select

from app.chat.models import AskRequest, ConversationScope
from app.chat.schema import chat_ab_pair_table
from app.evaluation.policy import default_policy_snapshot, weighted_score
from app.evaluation.schema import calibration_window_suggestion_table

from .conftest import NOW, FakeChatFactsPort, build_test_env, provision_and_login


def _seed_succeeded_run(
    env,
    *,
    run_id: str,
    space_id: str,
    comparator_key: str,
    policy: Any,
    metrics_by_config: dict[str, dict[str, Any]],
    weak_signals: dict[str, Any] | None = None,
    golden_version: str | None = "gv1",
) -> None:
    repo = env["runtime"].resolve("evaluation_repository")
    samples = tuple(
        {
            "item_id": f"{run_id}_item_{index}",
            "position": index,
            "question_text": f"{run_id} question {index}",
            "question_hash": f"{run_id}_hash_{index}",
            "evidence_hash": f"{run_id}_evidence_{index}",
            "weak_signals": weak_signals or {},
            "source_ref": f"{run_id}_message_{index}",
        }
        for index in range(1, 51)
    )
    frozen: dict[str, Any] = {"snapshot_id": f"snap_{run_id}"}
    if golden_version:
        frozen["golden_set_version"] = golden_version
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        repo.insert_run(
            connection,
            run_id=run_id,
            space_id=space_id,
            policy_version=policy.policy_version,
            comparator_key=comparator_key,
            candidate_config_versions=tuple(metrics_by_config),
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot=frozen,
            snapshot_id=f"snap_{run_id}",
            sample_items=samples,
            now=NOW,
            initiator_user_id="ops_1",
            request_hash=f"hash_{run_id}",
            idempotency_key=f"key_{run_id}",
        )
        claimed = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
        assert claimed is not None
        repo.transition_terminal(
            connection,
            run_id=run_id,
            attempt=claimed.attempt,
            owner="worker",
            fencing_token=claimed.fencing_token or "",
            to_state="succeeded",
            now=NOW,
            progress={"total": 100, "completed": 100, "failed": 0},
        )
        for index in range(1, 51):
            for config, metrics in metrics_by_config.items():
                repo.insert_result(
                    connection,
                    run_id=run_id,
                    sample_item_id=f"{run_id}_item_{index}",
                    candidate_config_version=config,
                    session_id=f"shadow:{run_id}:{index}:{config}",
                    metrics_json=dict(metrics),
                    weak_signals_json=dict(weak_signals or {}),
                    judged_at=NOW,
                )


_PASSING = {
    "faithfulness": 0.9,
    "answer_relevancy": 0.9,
    "refusal_rate": 1.0,
    "hit_at_k_final": 0.9,
    "mrr": 0.8,
    "p95_latency_ms": 100,
    "cost_per_query": 0.001,
}


def _actionable_suggestions(env, *, space_id: str) -> int:
    with env["engine"].connect() as connection:
        return int(
            connection.execute(
                select(func.count())
                .select_from(calibration_window_suggestion_table)
                .where(
                    calibration_window_suggestion_table.c.space_id == space_id,
                    calibration_window_suggestion_table.c.status == "actionable",
                )
            ).scalar_one()
        )


# ------------------------------------------------- calibration_open_score_gap


def test_score_gap_exactly_equal_to_the_threshold_still_opens_a_suggestion() -> None:
    """``abs(first - second) > gap`` skips: equality must suggest (§8.5 level 3)."""
    env = build_test_env()
    top = dict(_PASSING)
    second = {**_PASSING, "faithfulness": 0.7}
    gap = weighted_score(top) - weighted_score(second)
    assert 0.01 <= gap <= 0.10  # the policy validation range for the gap

    service = env["runtime"].resolve("evaluation_service")
    equal_policy = replace(
        default_policy_snapshot(now=NOW),
        policy_version="eval-gap-equal",
        calibration_open_score_gap=gap,
    )
    _seed_succeeded_run(
        env,
        run_id="run_gap_equal",
        space_id="space_1",
        comparator_key="cmp_gap",
        policy=equal_policy,
        metrics_by_config={"cfg_top": top, "cfg_second": second},
    )
    service.compute_suggestion("run_gap_equal")
    assert _actionable_suggestions(env, space_id="space_1") == 1

    below_policy = replace(
        default_policy_snapshot(now=NOW),
        policy_version="eval-gap-below",
        calibration_open_score_gap=math.nextafter(gap, 0.0),
    )
    _seed_succeeded_run(
        env,
        run_id="run_gap_below",
        space_id="space_2",
        comparator_key="cmp_gap",
        policy=below_policy,
        metrics_by_config={"cfg_top": top, "cfg_second": second},
    )
    service.compute_suggestion("run_gap_below")
    # One ulp below the exact gap: the strict ``>`` comparison skips the run.
    assert _actionable_suggestions(env, space_id="space_2") == 0


# ------------------------------------------------------------ sample rate 0


def test_zero_sample_rate_window_never_samples_an_ab_pair() -> None:
    """A window snapshot with sample_rate 0 samples nothing, at any draw."""
    from tests.chat.conftest import (
        FakeCalibration,
        open_window,
    )
    from tests.chat.conftest import (
        build_test_env as build_chat_env,
    )

    env = build_chat_env(
        calibration=FakeCalibration(window=open_window(sample_rate=0.0)),
        candidate_config_versions=("default", "candidate_b"),
        # The smallest possible draw still cannot pass a 0 rate (draw < rate).
        sampler=lambda: 0.0,
    )
    headers = {"Authorization": f"Bearer {provision_and_login(env['identity'], 'alice')[0]}"}
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(headers["Authorization"].split(" ")[1])
    conversation = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    personal = ConversationScope(space_ids=("personal:user_1",), document_ids=())
    for effort_level in ("quick", "think"):
        # quick uses the raw 0 rate; think applies the boost multiplier, which
        # must keep a zero rate at zero.
        service.ask(
            principal=principal,
            conversation_id=conversation,
            request=AskRequest(
                content=f"zero-rate question {effort_level}",
                effort_level=effort_level,
                scope=personal,
            ),
            idempotency_key=f"zero-rate-{effort_level}",
        )
    with env["engine"].connect() as connection:
        pairs = connection.execute(
            select(func.count()).select_from(chat_ab_pair_table)
        ).scalar_one()
    assert pairs == 0


# --------------------------------------------------------- min_real_queries


def test_min_real_queries_is_an_exact_floor() -> None:
    """N-1 real questions stay ineligible; exactly N are accepted."""
    facts = FakeChatFactsPort(sample_count=500)
    env = build_test_env(chat_facts=facts)
    token, _, user_id = provision_and_login(env["identity"], "ops1", role="ops")
    repository = env["runtime"].resolve("evaluation_repository")
    with env["engine"].connect() as connection:
        policy = repository.latest_policy(connection)
    assert policy is not None
    minimum = policy.min_real_queries

    def _post_run(key: str):
        return env["client"].post(
            "/v1/admin/evaluations/shadow-runs",
            json={"space_id": f"personal:{user_id}"},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )

    pool = list(facts.samples)
    facts.samples = pool[: minimum - 1]
    below = _post_run("boundary-below")
    assert below.status_code == 409
    assert below.json()["error"]["code"] == "evaluation_not_eligible"

    facts.samples = pool[:minimum]
    exact = _post_run("boundary-exact")
    assert exact.status_code == 202
    assert exact.json()["status"] == "queued"
