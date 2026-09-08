"""Read-only adapter over indexing's generation head/manifest.

The graph never creates, swaps or GCs generations; it only reads the active
generation id and its public-graph component manifest for the availability
read model.
"""

from __future__ import annotations

import inspect
from typing import Any

from sqlalchemy.engine import Connection


class GenerationGraphAvailability:
    """Window over SqlAlchemyIndexingRepository or GenerationManager.

    Repository form: methods accept a ``connection`` keyword. Manager form:
    ``active_generation_id`` is a read-only property and ``get_generation``
    takes no connection. The form is detected statically once, so no
    signature probing happens through caught exceptions.
    """

    def __init__(self, generation_source: Any) -> None:
        self._source = generation_source
        self._connection_aware = callable(
            inspect.getattr_static(type(generation_source), "active_generation_id", None)
        )

    def active_generation_id(self, *, connection: Connection | None = None) -> str:
        if self._connection_aware:
            return str(self._source.active_generation_id(connection=connection))
        return str(self._source.active_generation_id)

    def get_generation(self, generation_id: str, *, connection: Connection | None = None) -> Any:
        if self._connection_aware:
            return self._source.get_generation(generation_id, connection=connection)
        return self._source.get_generation(generation_id)


__all__ = ["GenerationGraphAvailability"]
