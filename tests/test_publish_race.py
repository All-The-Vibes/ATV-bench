"""Publish-race tripwire (santa re-review #3) — runs on EVERY push.

The trusted publish job appends the scored match to `league/` and pushes it back to the
default branch. The league PR shipped a single-shot push with no retry, which dropped a
match on a non-fast-forward race.

The FIX is optimistic-concurrency: fetch the latest trusted branch, re-apply THIS match
on top of it (re-ingest is idempotent — `matches.jsonl` dedups on `match_id`), rebuild,
push, and retry on rejection. This is race-safe on its own and — unlike a GitHub
`concurrency` group — cannot silently drop a match: GitHub keeps only ONE *pending* run
per concurrency group and cancels an older pending run when a newer one queues (even with
`cancel-in-progress: false`, which protects only the *in-progress* run), so serializing
publishes via a constant group would REINTRODUCE the drop. The retry loop is the real
guarantee.

Two subtle bugs this tripwire guards against (found in santa re-review round 2):
  1. `git diff --quiet -- league/` IGNORES untracked files. `league/matches.jsonl` is
     not tracked until the first match, so the loop must stage untracked store files
     (`git add -A league/` / `git clean`), or the first match is silently dropped.
  2. asserting the word "rebase" is satisfied by COMMENTS — assertions here run against
     comment-stripped shell so they prove real behavior, not documentation.

Mirrors the tests/test_action_isolation.py + test_arena_image.py tripwire pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "league-publish.yml"


@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.exists(), "league-publish.yml workflow must exist"
    return yaml.safe_load(WORKFLOW.read_text())


def _publish(wf):
    return wf["jobs"]["publish"]


def _persist_step(wf):
    for step in _publish(wf)["steps"]:
        if "git push" in str(step.get("run", "")):
            return step
    return None


def _persist_code(wf):
    """The persist step's shell with comment lines stripped (real behavior only)."""
    step = _persist_step(wf)
    if step is None:
        return ""
    lines = []
    for ln in str(step.get("run", "")).splitlines():
        code = ln.split("#", 1)[0]
        if code.strip():
            lines.append(code)
    return "\n".join(lines)


def test_publish_has_a_persist_step(wf):
    assert _persist_step(wf) is not None, "publish job must have a step that pushes the store"


def test_publish_persist_fetches_latest_before_push(wf):
    code = _persist_code(wf)
    assert "git fetch" in code or "git pull" in code, (
        "store push must fetch/pull the latest default branch inside the retry loop "
        "(a single-shot push drops the match on a non-fast-forward race)"
    )


def test_publish_persist_has_a_retry_loop(wf):
    code = _persist_code(wf)
    assert any(k in code for k in ("while ", "until ", "for ")), (
        "the store push must run inside a retry loop so a losing race re-applies "
        "the match instead of dropping it"
    )


def test_publish_persist_reingests_match_inside_the_loop(wf):
    # The REAL mechanism: on each attempt, re-apply THIS match on top of the freshly
    # fetched store (idempotent re-ingest), not a `git rebase` of derived files.
    code = _persist_code(wf)
    assert "publish ingest" in code, (
        "the persist step must re-ingest the match after fetching the latest store "
        "(optimistic-concurrency re-apply), not merely re-push a stale local commit"
    )


def test_publish_persist_handles_untracked_store_files(wf):
    # league/matches.jsonl is untracked until the first match. `git diff --quiet` ignores
    # untracked files, so the step must stage them (git add -A / add league) or clean
    # them between attempts — otherwise the FIRST match is silently dropped.
    code = _persist_code(wf)
    stages_untracked = (
        "git add -A" in code
        or "git add --all" in code
        or "git clean" in code
        or "--untracked" in code
    )
    assert stages_untracked, (
        "persist step must handle UNTRACKED store files (git add -A / git clean / "
        "--untracked) so the first match (untracked matches.jsonl) is not dropped"
    )


