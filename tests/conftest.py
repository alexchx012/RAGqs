"""Shared pytest configuration: closes every in-memory SQLite test engine at
session end.

The outbox/coordination tests build one in-memory SQLite engine per test via
`tests._support.build_engine()`. Without a session-end disposal those engines
(and their underlying sqlite3 connections) are only dropped by the GC mid-run,
emitting ResourceWarnings. This fixture keeps them alive and closes them
cleanly once the session finishes.
"""

from __future__ import annotations

import pytest

from tests._support import dispose_all_test_engines


@pytest.fixture(scope="session", autouse=True)
def _dispose_outbox_test_engines():
    """Dispose every build_engine() SQLite engine after the whole session."""
    yield
    dispose_all_test_engines()
