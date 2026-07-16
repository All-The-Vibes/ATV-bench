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
PUBLISH_WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "league-publish.yml"


@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.exists(), "league.yml workflow must exist"
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def pub_wf():
    """The TRUSTED publish workflow. santa re-review #7 split the privileged publish job
    out of the pull_request-triggered league.yml onto a workflow_run trigger so fork PRs
    (read-only pull_request token) can persist/deploy. The publish assertions target it."""
    assert PUBLISH_WORKFLOW.exists(), "league-publish.yml workflow must exist"
    return yaml.safe_load(PUBLISH_WORKFLOW.read_text())


def _jobs(wf):
    return wf["jobs"]


def _publish_job(pub_wf):
    return pub_wf["jobs"]["publish"]


def test_workflow_has_match_and_publish_jobs(wf, pub_wf):
    assert "match" in _jobs(wf), "untrusted match job required in league.yml"
    assert "publish" in _jobs(pub_wf), "trusted publish job required in league-publish.yml"


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
    # The match job references GITHUB_TOKEN in no ACTUAL command (comments explaining the
    # fork-token model are fine). Strip comments from every run body before checking.
    for step in match["steps"]:
        run = str(step.get("run", ""))
        code = "\n".join(ln.split("#", 1)[0] for ln in run.splitlines())
        assert "GITHUB_TOKEN" not in code, "match job must not use GITHUB_TOKEN in any command"


def test_publish_job_depends_on_match_and_consumes_artifact(pub_wf):
    # santa #7: publish now triggers on workflow_run (the match workflow completing), not
    # a `needs:` dependency — that is what lets fork PRs reach a trusted write token. It
    # still consumes the match artifact (downloaded from the triggering run).
    on = pub_wf.get("on") or pub_wf.get(True)
    assert "workflow_run" in on, "publish must trigger on the match workflow completing"
    assert "league" in [str(w) for w in on["workflow_run"].get("workflows", [])], \
        "publish must trigger on the league (match) workflow"
    body = yaml.safe_dump(_publish_job(pub_wf))
    assert "download-artifact" in body, "publish must consume the match artifact"


def test_publish_job_does_not_execute_bot(pub_wf):
    publish = _publish_job(pub_wf)
    body = yaml.safe_dump(publish).lower()
    # publish validates + builds the board; it must not run the bot or the arena
    for forbidden in ("docker run", "python main.py", "./run.sh", "arena", "codeclash run"):
        assert forbidden not in body, f"publish job must not execute bot: found {forbidden!r}"
    # it SHOULD validate the artifact against the schema (ingest --require-spec validates)
    assert "require-spec" in body or "validate" in body or "schema" in body


def test_publish_job_has_pages_write_but_match_does_not(wf, pub_wf):
    publish = _publish_job(pub_wf)
    perms = publish.get("permissions", {})
    # only the trusted job may write Pages
    assert perms.get("pages") == "write" or perms.get("contents") == "write"
    # and the untrusted match workflow must NOT hold pages/write scope anywhere
    for name, job in _jobs(wf).items():
        jperms = job.get("permissions", {})
        assert "pages" not in jperms, f"{name} in league.yml must not have pages scope"
        assert jperms.get("contents") != "write", f"{name} must not have contents:write"


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


def _checkout_steps(job):
    return [s for s in job["steps"] if "actions/checkout" in str(s.get("uses", ""))]


def test_publish_job_pins_checkout_to_trusted_ref(pub_wf):
    # santa round-1 (Reviewer B): the TRUSTED publish job (pages:write, id-token:write)
    # must pin to the default branch, never a PR head, or it executes PR-controlled code
    # with write privileges (pwn-request escalation). Under workflow_run this is doubly
    # important: the trigger runs in base-repo context with a full token.
    publish = _publish_job(pub_wf)
    checkouts = _checkout_steps(publish)
    assert checkouts, "publish job must check out trusted code explicitly"
    for step in checkouts:
        ref = str(step.get("with", {}).get("ref", ""))
        assert ref, "publish checkout must pin an explicit ref (not the PR merge ref)"
        assert "event.pull_request" not in ref and "head" not in ref.lower(), \
            f"publish checkout ref must be trusted, got: {ref}"


