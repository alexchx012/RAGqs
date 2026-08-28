from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify-core-platform.ps1"


def _write_fake_python(tmp_path: Path) -> Path:
    """首个命令（-m ruff）退出 7、其余退出 0 的假 Python，按平台可被执行。"""
    if os.name == "nt":
        fake_python = tmp_path / "fake-python.cmd"
        fake_python.write_text(
            '@echo off\r\nif /I "%2"=="ruff" exit /b 7\r\nexit /b 0\r\n',
            encoding="ascii",
        )
        return fake_python
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        '#!/bin/sh\nif [ "$2" = "ruff" ]; then\n    exit 7\nfi\nexit 0\n',
        encoding="ascii",
    )
    fake_python.chmod(0o755)
    return fake_python


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="requires PowerShell")
def test_verify_script_stops_on_the_first_external_command_failure(tmp_path: Path) -> None:
    fake_python = _write_fake_python(tmp_path)

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VERIFY_SCRIPT),
            "-Python",
            str(fake_python),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 7
