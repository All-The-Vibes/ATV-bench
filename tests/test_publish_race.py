"""Publish-race tripwire (santa re-review #3) — runs on EVERY push.

The trusted publish job appends the scored match to `league/` and pushes it back to the
default branch. Two problems shipped in the league PR:

  1. concurrency was scoped PER PR (`league-${{ pr.number }}`), so two different PRs'
     publish jobs ran concurrently and raced on the default-branch push;
  2. the push was a single-shot `git push` with no fetch/rebase/retry, so the losing
     race hit a non-fast-forward rejection, aborted the job, and SILENTLY DROPPED the
     recorded match — skewing ELO.

This test asserts, hermetically (YAML/text parse, no runner), that the publish path is
serialized globally AND that its store push is a fetch+rebase+retry loop so a losing
race re-applies instead of dropping a match.

Mirrors the tests/test_action_isolation.py tripwire pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "league.yml"


@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.exists(), "league.yml workflow must exist"
    return yaml.safe_load(WORKFLOW.read_text())


def _publish(wf):
    return wf["jobs"]["publish"]


def _persist_step(wf):
    for step in _publish(wf)["steps"]:
        run = str(step.get("run", ""))
        if "git push" in run:
            return step
    return None


def test_publish_job_is_serialized_globally(wf):
    # The publish job must have its OWN concurrency group that is NOT keyed on the PR
    # number/ref, so publishes from different PRs serialize instead of racing on the
    # default-branch push. A workflow-level group keyed on pr.number does not serialize
    # across PRs.
    publish = _publish(wf)
    group = str(publish.get("concurrency", {}).get("group", ""))
    assert group, "publish job must declare a job-level concurrency group"
    assert "pull_request.number" not in group and "pr.number" not in group, (
        "publish concurrency group must not be keyed on the PR number "
        "(that lets different PRs' publishes race on the store push)"
    )


def test_publish_persist_step_fetches_before_push(wf):
    step = _persist_step(wf)
    assert step is not None, "publish job must have a step that pushes the store"
    run = step["run"]
    assert "git fetch" in run or "git pull" in run, (
        "store push must fetch/pull the latest default branch before pushing "
        "(a single-shot push drops the match on a non-fast-forward race)"
    )


def test_publish_persist_step_rebases_and_retries(wf):
    step = _persist_step(wf)
    assert step is not None
    run = step["run"]
    assert "rebase" in run, "store push must rebase onto the latest default branch"
    # a retry loop so a lost race re-applies rather than aborting the job
    assert ("for " in run or "while " in run or "until " in run), (
        "store push must retry in a loop so a losing race re-applies the match "
        "instead of dropping it"
    )


def test_publish_persist_is_not_single_shot_push(wf):
    # Guard against regressing back to a bare single push with no retry scaffolding.
    step = _persist_step(wf)
    assert step is not None
    run = step["run"]
    push_count_context = run.count("git push")
    assert push_count_context >= 1
    # the presence of a loop keyword ensures the push is inside retry scaffolding
    assert any(k in run for k in ("for ", "while ", "until ")), (
        "the store push must be wrapped in retry scaffolding, not a single-shot push"
    )
