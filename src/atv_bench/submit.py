"""Submission contract + preflight (devex T3, T7).

A submission is a BOT + a harness fingerprint, never a self-reported result. The
7-check preflight runs before anything touches GitHub so failures are diagnosable up
front. Each check maps a failure to an actionable AtvError.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from atv_bench.errors import AtvError, ErrorCode
from atv_bench.fingerprint.scan import is_secret

# Bots are single small files (v1 arena bots). Guard shape/size before execution.
_MAX_BOT_BYTES = 256 * 1024
_REPO_URL = "https://github.com/All-The-Vibes/ATV-bench"
_REPO_SLUG = "All-The-Vibes/ATV-bench"

# A command runner: (cmd, **kwargs) -> (returncode, stdout, stderr). Injected so tests
# never touch a real gh/git. `default_command_runner` wraps subprocess for the live path.
CommandRunner = Callable[..., "tuple[int, str, str]"]


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
        "1. PR opened against All-The-Vibes/ATV-bench (`atv-bench submit --live` opens it via gh, or open it manually)",
        "2. A maintainer adds the `run-match` label → the sandboxed match job runs your bot",
        "3. Publish workflow recomputes ELO from history → the static leaderboard updates",
    ]
    if is_first_time:
        trail.insert(2, "→ First-time contributor: a maintainer must also approve the "
                        "workflow run before matches start (GitHub gate; expect a short wait).")
    return trail


def default_command_runner(cmd: list[str], *, cwd: str | None = None,
                           timeout: int = 120) -> "tuple[int, str, str]":
    """Live command runner used by the real submit path. Never invoked in tests (they
    inject their own). Captures output so a failing step yields an actionable Cause."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def gh_preflight_runner(check: PreflightCheck, *, runner: CommandRunner,
                        bot_path: str, identity: str,
                        workdir: str | None = None) -> "tuple[bool, str]":
    """Real gh/git-backed implementation of a single preflight check.

    Injected `runner` keeps it testable. Each check maps to a concrete, side-effect-free
    probe; anything that fails returns (False, detail) so run_preflight surfaces the first
    failure with its actionable AtvError. `workdir` is the submission working tree the live
    PR is committed from — the cleanliness check must run there, not the process cwd (H4).
    """
    cid = check.id
    if cid == "gh_installed":
        ok = shutil.which("gh") is not None
        return ok, "gh on PATH" if ok else "gh not found on PATH"
    if cid == "gh_authed":
        rc, out, err = runner(["gh", "auth", "status"])
        return rc == 0, (out or "authenticated") if rc == 0 else (err or "not authenticated")
    if cid == "repo_exists":
        rc, out, err = runner(["gh", "repo", "view", _REPO_SLUG, "--json", "name"])
        return rc == 0, _REPO_SLUG if rc == 0 else (err or "repo not reachable")
    if cid == "fork_exists":
        # F3: a first-time contributor has NO fork yet. This must be non-fatal — the live
        # open path creates it idempotently (`gh repo fork`). We PROBE for information only
        # and always return ok, so a missing fork never blocks the advertised bootstrap.
        rc, out, err = runner(["gh", "repo", "view", f"{identity}/ATV-bench", "--json", "name"])
        return True, "fork present" if rc == 0 else "no fork yet (submit will create one)"
    if cid == "branch_clean":
        # H4: check the SUBMISSION workdir, not the process cwd — the live PR commits from
        # `workdir`, so a dirty tree THERE (not here) is what could leak unrelated files.
        rc, out, err = runner(["git", "status", "--porcelain"], cwd=workdir)
        clean = rc == 0 and out.strip() == ""
        return clean, "clean" if clean else "working tree has uncommitted changes"
    if cid == "leak_scan":
        # cheap defense-in-depth: the bot file bytes must not contain a secret-shaped token.
        try:
            text = Path(bot_path).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            return False, f"cannot read bot: {e}"
        for token in text.split():
            if is_secret(token):
                return False, "bot text contains a secret-shaped value"
        return True, "no secret-shaped values in bot text"
    if cid == "bot_shape":
        try:
            validate_bot_shape(bot_path)
            return True, "bot shape/size valid"
        except AtvError as e:
            return False, e.cause or "invalid bot shape"
    return False, f"unknown check {cid!r}"


def _run_or_raise(runner: CommandRunner, cmd: list[str], *, cwd: str | None = None) -> str:
    """Run a submission step; raise a fail-closed AtvError on any non-zero exit."""
    rc, out, err = runner(cmd, cwd=cwd)
    if rc != 0:
        raise AtvError(ErrorCode.SUBMIT_PR_FAILED,
                       cause=f"`{' '.join(cmd)}` failed (exit {rc}): {err.strip() or out.strip()}")
    return out


