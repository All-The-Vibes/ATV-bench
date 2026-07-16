"""Harness adapter contract (design doc: 'Adapter Contract Schema').

A harness adapter takes a game repo + goal + budget and produces a code edit to
the game's bot file, driven fully headless. This module defines the typed
contract and the concrete headless CLI runners used by the spikes.

The contract is intentionally transport-agnostic: a `HarnessAdapter.run` returns
an `AdapterResult` regardless of whether it shelled out to copilot, claude, or a
BYOK provider.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class AdapterStatus(str, enum.Enum):
    OK = "ok"
    NO_EDIT = "no_edit"
    ERROR = "error"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_DENIED = "policy_denied"  # auth ok, org policy blocks (fallback-ladder signal)


@dataclasses.dataclass(frozen=True)
class Budget:
    max_turns: int = 10
    max_seconds: int = 300
    max_tokens: int = 200_000

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AdapterRequest:
    repo_path: str
    goal: str
    model: str = "auto"
    budget: Budget = dataclasses.field(default_factory=Budget)
    bot_file: str = "bot.py"

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


@dataclasses.dataclass(frozen=True)
class Usage:
    tokens: int = 0
    seconds: float = 0.0
    turns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AdapterResult:
    status: AdapterStatus
    diff: str
    log: str
    usage: Usage
    model: str = "unknown"  # underlying model actually used (thesis-integrity tagging)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "diff": self.diff,
            "log": self.log,
            "usage": self.usage.to_dict(),
            "model": self.model,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def git_diff(repo_path: str) -> str:
    """Unified diff of the working tree vs HEAD in repo_path."""
    out = subprocess.run(
        ["git", "-C", repo_path, "diff"],
        capture_output=True,
        text=True,
    )
    return out.stdout


class HarnessAdapter:
    """Base adapter. Subclasses implement `_invoke` to drive their CLI headless."""

    name: str = "base"

    def run(self, req: AdapterRequest) -> AdapterResult:
        raise NotImplementedError

    @staticmethod
    def available() -> bool:
        return False


class ClaudeCodeAdapter(HarnessAdapter):
    """Drives Claude Code headless via `claude -p ... --output-format json`.

    Auth: uses whatever Claude Code is already logged into, or ANTHROPIC_API_KEY.
    """

    name = "claude-code"

    @staticmethod
    def available() -> bool:
        return shutil.which("claude") is not None

    def run(self, req: AdapterRequest) -> AdapterResult:
        start = time.time()
        cmd = [
            "claude",
            "-p",
            req.goal,
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "json",
        ]
        if req.model and req.model != "auto":
            cmd += ["--model", req.model]
        try:
            proc = subprocess.run(
                cmd,
                cwd=req.repo_path,
                capture_output=True,
                text=True,
                timeout=req.budget.max_seconds,
            )
        except subprocess.TimeoutExpired:
            return AdapterResult(
                status=AdapterStatus.TIMEOUT,
                diff="",
                log="claude timed out",
                usage=Usage(seconds=time.time() - start),
            )

        elapsed = time.time() - start
        diff = git_diff(req.repo_path)
        model_used = "unknown"
        tokens = 0
        try:
            payload = json.loads(proc.stdout)
            mu = payload.get("modelUsage") or {}
            if mu:
                model_used = next(iter(mu))
                stats = mu[model_used]
                tokens = int(stats.get("inputTokens", 0)) + int(stats.get("outputTokens", 0))
        except (json.JSONDecodeError, ValueError, StopIteration):
            pass

        if proc.returncode != 0:
            return AdapterResult(
                status=AdapterStatus.ERROR,
                diff=diff,
                log=proc.stderr[-2000:],
                usage=Usage(seconds=elapsed),
                model=model_used,
            )
        status = AdapterStatus.OK if diff.strip() else AdapterStatus.NO_EDIT
        return AdapterResult(
            status=status,
            diff=diff,
            log=proc.stdout[-2000:],
            usage=Usage(tokens=tokens, seconds=elapsed, turns=1),
            model=model_used,
        )


class CopilotCliAdapter(HarnessAdapter):
    """Drives GitHub Copilot CLI headless via `copilot -p ... --allow-all-tools`.

    Auth (headless): COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN env var, or a
    prior `copilot login`. This is the design's device-flow-token path.

    Detects the org-policy-denied case explicitly so the runner can fall back
    (fallback ladder) instead of silently scoring a forfeit.
    """

    name = "copilot-cli"

    @staticmethod
    def available() -> bool:
        return shutil.which("copilot") is not None

    def run(self, req: AdapterRequest) -> AdapterResult:
        start = time.time()
        cmd = [
            "copilot",
            "-p",
            req.goal,
            "--allow-all-tools",
            "--no-ask-user",
            "-s",
        ]
        if req.model and req.model != "auto":
            cmd += ["--model", req.model]
        env = dict(os.environ)
        try:
            proc = subprocess.run(
                cmd,
                cwd=req.repo_path,
                capture_output=True,
                text=True,
                timeout=req.budget.max_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return AdapterResult(
                status=AdapterStatus.TIMEOUT,
                diff="",
                log="copilot timed out",
                usage=Usage(seconds=time.time() - start),
            )
        elapsed = time.time() - start
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "Access denied by policy" in combined:
            return AdapterResult(
                status=AdapterStatus.POLICY_DENIED,
                diff="",
                log=combined[-2000:],
                usage=Usage(seconds=elapsed),
            )
        diff = git_diff(req.repo_path)
        if proc.returncode != 0 and not diff.strip():
            return AdapterResult(
                status=AdapterStatus.ERROR,
                diff="",
                log=combined[-2000:],
                usage=Usage(seconds=elapsed),
            )
        status = AdapterStatus.OK if diff.strip() else AdapterStatus.NO_EDIT
        return AdapterResult(
            status=status,
            diff=diff,
            log=proc.stdout[-2000:],
            usage=Usage(seconds=elapsed, turns=1),
            model=req.model,
        )


ADAPTERS: dict[str, type[HarnessAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CopilotCliAdapter.name: CopilotCliAdapter,
}
