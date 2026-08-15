"""CalibrationWindowPort implementation owned by the evaluation domain."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from app.chat.models import CalibrationWindowSnapshot
from app.identity.schema import identity_user_table

from .schema import calibration_window_table, evaluation_policy_table


class EvaluationCalibrationWindowPort:
    """Adapts the evaluation-owned calibration window facts to chat's port.

    When the evaluation migration has not run yet the window table is absent
    and ``get_open_window`` degrades to ``None`` (safe pre-migration boot).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
        row = (
            connection.execute(
                select(calibration_window_table)
                .where(calibration_window_table.c.status == "open")
                .order_by(calibration_window_table.c.opened_at_utc.desc())
                .limit(1)
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


__all__ = ["EvaluationCalibrationWindowPort"]
