"""Shared pytest configuration: makes the outbox test helpers importable and
closes every in-memory SQLite test engine at session end.

The outbox/coordination tests build one in-memory SQLite engine per test via
`_helpers.build_engine()`. Without a session-end disposal those engines (and
their underlying sqlite3 connections) are only dropped by the GC mid-run,
emitting ResourceWarnings. This fixture keeps them alive and closes them
cleanly once the session finishes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "outbox"))


@pytest.fixture(scope="session", autouse=True)
def _dispose_outbox_test_engines():
    """Dispose every build_engine() SQLite engine after the whole session."""
    yield
    from _helpers import dispose_all_test_engines

    dispose_all_test_engines()
