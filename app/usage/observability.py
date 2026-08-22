"""Structured logging and in-process counters for usage resource controls."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

_LOGGER = logging.getLogger(__name__)


class UsageResourceMetrics:
    """Small deterministic metric snapshot used by local services and tests."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, str], int] = defaultdict(int)
        self.observations: dict[str, list[float]] = defaultdict(list)

    def increment(self, name: str, *, outcome: str = "completed") -> None:
        self.increment_by(name, 1, outcome=outcome)

    def increment_by(self, name: str, value: int, *, outcome: str = "completed") -> None:
        if value < 0:
            raise ValueError("metric increment must be non-negative")
        if value == 0:
            return
        key = (name, outcome)
        self.counters[key] += value
        _LOGGER.info(
            "usage resource metric",
            extra={"metric_name": name, "outcome": outcome, "value": value},
        )

    def observe(self, name: str, value: float) -> None:
        self.observations[name].append(value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": [
                {"name": name, "outcome": outcome, "value": value}
                for (name, outcome), value in sorted(self.counters.items())
            ],
            "observations": {
                name: list(values) for name, values in sorted(self.observations.items())
            },
        }


__all__ = ["UsageResourceMetrics"]
