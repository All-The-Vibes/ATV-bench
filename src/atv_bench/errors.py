"""User-facing errors (devex T4).

Every error carries problem + cause + fix + docs link. Enums alone are not
actionable — a user who hits `GH_NOT_AUTHED` needs to be told to run `gh auth login`,
not to look up an enum. The rendered string always includes a Fix: and Docs: line.
"""
from __future__ import annotations

import enum

# Base points at the repo tree root; each spec carries a repo-relative path so
# root-level docs (CONTRIBUTING.md) and docs/ files (docs/COMMUNITY_LEAGUE.md)
# both resolve to a real file on GitHub.
_DOCS_BASE = "https://github.com/All-The-Vibes/ATV-bench/blob/main"


class ErrorCode(str, enum.Enum):
    GH_NOT_INSTALLED = "gh_not_installed"
    GH_NOT_AUTHED = "gh_not_authed"
    REPO_NOT_FOUND = "repo_not_found"
    FORK_MISSING = "fork_missing"
    BRANCH_DIRTY = "branch_dirty"
    LEAK_DETECTED = "leak_detected"
    BOT_SHAPE_INVALID = "bot_shape_invalid"
    FINGERPRINT_LEAK = "fingerprint_leak"
    SUBMIT_PR_FAILED = "submit_pr_failed"


# problem + fix + docs anchor per code.
_SPECS: dict[ErrorCode, tuple[str, str, str]] = {
    ErrorCode.GH_NOT_INSTALLED: (
        "The GitHub CLI (gh) is required to open a submission PR.",
        "Install it: https://cli.github.com , then re-run `atv-bench submit`.",
        "CONTRIBUTING.md#prerequisites",
    ),
    ErrorCode.GH_NOT_AUTHED: (
        "gh is installed but not authenticated.",
        "Run `gh auth login` (choose GitHub.com, HTTPS), then retry.",
        "CONTRIBUTING.md#authentication",
    ),
    ErrorCode.REPO_NOT_FOUND: (
        "The league repo All-The-Vibes/ATV-bench is not reachable.",
        "Check network / gh auth, or open the PR manually against the repo.",
        "CONTRIBUTING.md#manual-pr-fallback",
    ),
    ErrorCode.FORK_MISSING: (
        "You have no fork of the league repo to push a branch to.",
        "Run `gh repo fork All-The-Vibes/ATV-bench --clone=false` (submit does this for you with --create-fork).",
        "CONTRIBUTING.md#forking",
    ),
    ErrorCode.BRANCH_DIRTY: (
        "Your working tree has uncommitted changes; submit needs a clean branch.",
        "Commit or stash your changes (`git stash`) before submitting.",
        "CONTRIBUTING.md#clean-branch",
    ),
    ErrorCode.LEAK_DETECTED: (
        "The pre-submit leak scan found a secret-shaped value in your bot or fingerprint.",
        "Run `atv-bench fingerprint --dry-run` to see what was flagged; remove the secret and retry.",
        "docs/COMMUNITY_LEAGUE.md#harness-fingerprint-the-credibility-gate",
    ),
    ErrorCode.BOT_SHAPE_INVALID: (
        "The bot file failed shape/size validation (wrong entrypoint, too large, or non-text).",
        "Ensure the bot is a single small text file with the expected entrypoint (e.g. main.py).",
        "CONTRIBUTING.md#bot-shape",
    ),
    ErrorCode.FINGERPRINT_LEAK: (
        "A fingerprint value passed to submit still looks like a secret; refusing to publish it.",
        "This is a bug guard — re-run the probe via `atv-bench fingerprint`; do not hand-edit the manifest.",
        "docs/COMMUNITY_LEAGUE.md#harness-fingerprint-the-credibility-gate",
    ),
    ErrorCode.SUBMIT_PR_FAILED: (
        "A step of the live submission (fork / branch / commit / push / PR create) failed.",
        "Read the Cause line for the failing gh/git command; fix it or fall back to the manual PR flow.",
        "CONTRIBUTING.md#manual-pr-fallback",
    ),
}


class AtvError(Exception):
    """An actionable, user-facing error."""

    def __init__(self, code: ErrorCode, *, cause: str = "") -> None:
        self.code = code
        problem, fix, anchor = _SPECS[code]
        self.problem = problem
        self.cause = cause
        self.fix = fix
        self.docs_url = f"{_DOCS_BASE}/{anchor}"
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"error [{self.code.value}]: {self.problem}"]
        if self.cause:
            lines.append(f"Cause: {self.cause}")
        lines.append(f"Fix: {self.fix}")
        lines.append(f"Docs: {self.docs_url}")
        return "\n".join(lines)

    def __str__(self) -> str:  # noqa: D401
        return self._render()


def render_error(err: object) -> str:
    """One shared render shape for AtvError and RunError (DX-3).

    Both subsystems surface the same structure: Problem / Cause? / Fix / exit N,
    so a user sees a consistent error regardless of which raised it.
    """
    # Local import to avoid a module cycle (run_envelope imports nothing here).
    from atv_bench.run_envelope import EXIT_CODES, RunError

    if isinstance(err, AtvError):
        problem = err.problem
        cause = err.cause
        fix = err.fix
        docs = err.docs_url
        # AtvError codes are not in the run exit-code map; they exit as usage (2).
        exit_code = EXIT_CODES["usage"]
    elif isinstance(err, RunError):
        problem = err.message
        cause = ""
        fix = err.fix
        docs = ""
        exit_code = err.exit_code
    else:  # pragma: no cover - defensive
        raise TypeError(f"render_error: unsupported error type {type(err)!r}")

    lines = [f"Problem: {problem}"]
    if cause:
        lines.append(f"Cause: {cause}")
    if fix:
        lines.append(f"Fix: {fix}")
    if docs:
        lines.append(f"Docs: {docs}")
    lines.append(f"exit {exit_code}")
    return "\n".join(lines)
