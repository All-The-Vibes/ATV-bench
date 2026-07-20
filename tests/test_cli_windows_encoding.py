"""End-to-end checks for legacy Windows console encodings."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _run_with_cp1252(*args: str) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252:strict"
    env["PYTHONUTF8"] = "0"
    pythonpath = str(ROOT / "src")
    if env.get("PYTHONPATH"):
        pythonpath += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from atv_bench.cli import app; app()",
            *args,
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ("--help",),
        ("submit", "--help"),
        ("doctor",),
        ("harnesses",),
        ("games",),
    ],
)
def test_cli_commands_do_not_crash_on_cp1252(args):
    result = _run_with_cp1252(*args)
    stderr = result.stderr.decode("cp1252", errors="replace")

    assert result.returncode == 0, stderr
    assert result.stdout
    assert "UnicodeEncodeError" not in stderr