def open_submission_pr(*, record: dict[str, Any], bot_path: str, identity: str,
                       runner: CommandRunner = default_command_runner,
                       workdir: str, branch: str | None = None,
                       repo_slug: str = _REPO_SLUG) -> dict[str, Any]:
    """Open the submission PR live: fork → clone → branch → stage → commit → push →
    `gh pr create` → backfill the record's real PR URL → re-push.

    Every step goes through the injected `runner` so the whole flow is hermetically
    testable. Fails closed: identity is required, and any non-zero gh/git step aborts with
    a SUBMIT_PR_FAILED AtvError before a later step (never a half-open PR). Returns
    {"pr_url": ...} parsed from `gh pr create` stdout.

    First-timer safe (F3): a missing fork is bootstrapped by `gh repo fork`, and if
    `workdir` is not already a git checkout it is cloned from the fork before branching.
    The committed submission.json is rewritten with the real PR URL after the PR exists
    (the pre-PR record can only carry a placeholder), then re-pushed so the merged record
    links to the actual PR.

    The bot + submission.json are materialized at the IDENTITY-PINNED path the match job
    reads — league/submissions/<identity>/ — so the opened PR is directly scoreable.
    """
    if not identity.strip():
        raise AtvError(ErrorCode.SUBMIT_PR_FAILED,
                       cause="identity is required to open a submission PR (attribution + bot path)")
    ident = identity.strip()
    branch = branch or f"submit/{ident}"
    wt = Path(workdir)

    # Ensure the fork exists (idempotent — no-op if already forked). This is the bootstrap
    # a first-time contributor needs; it is never a preflight prerequisite.
    _run_or_raise(runner, ["gh", "repo", "fork", repo_slug, "--clone=false"])

    # Ensure a working tree is present. A first-timer runs from an arbitrary cwd with no
    # ATV-bench checkout. Probe `git rev-parse` INSIDE the target workdir (G2: never with
    # cwd=None — that probes the process cwd and, run from any repo, falsely reports a
    # checkout so the clone is skipped and a later `checkout -b` runs in a non-repo). An
    # absent or non-repo workdir triggers a clone of the fork.
    wt.mkdir(parents=True, exist_ok=True)
    rc, _out, _err = runner(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(wt))
    if rc != 0:
        _run_or_raise(runner, ["gh", "repo", "clone", f"{ident}/ATV-bench", str(wt)])
    else:
        # An EXISTING repo at workdir must actually be an ATV-bench checkout (santa round-4,
        # Reviewer B): otherwise we would commit league/submissions/... into an unrelated
        # repo and push it before `gh pr create` fails. Verify origin points at ATV-bench;
        # fail closed if it does not (empty origin is tolerated — a bare/fresh checkout).
        orc, oout, _oerr = runner(["git", "remote", "get-url", "origin"], cwd=str(wt))
        origin = oout.strip()
        if orc == 0 and origin and "atv-bench" not in origin.lower():
            raise AtvError(
                ErrorCode.SUBMIT_PR_FAILED,
                cause=(f"workdir {wt} is a git repo whose origin ({origin}) is not an "
                       "ATV-bench fork; refusing to commit a submission into the wrong repo. "
                       "Use --workdir pointing at your ATV-bench fork clone (or an empty dir)."))

    # Stage the bot + record at the identity-pinned path BEFORE committing. We write files
    # directly (the runner handles only gh/git), so tests observe a real materialized tree.
    dest = wt / "league" / "submissions" / ident
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "main.py").write_text(Path(bot_path).read_text())
    (dest / "submission.json").write_text(json.dumps(record, indent=2, sort_keys=True))

    _run_or_raise(runner, ["git", "checkout", "-b", branch], cwd=str(wt))
    _run_or_raise(runner, ["git", "add", "league/submissions"], cwd=str(wt))
    _run_or_raise(runner, ["git", "commit", "-m", f"league: submit bot for {ident}"], cwd=str(wt))
    _run_or_raise(runner, ["git", "push", "-u", "origin", branch], cwd=str(wt))

    out = _run_or_raise(runner, [
        "gh", "pr", "create", "--repo", repo_slug,
        "--title", f"League submission: {ident}",
        "--body", f"Automated submission for `{ident}` (bot + harness fingerprint).",
        "--head", f"{ident}:{branch}",
    ], cwd=str(wt))
    pr_url = out.strip().splitlines()[-1].strip() if out.strip() else ""

    # Backfill the real PR URL into the committed record (the pre-PR record can only carry
    # a placeholder) and re-push. The PR is ALREADY OPEN at this point, so a backfill
    # failure must NOT raise a fail-closed SUBMIT_PR_FAILED (that would imply no PR exists,
    # G3). Instead surface partial success: return the live pr_url with backfilled=False so
    # the caller/user knows the PR is up but the URL wasn't stamped into the record.
    backfilled_ok = False
    if pr_url:
        try:
            updated = {**record, "pr_url": pr_url}
            (dest / "submission.json").write_text(json.dumps(updated, indent=2, sort_keys=True))
            _run_or_raise(runner, ["git", "add", "league/submissions"], cwd=str(wt))
            _run_or_raise(runner, ["git", "commit", "-m", f"league: backfill PR url for {ident}"], cwd=str(wt))
            _run_or_raise(runner, ["git", "push", "origin", branch], cwd=str(wt))
            backfilled_ok = True
        except AtvError:
            backfilled_ok = False

    return {"pr_url": pr_url, "branch": branch, "identity": ident, "backfilled": backfilled_ok}
