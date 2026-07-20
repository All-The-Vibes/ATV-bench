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

from atv_bench.adapters.snapshot import capture_diff


class AdapterStatus(str, enum.Enum):
    OK = "ok"
    EDITED = "edited"  # union-derived: harness produced a real edit (committed/staged/unstaged/untracked)
    NO_EDIT = "no_edit"
    ERROR = "error"
    TIMEOUT = "timeout"
    CRASH = "crash"  # subprocess died by signal / uncaught exception (fit-excluded)
    MALFORMED = "malformed"  # turn output unparseable / structurally invalid (fit-excluded)
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_DENIED = "policy_denied"  # auth ok, org policy blocks (fallback-ladder signal)


# Outcomes that must be dropped from the rating fit and logged to unknown[]
# (they are not scoreable wins/draws/losses — the harness never produced a valid turn).
FIT_EXCLUDED_STATUSES = frozenset(
    {AdapterStatus.CRASH, AdapterStatus.TIMEOUT, AdapterStatus.MALFORMED}
)


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

    @property
    def fit_exclude(self) -> bool:
        """True for CRASH/TIMEOUT/MALFORMED — dropped from the rating fit, logged to unknown[]."""
        return self.status in FIT_EXCLUDED_STATUSES

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


def _head_sha(repo_path: str) -> str | None:
    """Record HEAD at start-of-run so status can be derived from the base..HEAD union.

    Returns None if the repo has no commits / isn't a git repo (fall back to `git diff`).
    """
    proc = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def derive_status(repo_path: str, base: str) -> AdapterStatus:
    """Authoritative edit/no-edit decision via the snapshot UNION, not plain `git diff`.

    Reflects base..HEAD ∪ staged ∪ working-tree ∪ untracked (via snapshot.capture_diff),
    so a harness that COMMITS its edit (clean working tree) is EDITED, never a false
    NO_EDIT forfeit (ENG-A). Genuinely empty union ⇒ NO_EDIT.
    """
    from atv_bench.adapters.snapshot import capture_diff

    union = capture_diff(Path(repo_path), base)
    return AdapterStatus.EDITED if union.strip() else AdapterStatus.NO_EDIT


def classify_outcome(
    *,
    returncode: int | None = None,
    crashed: bool = False,
    timed_out: bool = False,
    malformed: bool = False,
) -> AdapterStatus:
    """Map a non-win failure mode to a distinct, fit-excluded status.

    Priority: timeout > crash > malformed > (returncode<0 ⇒ crash) > ERROR.
    These are separate from win/draw/loss and from NO_EDIT so the rating engine can
    drop them (fit_exclude) and log to unknown[].
    """
    if timed_out:
        return AdapterStatus.TIMEOUT
    if crashed:
        return AdapterStatus.CRASH
    if malformed:
        return AdapterStatus.MALFORMED
    if returncode is not None and returncode < 0:
        return AdapterStatus.CRASH  # killed by signal
    return AdapterStatus.ERROR


def parse_copilot_model(jsonl: str) -> str:
    """Parse the REAL model Copilot invoked from its `--output-format json` (JSONL).

    Model-tag integrity (Eng Decision #5): the tag must reflect what the harness
    actually ran, never the input `--model` string. gap #15 asked whether Copilot
    exposes a machine-readable model at all — it does: `assistant.message` events
    carry `data.model`, and `session.usage_checkpoint` carries
    `data.modelCacheState[].modelId`. We read those, in that priority order.

    Returns the parsed model id, or "unknown" if none is machine-readable — NEVER
    an echoed input like "auto".
    """
    message_model: str | None = None
    checkpoint_model: str | None = None
    for line in jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        data = evt.get("data")
        if not isinstance(data, dict):
            continue
        if evt.get("type") == "assistant.message":
            m = data.get("model")
            if isinstance(m, str) and m and m != "auto":
                message_model = m  # last assistant.message wins (final turn)
        elif evt.get("type") == "session.usage_checkpoint":
            state = data.get("modelCacheState")
            if isinstance(state, list) and state and isinstance(state[0], dict):
                mid = state[0].get("modelId")
                if isinstance(mid, str) and mid and mid != "auto":
                    checkpoint_model = mid
    return message_model or checkpoint_model or "unknown"


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
        base = _head_sha(req.repo_path)
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
        diff = capture_diff(Path(req.repo_path), base) if base else git_diff(req.repo_path)
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
        status = derive_status(req.repo_path, base) if base else (
            AdapterStatus.EDITED if diff.strip() else AdapterStatus.NO_EDIT
        )
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
        base = _head_sha(req.repo_path)
        cmd = [
            "copilot",
            "-p",
            req.goal,
            "--allow-all-tools",
            "--no-ask-user",
            "--output-format",
            "json",
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
        # Model-tag integrity: parse the REAL model from Copilot's JSONL, never echo
        # req.model. Unparseable -> "unknown" (Eng Decision #5, gap #15).
        model_used = parse_copilot_model(proc.stdout or "")
        if "Access denied by policy" in combined:
            return AdapterResult(
                status=AdapterStatus.POLICY_DENIED,
                diff="",
                log=combined[-2000:],
                usage=Usage(seconds=elapsed),
                model=model_used,
            )
        diff = capture_diff(Path(req.repo_path), base) if base else git_diff(req.repo_path)
        if proc.returncode != 0 and not diff.strip():
            return AdapterResult(
                status=AdapterStatus.ERROR,
                diff="",
                log=combined[-2000:],
                usage=Usage(seconds=elapsed),
                model=model_used,
            )
        status = derive_status(req.repo_path, base) if base else (
            AdapterStatus.EDITED if diff.strip() else AdapterStatus.NO_EDIT
        )
        return AdapterResult(
            status=status,
            diff=diff,
            log=(proc.stdout or "")[-2000:],
            usage=Usage(seconds=elapsed, turns=1),
            model=model_used,
        )


ADAPTERS: dict[str, type[HarnessAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CopilotCliAdapter.name: CopilotCliAdapter,
}
