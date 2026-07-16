"""Live gh PR submission automation (follow-up item 2).

`atv-bench submit` builds the store-ingestable record and, until now, made the contributor
open the PR by hand. This wires the live `gh` path — fork, branch, stage the bot +
submission record under league/submissions/<identity>/, commit, push, open the PR — behind
the existing 7-check preflight, with an INJECTED command runner so the flow is fully
testable without touching a real gh/git.

Fail-closed: the PR is opened only if preflight passes AND every gh/git step succeeds. Any
non-zero step aborts with an actionable AtvError and no half-open PR.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.errors import AtvError, ErrorCode
from atv_bench.submit import (
    PREFLIGHT_CHECKS,
    build_submission,
    open_submission_pr,
    gh_preflight_runner,
)


_BOT_SRC = "def move(state):\n    return 'up'\n"


def _write_bot(tmp_path):
    p = tmp_path / "main.py"
    p.write_text(_BOT_SRC)
    return str(p)


def _fingerprint():
    return {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["gstack"], "mcps": [], "plugins": [], "custom_agents_count": 0,
        "unknown": [], "probe_version": "1.0.0",
    }


def _record(tmp_path):
    return build_submission(bot_path=_write_bot(tmp_path), fingerprint=_fingerprint(),
                            identity="octocat", game="battlesnake")


class _RecordingRunner:
    """Captures the (cmd, ...) calls and returns scripted results.

    results: maps a substring of the joined command to (returncode, stdout, stderr).
    A command matching no key defaults to success with empty output.
    """
    def __init__(self, results=None):
        self.results = results or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for needle, res in self.results.items():
            if needle in joined:
                return res
        return (0, "", "")

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


# --- happy path ---

def test_open_pr_runs_full_sequence_and_returns_url(tmp_path):
    record = _record(tmp_path)
    runner = _RecordingRunner({
        "pr create": (0, "https://github.com/All-The-Vibes/ATV-bench/pull/42\n", ""),
    })
    result = open_submission_pr(
        record=record, bot_path=_write_bot(tmp_path), identity="octocat",
        runner=runner, workdir=str(tmp_path / "wt"),
    )
    assert result["pr_url"] == "https://github.com/All-The-Vibes/ATV-bench/pull/42"
    # the load-bearing steps all ran, in a fork/branch/commit/push/pr-create shape
    assert runner.ran("gh repo fork")
    assert runner.ran("git checkout -b") or runner.ran("git switch -c")
    assert runner.ran("git add")
    assert runner.ran("git commit")
    assert runner.ran("git push")
    assert runner.ran("gh pr create")


def test_open_pr_stages_bot_and_record_under_identity_path(tmp_path):
    record = _record(tmp_path)
    wt = tmp_path / "wt"
    runner = _RecordingRunner({"pr create": (0, "https://github.com/x/y/pull/1\n", "")})
    open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                       identity="octocat", runner=runner, workdir=str(wt))
    # the bot + submission.json must be materialized at the identity-pinned path the
    # match job reads (league/submissions/<identity>/main.py)
    bot = wt / "league" / "submissions" / "octocat" / "main.py"
    rec = wt / "league" / "submissions" / "octocat" / "submission.json"
    assert bot.exists() and bot.read_text() == _BOT_SRC
    assert rec.exists()
    assert json.loads(rec.read_text())["identity"] == "octocat"


# --- fail closed ---

def test_open_pr_aborts_when_a_git_step_fails(tmp_path):
    record = _record(tmp_path)
    runner = _RecordingRunner({"git push": (1, "", "remote rejected")})
    with pytest.raises(AtvError) as ei:
        open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                           identity="octocat", runner=runner, workdir=str(tmp_path / "wt"))
    # never reaches pr create after a failed push
    assert not runner.ran("gh pr create")
    assert ei.value.code == ErrorCode.SUBMIT_PR_FAILED


def test_open_pr_aborts_when_pr_create_fails(tmp_path):
    record = _record(tmp_path)
    runner = _RecordingRunner({"pr create": (1, "", "could not create pull request")})
    with pytest.raises(AtvError):
        open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                           identity="octocat", runner=runner, workdir=str(tmp_path / "wt"))


def test_open_pr_requires_identity(tmp_path):
    record = _record(tmp_path)
    runner = _RecordingRunner()
    with pytest.raises(AtvError):
        open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                           identity="", runner=runner, workdir=str(tmp_path / "wt"))


# --- the real gh-backed preflight runner ---

def test_gh_preflight_runner_reports_pass_when_commands_succeed(tmp_path):
    def fake_cmd(cmd, **kwargs):
        joined = " ".join(cmd)
        if "gh auth status" in joined:
            return (0, "Logged in", "")
        if "gh repo view" in joined:
            return (0, "All-The-Vibes/ATV-bench", "")
        if "git status" in joined:
            return (0, "", "")  # clean
        return (0, "ok", "")
    # bot present + shaped
    bot = _write_bot(tmp_path)
    for check in PREFLIGHT_CHECKS:
        ok, detail = gh_preflight_runner(check, runner=fake_cmd, bot_path=bot,
                                         identity="octocat")
        assert ok, f"{check.id} should pass: {detail}"


def test_gh_preflight_runner_flags_dirty_tree(tmp_path):
    def fake_cmd(cmd, **kwargs):
        joined = " ".join(cmd)
        if "git status" in joined:
            return (0, " M some_file.py", "")  # dirty
        if "gh auth status" in joined:
            return (0, "Logged in", "")
        return (0, "ok", "")
    check = next(c for c in PREFLIGHT_CHECKS if c.id == "branch_clean")
    ok, detail = gh_preflight_runner(check, runner=fake_cmd, bot_path=_write_bot(tmp_path),
                                     identity="octocat")
    assert not ok


def test_gh_preflight_runner_flags_unauthed(tmp_path):
    def fake_cmd(cmd, **kwargs):
        if "gh auth status" in " ".join(cmd):
            return (1, "", "not logged in")
        return (0, "ok", "")
    check = next(c for c in PREFLIGHT_CHECKS if c.id == "gh_authed")
    ok, detail = gh_preflight_runner(check, runner=fake_cmd, bot_path=_write_bot(tmp_path),
                                     identity="octocat")
    assert not ok
