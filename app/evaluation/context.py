"""Shared evaluation context helpers."""

from __future__ import annotations

from app.evaluation.models import GoldenExample


def example_space_id(example: GoldenExample) -> str:
    """Return the knowledge-space id configured for one golden example."""

    value = example.metadata.get("spaceId") or example.metadata.get("space_id") or "default"
    return str(value).strip() or "default"
