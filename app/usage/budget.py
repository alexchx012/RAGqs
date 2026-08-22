"""Persistent generation resource budget gate."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.platform.errors import PlatformError

from ._sql import _insert_do_nothing
from .ledger import Clock
from .observability import UsageResourceMetrics
from .schema import generation_budget_meter_table, generation_budget_reservation_table

BUDGET_POLICY_VERSION = "chat-rag-budget-v2"
_EFFORTS = ("quick", "think", "deep")
_UPGRADES = {"quick": "think", "think": "deep"}
_DEFAULT_EFFORT_LIMITS = {
    "quick": (1, 20, 12000, 5),
    "think": (4, 60, 24000, 7),
    "deep": (10, 180, 48000, 9),
}


@dataclass(frozen=True, slots=True)
class BudgetEffortPolicy:
    max_rag_calls: int
    max_wall_seconds: int
    max_total_tokens: int
    max_estimated_cost_amount: Decimal
    candidate_document_limit: int


@dataclass(frozen=True, slots=True)
class BudgetMeterPolicy:
    efforts: dict[str, BudgetEffortPolicy]
    price_version_id: str
    currency_code: str
    cost_estimator: Callable[[str, int], Decimal] | None = None
    version: str = BUDGET_POLICY_VERSION

    @classmethod
    def production(
        cls,
        *,
        efforts: dict[str, BudgetEffortPolicy],
        price_version_id: str,
        currency_code: str,
        cost_estimator: Callable[[str, int], Decimal] | None,
    ) -> BudgetMeterPolicy:
        policy = cls(
            efforts=efforts,
            price_version_id=price_version_id,
            currency_code=currency_code,
            cost_estimator=cost_estimator,
        )
        policy.validate(production=True)
        return policy

    @classmethod
    def configured(
        cls,
        *,
        price_version_id: str,
        currency_code: str,
        max_estimated_cost_amounts: dict[str, Decimal],
        cost_estimator: Callable[[str, int], Decimal],
    ) -> BudgetMeterPolicy:
        efforts = {
            effort: BudgetEffortPolicy(
                max_rag_calls=limits[0],
                max_wall_seconds=limits[1],
                max_total_tokens=limits[2],
                max_estimated_cost_amount=max_estimated_cost_amounts[effort],
                candidate_document_limit=limits[3],
            )
            for effort, limits in _DEFAULT_EFFORT_LIMITS.items()
        }
        policy = cls(
            efforts=efforts,
            price_version_id=price_version_id,
            currency_code=currency_code,
            cost_estimator=cost_estimator,
        )
        policy.validate(production=True)
        return policy

    def validate(self, *, production: bool = False) -> None:
        if not self.price_version_id or not self.currency_code:
            raise PlatformError(
                "budget_policy_invalid", "Budget policy requires price and currency", {}, 422
            )
        for effort in _EFFORTS:
            item = self.efforts.get(effort)
            if item is None:
                raise PlatformError(
                    "budget_policy_invalid",
                    f"Budget policy is missing effort {effort}",
                    {},
                    422,
                )
            if (
                min(
                    item.max_rag_calls,
                    item.max_wall_seconds,
                    item.max_total_tokens,
                    item.candidate_document_limit,
                )
                <= 0
            ):
                raise PlatformError(
                    "budget_policy_invalid", "Budget limits must be positive", {}, 422
                )
            if item.max_estimated_cost_amount <= 0:
                raise PlatformError(
                    "budget_policy_invalid", "Budget cost limit must be positive", {}, 422
                )
        if production and not callable(self.cost_estimator):
            raise PlatformError(
                "budget_policy_invalid",
                "Production budget policy requires a cost estimator",
                {},
                422,
            )


@dataclass(frozen=True, slots=True)
class BudgetMeterSnapshot:
    generation_id: str
    effort_level: str
    status: str
    deadline_at_utc: datetime
    max_rag_calls: int
    max_total_tokens: int
    max_estimated_cost_amount: Decimal
    candidate_document_limit: int
    rag_calls_used: int
    total_tokens_used: int
    reserved_tokens: int
    settled_cost_amount: Decimal
    reserved_cost_amount: Decimal


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    generation_id: str
    operation_kind: str
    estimated_tokens: int
    estimated_cost_amount: Decimal
    is_rag: bool
    status: str


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlatformError("validation_error", f"{name} must be a non-negative integer", {}, 422)
    return value


def _money(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise PlatformError("validation_error", f"{name} must be a non-negative Decimal", {}, 422)
    return value


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BudgetMeterService:
    """Reserve before egress and settle only from returned usage facts."""

    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        policy: BudgetMeterPolicy,
        metrics: UsageResourceMetrics | None = None,
    ) -> None:
        policy.validate()
        self.engine = engine
        self.clock = clock
        self.policy = policy
        self.metrics = metrics or UsageResourceMetrics()

    def ensure_meter(
        self,
        *,
        generation_id: str,
        effort_level: str,
        deadline_at_utc: datetime,
    ) -> BudgetMeterSnapshot:
        with self.engine.begin() as connection:
            return self.ensure_meter_in_transaction(
                connection,
                generation_id=generation_id,
                effort_level=effort_level,
                deadline_at_utc=deadline_at_utc,
            )

    def ensure_meter_in_transaction(
        self,
        connection,
        *,
        generation_id: str,
        effort_level: str,
        deadline_at_utc: datetime,
    ) -> BudgetMeterSnapshot:
        if effort_level not in _EFFORTS:
            raise PlatformError("validation_error", "effort_level is invalid", {}, 422)
        effort = self.policy.efforts[effort_level]
        now = _utc(self.clock.now_utc(connection))
        row = self._locked_meter(connection, generation_id)
        if row is not None:
            return self._snapshot(row)
        wall_deadline = now + timedelta(seconds=effort.max_wall_seconds)
        effective_deadline = min(_utc(deadline_at_utc), wall_deadline)
        values = {
            "generation_budget_meter_id": f"gbm_{secrets.token_urlsafe(9)}",
            "generation_id": generation_id,
            "policy_version": self.policy.version,
            "requested_effort_level": effort_level,
            "effort_level": effort_level,
            "price_version_id": self.policy.price_version_id,
            "currency_code": self.policy.currency_code,
            "deadline_at_utc": effective_deadline,
            "max_rag_calls": effort.max_rag_calls,
            "max_wall_seconds": effort.max_wall_seconds,
            "max_total_tokens": effort.max_total_tokens,
            "max_estimated_cost_amount": effort.max_estimated_cost_amount,
            "candidate_document_limit": effort.candidate_document_limit,
            "rag_calls_used": 0,
            "total_tokens_used": 0,
            "reserved_tokens": 0,
            "settled_cost_amount": Decimal("0"),
            "reserved_cost_amount": Decimal("0"),
            "upgrade_count": 0,
            "status": "active",
            "exhausted_reason": None,
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        inserted = _insert_do_nothing(
            connection,
            generation_budget_meter_table,
            values,
            ["generation_id"],
        )
        if not inserted:
            row = self._locked_meter(connection, generation_id)
            if row is None:
                raise PlatformError(
                    "budget_invariant",
                    "Budget meter disappeared after concurrent creation",
                    {},
                    500,
                )
            return self._snapshot(row)
        self.metrics.increment("generation_budget_meter_created")
        return self._snapshot(values)

    def meter(self, *, generation_id: str) -> BudgetMeterSnapshot:
        with self.engine.connect() as connection:
            row = self._locked_meter(connection, generation_id)
            if row is None:
                raise PlatformError("budget_meter_not_found", "Budget meter not found", {}, 404)
            return self._snapshot(row)

    def estimate_cost(self, operation_kind: str, tokens: int) -> Decimal:
        if not callable(self.policy.cost_estimator):
            raise PlatformError("cost_unavailable", "No price estimator is available", {}, 422)
        return _money(
            self.policy.cost_estimator(operation_kind, _positive_int(tokens, "tokens")),
            "estimated cost",
        )

    def mark_unknown(self, *, generation_id: str, reservation_id: str) -> None:
        """Fail closed when an already-dispatched provider result is unknown."""

        with self.engine.begin() as connection:
            meter = self._locked_meter(connection, generation_id)
            reservation = self._reservation_row(connection, reservation_id)
            if meter is None or reservation is None:
                raise PlatformError(
                    "budget_reservation_not_found", "Reservation not found", {}, 404
                )
            now = self.clock.now_utc(connection)
            if reservation["status"] == "reserved":
                connection.execute(
                    generation_budget_reservation_table.update()
                    .where(generation_budget_reservation_table.c.reservation_id == reservation_id)
                    .values(status="released", settled_at_utc=now)
                )
                connection.execute(
                    generation_budget_meter_table.update()
                    .where(generation_budget_meter_table.c.generation_id == generation_id)
                    .values(
                        reserved_tokens=int(meter["reserved_tokens"])
                        - int(reservation["estimated_tokens"]),
                        reserved_cost_amount=Decimal(str(meter["reserved_cost_amount"]))
                        - Decimal(str(reservation["estimated_cost_amount"])),
                        status="exhausted",
                        exhausted_reason="provider_result_unknown",
                        updated_at_utc=now,
                    )
                )
                self.metrics.increment("generation_budget_gate", outcome="provider_result_unknown")

    def reserve(
        self,
        *,
        generation_id: str,
        reservation_id: str,
        operation_kind: str,
        estimated_tokens: int,
        estimated_cost: Decimal | None,
        is_rag: bool,
        request_fingerprint: str,
    ) -> BudgetReservation:
        if not reservation_id or not operation_kind or not request_fingerprint:
            raise PlatformError("validation_error", "reservation identity is required", {}, 422)
        tokens = _positive_int(estimated_tokens, "estimated_tokens")
        if estimated_cost is None:
            raise PlatformError(
                "cost_unavailable", "Provider call cost cannot be estimated", {}, 422
            )
        cost = _money(estimated_cost, "estimated_cost")
        with self.engine.begin() as connection:
            meter = self._locked_meter(connection, generation_id)
            if meter is None:
                raise PlatformError("budget_meter_not_found", "Budget meter not found", {}, 404)
            existing = (
                connection.execute(
                    select(generation_budget_reservation_table).where(
                        (generation_budget_reservation_table.c.generation_id == generation_id)
                        & (
                            generation_budget_reservation_table.c.request_fingerprint
                            == request_fingerprint
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            replay = self._reservation_row(connection, reservation_id)
            if replay is not None or existing is not None:
                candidate = replay or existing
                same = (
                    candidate is not None
                    and candidate["reservation_id"] == reservation_id
                    and candidate["request_fingerprint"] == request_fingerprint
                    and candidate["operation_kind"] == operation_kind
                    and candidate["estimated_tokens"] == tokens
                    and Decimal(str(candidate["estimated_cost_amount"])) == cost
                    and bool(candidate["is_rag"]) == is_rag
                )
                if not same:
                    raise PlatformError(
                        "budget_reservation_conflict",
                        "Reservation identity already exists with different input",
                        {},
                        409,
                    )
                return self._reservation(candidate)

            now = self.clock.now_utc(connection)
            if meter["status"] != "active":
                self._reject(meter, "budget_exhausted")
            if _utc(now) >= _utc(meter["deadline_at_utc"]):
                self._mark_exhausted(connection, meter, "wall_clock")
                self._reject(meter, "budget_exhausted")
            if is_rag and meter["rag_calls_used"] >= meter["max_rag_calls"]:
                self._mark_exhausted(connection, meter, "rag_calls")
                self._reject(meter, "budget_exhausted")
            token_available = (
                int(meter["max_total_tokens"])
                - int(meter["total_tokens_used"])
                - int(meter["reserved_tokens"])
            )
            if tokens > token_available:
                self._mark_exhausted(connection, meter, "total_tokens")
                self._reject(meter, "budget_exhausted")
            cost_available = (
                Decimal(str(meter["max_estimated_cost_amount"]))
                - Decimal(str(meter["settled_cost_amount"]))
                - Decimal(str(meter["reserved_cost_amount"]))
            )
            if cost > cost_available:
                self._mark_exhausted(connection, meter, "cost")
                self._reject(meter, "budget_exhausted")

            reservation_values = {
                "reservation_id": reservation_id,
                "generation_id": generation_id,
                "operation_kind": operation_kind,
                "request_fingerprint": request_fingerprint,
                "estimated_tokens": tokens,
                "estimated_cost_amount": cost,
                "is_rag": int(is_rag),
                "status": "reserved",
                "created_at_utc": now,
            }
            inserted = _insert_do_nothing(
                connection,
                generation_budget_reservation_table,
                reservation_values,
                ["reservation_id"],
            )
            if not inserted:
                replay = self._reservation_row(connection, reservation_id)
                if replay is None:
                    raise PlatformError(
                        "budget_invariant",
                        "Budget reservation disappeared after concurrent insert",
                        {},
                        500,
                    )
                return self._reservation(replay)
            connection.execute(
                generation_budget_meter_table.update()
                .where(generation_budget_meter_table.c.generation_id == generation_id)
                .values(
                    rag_calls_used=int(meter["rag_calls_used"]) + int(is_rag),
                    reserved_tokens=int(meter["reserved_tokens"]) + tokens,
                    reserved_cost_amount=Decimal(str(meter["reserved_cost_amount"])) + cost,
                    updated_at_utc=now,
                )
            )
            self.metrics.increment("generation_budget_reservation", outcome="created")
            return self._reservation(reservation_values)

    def settle(
        self,
        *,
        generation_id: str,
        reservation_id: str,
        actual_tokens: int,
        actual_cost: Decimal,
    ) -> BudgetMeterSnapshot:
        tokens = _positive_int(actual_tokens, "actual_tokens")
        cost = _money(actual_cost, "actual_cost")
        with self.engine.begin() as connection:
            meter = self._locked_meter(connection, generation_id)
            reservation = self._reservation_row(connection, reservation_id)
            if meter is None or reservation is None:
                raise PlatformError(
                    "budget_reservation_not_found", "Reservation not found", {}, 404
                )
            if reservation["status"] == "settled":
                return self._snapshot(meter)
            if reservation["status"] != "reserved":
                raise PlatformError(
                    "budget_reservation_terminal", "Reservation is already terminal", {}, 409
                )
            now = self.clock.now_utc(connection)
            used_tokens = int(meter["total_tokens_used"]) + tokens
            settled_cost = Decimal(str(meter["settled_cost_amount"])) + cost
            exhausted = (
                used_tokens > int(meter["max_total_tokens"])
                or settled_cost > Decimal(str(meter["max_estimated_cost_amount"]))
                or cost > Decimal(str(reservation["estimated_cost_amount"]))
            )
            connection.execute(
                generation_budget_reservation_table.update()
                .where(generation_budget_reservation_table.c.reservation_id == reservation_id)
                .values(
                    status="settled",
                    actual_tokens=tokens,
                    actual_cost_amount=cost,
                    settled_at_utc=now,
                )
            )
            connection.execute(
                generation_budget_meter_table.update()
                .where(generation_budget_meter_table.c.generation_id == generation_id)
                .values(
                    total_tokens_used=used_tokens,
                    settled_cost_amount=settled_cost,
                    reserved_tokens=int(meter["reserved_tokens"])
                    - int(reservation["estimated_tokens"]),
                    reserved_cost_amount=Decimal(str(meter["reserved_cost_amount"]))
                    - Decimal(str(reservation["estimated_cost_amount"])),
                    status="exhausted" if exhausted else meter["status"],
                    exhausted_reason=(
                        "actual_over_reservation" if exhausted else meter["exhausted_reason"]
                    ),
                    updated_at_utc=now,
                )
            )
            updated = self._locked_meter(connection, generation_id)
            assert updated is not None
            self.metrics.increment(
                "generation_budget_reservation",
                outcome="actual_over_reservation" if exhausted else "settled",
            )
            return self._snapshot(updated)

    def release(self, *, generation_id: str, reservation_id: str) -> None:
        with self.engine.begin() as connection:
            meter = self._locked_meter(connection, generation_id)
            reservation = self._reservation_row(connection, reservation_id)
            if meter is None or reservation is None:
                raise PlatformError(
                    "budget_reservation_not_found", "Reservation not found", {}, 404
                )
            if reservation["status"] != "reserved":
                return
            now = self.clock.now_utc(connection)
            connection.execute(
                generation_budget_reservation_table.update()
                .where(generation_budget_reservation_table.c.reservation_id == reservation_id)
                .values(status="released", settled_at_utc=now)
            )
            connection.execute(
                generation_budget_meter_table.update()
                .where(generation_budget_meter_table.c.generation_id == generation_id)
                .values(
                    rag_calls_used=int(meter["rag_calls_used"]) - int(reservation["is_rag"]),
                    reserved_tokens=int(meter["reserved_tokens"])
                    - int(reservation["estimated_tokens"]),
                    reserved_cost_amount=Decimal(str(meter["reserved_cost_amount"]))
                    - Decimal(str(reservation["estimated_cost_amount"])),
                    updated_at_utc=now,
                )
            )

    def upgrade(
        self,
        *,
        generation_id: str,
        next_step_tokens: int = 1,
        next_step_cost: Decimal = Decimal("0.0000000001"),
        next_step_is_rag: bool = True,
    ) -> str | None:
        with self.engine.begin() as connection:
            meter = self._locked_meter(connection, generation_id)
            if meter is None:
                raise PlatformError("budget_meter_not_found", "Budget meter not found", {}, 404)
            if int(meter["upgrade_count"]) >= 1 or meter["status"] != "active":
                return None
            target = _UPGRADES.get(str(meter["effort_level"]))
            if target is None:
                return None
            effort = self.policy.efforts[target]
            next_tokens = _positive_int(next_step_tokens, "next_step_tokens")
            next_cost = _money(next_step_cost, "next_step_cost")
            rag_available = (
                int(meter["rag_calls_used"]) < effort.max_rag_calls if next_step_is_rag else True
            )
            has_budget = (
                rag_available
                and int(meter["total_tokens_used"]) + int(meter["reserved_tokens"]) + next_tokens
                <= effort.max_total_tokens
                and Decimal(str(meter["settled_cost_amount"]))
                + Decimal(str(meter["reserved_cost_amount"]))
                + next_cost
                <= effort.max_estimated_cost_amount
                and _utc(self.clock.now_utc(connection)) < _utc(meter["deadline_at_utc"])
            )
            if not has_budget:
                return None
            now = self.clock.now_utc(connection)
            connection.execute(
                generation_budget_meter_table.update()
                .where(generation_budget_meter_table.c.generation_id == generation_id)
                .values(
                    effort_level=target,
                    max_rag_calls=effort.max_rag_calls,
                    max_wall_seconds=effort.max_wall_seconds,
                    max_total_tokens=effort.max_total_tokens,
                    max_estimated_cost_amount=effort.max_estimated_cost_amount,
                    candidate_document_limit=effort.candidate_document_limit,
                    upgrade_count=1,
                    updated_at_utc=now,
                )
            )
            self.metrics.increment("generation_budget_upgrade")
            return target

    def _locked_meter(self, connection, generation_id: str):
        return (
            connection.execute(
                select(generation_budget_meter_table)
                .where(generation_budget_meter_table.c.generation_id == generation_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )

    def _reservation_row(self, connection, reservation_id: str):
        return (
            connection.execute(
                select(generation_budget_reservation_table).where(
                    generation_budget_reservation_table.c.reservation_id == reservation_id
                )
            )
            .mappings()
            .one_or_none()
        )

    def _mark_exhausted(self, connection, meter, reason: str) -> None:
        now = self.clock.now_utc(connection)
        connection.execute(
            generation_budget_meter_table.update()
            .where(
                generation_budget_meter_table.c.generation_budget_meter_id
                == meter["generation_budget_meter_id"]
            )
            .values(status="exhausted", exhausted_reason=reason, updated_at_utc=now)
        )
        self.metrics.increment("generation_budget_gate", outcome=reason)
        meter = {**meter, "status": "exhausted", "exhausted_reason": reason}

    def _reject(self, meter, code: str) -> None:
        raise PlatformError(
            code,
            "The generation resource budget does not allow another provider call",
            {"reason": meter["exhausted_reason"]},
            429,
        )

    @staticmethod
    def _snapshot(row) -> BudgetMeterSnapshot:
        return BudgetMeterSnapshot(
            generation_id=str(row["generation_id"]),
            effort_level=str(row["effort_level"]),
            status=str(row["status"]),
            deadline_at_utc=_utc(row["deadline_at_utc"]),
            max_rag_calls=int(row["max_rag_calls"]),
            max_total_tokens=int(row["max_total_tokens"]),
            max_estimated_cost_amount=Decimal(str(row["max_estimated_cost_amount"])),
            candidate_document_limit=int(row["candidate_document_limit"]),
            rag_calls_used=int(row["rag_calls_used"]),
            total_tokens_used=int(row["total_tokens_used"]),
            reserved_tokens=int(row["reserved_tokens"]),
            settled_cost_amount=Decimal(str(row["settled_cost_amount"])),
            reserved_cost_amount=Decimal(str(row["reserved_cost_amount"])),
        )

    @staticmethod
    def _reservation(row) -> BudgetReservation:
        return BudgetReservation(
            reservation_id=str(row["reservation_id"]),
            generation_id=str(row["generation_id"]),
            operation_kind=str(row["operation_kind"]),
            estimated_tokens=int(row["estimated_tokens"]),
            estimated_cost_amount=Decimal(str(row["estimated_cost_amount"])),
            is_rag=bool(row["is_rag"]),
            status=str(row["status"]),
        )


__all__ = [
    "BudgetEffortPolicy",
    "BudgetMeterPolicy",
    "BudgetMeterService",
    "BudgetMeterSnapshot",
    "BudgetReservation",
]