def test_publish_persist_does_not_short_circuit_on_git_diff_quiet(wf):
    # `git diff --quiet -- league/` before any staging is the exact bug: it ignores the
    # untracked first-match file and exits 'no change'. If a change-detection guard
    # exists it must run AFTER staging (git diff --cached), never as a bare pre-loop
    # `git diff --quiet -- league/` early-exit.
    code = _persist_code(wf)
    assert "git diff --quiet -- league" not in code, (
        "must not gate on `git diff --quiet -- league/` (ignores the untracked "
        "first-match file); detect changes AFTER staging via `git diff --cached`"
    )


def test_publish_persist_does_not_silently_swallow_push_errors(wf):
    # The trusted publish path must fail closed. A `git push ... || true` would hide a
    # push failure and drop the match silently.
    code = _persist_code(wf)
    for line in code.splitlines():
        if "git push" in line:
            assert "|| true" not in line, (
                "git push must not be `|| true`-swallowed (would silently drop a match)"
            )


def test_publish_persist_fails_closed_after_max_attempts(wf):
    # After exhausting retries the step must exit non-zero (loud, re-runnable failure),
    # never exit 0 having pushed nothing.
    code = _persist_code(wf)
    assert "exit 1" in code, (
        "persist step must exit non-zero after exhausting retry attempts (fail closed)"
    )


def test_publish_job_does_not_use_a_pending_cancelling_concurrency_group(wf):
    # A job-level `concurrency` group SERIALIZES publishes via GitHub — but GitHub keeps
    # only one PENDING run per group and cancels an older pending run when a newer queues
    # (cancel-in-progress:false protects only the in-progress run). That silently drops
    # the cancelled publish's match — the exact bug #3 targets. The idempotent retry loop
    # is the correct mechanism; the publish job must NOT rely on a serializing group.
    publish = _publish(wf)
    assert "concurrency" not in publish, (
        "publish job must NOT declare a job-level concurrency group: GitHub cancels "
        "pending runs in a group, silently dropping a scored match. Rely on the "
        "idempotent fetch+re-ingest+retry loop instead."
    )


def test_publish_persist_retry_is_deadline_bounded_not_a_small_fixed_cap(wf):
    # A small fixed attempt cap (e.g. 5) can exhaust under a bursty label storm — only one
    # publisher wins per push round — and fail a job with its match unpushed. The retry
    # must be bounded by a DEADLINE (with backoff), so every scored match persists within
    # the job's time budget rather than needing a manual re-run.
    code = _persist_code(wf)
    assert "deadline" in code or "date +%s" in code, (
        "retry must be deadline-bounded (time budget), not a small fixed attempt count, "
        "so a bursty publish storm cannot exhaust retries and drop a match"
    )
    assert "sleep" in code, (
        "retry must back off between attempts (sleep) so concurrent publishers "
        "de-synchronize instead of colliding every round"
    )


def test_publish_board_timestamp_is_not_stale_previous_commit_time(wf):
    # `--updated-at "$(git show -s --format=%cI HEAD)"` inside the loop (after reset --hard,
    # before the new commit) reflects the PREVIOUS store commit, leaving the board one
    # match behind. The build must stamp a current time instead.
    code = _persist_code(wf)
    assert "git show -s --format=%cI HEAD" not in code, (
        "board --updated-at must not be the previous commit's time (stale by one match); "
        "use a current timestamp"
    )


def test_workflow_concurrency_does_not_cancel_scoreable_runs(wf):
    # The WORKFLOW-level concurrency group must not create a pending-cancel path for
    # scoreable runs. A per-PR group (keyed on pull_request.number / github.ref) with
    # GitHub's pending-cancel behavior would cancel an older queued run-match when the
    # same PR is re-labeled — silently dropping that match one level ABOVE the publish
    # loop. Each labeled run is a distinct match, so the group must be unique per run
    # (keyed on github.run_id) so it never cancels another run.
    concurrency = wf.get("concurrency")
    if concurrency is None:
        return  # no group at all is also safe
    group = str(concurrency.get("group", ""))
    assert "run_id" in group or "run.id" in group, (
        "workflow concurrency group must be keyed on github.run_id (unique per run) so it "
        f"never cancels a queued scoreable run; got a cancellable group: {group!r}"
    )
    assert "pull_request.number" not in group and "github.ref" not in group, (
        "workflow concurrency group must not be keyed per-PR/ref (pending-cancel would "
        "drop a re-labeled PR's queued match)"
    )
