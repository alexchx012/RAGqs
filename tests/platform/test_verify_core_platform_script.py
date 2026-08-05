from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify-core-platform.ps1"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="requires PowerShell")
def test_verify_script_stops_on_the_first_external_command_failure(tmp_path: Path) -> None:
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_text(
        '@echo off\r\nif /I "%2"=="ruff" exit /b 7\r\nexit /b 0\r\n',
        encoding="ascii",
    )

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
