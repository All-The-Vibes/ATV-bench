"""Shared preflight checks (DX-4).

`doctor` and `run`'s preflight BOTH call these — one source of truth for "is the
environment ready to run a live match?" `run` aggregates ALL failures into one
report (never one-at-a-time) and maps the first blocking failure to its exit code;
`doctor` prints the full readiness report and always exits 0.
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess


@dataclasses.dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def check_cli_on_path(binary: str) -> CheckResult:
    path = shutil.which(binary)
    if path:
        return CheckResult(binary, True, f"found at {path}")
    return CheckResult(
        binary, False, f"{binary} not found on PATH",
        fix=f"install the {binary} CLI and ensure it is on your PATH",
    )


def check_cli_authenticated(binary: str, probe_args: list[str]) -> CheckResult:
    """Best-effort auth check: run a cheap probe command and read its exit code."""
    if not shutil.which(binary):
        return CheckResult(
            f"{binary}-auth", False, f"{binary} not installed",
            fix=f"install the {binary} CLI first",
        )
    try:
        proc = subprocess.run(
            [binary, *probe_args], capture_output=True, timeout=15, text=True
        )
        ok = proc.returncode == 0
        return CheckResult(
            f"{binary}-auth", ok,
            "authenticated" if ok else f"{binary} not authenticated",
            fix="" if ok else f"authenticate the {binary} CLI (see its login command)",
        )
    except Exception as exc:  # timeout / spawn failure
        return CheckResult(
            f"{binary}-auth", False, f"{binary} auth probe failed: {exc}",
            fix=f"check the {binary} CLI works interactively",
        )


def check_docker() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            "docker", False, "docker not installed",
            fix="install Docker and start the daemon (https://docs.docker.com/get-docker/)",
        )
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=15, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return CheckResult("docker", True, f"daemon running (v{proc.stdout.strip()})")
        return CheckResult(
            "docker", False, "docker installed but daemon not reachable",
            fix="start the Docker daemon (e.g. `sudo systemctl start docker`)",
        )
    except Exception as exc:
        return CheckResult(
            "docker", False, f"docker check failed: {exc}",
            fix="ensure the Docker daemon is running",
        )


def check_codeclash() -> CheckResult:
    from atv_bench.codeclash_env import codeclash_available

    # Same recovery text whether or not it is currently importable, so the
    # remediation is discoverable (and correct for a tool-installed user).
    fix = ("reinstall the tool to pull the git dep: "
           "`uv tool install --reinstall --from git+https://github.com/All-The-Vibes/ATV-bench atv-bench` "
           "(or `uv tool upgrade atv-bench`); from a source checkout run "
           "`git submodule update --init` && `uv pip install -e '.[run]'`, "
           "or run `atv-bench doctor` for a full prerequisite report")
    if codeclash_available():
        return CheckResult("codeclash", True, "importable at pinned version", fix=fix)
    return CheckResult(
        "codeclash", False, "vendored CodeClash not importable", fix=fix,
    )


def aggregate(checks: list[CheckResult]) -> tuple[bool, list[CheckResult]]:
    """Return (all_ok, [failures]). Reports EVERY failure at once (DX-4)."""
    failures = [c for c in checks if not c.ok]
    return (not failures, failures)
