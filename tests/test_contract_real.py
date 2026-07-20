"""RED tests for the adapter-contract status derivation regression (ENG-A) and the
distinct non-win outcome taxonomy (CRASH / TIMEOUT / MALFORMED + fit_exclude).

These exercise the REAL derivation path — not FakeAdapter's hardcoded status=OK —
so they catch the false-forfeit bug where a harness that COMMITS its edit produces
an empty `git diff` and is scored NO_EDIT.

The fix (plan item 8) should:
  * add `AdapterStatus.EDITED`, `AdapterStatus.CRASH`, `AdapterStatus.MALFORMED`
  * add `derive_status(repo_path, base)` that reflects the
    base..HEAD ∪ working-tree ∪ untracked UNION (via snapshot.capture_diff),
    NOT plain `git diff`
  * make ClaudeCodeAdapter/CopilotCliAdapter record HEAD at start-of-run and derive
    status from that base (so a committed edit is EDITED, never NO_EDIT)
  * add `classify_outcome(...)` mapping crash/timeout/malformed to distinct statuses
  * expose `AdapterResult.fit_exclude` (True for CRASH/TIMEOUT/MALFORMED) so the
    rating engine can drop them and log to unknown[]
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    HarnessAdapter,
    Usage,
)
from atv_bench.adapters.snapshot import seed_base


# --------------------------------------------------------------------------- helpers


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(repo: Path, bot: str = "main.py") -> str:
    """Seed a git repo with one committed bot file. Return the base SHA (atv-base)."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / bot).write_text("def get_move(o):\n    return 'N'\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "seed")
    return seed_base(repo)


def _commit_edit(repo: Path, bot: str = "main.py") -> None:
    (repo / bot).write_text("def get_move(o):\n    return 'S'\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "edit")


# --------------------------------------------------------------------------- adapters


class CommittingAdapter(HarnessAdapter):
    """A thin *real* adapter: records HEAD, commits its edit, derives status via the
    real derivation path (NOT a hardcoded OK). Models ClaudeCodeAdapter/CopilotCliAdapter
    when the underlying CLI commits its work."""

    name = "committing"

    def run(self, req: AdapterRequest) -> AdapterResult:
        from atv_bench.adapters.contract import derive_status  # target API (missing → red)

        repo = Path(req.repo_path)
        pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _commit_edit(repo, req.bot_file)  # working tree now CLEAN vs HEAD
        status = derive_status(str(repo), pre)
        return AdapterResult(status=status, diff="", log="", usage=Usage(), model="m")


class CrashingAdapter(HarnessAdapter):
    """Adapter whose subprocess dies by signal (returncode < 0)."""

    name = "crashing"

    def run(self, req: AdapterRequest) -> AdapterResult:
        from atv_bench.adapters.contract import classify_outcome  # target API (missing → red)

        status = classify_outcome(returncode=-11, crashed=True)
        return AdapterResult(status=status, diff="", log="segfault", usage=Usage(), model="m")


class TimingOutAdapter(HarnessAdapter):
    name = "timing-out"

    def run(self, req: AdapterRequest) -> AdapterResult:
        from atv_bench.adapters.contract import classify_outcome

        status = classify_outcome(timed_out=True)
        return AdapterResult(status=status, diff="", log="timeout", usage=Usage(), model="m")


class MalformedAdapter(HarnessAdapter):
    name = "malformed"

    def run(self, req: AdapterRequest) -> AdapterResult:
        from atv_bench.adapters.contract import classify_outcome

        status = classify_outcome(returncode=0, malformed=True)
        return AdapterResult(status=status, diff="", log="garbage", usage=Usage(), model="m")


def _req(repo: Path) -> AdapterRequest:
    return AdapterRequest(repo_path=str(repo), goal="win", model="m", budget=Budget())


# --------------------------------------------------------------------------- tests


def test_committed_edit_no_forfeit(tmp_path):
    """REGRESSION (ENG-A): a harness that COMMITS its edit must NOT be a false forfeit.

    The working tree is clean vs HEAD after the commit, so plain `git diff` is empty →
    the buggy derivation scores NO_EDIT. The union-based derivation must see the
    committed change and return EDITED/OK.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = CommittingAdapter().run(_req(repo))
    assert result.status != AdapterStatus.NO_EDIT, "committed edit was falsely forfeited"
    assert result.status in {AdapterStatus.EDITED, AdapterStatus.OK}


def test_status_from_diff_union(tmp_path):
    """derive_status reflects base..HEAD ∪ working ∪ untracked, even with a clean tree."""
    from atv_bench.adapters.contract import derive_status

    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit_edit(repo)  # committed → clean working tree, but base..HEAD is non-empty
    status = derive_status(str(repo), base)
    assert status in {AdapterStatus.EDITED, AdapterStatus.OK}
    assert status != AdapterStatus.NO_EDIT


def test_true_no_edit_still_forfeits(tmp_path):
    """Guard against over-correcting: genuinely no change ⇒ NO_EDIT."""
    from atv_bench.adapters.contract import derive_status

    repo = tmp_path / "repo"
    base = _init_repo(repo)
    status = derive_status(str(repo), base)  # nothing touched since base
    assert status == AdapterStatus.NO_EDIT


def test_crash_classified_as_crash_not_draw(tmp_path):
    """An adapter that crashes ⇒ a distinct CRASH outcome, never draw/loss/no_edit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = CrashingAdapter().run(_req(repo))
    assert result.status == AdapterStatus.CRASH
    assert result.status not in {AdapterStatus.OK, AdapterStatus.EDITED, AdapterStatus.NO_EDIT}


def test_timeout_scored_distinctly(tmp_path):
    """A timed-out adapter ⇒ distinct TIMEOUT outcome, not CRASH/ERROR."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = TimingOutAdapter().run(_req(repo))
    assert result.status == AdapterStatus.TIMEOUT
    assert result.status not in {AdapterStatus.CRASH, AdapterStatus.ERROR}


def test_malformed_output_classified(tmp_path):
    """Malformed turn output ⇒ distinct MALFORMED outcome."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = MalformedAdapter().run(_req(repo))
    assert result.status == AdapterStatus.MALFORMED


def test_nonwin_outcomes_flagged_for_fit_exclusion():
    """CRASH/TIMEOUT/MALFORMED carry fit_exclude=True; scoreable outcomes do not."""
    excluded = [AdapterStatus.CRASH, AdapterStatus.TIMEOUT, AdapterStatus.MALFORMED]
    for st in excluded:
        res = AdapterResult(status=st, diff="", log="", usage=Usage(), model="m")
        assert res.fit_exclude is True, f"{st} must be flagged for fit exclusion"

    for st in [AdapterStatus.OK, AdapterStatus.EDITED, AdapterStatus.NO_EDIT]:
        res = AdapterResult(status=st, diff="", log="", usage=Usage(), model="m")
        assert res.fit_exclude is False, f"{st} must remain in the fit"
