"""Read-only adapter over indexing's generation head/manifest.

The graph never creates, swaps or GCs generations; it only reads the active
generation id and its public-graph component manifest for the availability
read model.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Connection


class GenerationGraphAvailability:
    """Duck-typed window over SqlAlchemyIndexingRepository or GenerationManager."""

    def __init__(self, generation_source: Any) -> None:
        self._source = generation_source

    def active_generation_id(self, *, connection: Connection | None = None) -> str:
        source = self._source
        if connection is not None and hasattr(source, "active_generation_id"):
            try:
                return str(source.active_generation_id(connection=connection))
            except TypeError:
                pass
        return str(source.active_generation_id)

    def get_generation(self, generation_id: str, *, connection: Connection | None = None) -> Any:
        source = self._source
        if connection is not None:
            try:
                return source.get_generation(generation_id, connection=connection)
            except TypeError:
                pass
        return source.get_generation(generation_id)


__all__ = ["GenerationGraphAvailability"]
