"""Action-perms CI tripwire (eng T7) — runs on EVERY push.

The league Action runs untrusted, harness-authored bots. The security model is a
two-job split: an untrusted MATCH job with no token / no egress / caps that writes
only an artifact, and a trusted PUBLISH job that consumes the artifact and never
executes bot code. This test parses the workflow YAML and asserts those properties
so a permissions regression breaks CI instead of shipping a relocated-RCE hole.

Mirrors v1's argv-tripwire pattern. Fast, hermetic (pure YAML parse, no runner).
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


def _jobs(wf):
    return wf["jobs"]


def test_workflow_has_match_and_publish_jobs(wf):
    jobs = _jobs(wf)
    assert "match" in jobs, "untrusted match job required"
    assert "publish" in jobs, "trusted publish job required"


def test_match_job_has_no_permissions(wf):
    match = _jobs(wf)["match"]
    # permissions: {} (or contents: read only) — no write scope, no token power
    perms = match.get("permissions", None)
    assert perms == {} or perms == {"contents": "read"}, f"match perms too broad: {perms}"


def test_match_job_never_persists_credentials(wf):
    match = _jobs(wf)["match"]
    checkouts = [s for s in match["steps"] if "actions/checkout" in str(s.get("uses", ""))]
    for step in checkouts:
        with_ = step.get("with", {})
        assert with_.get("persist-credentials") is False, \
            "match job must checkout with persist-credentials: false"


def test_match_job_is_github_hosted_not_self_hosted(wf):
    for name, job in _jobs(wf).items():
        runs_on = str(job.get("runs-on", ""))
        assert "self-hosted" not in runs_on, f"{name} must not use a self-hosted runner"
        assert runs_on.startswith("ubuntu-"), f"{name} must pin a GitHub-hosted runner"


def test_match_job_has_resource_and_time_caps(wf):
    match = _jobs(wf)["match"]
    # job-level timeout so an infinite-loop bot can't run forever
    assert isinstance(match.get("timeout-minutes"), int) and match["timeout-minutes"] <= 30
    # the bot runs in a container with cpu/mem/pid caps + no network + non-root RO
    body = yaml.safe_dump(match)
    assert "--network" in body and "none" in body, "match must run bot with --network none"
    assert "--memory" in body, "match must cap bot memory"
    assert "--pids-limit" in body, "match must cap bot pids"
    assert "--read-only" in body, "match must run bot container read-only"
    assert "--user" in body, "match must run bot as non-root user"


def test_match_job_has_no_pages_or_oidc_token(wf):
    match = _jobs(wf)["match"]
    perms = match.get("permissions", {})
    assert "pages" not in perms
    assert "id-token" not in perms  # no OIDC
    body = yaml.safe_dump(match)
    assert "GITHUB_TOKEN" not in body, "match job must not reference GITHUB_TOKEN"


def test_publish_job_depends_on_match_and_consumes_artifact(wf):
    publish = _jobs(wf)["publish"]
    needs = publish.get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    assert "match" in needs, "publish must depend on match"
    body = yaml.safe_dump(publish)
    assert "download-artifact" in body, "publish must consume the match artifact"


def test_publish_job_does_not_execute_bot(wf):
    publish = _jobs(wf)["publish"]
    body = yaml.safe_dump(publish).lower()
    # publish validates + builds the board; it must not run the bot or the arena
    for forbidden in ("docker run", "python main.py", "./run.sh", "arena", "codeclash run"):
        assert forbidden not in body, f"publish job must not execute bot: found {forbidden!r}"
    # it SHOULD validate the artifact against the schema
    assert "validate" in body or "schema" in body


def test_publish_job_has_pages_write_but_match_does_not(wf):
    publish = _jobs(wf)["publish"]
    perms = publish.get("permissions", {})
    # only the trusted job may write Pages
    assert perms.get("pages") == "write" or perms.get("contents") == "write"


def test_first_time_contributor_gate_via_environment(wf):
    # first-time contributor runs require manual approval — via a protected
    # environment on the match job (GitHub gates deployments to environments).
    match = _jobs(wf)["match"]
    assert "environment" in match, "match job must use a protected environment for approval gate"


def test_match_job_suppresses_bot_stderr(wf):
    # untrusted bot stderr must NOT reach the public job log (only stdout JSON, and
    # only into the artifact). Assert the docker run redirects stderr away.
    match = _jobs(wf)["match"]
    body = yaml.safe_dump(match)
    assert "2>/dev/null" in body or "2> /dev/null" in body, \
        "match job must redirect untrusted bot stderr to /dev/null (public-log leak)"


def test_match_job_validates_json_before_upload(wf):
    # a bot can exit 0 while printing non-JSON; the match job must sanitize the
    # artifact to a schema-shaped record before upload-artifact runs.
    match = _jobs(wf)["match"]
    steps = match["steps"]
    step_names = [s.get("name", "") for s in steps]
    validate_idx = next((i for i, n in enumerate(step_names) if "Validate result" in n), None)
    upload_idx = next((i for i, n in enumerate(step_names) if "Upload result" in n), None)
    assert validate_idx is not None, "match job must validate JSON before upload"
    assert upload_idx is not None and validate_idx < upload_idx, \
        "JSON validation must run BEFORE the artifact upload"