def test_publish_job_does_not_editable_install_pr_code(pub_wf):
    # the trusted job must not `pip install -e` (which runs PR-authored build/setup
    # code). It may install pinned deps, but never execute the checked-out package's
    # build hooks from an untrusted ref. Check the actual run: commands, not comments.
    publish = _publish_job(pub_wf)
    for step in publish["steps"]:
        run = step.get("run", "")
        run_lines = [ln.split("#", 1)[0] for ln in run.splitlines()]  # strip comments
        code = "\n".join(run_lines)
        assert "pip install -e" not in code, \
            "publish job must not editable-install PR-controlled code"


def test_match_job_stages_the_submitted_bot(wf):
    # the match job must actually materialize the submitted bot into the mount dir,
    # else every match mounts an empty dir and yields a crash (no real matches). The
    # bot is staged as DATA (checked out into a path, copied into submission/), never
    # executed on the host — only inside the sandbox container.
    match = _jobs(wf)["match"]
    body = yaml.safe_dump(match)
    assert "submission/main.py" in body or "submission" in body
    stages_bot = (
        "refs/pull/" in body            # checks out the PR head as data
        and "main.py" in body            # extracts the bot file
    )
    assert stages_bot, "match job must stage the submitted bot into the submission dir"


def test_match_job_bot_path_is_identity_pinned(wf):
    # R4 (Reviewer B): the bot must be the submitter's OWN identity-pinned path, not
    # the first main.py found anywhere (a PR could smuggle another identity's bot).
    match = _jobs(wf)["match"]
    body = yaml.safe_dump(match)
    assert "submissions/${SUBMITTER}/main.py" in body or "submissions/$SUBMITTER/main.py" in body, \
        "bot path must be pinned to league/submissions/<submitter>/main.py"
    # must NOT pick an arbitrary bot via find|head
    assert "head -n1" not in body and "head -1" not in body, \
        "must not select an arbitrary bot with find|head"


def test_match_job_stages_bot_without_credentials(wf):
    # every checkout in the match job (including the PR-head bot staging) must be
    # credential-less — the untrusted job never holds a token.
    match = _jobs(wf)["match"]
    for step in _checkout_steps(match):
        assert step.get("with", {}).get("persist-credentials") is False, \
            "every match-job checkout must set persist-credentials: false"


def test_publish_ingest_binds_ok_artifact_to_trusted_spec(pub_wf):
    # Reviewer B (held FAIL R1-5): an ok artifact's player_a/player_b/match_id come from
    # the untrusted bot's stdout. The publish ingest step must bind them to a trusted
    # match spec (--require-spec + ATV_SUBMITTER/OPPONENT/MATCH_ID). Under the santa #7
    # workflow_run split, the spec is loaded from the TRUSTED match-meta artifact (authored
    # by the match job from GitHub context) into steps.meta.outputs.*, then exported here.
    publish = _publish_job(pub_wf)
    ingest = next((s for s in publish["steps"]
                   if "ingest" in str(s.get("run", "")) and "--require-spec" in str(s.get("run", ""))),
                  None)
    assert ingest is not None, "publish job must have an ingest --require-spec step"
    env = ingest.get("env", {})
    for key in ("ATV_SUBMITTER", "ATV_OPPONENT", "ATV_MATCH_ID"):
        assert key in env, f"ingest must export {key} for the trusted match spec"
    # the spec identities come from the trusted meta artifact (steps.meta.outputs), never
    # from the bot artifact
    assert "steps.meta.outputs" in str(env["ATV_SUBMITTER"]), \
        "ATV_SUBMITTER must come from the trusted match-meta, not a bot-supplied value"
    assert "steps.meta.outputs" in str(env["ATV_MATCH_ID"]), \
        "ATV_MATCH_ID must come from the trusted match-meta, not a bot-supplied value"


def test_match_meta_is_authored_from_github_context(wf):
    # The trust anchor: the match-meta the publish workflow binds to must be built by the
    # match job from GitHub context (the PR author's login + the run id), never bot stdout.
    match = _jobs(wf)["match"]
    meta_step = next((s for s in match["steps"]
                      if "match-meta.json" in str(s.get("run", ""))), None)
    assert meta_step is not None, "match job must write the trusted match-meta.json"
    env = meta_step.get("env", {})
    assert "pull_request.user.login" in str(env.get("SUBMITTER", "")), \
        "match-meta SUBMITTER must be the PR author's GitHub identity"
    assert "github.run_id" in str(env.get("MATCH_ID", "")), \
        "match-meta MATCH_ID must be the run-scoped id"


