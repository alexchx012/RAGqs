from __future__ import annotations

from pathlib import Path
from tomllib import loads

ROOT = Path(__file__).resolve().parents[2]


def test_chat_maintenance_console_script_is_registered() -> None:
    pyproject = loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["ragqs-chat-maintenance"] == "app.chat.maintenance:main"


def test_operations_documentation_lists_chat_maintenance() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    assert "ragqs-chat-maintenance" in operations


def test_worker_scheduling_console_scripts_and_operations_are_registered() -> None:
    pyproject = loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    assert pyproject["project"]["scripts"]["ragqs-ingestion-worker"] == "app.documents.worker:main"
    assert (
        pyproject["project"]["scripts"]["ragqs-documents-maintenance"]
        == "app.documents.maintenance:main"
    )
    assert "ragqs-ingestion-worker" in operations
    assert "ragqs-documents-maintenance" in operations
