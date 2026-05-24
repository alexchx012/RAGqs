"""Golden dataset loading utilities."""

from __future__ import annotations

from pathlib import Path

from app.evaluation.models import GoldenExample


def load_golden_dataset(path: str | Path) -> list[GoldenExample]:
    """Load UTF-8 JSONL golden examples."""

    dataset_path = Path(path)
    lines = dataset_path.read_text(encoding="utf-8").splitlines()
    examples: list[GoldenExample] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        examples.append(GoldenExample.model_validate_json(stripped))
    return examples
