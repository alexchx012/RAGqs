"""CalibrationWindowPort implementation owned by the evaluation domain."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from app.chat.models import CalibrationWindowSnapshot
from app.chat.schema import chat_ab_candidate_table, chat_ab_pair_table, chat_ab_vote_table
from app.identity.schema import identity_user_table
from app.platform.errors import PlatformError

from .policy import (
    aggregate_result_metrics,
    config_threshold_eligibility,
    validate_policy,
    weighted_score,
)
from .repository import SqlAlchemyEvaluationRepository
from .schema import (
    calibration_window_table,
    evaluation_ab_golden_seed_table,
    evaluation_policy_table,
    shadow_evaluation_run_table,
)

# Effective A/B votes (choice 0/1) required before the active default may change
# (后端设计 §8.6: the first 10 votes never switch the default). A fixed
# constant by design — no configuration surface.
MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT = 10


class EvaluationCalibrationWindowPort:
    """Adapts the evaluation-owned calibration window facts to chat's port.

    When the evaluation migration has not run yet the window table is absent
    and ``get_open_window`` degrades to ``None`` (safe pre-migration boot).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._repository = SqlAlchemyEvaluationRepository(engine)

    @staticmethod
    def _table_present(connection: Connection) -> bool:
        return sqlalchemy_inspect(connection).has_table("calibration_window")

    def get_open_window(
        self,
        connection: Connection,
        *,
        now: datetime,
        user_id: str,
    ) -> CalibrationWindowSnapshot | None:
        del now, user_id
        if not self._table_present(connection):
            return None
        # Explicit row lock so concurrent samplers serialize against window
        # close and cannot claim samples into a closing/closed window (A6).
        row = (
            connection.execute(
                select(calibration_window_table)
                .where(calibration_window_table.c.status == "open")
                .order_by(calibration_window_table.c.opened_at_utc.desc())
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        ttl = self._pair_vote_ttl_seconds(connection, policy_version=str(row["policy_version"]))
        return CalibrationWindowSnapshot(
            window_id=str(row["window_id"]),
            status=str(row["status"]),
            policy_version=str(row["policy_version"]),
            sample_rate=float(row["sample_rate"]),
            window_kind=str(row["window_kind"]),
            expires_at_utc=row["close_deadline_at_utc"],
            close_deadline_at_utc=row["close_deadline_at_utc"],
            pair_vote_ttl_seconds=ttl,
        )

    @staticmethod
    def _pair_vote_ttl_seconds(connection: Connection, *, policy_version: str) -> int | None:
        if not sqlalchemy_inspect(connection).has_table("evaluation_policy"):
            return None
        value = connection.execute(
            select(evaluation_policy_table.c.pair_vote_ttl_seconds).where(
                evaluation_policy_table.c.policy_version == policy_version
            )
        ).scalar_one_or_none()
        return int(value) if value is not None else None

    def user_ab_opt_out(self, connection: Connection, *, user_id: str) -> bool:
        if not sqlalchemy_inspect(connection).has_table("identity_user"):
            return False
        preferences = connection.execute(
            select(identity_user_table.c.preferences_json).where(
                identity_user_table.c.id == user_id
            )
        ).scalar_one_or_none()
        if not preferences:
            return False
        return bool(preferences.get("ab_opt_out", False))

    def increment_pairs_collected(self, connection: Connection, window_id: str) -> None:
        if not self._table_present(connection):
            return
        # Same-transaction increment tied to the successful unique A/B vote.
        connection.execute(
            update(calibration_window_table)
            .where(calibration_window_table.c.window_id == window_id)
            .values(
                pairs_collected=calibration_window_table.c.pairs_collected + 1,
                version=calibration_window_table.c.version + 1,
                updated_at_utc=datetime.now(UTC),
            )
        )

    def count_effective_ab_votes(self, connection: Connection, *, space_id: str) -> int:
        """Effective votes for a space: choice 0/1 only (A6).

        Nearly-identical pairs never open for voting (no ``ab_vote`` row can
        exist for them) and ``neither`` votes carry no preference, so both are
        excluded structurally by the choice filter.
        """
        rows = connection.execute(
            select(chat_ab_vote_table.c.pair_id)
            .join(
                chat_ab_pair_table,
                chat_ab_pair_table.c.pair_id == chat_ab_vote_table.c.pair_id,
            )
            .where(
                chat_ab_pair_table.c.space_id == space_id,
                chat_ab_vote_table.c.choice.in_(("0", "1")),
            )
        ).all()
        return len(rows)

    def maybe_adopt_active_default(
        self, connection: Connection, *, space_id: str, now: datetime
    ) -> None:
        """Active-default recalculation after an effective A/B vote (A5).

        The first 10 effective votes only collect data: below
        ``MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT`` the active default never changes.
        From the 10th vote on, the existing admission ladder keeps deciding:
        the space's latest succeeded shadow run must cover
        ``min_real_queries`` distinct samples and only threshold-eligible
        candidate configs can be adopted (A7).
        """
        if not sqlalchemy_inspect(connection).has_table("evaluation_active_default"):
            return
        votes = self.count_effective_ab_votes(connection, space_id=space_id)
        if votes < MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT:
            return
        run = (
            connection.execute(
                select(
                    shadow_evaluation_run_table.c.run_id,
                    shadow_evaluation_run_table.c.comparator_key,
                    shadow_evaluation_run_table.c.frozen_snapshot_json,
                )
                .where(
                    shadow_evaluation_run_table.c.space_id == space_id,
                    shadow_evaluation_run_table.c.state == "succeeded",
                )
                .order_by(shadow_evaluation_run_table.c.completed_at_utc.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            return
        policy = self._repository.latest_policy(connection)
        if policy is None:
            return
        try:
            validate_policy(policy)
        except PlatformError:
            # An invalid policy forbids switching the default (后端设计 §8.2).
            return
        # A golden-less run adopts through the §8.4 weak-signal replacement
        # ladder, so a cold-start library can still earn its own default.
        has_golden = bool((run["frozen_snapshot_json"] or {}).get("golden_set_version"))
        results = self._repository.list_results(connection, run_id=str(run["run_id"]))
        distinct_samples = {str(result["sample_item_id"]) for result in results}
        if len(distinct_samples) < policy.min_real_queries:
            return
        by_config: dict[str, list[Mapping[str, Any]]] = {}
        for result in results:
            by_config.setdefault(str(result["candidate_config_version"]), []).append(result)
        eligible: list[tuple[str, float]] = []
        for config, config_results in by_config.items():
            metrics = aggregate_result_metrics(config_results)
            if config_threshold_eligibility(config_results, metrics, policy, has_golden=has_golden):
                eligible.append((config, weighted_score(metrics)))
        if not eligible:
            return
        eligible.sort(key=lambda item: item[1], reverse=True)
        top_config = eligible[0][0]
        current = self._repository.get_active_default(connection, space_id=space_id)
        if (
            current is not None
            and str(current["candidate_config_version"]) == top_config
            and str(current["source_run_id"] or "") == str(run["run_id"])
        ):
            return
        self._repository.set_active_default(
            connection,
            space_id=space_id,
            candidate_config_version=top_config,
            comparator_key=run["comparator_key"],
            source_run_id=str(run["run_id"]),
            now=now,
        )

    def record_golden_seed(
        self,
        connection: Connection,
        *,
        pair_id: str,
        space_id: str,
        question_text: str,
        preferred_candidate: int,
        preferred_content: str,
        preferred_citations: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
        rejected_candidate: int,
        policy_version: str,
        now: datetime,
    ) -> None:
        """Persist one A/B preference pair as a golden-set seed candidate (A8).

        The seed pool is only an input source for the deployment-side
        ``publish_golden_set``; nothing here publishes or touches the active
        golden version (A9).
        """
        if not sqlalchemy_inspect(connection).has_table("evaluation_ab_golden_seed"):
            return
        config_rows = (
            connection.execute(
                select(
                    chat_ab_candidate_table.c.candidate,
                    chat_ab_candidate_table.c.candidate_config_version,
                ).where(chat_ab_candidate_table.c.pair_id == pair_id)
            )
            .mappings()
            .all()
        )
        config_by_candidate = {
            int(row["candidate"]): (
                str(row["candidate_config_version"])
                if row["candidate_config_version"] is not None
                else None
            )
            for row in config_rows
        }
        connection.execute(
            evaluation_ab_golden_seed_table.insert().values(
                seed_id=f"seed_{secrets.token_urlsafe(15)}",
                pair_id=pair_id,
                space_id=space_id,
                question_text=question_text,
                preferred_candidate=int(preferred_candidate),
                preferred_candidate_config_version=config_by_candidate.get(
                    int(preferred_candidate)
                ),
                preferred_content=preferred_content,
                preferred_citations_json=list(preferred_citations),
                rejected_candidate=int(rejected_candidate),
                rejected_candidate_config_version=config_by_candidate.get(int(rejected_candidate)),
                policy_version=policy_version,
                created_at_utc=now,
            )
        )


__all__ = [
    "MIN_EFFECTIVE_AB_VOTES_FOR_DEFAULT",
    "EvaluationCalibrationWindowPort",
]
