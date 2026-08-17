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
