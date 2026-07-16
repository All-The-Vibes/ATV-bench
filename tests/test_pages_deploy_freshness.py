"""Stale-Pages-deploy tripwire (santa re-review #6) — runs on EVERY push.

The persist retry loop makes the `league/` STORE push race-safe, but the PR shipped a
Pages deploy that was NOT: each publish built `./site` inside its own loop attempt from
its own snapshot, then `upload-pages-artifact` + `deploy-pages` ran AFTER the loop with no
final rebuild from the settled default-branch head. If an older publisher's deploy
finishes last, GitHub Pages regresses to a stale board even though the store is correct.

THE FIX: after the store push settles, rebuild `./site` from a fresh
`origin/<default_branch>` immediately before `upload-pages-artifact`, so the deployed board
always reflects the latest settled store (this match plus any that landed concurrently).

Mirrors the test_publish_race / test_action_isolation tripwire pattern: comment-stripped,
real-behavior assertions against the parsed workflow.
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


def _publish_steps(wf):
    return wf["jobs"]["publish"]["steps"]


def _step_index(steps, predicate):
    for i, step in enumerate(steps):
        if predicate(step):
            return i
    return -1


def _uses(step, action_prefix):
    return str(step.get("uses", "")).startswith(action_prefix)


def _code(step):
    """A step's run shell with comment lines stripped (real behavior only)."""
    lines = []
    for ln in str(step.get("run", "")).splitlines():
        c = ln.split("#", 1)[0]
        if c.strip():
            lines.append(c)
    return "\n".join(lines)


def test_has_a_rebuild_before_pages_upload(wf):
    """There must be a step that rebuilds ./site from the settled store AFTER the persist
    push and BEFORE upload-pages-artifact — otherwise the deployed board can be stale.

    The rebuild must be a DEDICATED step, distinct from the persist loop (whose in-loop
    build reflects only that attempt's snapshot, not the final settled head)."""
    steps = _publish_steps(wf)
    upload_idx = _step_index(steps, lambda s: _uses(s, "actions/upload-pages-artifact"))
    assert upload_idx != -1, "publish job must upload a Pages artifact"

    persist_idx = _step_index(steps, lambda s: "git push" in _code(s))
    assert persist_idx != -1, "publish job must have a persist (git push) step"

    # A settled-rebuild step: fetches/resets to origin default and rebuilds ./site, and it
    # is NOT the persist step itself (must run after the push settles).
    def is_settled_rebuild(s):
        code = _code(s)
        fetches = "git fetch" in code or "git pull" in code
        rebuilds = "publish build" in code and "./site" in code
        pushes = "git push" in code
        return fetches and rebuilds and not pushes

    rebuild_idx = _step_index(steps, is_settled_rebuild)
    assert rebuild_idx != -1, (
        "publish job must have a DEDICATED step that rebuilds ./site from a fresh "
        "origin/<default_branch> before uploading the Pages artifact (a rebuild that is "
        "part of the push loop reflects only that attempt's snapshot, not the settled head)"
    )
    assert persist_idx < rebuild_idx < upload_idx, (
        "the settled-store rebuild must run AFTER the persist push and BEFORE "
        "upload-pages-artifact"
    )


def test_rebuild_resets_to_origin_default_branch(wf):
    """The final rebuild must derive from the fetched trusted branch head, not this job's
    possibly-behind local snapshot."""
    steps = _publish_steps(wf)

    def is_settled_rebuild(s):
        code = _code(s)
        return (("git fetch" in code or "git pull" in code)
                and "publish build" in code and "git push" not in code)

    idx = _step_index(steps, is_settled_rebuild)
    assert idx != -1
    code = _code(steps[idx])
    assert "reset --hard" in code and "origin/" in code, (
        "the pre-deploy rebuild must reset --hard to origin/<default_branch> so the "
        "deployed board reflects the settled store, including concurrently-landed matches"
    )


def test_upload_uses_the_rebuilt_site_dir(wf):
    """The upload-pages-artifact must point at ./site (the rebuilt dir)."""
    steps = _publish_steps(wf)
    upload = next(s for s in steps if _uses(s, "actions/upload-pages-artifact"))
    path = str(upload.get("with", {}).get("path", ""))
    assert path.strip("./").rstrip("/") == "site", (
        f"upload-pages-artifact must publish ./site (the rebuilt dir), got {path!r}"
    )
