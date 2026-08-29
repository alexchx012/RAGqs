from __future__ import annotations

import ast
from pathlib import Path

from app.platform.error_codes import REGISTERED_ERROR_CODES


def test_literal_platform_errors_are_registered() -> None:
    used: set[str] = set()
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name) and node.func.id == "PlatformError"
            ) or not node.args:
                continue
            code = node.args[0]
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                used.add(code.value)
    assert used <= REGISTERED_ERROR_CODES
