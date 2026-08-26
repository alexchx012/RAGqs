"""EvaluationCalibrationWindowPort against chat generation (A32) and the
A/B calibration closure hooks (A1/A2/A5/A6/A7/A8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select, update

from app.chat.schema import (
    chat_ab_pair_table,
    chat_ab_vote_table,
    chat_conversation_table,
    chat_generation_table,
    chat_message_table,
)
from app.evaluation.calibration_port import (
    MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT,
    EvaluationCalibrationWindowPort,
)
from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import (
    calibration_window_table,
    evaluation_ab_golden_seed_table,
    evaluation_active_default_table,
    evaluation_golden_item_table,
    shadow_evaluation_result_table,
    shadow_evaluation_run_table,
)
from app.identity.schema import identity_user_table

from .conftest import NOW, build_test_env, provision_and_login

_PASSING_METRICS = {
    "faithfulness": 0.9,
    "answer_relevancy": 0.9,
    "refusal_rate": 0.9,
    "hit_at_k_final": 0.9,
    "mrr": 0.9,
    "p95_latency_ms": 100.0,
    "cost_per_query": 0.01,
}


def _seed_window(env, *, status: str = "open") -> str:
    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=NOW)
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        window = repo.create_window(
            connection,
            window_id="window_1",
            status=status,
            window_kind="manual",
            policy_version=policy.policy_version,
            sample_rate=0.5,
            opened_by="ops_1",
            now=NOW,
        )
    assert window.window_id is not None
    return window.window_id


def test_get_open_window_returns_snapshot() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        snapshot = port.get_open_window(connection, now=NOW, user_id="u1")
    assert snapshot is not None
    assert snapshot.window_id == "window_1"
    assert snapshot.status == "open"
    assert snapshot.sample_rate == 0.5
    assert snapshot.window_kind == "manual"


def test_get_open_window_none_when_closed() -> None:
    env = build_test_env()
    _seed_window(env, status="closed")
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        snapshot = port.get_open_window(connection, now=NOW, user_id="u1")
    assert snapshot is None


def test_user_ab_opt_out_reads_preferences() -> None:
    env = build_test_env()
    token, _, user_id = provision_and_login(env["identity"], "u1", role="user")
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(preferences_json={"ab_opt_out": True})
        )
    with env["engine"].connect() as connection:
        assert port.user_ab_opt_out(connection, user_id=user_id) is True


def test_increment_pairs_collected_same_transaction_and_once() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.increment_pairs_collected(connection, "window_1")
        port.increment_pairs_collected(connection, "window_1")
    with env["engine"].connect() as connection:
        value = connection.execute(
            select(calibration_window_table.c.pairs_collected).where(
                calibration_window_table.c.window_id == "window_1"
            )
        ).scalar_one()
    assert value == 2


def test_increment_pairs_collected_updates_the_window_timestamp() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        before = connection.execute(
            select(calibration_window_table.c.updated_at_utc).where(
                calibration_window_table.c.window_id == "window_1"
            )
        ).scalar_one()
    with env["engine"].begin() as connection:
        port.increment_pairs_collected(connection, "window_1")
    with env["engine"].connect() as connection:
        after = connection.execute(
            select(calibration_window_table.c.updated_at_utc).where(
                calibration_window_table.c.window_id == "window_1"
            )
        ).scalar_one()
    assert after != before


def test_increment_unknown_window_is_silent() -> None:
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.increment_pairs_collected(connection, "missing_window")


def test_chat_generation_calls_port_compatible() -> None:
    # The chat generation service calls get_open_window/user_ab_opt_out/
    # increment_pairs_collected exactly through this protocol shape.
    env = build_test_env()
    _seed_window(env)
    port = EvaluationCalibrationWindowPort(env["engine"])
    assert callable(port.get_open_window)
    assert callable(port.user_ab_opt_out)
    assert callable(port.increment_pairs_collected)
    with env["engine"].connect() as connection:
        snapshot = port.get_open_window(connection, now=NOW, user_id="u1")
    assert snapshot is not None and snapshot.status == "open"


# ------------------------------------------------- effective votes & adoption


def _seed_vote(
    connection,
    *,
    index: int,
    space_id: str,
    choice: str = "0",
) -> None:
    """One voted A/B pair with its single vote fact in chat tables."""
    connection.execute(
        chat_generation_table.insert().values(
            id=f"generation_{index}",
            conversation_id="conversation_1",
            owner_user_id="owner_1",
            user_message_id=f"user_message_{index}",
            message_id=f"message_{index}",
            root_generation_id=f"generation_{index}",
            attempt_number=1,
            status="completed",
            requested_effort_level="quick",
            effective_effort_level="quick",
            retrieval_profile_id="default",
            retrieval_profile_version="1",
            rag_budget_policy_version="budget_1",
            absolute_deadline_at_utc=NOW + timedelta(hours=1),
            auth_session_id="session_1",
            control_version=1,
            request_content=f"question {index}",
            request_scope_json={},
            version=1,
            created_at_utc=NOW,
            updated_at_utc=NOW,
        )
    )
    connection.execute(
        chat_message_table.insert().values(
            id=f"message_{index}",
            conversation_id="conversation_1",
            owner_user_id="owner_1",
            role="assistant",
            content=f"answer {index}",
            generation_id=f"generation_{index}",
            created_at_utc=NOW,
            updated_at_utc=NOW,
        )
    )
    connection.execute(
        chat_ab_pair_table.insert().values(
            pair_id=f"pair_{index}",
            generation_id=f"generation_{index}",
            message_id=f"message_{index}",
            window_id="window_1",
            owner_user_id="owner_1",
            space_id=space_id,
            status="voted",
            voted=True,
            choice=choice,
            voted_at_utc=NOW,
            version=1,
            created_at_utc=NOW,
            updated_at_utc=NOW,
        )
    )
    if choice != "expired":
        connection.execute(
            chat_ab_vote_table.insert().values(
                pair_id=f"pair_{index}",
                voter_user_id=f"voter_{index}",
                choice=choice,
                created_at_utc=NOW,
            )
        )


def _seed_votes(env, *, space_id: str, effective: int, neither: int = 0) -> None:
    with env["engine"].begin() as connection:
        connection.execute(
            chat_conversation_table.insert().values(
                id="conversation_1",
                owner_user_id="owner_1",
                title="Votes",
                pinned=False,
                effort_level="quick",
                scope_json={"space_ids": [space_id]},
                last_active_at_utc=NOW,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        index = 0
        for _ in range(effective):
            index += 1
            _seed_vote(connection, index=index, space_id=space_id, choice="0")
        for _ in range(neither):
            index += 1
            _seed_vote(connection, index=index, space_id=space_id, choice="neither")


def _seed_admissible_run(
    env,
    *,
    space_id: str,
    samples: int = 20,
    metrics_by_config: dict[str, dict] | None = None,
) -> str:
    """A succeeded shadow run whose configs' metrics may pass the ladder."""
    metrics_by_config = metrics_by_config or {"cfg_top": dict(_PASSING_METRICS)}
    with env["engine"].begin() as connection:
        connection.execute(
            shadow_evaluation_run_table.insert().values(
                run_id="run_1",
                space_id=space_id,
                state="succeeded",
                attempt=1,
                progress_json={"total": samples, "completed": samples, "failed": 0},
                policy_version="eval-v1",
                comparator_key="cmp_1",
                candidate_config_versions_json=list(metrics_by_config),
                index_generation_id="generation_1",
                index_revision=1,
                frozen_snapshot_json={},
                created_at_utc=NOW,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                version=1,
            )
        )
        for config, metrics in metrics_by_config.items():
            for sample in range(1, samples + 1):
                connection.execute(
                    shadow_evaluation_result_table.insert().values(
                        run_id="run_1",
                        sample_item_id=f"sample_{sample}",
                        candidate_config_version=config,
                        session_id=f"session_{config}_{sample}",
                        metrics_json=metrics,
                        weak_signals_json={},
                        judged_at_utc=NOW,
                    )
                )
    return "run_1"


