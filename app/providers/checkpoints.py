"""LangGraph checkpoint provider implementations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver


class InMemoryCheckpointProvider:
    """Process-local LangGraph checkpointer provider."""

    def __init__(self):
        self._checkpointer: Any | None = None

    def create_checkpointer(self) -> Any:
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    def close(self) -> None:
        """Compatibility hook for providers that hold resources."""


class SQLiteCheckpointProvider:
    """SQLite-backed LangGraph checkpointer provider for local durability."""

    def __init__(self, path: str):
        self.path = path
        self._context: AbstractContextManager | None = None
        self._checkpointer: Any | None = None

    def create_checkpointer(self) -> Any:
        if self._checkpointer is None:
            checkpoint_path = Path(self.path)
            if checkpoint_path.parent != Path("."):
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

            from langgraph.checkpoint.sqlite import SqliteSaver

            self._context = SqliteSaver.from_conn_string(str(checkpoint_path))
            self._checkpointer = self._context.__enter__()
            if hasattr(self._checkpointer, "setup"):
                self._checkpointer.setup()
        return self._checkpointer

    def close(self) -> None:
        if self._context is not None:
            self._context.__exit__(None, None, None)
            self._context = None
            self._checkpointer = None


PostgresSaverFactory = Callable[[str], AbstractContextManager]


class PostgresCheckpointProvider:
    """PostgreSQL-backed LangGraph checkpointer provider for multi-instance runtimes."""

    def __init__(
        self,
        dsn: str,
        *,
        saver_factory: PostgresSaverFactory | None = None,
    ):
        self.dsn = dsn
        self.saver_factory = saver_factory or _default_postgres_saver_factory
        self._context: AbstractContextManager | None = None
        self._checkpointer: Any | None = None

    def create_checkpointer(self) -> Any:
        if self._checkpointer is None:
            self._context = self.saver_factory(self.dsn)
            self._checkpointer = self._context.__enter__()
            if hasattr(self._checkpointer, "setup"):
                self._checkpointer.setup()
        return self._checkpointer

    def close(self) -> None:
        if self._context is not None:
            self._context.__exit__(None, None, None)
            self._context = None
            self._checkpointer = None


def _default_postgres_saver_factory(dsn: str) -> AbstractContextManager:
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise RuntimeError(
            "CHECKPOINT_PROVIDER=postgres requires installing the postgres extras, "
            'for example: uv pip install -e ".[postgres]"'
        ) from exc

    return PostgresSaver.from_conn_string(dsn)
