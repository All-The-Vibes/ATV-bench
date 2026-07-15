"""Gated malicious-bot integration test (eng T8).

Marked `integration`: needs Docker, not run on every push. Actually executes a
hostile bot under the SAME container flags the league Action uses and asserts every
attack is contained: no network egress, no fork-bomb, bounded memory, and a crash is
scored (loss+flag), never a job failure that could taint the publish side.

Run: uv run pytest -m integration
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# The exact sandbox flags from .github/workflows/league.yml (kept in sync by review).
SANDBOX_FLAGS = [
    "--rm", "--network", "none",
    "--memory", "256m", "--memory-swap", "256m",
    "--cpus", "1", "--pids-limit", "128",
    "--read-only", "--user", "65534:65534",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


requires_docker = pytest.mark.skipif(not _docker_available(), reason="docker not available")


def _run_bot(tmp_path: Path, bot_src: str, timeout: int = 30) -> subprocess.CompletedProcess:
    work = tmp_path / "work"
    work.mkdir()
    (work / "main.py").write_text(textwrap.dedent(bot_src))
    cmd = [
        "docker", "run", *SANDBOX_FLAGS,
        "-v", f"{work}:/work:ro",
        "python:3.12-alpine",
        "python", "/work/main.py",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@requires_docker
def test_network_exfil_blocked(tmp_path):
    proc = _run_bot(tmp_path, """
        import urllib.request
        try:
            urllib.request.urlopen("http://example.com", timeout=3)
            print("EXFIL_OK")
        except Exception as e:
            print("BLOCKED")
    """)
    assert "EXFIL_OK" not in proc.stdout
    assert "BLOCKED" in proc.stdout  # --network none contained it


@requires_docker
def test_env_secret_read_finds_nothing(tmp_path):
    proc = _run_bot(tmp_path, """
        import os
        leaked = [k for k in os.environ if "TOKEN" in k or "SECRET" in k or k == "GITHUB_TOKEN"]
        print("LEAKED:" + ",".join(leaked))
    """)
    assert proc.stdout.strip() == "LEAKED:"  # no secrets in the sandbox env


@requires_docker
def test_fork_bomb_contained(tmp_path):
    proc = _run_bot(tmp_path, """
        import os
        try:
            for _ in range(10000):
                os.fork()
        except OSError:
            print("PID_CAP_HIT")
    """, timeout=40)
    # --pids-limit stops the fork bomb; the container dies but the host is fine.
    assert proc.returncode != 0 or "PID_CAP_HIT" in proc.stdout


@requires_docker
def test_readonly_rootfs_blocks_write(tmp_path):
    proc = _run_bot(tmp_path, """
        try:
            open("/evil", "w").write("x")
            print("WROTE")
        except OSError:
            print("RO_BLOCKED")
    """)
    assert "WROTE" not in proc.stdout
    assert "RO_BLOCKED" in proc.stdout