def _seed_policy(env, *, min_real_queries: int = 20) -> None:
    repo = env["runtime"].resolve("evaluation_repository")
    # A distinct, strictly newer version: runtime boot already seeded the
    # default "eval-v1", and ensure_policy skips existing versions.
    policy = replace(
        default_policy_snapshot(now=NOW + timedelta(seconds=1)),
        policy_version="eval-test",
        min_real_queries=min_real_queries,
    )
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)


def _active_default(env) -> dict | None:
    with env["engine"].connect() as connection:
        row = connection.execute(select(evaluation_active_default_table)).mappings().one_or_none()
    return dict(row) if row is not None else None


def test_effective_vote_count_excludes_neither() -> None:
    env = build_test_env()
    _seed_votes(env, space_id="space_1", effective=3, neither=2)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].connect() as connection:
        assert port.count_effective_ab_votes(connection, space_id="space_1") == 3
        assert port.count_effective_ab_votes(connection, space_id="space_2") == 0


def test_nine_effective_votes_never_change_active_default() -> None:
    """A1/A5: below 10 effective votes the recalc only collects data."""
    env = build_test_env()
    _seed_policy(env)
    _seed_admissible_run(env, space_id="space_1")
    _seed_votes(env, space_id="space_1", effective=MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT - 1)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.maybe_adopt_active_default(connection, space_id="space_1", now=NOW)
    assert _active_default(env) is None


