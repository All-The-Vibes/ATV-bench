"""Submission contract + preflight (devex T3, T7).

A submission is a BOT + a harness fingerprint, never a self-reported result. The
7-check preflight runs before anything touches GitHub so failures are diagnosable up
front. Each check maps a failure to an actionable AtvError.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from atv_bench.errors import AtvError, ErrorCode
from atv_bench.fingerprint.scan import is_secret

# Bots are single small files (v1 arena bots). Guard shape/size before execution.
_MAX_BOT_BYTES = 256 * 1024
_REPO_URL = "https://github.com/All-The-Vibes/ATV-bench"


@dataclass(frozen=True)
class PreflightCheck:
    id: str
    description: str
    error_code: ErrorCode


PREFLIGHT_CHECKS: tuple[PreflightCheck, ...] = (
    PreflightCheck("gh_installed", "GitHub CLI (gh) is on PATH", ErrorCode.GH_NOT_INSTALLED),
    PreflightCheck("gh_authed", "gh is authenticated", ErrorCode.GH_NOT_AUTHED),
    PreflightCheck("repo_exists", "league repo is reachable", ErrorCode.REPO_NOT_FOUND),
    PreflightCheck("fork_exists", "a fork exists to push to", ErrorCode.FORK_MISSING),
    PreflightCheck("branch_clean", "working tree is clean", ErrorCode.BRANCH_DIRTY),
    PreflightCheck("leak_scan", "bot + fingerprint pass the leak scan", ErrorCode.LEAK_DETECTED),
    PreflightCheck("bot_shape", "bot file shape/size is valid", ErrorCode.BOT_SHAPE_INVALID),
)

# runner(check) -> (ok, detail). Injected so tests don't touch the real gh CLI.
PreflightRunner = Callable[[PreflightCheck], "tuple[bool, str]"]


def run_preflight(runner: PreflightRunner) -> dict[str, Any]:
    """Run all 7 checks. Report every result; the plan surfaces the first failure."""
    results: list[dict[str, Any]] = []
    passed = True
    for check in PREFLIGHT_CHECKS:
        ok, detail = runner(check)
        entry: dict[str, Any] = {
            "id": check.id,
            "description": check.description,
            "ok": ok,
            "detail": detail,
        }
        if not ok:
            passed = False
            err = AtvError(check.error_code, cause=detail)
            entry["fix"] = err.fix
            entry["docs_url"] = err.docs_url
        results.append(entry)
    return {"passed": passed, "results": results}


def validate_bot_shape(bot_path: str) -> None:
    """Cheap shape/size guard before a bot is ever executed."""
    p = Path(bot_path)
    if not p.is_file():
        raise AtvError(ErrorCode.BOT_SHAPE_INVALID, cause=f"{bot_path} is not a file")
    size = p.stat().st_size
    if size == 0 or size > _MAX_BOT_BYTES:
        raise AtvError(ErrorCode.BOT_SHAPE_INVALID,
                       cause=f"bot is {size} bytes (must be 1..{_MAX_BOT_BYTES})")
    try:
        p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise AtvError(ErrorCode.BOT_SHAPE_INVALID, cause=f"bot is not UTF-8 text: {e}")


def _fingerprint_has_leak(fingerprint: dict[str, Any]) -> str | None:
    """Defense-in-depth: refuse to submit a fingerprint whose emitted names still
    look secret-shaped (the probe should already have caught this)."""
    for field in ("skills", "mcps", "plugins"):
        for name in fingerprint.get(field, []):
            if is_secret(name):
                return f"{field}: {name[:6]}… flagged by scanner"
    model = fingerprint.get("model", "")
    if isinstance(model, str) and is_secret(model):
        return "model flagged by scanner"
    return None


def build_submission(
    *,
    bot_path: str,
    fingerprint: dict[str, Any],
    identity: str,
    game: str,
    pr_url: str = "",
    logs_url: str = "",
) -> dict[str, Any]:
    """Compose the submission artifact PR'd to the league repo.

    This is the SINGLE canonical submission shape consumed by LeagueStore.add_submission
    and build_leaderboard_doc. `pr_url`/`logs_url` are known only once the PR exists;
    they default to the repo URL and are backfilled by the merge/publish step.
    """
    validate_bot_shape(bot_path)
    leak = _fingerprint_has_leak(fingerprint)
    if leak:
        raise AtvError(ErrorCode.FINGERPRINT_LEAK, cause=leak)
    data = Path(bot_path).read_bytes()
    return {
        "identity": identity,
        "game": game,
        "bot_sha256": hashlib.sha256(data).hexdigest(),
        "bot_filename": Path(bot_path).name,
        "pr_url": pr_url or _REPO_URL,
        "logs_url": logs_url or _REPO_URL,
        "fingerprint": fingerprint,
    }


def submission_status_trail(is_first_time: bool) -> list[str]:
    """Copy for the submission status trail (devex T7).

    Surfaces the first-timer manual-approval wait so the virality moment doesn't
    read as silent latency.
    """
    trail = [
        "1. PR opened against All-The-Vibes/ATV-bench (you open it; live automation is not wired yet)",
        "2. A maintainer adds the `run-match` label → the sandboxed match job runs your bot",
        "3. Publish job recomputes ELO from history → the static leaderboard updates",
    ]
    if is_first_time:
        trail.insert(2, "→ First-time contributor: a maintainer must also approve the "
                        "workflow run before matches start (GitHub gate; expect a short wait).")
    return trail
