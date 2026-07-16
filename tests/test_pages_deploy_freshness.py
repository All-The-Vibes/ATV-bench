"""Pages-deploy freshness + G4/G5 (santa rounds 1-2).

History:
- santa #6: the original PR deployed a per-attempt snapshot with no settled-store rebuild.
- F4 (round 1): added a head-SHA fence loop inside league-publish.yml before upload.
- G4 (round 2, Reviewer B): the fence still left a residual TOCTOU — origin could advance
  between the final SHA re-check and the separate upload-pages-artifact step, so a slower
  older publish could still deploy a stale board.
- G5 (round 2, Reviewer B): a merged submission PR did not trigger a rebuild, so a first-
  time entrant's row did not appear until the NEXT match landed.

THE CANONICAL FIX (both reviewers' top suggestion): move Pages DEPLOY out of the
event-driven publish job into a dedicated workflow triggered by `push` to the default
branch (paths: league/**). The triggering commit IS the settled store head, so:
- G4: the deployed artifact is built from the exact commit that triggered the run; a
  `pages` concurrency group with cancel-in-progress makes the newest push supersede any
  older in-flight deploy (last-write-wins) — no stale board.
- G5: a merged submission PR pushes to the default branch, which triggers a rebuild+deploy,
  so the new entrant's row appears on merge without waiting for another match.

These tests assert the deploy topology. Comment-stripped, real-behavior assertions.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WF_DIR = Path(__file__).parent.parent / ".github" / "workflows"
PUBLISH_WORKFLOW = WF_DIR / "league-publish.yml"
DEPLOY_WORKFLOW = WF_DIR / "league-deploy.yml"


@pytest.fixture(scope="module")
def deploy_wf():
    assert DEPLOY_WORKFLOW.exists(), (
        "league-deploy.yml must exist: Pages deploy moved to a dedicated push-triggered "
        "workflow so the deployed board reflects the settled store head (G4/G5)"
    )
    return yaml.safe_load(DEPLOY_WORKFLOW.read_text())


@pytest.fixture(scope="module")
def publish_wf():
    return yaml.safe_load(PUBLISH_WORKFLOW.read_text())


def _on(wf):
    return wf.get("on") or wf.get(True)  # PyYAML parses bare `on:` as boolean True


def _steps(wf, job):
    return wf["jobs"][job]["steps"]


def _uses(step, action_prefix):
    return str(step.get("uses", "")).startswith(action_prefix)


def _code(step):
    lines = []
    for ln in str(step.get("run", "")).splitlines():
        c = ln.split("#", 1)[0]
        if c.strip():
            lines.append(c)
    return "\n".join(lines)


# --- G4/G5: deploy is triggered by the push to the default branch (settled store) ---

def test_deploy_workflow_triggers_on_push_to_default_branch(deploy_wf):
    on = _on(deploy_wf)
    assert "push" in on, "deploy must trigger on push (the settled store commit)"
    branches = on["push"].get("branches", [])
    assert any(b in ("main", "master") for b in branches), (
        "deploy must trigger on push to the default branch, where the store settles"
    )
    # scoped to store changes so unrelated pushes don't redeploy
    paths = on["push"].get("paths", [])
    assert any("league" in p for p in paths), "deploy should be scoped to league/ changes"


def test_deploy_workflow_has_pages_concurrency_group_newest_wins(deploy_wf):
    """A pages concurrency group with cancel-in-progress makes the newest push supersede an
    older in-flight deploy — last-write-wins closes the residual stale-deploy window (G4)."""
    conc = deploy_wf.get("concurrency")
    assert conc, "deploy workflow must declare a concurrency group"
    if isinstance(conc, str):
        pytest.fail("concurrency must set cancel-in-progress: true (map form), not a bare group")
    assert conc.get("cancel-in-progress") is True, (
        "cancel-in-progress: true so a newer settled-store deploy cancels an older stale one"
    )


def test_deploy_builds_from_the_triggering_commit(deploy_wf):
    """The deploy builds ./site from the checked-out triggering commit — no head-SHA fence
    loop needed because the trigger commit IS the settled head being deployed."""
    steps = _steps(deploy_wf, "deploy")
    assert any(_uses(s, "actions/checkout") for s in steps), "must check out the repo"
    build = next((s for s in steps if "publish build" in _code(s)), None)
    assert build is not None, "deploy must rebuild ./site from the store"
    assert "./site" in _code(build)
    assert any(_uses(s, "actions/upload-pages-artifact") for s in steps)
    assert any(_uses(s, "actions/deploy-pages") for s in steps)


def test_deploy_never_executes_bot_or_pr_code(deploy_wf):
    """Trusted deploy: it runs on push to the default branch (already-merged, reviewed
    code) and must NOT do an editable install (build hooks) of the checked-out tree."""
    steps = _steps(deploy_wf, "deploy")
    for s in steps:
        assert "pip install -e" not in _code(s), "no editable install (executes build hooks)"


# --- the publish (workflow_run) job no longer owns Pages deploy ---

def test_publish_workflow_no_longer_deploys_pages(publish_wf):
    """Deploy moved out of the event-driven publish job (G4): keeping it there reintroduced
    the fence→upload TOCTOU. The publish job now only PERSISTS the store; the push that
    persist creates triggers league-deploy.yml."""
    steps = _steps(publish_wf, "publish")
    assert not any(_uses(s, "actions/deploy-pages") for s in steps), (
        "publish job must not deploy Pages; the dedicated push-triggered deploy workflow does"
    )
    assert not any(_uses(s, "actions/upload-pages-artifact") for s in steps), (
        "publish job must not upload the Pages artifact anymore"
    )


def test_publish_still_persists_the_store(publish_wf):
    """The persist loop (durable history) must remain in the publish job."""
    steps = _steps(publish_wf, "publish")
    assert any("git push" in _code(s) and "ingest" in _code(s) for s in steps), (
        "publish must still ingest + persist the match to the store"
    )
