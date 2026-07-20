"""Fork-safe League publication and PR-workflow forgery tripwires."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

WF_DIR = Path(__file__).parent.parent / ".github" / "workflows"
MATCH_WF = WF_DIR / "league.yml"
PUBLISH_WF = WF_DIR / "league-publish.yml"
DEPLOY_WF = WF_DIR / "league-deploy.yml"

RUN_ID = "123"
PR_NUMBER = 7
RUN_SHA = "c" * 40
HEAD_SHA = RUN_SHA
TRUSTED_SHA = "b" * 40
WORKFLOW_BLOB = "d" * 40
BOT_BLOB = "e" * 40
BOT_BYTES = b"print('safe bot')\n"
BOT_SHA256 = hashlib.sha256(BOT_BYTES).hexdigest()


@pytest.fixture(scope="module")
def match_wf():
    return yaml.safe_load(MATCH_WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def publish_wf():
    return yaml.safe_load(PUBLISH_WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def deploy_wf():
    return yaml.safe_load(DEPLOY_WF.read_text(encoding="utf-8"))


def _on(workflow):
    return workflow.get("on") or workflow.get(True)


def _step(workflow, *, job="publish", step_id=None, name_fragment=None):
    for item in workflow["jobs"][job]["steps"]:
        if step_id is not None and item.get("id") == step_id:
            return item
        if name_fragment is not None and name_fragment.lower() in item.get("name", "").lower():
            return item
    raise AssertionError(f"step not found: id={step_id!r}, name={name_fragment!r}")


def _python_heredoc(step) -> str:
    run = step["run"]
    prefix = "python3 - <<'PY'\n"
    assert prefix in run
    return run.split(prefix, 1)[1].rsplit("\nPY", 1)[0]


def _default_api_responses(*, workflow_blob=WORKFLOW_BLOB, bot_mode="100644"):
    workflow_path = ".github/workflows/league.yml"
    bot_path = "league/submissions/octocat/main.py"
    return {
        f"repos/All-The-Vibes/ATV-bench/actions/runs/{RUN_ID}": {
            "id": int(RUN_ID),
            "event": "pull_request",
            "conclusion": "success",
            "path": workflow_path,
            "head_sha": RUN_SHA,
            "pull_requests": [],
        },
        f"repos/All-The-Vibes/ATV-bench/commits/{RUN_SHA}/pulls": [
            {"number": PR_NUMBER, "user": {"login": "octocat"}}
        ],
        "repos/All-The-Vibes/ATV-bench": {"default_branch": "main"},
        "repos/All-The-Vibes/ATV-bench/git/ref/heads/main": {
            "object": {"sha": TRUSTED_SHA}
        },
        f"repos/All-The-Vibes/ATV-bench/git/trees/{RUN_SHA}?recursive=1": {
            "truncated": False,
            "tree": [
                {
                    "path": workflow_path,
                    "type": "blob",
                    "mode": "100644",
                    "sha": workflow_blob,
                },
                {
                    "path": bot_path,
                    "type": "blob",
                    "mode": bot_mode,
                    "sha": BOT_BLOB,
                    "size": len(BOT_BYTES),
                }
            ],
        },
        f"repos/All-The-Vibes/ATV-bench/git/trees/{TRUSTED_SHA}?recursive=1": {
            "truncated": False,
            "tree": [
                {
                    "path": workflow_path,
                    "type": "blob",
                    "mode": "100644",
                    "sha": WORKFLOW_BLOB,
                }
            ],
        },
        f"repos/All-The-Vibes/ATV-bench/git/blobs/{BOT_BLOB}": {
            "encoding": "base64",
            "content": base64.b64encode(BOT_BYTES).decode("ascii"),
        },
    }


def _execute_preflight(
    publish_wf,
    monkeypatch,
    tmp_path,
    *,
    responses=None,
    api_error: Exception | None = None,
):
    output = tmp_path / "github-output.txt"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "All-The-Vibes/ATV-bench")
    monkeypatch.setenv("RUN_ID", RUN_ID)
    monkeypatch.setenv("TRUSTED_WORKFLOW_PATH", ".github/workflows/league.yml")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    calls = []
    table = responses or _default_api_responses()

    def fake_check_output(args, **_kwargs):
        calls.append(args)
        if api_error is not None:
            raise api_error
        endpoint = args[2]
        if endpoint not in table:
            raise AssertionError(f"unexpected API endpoint: {endpoint}")
        return json.dumps(table[endpoint])

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    script = _python_heredoc(_step(publish_wf, step_id="meta"))
    try:
        exec(compile(script, "league-publish-preflight.py", "exec"), {})
    except SystemExit as exc:
        return int(exc.code or 0), output.read_text() if output.exists() else "", calls
    return 0, output.read_text(encoding="utf-8"), calls


def _execute_artifact_check(publish_wf, monkeypatch, tmp_path, meta):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "match-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "match-result.json").write_text(
        json.dumps(
            {
                "status": "crash",
                "loser": "octocat",
                "opponent": "byok-anchor",
                "match_id": RUN_ID,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    expected = {
        "EXPECTED_SUBMITTER": "octocat",
        "EXPECTED_OPPONENT": "byok-anchor",
        "EXPECTED_MATCH_ID": RUN_ID,
        "EXPECTED_BOT_SHA256": BOT_SHA256,
        "EXPECTED_PR_NUMBER": str(PR_NUMBER),
        "EXPECTED_HEAD_SHA": HEAD_SHA,
    }
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    step = _step(publish_wf, name_fragment="artifact metadata")
    try:
        exec(compile(_python_heredoc(step), "league-artifact-check.py", "exec"), {})
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_match_workflow_is_read_only_and_uses_immutable_base_and_head(match_wf):
    match = match_wf["jobs"]["match"]
    assert match["permissions"] == {"contents": "read"}
    checkouts = [s for s in match["steps"] if "actions/checkout" in str(s.get("uses", ""))]
    assert len(checkouts) == 2
    assert "pull_request.base.sha" in str(checkouts[0]["with"]["ref"])
    assert checkouts[0]["with"]["persist-credentials"] is False
    assert "pull_request.head.repo.full_name" in str(checkouts[1]["with"]["repository"])
    assert "pull_request.head.sha" in str(checkouts[1]["with"]["ref"])
    assert checkouts[1]["with"]["persist-credentials"] is False


def test_match_workflow_uses_name_status_guard_and_emits_audit_meta(match_wf):
    body = yaml.safe_dump(match_wf["jobs"]["match"], width=10**9)
    assert "diff --name-status" in body
    assert "--name-status" in body
    assert "--no-textconv" in body
    for field in ("pr_number", "head_sha", "bot_sha256"):
        assert field in body


def test_publish_is_a_success_gated_workflow_run_consumer(publish_wf):
    trigger = _on(publish_wf)["workflow_run"]
    assert "league" in trigger["workflows"]
    assert "completed" in trigger["types"]
    assert "workflow_run.conclusion" in publish_wf["jobs"]["publish"]["if"]
    permissions = publish_wf["jobs"]["publish"]["permissions"]
    assert permissions["actions"] == "read"
    assert permissions["pull-requests"] == "read"
    assert permissions["contents"] == "read"


def test_publish_preflight_runs_before_artifact_download(publish_wf):
    steps = publish_wf["jobs"]["publish"]["steps"]
    preflight = steps.index(_step(publish_wf, step_id="meta"))
    download = next(i for i, step in enumerate(steps) if "download-artifact" in str(step.get("uses", "")))
    assert preflight < download


def test_publish_preflight_independently_derives_all_fields(
    publish_wf, monkeypatch, tmp_path
):
    code, output, calls = _execute_preflight(publish_wf, monkeypatch, tmp_path)
    assert code == 0
    assert "submitter=octocat\n" in output
    assert "opponent=byok-anchor\n" in output
    assert f"match_id={RUN_ID}\n" in output
    assert f"bot_sha256={BOT_SHA256}\n" in output
    assert f"pr_number={PR_NUMBER}\n" in output
    assert f"head_sha={HEAD_SHA}\n" in output
    endpoints = {call[2] for call in calls}
    assert f"repos/All-The-Vibes/ATV-bench/actions/runs/{RUN_ID}" in endpoints
    assert f"repos/All-The-Vibes/ATV-bench/commits/{RUN_SHA}/pulls" in endpoints
    assert f"repos/All-The-Vibes/ATV-bench/git/blobs/{BOT_BLOB}" in endpoints


def test_pr_modified_league_workflow_is_rejected_before_publication(
    publish_wf, monkeypatch, tmp_path
):
    responses = _default_api_responses(workflow_blob="f" * 40)
    code, output, _calls = _execute_preflight(
        publish_wf, monkeypatch, tmp_path, responses=responses
    )
    assert code == 1
    assert output == ""


@pytest.mark.parametrize(
    "mutator",
    [
        lambda responses: responses[
            f"repos/All-The-Vibes/ATV-bench/commits/{RUN_SHA}/pulls"
        ].clear(),
        lambda responses: responses[
            f"repos/All-The-Vibes/ATV-bench/actions/runs/{RUN_ID}"
        ].update({"path": ".github/workflows/evil.yml"}),
        lambda responses: responses[
            f"repos/All-The-Vibes/ATV-bench/git/trees/{RUN_SHA}?recursive=1"
        ].update({"truncated": True}),
    ],
)
def test_ambiguous_or_unverifiable_run_metadata_fails_closed(
    publish_wf, monkeypatch, tmp_path, mutator
):
    responses = _default_api_responses()
    mutator(responses)
    code, output, _calls = _execute_preflight(
        publish_wf, monkeypatch, tmp_path, responses=responses
    )
    assert code == 1
    assert output == ""


def test_api_failure_stops_publication(publish_wf, monkeypatch, tmp_path):
    code, output, _calls = _execute_preflight(
        publish_wf,
        monkeypatch,
        tmp_path,
        api_error=RuntimeError("API unavailable"),
    )
    assert code == 1
    assert output == ""


def test_invalid_bot_type_derives_an_empty_hash_for_a_scored_crash(
    publish_wf, monkeypatch, tmp_path
):
    responses = _default_api_responses(bot_mode="120000")
    code, output, calls = _execute_preflight(
        publish_wf, monkeypatch, tmp_path, responses=responses
    )
    assert code == 0
    assert "bot_sha256=\n" in output
    assert not any("/git/blobs/" in call[2] for call in calls)


def test_executable_regular_bot_is_hashed(publish_wf, monkeypatch, tmp_path):
    responses = _default_api_responses(bot_mode="100755")
    code, output, calls = _execute_preflight(
        publish_wf, monkeypatch, tmp_path, responses=responses
    )
    assert code == 0
    assert f"bot_sha256={BOT_SHA256}\n" in output
    assert any("/git/blobs/" in call[2] for call in calls)


def test_artifact_meta_must_exactly_match_the_derived_spec(
    publish_wf, monkeypatch, tmp_path
):
    meta = {
        "submitter": "octocat",
        "opponent": "byok-anchor",
        "match_id": RUN_ID,
        "bot_sha256": BOT_SHA256,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
    }
    assert _execute_artifact_check(publish_wf, monkeypatch, tmp_path, meta) == 0


def test_forged_artifact_meta_stops_publication(publish_wf, monkeypatch, tmp_path):
    meta = {
        "submitter": "mallory",
        "opponent": "byok-anchor",
        "match_id": RUN_ID,
        "bot_sha256": BOT_SHA256,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
    }
    assert _execute_artifact_check(publish_wf, monkeypatch, tmp_path, meta) == 1


def test_publish_checks_out_only_trusted_code_and_never_executes_submission(publish_wf):
    publish = publish_wf["jobs"]["publish"]
    for step in publish["steps"]:
        if "actions/checkout" in str(step.get("uses", "")):
            ref = str(step.get("with", {}).get("ref", ""))
            assert "default_branch" in ref
            assert step["with"]["persist-credentials"] is False
    body = yaml.safe_dump(publish).lower()
    for forbidden in ("docker run", "refs/pull/", "codeclash run"):
        assert forbidden not in body


def test_publish_binds_ingest_to_independently_derived_outputs(publish_wf):
    persist = _step(publish_wf, name_fragment="protected bot PR")
    assert "--require-spec" in persist["run"]
    for key in ("ATV_SUBMITTER", "ATV_OPPONENT", "ATV_MATCH_ID", "ATV_BOT_SHA256"):
        assert "steps.meta.outputs" in str(persist["env"][key])


def test_publish_uses_a_protected_bot_pr_and_never_pushes_default_directly(publish_wf):
    persist = _step(publish_wf, name_fragment="protected bot PR")
    code = persist["run"]
    assert "LEAGUE_BOT_TOKEN" in persist["env"]
    assert "league/match-${MATCH_RUN_ID}" in code
    assert "gh pr create" in code
    assert "gh pr merge" in code and "--auto" in code
    assert "git push" in code
    assert "GIT_ASKPASS" in code
    assert "x-access-token:${LEAGUE_BOT_TOKEN}@" not in code
    assert "HEAD:$DEFAULT_BRANCH" not in code
    assert "HEAD:${DEFAULT_BRANCH}" not in code
    assert "refs/heads/${branch}" in code


def test_publish_retry_reingests_on_latest_default_branch(publish_wf):
    code = _step(publish_wf, name_fragment="protected bot PR")["run"]
    assert "while :" in code
    assert "deadline" in code
    assert "git fetch origin \"$DEFAULT_BRANCH\"" in code
    assert "refs/remotes/origin/${branch}" in code
    assert "--force-with-lease=refs/heads/${branch}:${branch_lease}" in code
    assert "--force-with-lease=refs/heads/${branch}:" in code
    assert "publish ingest" in code
    assert "git add -A league/" in code
    assert "git diff --cached --quiet" in code
    assert "sleep" in code
    assert "exit 1" in code


def test_publish_does_not_deploy_pages(publish_wf):
    body = yaml.safe_dump(publish_wf["jobs"]["publish"])
    assert "upload-pages-artifact" not in body
    assert "deploy-pages" not in body


def test_deploy_ignores_pre_merge_publish_completion(deploy_wf):
    # The legacy workflow_run trigger may remain, but the deploy job must only run after
    # the bot PR actually merges and produces a default-branch push.
    assert "workflow_run" in _on(deploy_wf)
    assert "event_name == 'push'" in deploy_wf["jobs"]["deploy"]["if"]
