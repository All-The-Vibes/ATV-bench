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


# --- F3 (santa round-1, Reviewer B): the live flow must actually work for a first-time
#     user with no fork and no local checkout, and must backfill the PR/log URLs. ---

def test_missing_fork_is_non_fatal_for_first_timer(tmp_path):
    """A first-time contributor has no fork yet. `fork_exists` must NOT fail preflight and
    block submission — open_submission_pr creates the fork idempotently (bootstrap, not a
    prerequisite). Before the fix, a missing fork failed preflight and the advertised
    `gh repo fork` bootstrap never ran."""
    def fake_cmd(cmd, **kwargs):
        joined = " ".join(cmd)
        if f"gh repo view octocat/ATV-bench" in joined:
            return (1, "", "Could not resolve to a Repository")  # no fork yet
        if "gh auth status" in joined:
            return (0, "Logged in", "")
        if "gh repo view" in joined:
            return (0, "All-The-Vibes/ATV-bench", "")
        if "git status" in joined:
            return (0, "", "")
        return (0, "ok", "")
    check = next(c for c in PREFLIGHT_CHECKS if c.id == "fork_exists")
    ok, detail = gh_preflight_runner(check, runner=fake_cmd, bot_path=_write_bot(tmp_path),
                                     identity="octocat")
    assert ok, "missing fork must be non-fatal (submit bootstraps it): " + detail


def test_open_pr_bootstraps_checkout_when_workdir_not_a_repo(tmp_path):
    """A first-timer runs from an arbitrary cwd with no ATV-bench checkout. open_submission_pr
    must clone the fork into a working tree before `git checkout -b` — before the fix it ran
    checkout in a non-repo and failed."""
    record = _record(tmp_path)
    wt = tmp_path / "wt"  # does NOT exist / is not a git repo
    runner = _RecordingRunner({
        "pr create": (0, "https://github.com/All-The-Vibes/ATV-bench/pull/7\n", ""),
        # a rev-parse probe on a non-repo returns non-zero -> triggers clone
        "rev-parse": (1, "", "not a git repository"),
    })
    open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                       identity="octocat", runner=runner, workdir=str(wt))
    assert runner.ran("gh repo clone") or runner.ran("git clone"), \
        "must clone the fork when workdir is not already a checkout"


def test_open_pr_backfills_real_pr_url_in_committed_record(tmp_path):
    """pr_url/logs_url are unknown until the PR exists. After `gh pr create`, the committed
    submission.json must be rewritten with the real PR URL (and re-pushed) so the merged
    record carries a real link, not the repo-root placeholder."""
    record = _record(tmp_path)
    wt = tmp_path / "wt"
    pr = "https://github.com/All-The-Vibes/ATV-bench/pull/99"
    runner = _RecordingRunner({"pr create": (0, pr + "\n", "")})
    result = open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                                identity="octocat", runner=runner, workdir=str(wt))
    assert result["pr_url"] == pr
    rec = json.loads((wt / "league" / "submissions" / "octocat" / "submission.json").read_text())
    assert rec["pr_url"] == pr, "committed record must carry the real PR URL after backfill"
    # a second push must carry the backfilled record
    push_calls = [c for c in runner.calls if "push" in " ".join(c)]
    assert len(push_calls) >= 2, "backfilled record must be pushed (a second push after pr create)"


# --- santa round-2: gaps the round-1 F3 fix missed ---

def test_rev_parse_probe_runs_in_target_workdir_not_cwd(tmp_path):
    """G2 (Reviewer B, CRITICAL): when workdir does not exist, the rev-parse probe was run
    with cwd=None (the process cwd). Run from inside ANY git repo, that falsely reports a
    checkout and skips the clone, so `git checkout -b` later runs in a non-repo. The probe
    must target the workdir itself; a non-existent/absent workdir must trigger a clone."""
    record = _record(tmp_path)
    wt = tmp_path / "does_not_exist_yet"
    calls_cwd = []

    class _CwdRunner(_RecordingRunner):
        def __call__(self, cmd, **kwargs):
            calls_cwd.append((list(cmd), kwargs.get("cwd")))
            return super().__call__(cmd, **kwargs)

    runner = _CwdRunner({
        "pr create": (0, "https://github.com/x/y/pull/1\n", ""),
        "rev-parse": (128, "", "fatal: not a git repository"),
    })
    open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                       identity="octocat", runner=runner, workdir=str(wt))
    # the rev-parse probe must NOT be invoked with cwd=None (the process cwd); it must
    # target the intended workdir (or the clone must run because workdir is absent)
    rev = [(c, cwd) for c, cwd in calls_cwd if "rev-parse" in " ".join(c)]
    assert rev, "must probe for an existing checkout"
    for _cmd, cwd in rev:
        assert cwd != None, "rev-parse must not run in the process cwd (cwd=None)"  # noqa: E711
    assert runner.ran("gh repo clone") or runner.ran("git clone"), \
        "absent workdir must trigger a clone, not fall through to checkout in a non-repo"


def test_backfill_failure_after_pr_create_is_surfaced_not_silent(tmp_path):
    """G3 (Reviewer B): the PR-url backfill commits+pushes AFTER `gh pr create` succeeds.
    If that second push fails, the flow must not raise a bare fail-closed error implying no
    PR exists — the PR is already open. Surface partial success (return the pr_url) so the
    caller knows the PR is live even though backfill didn't land."""
    record = _record(tmp_path)
    wt = tmp_path / "wt"
    pr = "https://github.com/All-The-Vibes/ATV-bench/pull/55"
    # first push ok; the SECOND push (after pr create) fails
    calls = {"n": 0}

    class _SecondPushFails(_RecordingRunner):
        def __call__(self, cmd, **kwargs):
            joined = " ".join(cmd)
            if "pr create" in joined:
                self.calls.append(list(cmd))
                return (0, pr + "\n", "")
            if joined.startswith("git push") or " push " in joined or joined.endswith("push"):
                calls["n"] += 1
                self.calls.append(list(cmd))
                if calls["n"] >= 2:
                    return (1, "", "backfill push rejected")
                return (0, "", "")
            return super().__call__(cmd, **kwargs)

    runner = _SecondPushFails()
    result = open_submission_pr(record=record, bot_path=_write_bot(tmp_path),
                                identity="octocat", runner=runner, workdir=str(wt))
    # PR is live: the caller must learn the URL, not get a misleading SUBMIT_PR_FAILED
    assert result["pr_url"] == pr
    assert result.get("backfilled") is False