def test_tenth_effective_vote_adopts_eligible_top_config() -> None:
    env = build_test_env()
    _seed_policy(env)
    _seed_admissible_run(
        env,
        space_id="space_1",
        metrics_by_config={
            "cfg_top": dict(_PASSING_METRICS),
            "cfg_second": {**_PASSING_METRICS, "faithfulness": 0.65, "mrr": 0.5},
        },
    )
    _seed_votes(env, space_id="space_1", effective=MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.maybe_adopt_active_default(connection, space_id="space_1", now=NOW)
    adopted = _active_default(env)
    assert adopted is not None
    assert adopted["candidate_config_version"] == "cfg_top"
    assert adopted["source_run_id"] == "run_1"
    first_adopted_at = adopted["adopted_at_utc"]
    # Recalculation on a later vote does not churn the same adoption.
    with env["engine"].begin() as connection:
        port.maybe_adopt_active_default(
            connection, space_id="space_1", now=NOW + timedelta(hours=1)
        )
    again = _active_default(env)
    assert again is not None
    assert again["adopted_at_utc"] == first_adopted_at


def test_ten_votes_without_succeeded_run_keep_default() -> None:
    env = build_test_env()
    _seed_policy(env)
    _seed_votes(env, space_id="space_1", effective=MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.maybe_adopt_active_default(connection, space_id="space_1", now=NOW)
    assert _active_default(env) is None


def test_ten_votes_below_min_real_queries_keep_default() -> None:
    """A7: the 10-vote gate does not replace the existing admission ladder."""
    env = build_test_env()
    _seed_policy(env, min_real_queries=20)
    _seed_admissible_run(env, space_id="space_1", samples=10)
    _seed_votes(env, space_id="space_1", effective=MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.maybe_adopt_active_default(connection, space_id="space_1", now=NOW)
    assert _active_default(env) is None


def test_ten_votes_only_adopt_threshold_eligible_configs() -> None:
    """A7: a higher score never beats a failed threshold."""
    env = build_test_env()
    _seed_policy(env)
    _seed_admissible_run(
        env,
        space_id="space_1",
        metrics_by_config={
            "cfg_failing": {**_PASSING_METRICS, "faithfulness": 0.1},
            "cfg_eligible": dict(_PASSING_METRICS),
        },
    )
    _seed_votes(env, space_id="space_1", effective=MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT)
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.maybe_adopt_active_default(connection, space_id="space_1", now=NOW)
    adopted = _active_default(env)
    assert adopted is not None
    assert adopted["candidate_config_version"] == "cfg_eligible"


# -------------------------------------------------------------- golden seeds


def test_effective_vote_records_golden_seed_row() -> None:
    """A2/A8: the port persists the full preference-pair seed."""
    env = build_test_env()
    port = EvaluationCalibrationWindowPort(env["engine"])
    citations = [{"document_id": "doc_1", "chunk_id": "chunk_1"}]
    with env["engine"].begin() as connection:
        port.record_golden_seed(
            connection,
            pair_id="pair_1",
            space_id="space_1",
            question_text="Which source supports the answer?",
            preferred_candidate=0,
            preferred_content="The completed answer.",
            preferred_citations=tuple(citations),
            rejected_candidate=1,
            policy_version="cal-v1",
            now=NOW,
        )
    with env["engine"].connect() as connection:
        seed = connection.execute(select(evaluation_ab_golden_seed_table)).mappings().one()
    assert str(seed["pair_id"]) == "pair_1"
    assert str(seed["space_id"]) == "space_1"
    assert str(seed["question_text"]) == "Which source supports the answer?"
    assert seed["preferred_candidate"] == 0
    assert str(seed["preferred_content"]) == "The completed answer."
    assert list(seed["preferred_citations_json"]) == citations
    assert seed["rejected_candidate"] == 1
    assert str(seed["policy_version"]) == "cal-v1"


def test_golden_seed_pool_feeds_deployment_side_publish() -> None:
    """A2/A9: the pool is an input source; publish_golden_set can absorb it."""
    env = build_test_env()
    port = EvaluationCalibrationWindowPort(env["engine"])
    with env["engine"].begin() as connection:
        port.record_golden_seed(
            connection,
            pair_id="pair_1",
            space_id="space_1",
            question_text="Which source supports the answer?",
            preferred_candidate=1,
            preferred_content="Preferred answer.",
            preferred_citations=({"document_id": "doc_1", "chunk_id": "chunk_1"},),
            rejected_candidate=0,
            policy_version="cal-v1",
            now=NOW,
        )
    with env["engine"].connect() as connection:
        seeds = connection.execute(select(evaluation_ab_golden_seed_table)).mappings().all()
    items = [
        {
            "question_text": str(seed["question_text"]),
            "expected_sources": list(seed["preferred_citations_json"]),
            "expects_refusal": False,
        }
        for seed in seeds
    ]
    service = env["runtime"].resolve("evaluation_service")
    version = service.publish_golden_set(
        space_id="space_1", version="golden_v1", items=tuple(items)
    )
    assert version == "golden_v1"
    with env["engine"].connect() as connection:
        stored = connection.execute(
            select(evaluation_golden_item_table.c.question_text).where(
                evaluation_golden_item_table.c.space_id == "space_1",
                evaluation_golden_item_table.c.golden_version == "golden_v1",
            )
        ).scalar_one()
    assert stored == "Which source supports the answer?"
