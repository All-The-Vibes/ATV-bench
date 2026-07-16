"""Fork-safe publish split (santa re-review #7) — runs on EVERY push.

THE BUG: `CONTRIBUTING.md` documents "fork → open PR → maintainer labels run-match", but
the original single-workflow design ran the trusted publish job (contents/pages/id-token
write) inside the SAME `pull_request` run as the untrusted match job. GitHub gives a
`pull_request` run from a FORKED repo a READ-ONLY `GITHUB_TOKEN`, so the documented
external-contributor flow could score in-workspace but could never persist `league/` or
deploy Pages. It worked only for same-repo branches.

THE FIX (documented GitHub pattern): split the privileged phase onto a separate
`workflow_run` workflow that runs in the TRUSTED base-repo context with a full write token
even for fork PRs, and never checks out or executes untrusted PR code. The untrusted match
job still holds no token; it hands the trusted publish workflow (a) the sanitized result
artifact and (b) a trusted metadata artifact (submitter/opponent/match_id/bot_sha256 built
from GitHub context, NOT bot stdout).

These tests assert the two-workflow topology and that the split preserves every isolation
property. Comment-stripped, real-behavior assertions against the parsed workflows.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WF_DIR = Path(__file__).parent.parent / ".github" / "workflows"
MATCH_WF = WF_DIR / "league.yml"
PUBLISH_WF = WF_DIR / "league-publish.yml"


@pytest.fixture(scope="module")
def match_wf():
    assert MATCH_WF.exists(), "league.yml (match workflow) must exist"
    return yaml.safe_load(MATCH_WF.read_text())


@pytest.fixture(scope="module")
def publish_wf():
    assert PUBLISH_WF.exists(), "league-publish.yml (trusted publish workflow) must exist"
    return yaml.safe_load(PUBLISH_WF.read_text())


def _on(wf):
    return wf.get("on") or wf.get(True)  # PyYAML parses bare `on:` as boolean True


# --- the match workflow no longer holds the privileged publish job ---

def test_match_workflow_has_no_privileged_publish_job(match_wf):
    """The pull_request-triggered workflow must NOT contain a job with write scope: on a
    fork PR that token is read-only, so privileged work must live in the workflow_run
    workflow instead. Every job here must be read-only / no-token."""
    for name, job in match_wf["jobs"].items():
        perms = job.get("permissions", {})
        assert perms.get("contents") != "write", f"{name} must not have contents:write in the PR workflow"
        assert "pages" not in perms, f"{name} must not have pages scope in the PR workflow"
        assert "id-token" not in perms, f"{name} must not have id-token scope in the PR workflow"


def test_match_workflow_still_has_untrusted_match_job(match_wf):
    assert "match" in match_wf["jobs"], "match job must remain in league.yml"
    match = match_wf["jobs"]["match"]
    assert match.get("permissions") in ({}, {"contents": "read"}), \
        "match job must remain no-token / read-only"


def test_match_job_uploads_trusted_meta_artifact(match_wf):
    """The match job must emit the trusted match spec (submitter/opponent/match_id/
    bot_sha256, all from GitHub context) as a SEPARATE artifact the publish workflow reads,
    since a workflow_run event does not carry the PR author directly."""
    match = match_wf["jobs"]["match"]
    body = yaml.safe_dump(match)
    # a metadata file distinct from the bot-controlled match-result.json
    assert "match-meta" in body, "match job must produce a trusted match-meta artifact"
    uploads = [s for s in match["steps"]
               if "upload-artifact" in str(s.get("uses", ""))]
    assert uploads, "match job must upload its outputs"
    # the meta file must be included in an upload path (same or separate artifact)
    assert any("match-meta" in str(s.get("with", {}).get("path", "")) for s in uploads), \
        "an upload-artifact step must include match-meta.json in its path"


# --- the trusted publish workflow ---

def test_publish_workflow_triggers_on_workflow_run(publish_wf):
    on = _on(publish_wf)
    assert "workflow_run" in on, "publish workflow must trigger on workflow_run (fork-safe)"
    wr = on["workflow_run"]
    assert "league" in [str(w) for w in wr.get("workflows", [])], \
        "publish must trigger on the league (match) workflow completing"
    assert "completed" in [str(t) for t in wr.get("types", [])], \
        "publish must trigger on the 'completed' type"


def test_publish_workflow_runs_only_on_success(publish_wf):
    """Must gate on the triggering run's conclusion == success so a failed/cancelled match
    (no valid artifact) never drives a publish."""
    body = yaml.safe_dump(publish_wf)
    assert "conclusion" in body and "success" in body, \
        "publish workflow must gate on workflow_run.conclusion == 'success'"


def test_publish_workflow_has_write_scopes(publish_wf):
    """The trusted workflow_run context has a full token even for fork PRs — it needs
    contents:write to persist the store. Pages deploy moved to league-deploy.yml (G4,
    santa round-2), so the publish job no longer holds pages/id-token scope."""
    job = next(iter(publish_wf["jobs"].values()))
    perms = job.get("permissions", {}) or publish_wf.get("permissions", {})
    assert perms.get("contents") == "write", "publish must have contents:write"
    # actions:read is required to download artifacts from the triggering run
    assert perms.get("actions") == "read", "publish must have actions:read to fetch artifacts"
    # Pages scopes moved out with the deploy step (no fence→upload TOCTOU here anymore).
    assert "pages" not in perms, "publish no longer deploys Pages; scope moved to league-deploy.yml"
    assert "id-token" not in perms, "publish no longer needs Pages OIDC scope"


def test_publish_workflow_never_executes_bot(publish_wf):
    body = yaml.safe_dump(publish_wf).lower()
    for forbidden in ("docker run", "arena", "refs/pull/", "codeclash run"):
        assert forbidden not in body, f"publish workflow must not execute bot code: {forbidden!r}"


def test_publish_workflow_downloads_artifacts_from_triggering_run(publish_wf):
    """It must fetch the artifacts from the TRIGGERING run (cross-run download needs the
    run id + token), not re-run the match."""
    body = yaml.safe_dump(publish_wf)
    assert "download-artifact" in body or "gh run download" in body or "gh api" in body, \
        "publish workflow must download the match artifacts from the triggering run"
    assert "workflow_run.id" in body or "event.workflow_run.id" in body, \
        "cross-run artifact download must reference the triggering workflow_run.id"


def test_publish_workflow_checks_out_trusted_ref_only(publish_wf):
    """Every checkout must pin a trusted ref (default branch), never the PR head — the
    whole point of the split is to never run PR-controlled code with a write token."""
    job = next(iter(publish_wf["jobs"].values()))
    checkouts = [s for s in job["steps"] if "actions/checkout" in str(s.get("uses", ""))]
    for step in checkouts:
        ref = str(step.get("with", {}).get("ref", ""))
        assert "pull_request" not in ref and "head" not in ref.lower(), \
            f"publish checkout must be a trusted ref, got {ref!r}"


def test_publish_workflow_ingests_with_require_spec(publish_wf):
    body = yaml.safe_dump(publish_wf)
    assert "--require-spec" in body, \
        "publish must ingest with --require-spec (bind ok artifact to the trusted spec)"
    for key in ("ATV_SUBMITTER", "ATV_OPPONENT", "ATV_MATCH_ID"):
        assert key in body, f"publish must export {key} from the trusted meta artifact"


def test_publish_workflow_persists_with_retry_loop(publish_wf):
    """The optimistic-concurrency persist loop (santa #3) must live here now."""
    body = yaml.safe_dump(publish_wf)
    assert "git push" in body, "publish must persist the store"
    assert "while" in body or "until" in body, "persist must run in a retry loop"
    assert "publish ingest" in body, "persist loop must re-ingest (optimistic re-apply)"


def test_publish_workflow_persists_but_does_not_deploy(publish_wf):
    """G4 (santa round-2): Pages deploy moved to league-deploy.yml (push-triggered) to close
    the fence→upload TOCTOU. The publish job must still persist the store (durable history)
    but must NOT upload/deploy Pages — that is the deploy workflow's job, triggered by the
    push this persist step makes."""
    job = next(iter(publish_wf["jobs"].values()))
    steps = job["steps"]

    def code(s):
        return "\n".join(ln.split("#", 1)[0] for ln in str(s.get("run", "")).splitlines())

    assert any("ingest" in code(s) and "git push" in code(s) for s in steps), \
        "publish must still ingest + persist the match to the store"
    assert not any("upload-pages-artifact" in str(s.get("uses", "")) for s in steps), \
        "publish must NOT upload Pages artifact (deploy moved to league-deploy.yml)"
    assert not any("deploy-pages" in str(s.get("uses", "")) for s in steps), \
        "publish must NOT deploy Pages (deploy moved to league-deploy.yml)"