def test_match_id_is_stable_across_job_reruns(wf):
    # Reviewer B (santa round-1): match_id must NOT include github.run_attempt. A publish
    # re-run increments run_attempt, so an attempt-scoped id would make the spec expect a
    # different match_id than the retained artifact carries -> an honest ok result rebinds
    # to a CRASH forfeit against a legitimate submitter. Pin to github.run_id alone.
    body = yaml.safe_dump(wf)
    assert "github.run_attempt" not in body, \
        "match_id must be stable across job reruns (no github.run_attempt)"


def test_publish_ingest_is_the_single_fail_closed_gate(pub_wf):
    # Reviewer B (santa round-1): a bot can emit a known status with an invalid schema
    # (e.g. {"status":"ok"} with no players). A standalone hard-failing `publish validate`
    # step before ingest would abort the whole job -> NO score (bot-controlled no-score
    # DoS). The ingest step must be the single gate that converts any validation failure
    # into a spec-bound submitter forfeit, so there is no separate pre-ingest validate
    # step that can abort scoring.
    publish = _publish_job(pub_wf)
    runs = [str(s.get("run", "")) for s in publish["steps"]]
    ingest_runs = [r for r in runs if "publish ingest" in r]
    assert ingest_runs, "publish job must ingest the artifact"
    standalone_validate = [r for r in runs if "publish validate" in r and "ingest" not in r]
    assert not standalone_validate, \
        "no standalone `publish validate` step may abort scoring before ingest"


def test_match_job_wraps_bot_in_a_container_timeout(wf):
    # Reviewer B (santa round-2): the job-level timeout cancels the WHOLE job before the
    # CRASH fallback + upload can run, so a hanging bot yields NO score. The docker run
    # must be wrapped in a `timeout` shorter than the job so expiry falls through to the
    # CRASH fallback within the step and still uploads a scoreable artifact.
    match = _jobs(wf)["match"]
    body = yaml.safe_dump(match)
    assert "timeout " in body, \
        "match job must wrap the bot container in a `timeout` (per-container time cap)"


def test_match_job_does_not_mask_container_exit_status_with_head(wf):
    # Reviewer B (santa round-3): `timeout docker run ... | head -c N > f` makes the
    # pipeline exit status `head`'s, not the container's. A bot that prints valid ok JSON
    # then hangs/exits non-zero would be scored as ok (timeout kills it, but the captured
    # JSON is valid+non-empty). The step must capture the timeout/docker status (pipefail
    # or an explicit status check) so a non-zero container run falls back to CRASH.
    match = _jobs(wf)["match"]
    body = yaml.safe_dump(match)
    assert "pipefail" in body or "PIPESTATUS" in body or "${status" in body, \
        "match job must not let `head` mask the container exit status (use pipefail / capture status)"


def test_match_job_caps_artifact_size_before_upload(wf):
    # Reviewer B (santa round-2): bot stdout is redirected to match-result.json with no
    # cap; the sanitizer and publish both read it whole. A multi-GB artifact can OOM/kill
    # the trusted publish job before scoring. The match job must bound the artifact size
    # and fall back to a CRASH record when exceeded.
    match = _jobs(wf)["match"]
    # width high so long shell lines aren't folded across `\n\` continuations (folding
    # can split the `wc -c < match-result.json` token and defeat a naive substring test).
    body = yaml.safe_dump(match, width=10**9)
    assert "wc -c < match-result.json" in body or "stat -c%s match-result.json" in body, \
        "match job must measure match-result.json size before upload"


def test_workflow_only_triggers_on_pull_request(wf):
    # Reviewer B (santa round-2): workflow_dispatch has no PR author, so MatchSpec.from_env
    # aborts the publish job (no trusted submitter to score). Remove the half-wired trigger
    # rather than leave a path that runs the untrusted bot but can never score it.
    on = wf.get("on") or wf.get(True)  # PyYAML parses bare `on:` as boolean True
    assert "workflow_dispatch" not in on, \
        "workflow_dispatch has no scoreable path (no PR-authored submitter); remove it"
    assert "pull_request" in on
