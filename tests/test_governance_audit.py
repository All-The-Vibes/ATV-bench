"""Offline, fixture-driven tests for the dynamic GitHub governance audit."""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from atv_bench.governance import audit_governance

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "audit_github_governance.py"
WORKFLOW_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/league-deploy.yml",
)


def _api(
    key: str,
    data=None,
    *,
    status: int | None = 200,
    error: str | None = None,
):
    return {
        "endpoint": f"fixture/{key}",
        "status": status,
        "data": data,
        "error": error,
    }


def _classic_protection():
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": ["hermetic", "pr-path-guard"],
            "checks": [],
        },
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "require_code_owner_reviews": True,
            "bypass_pull_request_allowances": {
                "users": [],
                "teams": [],
                "apps": [],
            },
        },
    }


def _branch_ruleset():
    return {
        "id": 101,
        "name": "default-branch-governance",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            }
        },
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "require_code_owner_review": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "hermetic"},
                        {"context": "pr-path-guard"},
                    ]
                },
            },
        ],
    }


def _tag_ruleset():
    return {
        "id": 202,
        "name": "immutable-release-tags",
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["refs/tags/v*"],
                "exclude": [],
            }
        },
        "rules": [
            {"type": "deletion"},
            {"type": "update"},
        ],
    }


def _workflow_bytes(path):
    # Git may materialize a mixed CRLF working tree on Windows even though the
    # committed workflow blob and GitHub Contents API response are LF-only.
    return (ROOT / path).read_bytes().replace(b"\r\n", b"\n")


def _workflow_contents(path):
    content = _workflow_bytes(path)
    return {
        "type": "file",
        "path": path,
        "sha": "a" * 40,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def _codeowners_contents():
    path = ".github/CODEOWNERS"
    content = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
    return {
        "type": "file",
        "path": path,
        "sha": "c" * 40,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def _workflow_catalog(paths=WORKFLOW_PATHS):
    return [
        {
            "id": 1000 + index,
            "name": Path(path).stem,
            "path": path,
            "state": "active",
        }
        for index, path in enumerate(paths)
    ]


def _workflow_sources(rows):
    return [
        {
            "id": row["id"],
            "path": row["path"],
            "state": row["state"],
            "source": _api(
                f"source-{index}",
                _workflow_contents(row["path"]),
            ),
        }
        for index, row in enumerate(rows)
        if row["state"] == "active"
    ]


@pytest.fixture
def compliant_snapshot():
    workflow_rows = _workflow_catalog()
    return {
        "repository": _api(
            "repository",
            {
                "full_name": "All-The-Vibes/ATV-bench",
                "default_branch": "main",
            },
        ),
        "branch_protection": _api(
            "branch_protection",
            _classic_protection(),
        ),
        "codeowners": _api("codeowners", _codeowners_contents()),
        "rulesets": _api("rulesets", []),
        "pages_environment": _api(
            "pages_environment",
            {
                "name": "github-pages",
                "can_admins_bypass": True,
                "protection_rules": [{"type": "branch_policy"}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
        ),
        "pages_branch_policies": _api(
            "pages_branch_policies",
            {
                "total_count": 1,
                "branch_policies": [
                    {"id": 501, "name": "main", "type": "branch"}
                ],
            },
        ),
        "actions_permissions": _api(
            "actions_permissions",
            {
                "enabled": True,
                "allowed_actions": "selected",
                "sha_pinning_required": True,
            },
        ),
        "selected_actions": _api(
            "selected_actions",
            {
                "github_owned_allowed": True,
                "verified_allowed": False,
                "patterns_allowed": [],
            },
        ),
        "workflows": _api(
            "workflows",
            {
                "total_count": len(workflow_rows),
                "workflows": workflow_rows,
            },
        ),
        "workflow_sources": _api(
            "workflow_sources",
            _workflow_sources(workflow_rows),
        ),
        "releases": _api(
            "releases",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "immutable": True,
                }
            ],
        ),
        "tags": _api(
            "tags",
            [{"name": "v1.0.0", "commit": {"sha": "a" * 40}}],
        ),
    }


def _finding(report, finding_id):
    return next(item for item in report["findings"] if item["id"] == finding_id)


def _audit(snapshot):
    return audit_governance(
        snapshot,
        repository="All-The-Vibes/ATV-bench",
        generated_at="2026-07-19T12:00:00Z",
    )


def test_compliant_classic_protection_snapshot_passes(compliant_snapshot):
    report = _audit(compliant_snapshot)

    assert report["passed"] is True
    assert report["failure_count"] == 0
    assert report["failures"] == []
    assert report["default_branch"] == "main"


def test_compliant_ruleset_only_snapshot_passes(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    snapshot["branch_protection"] = _api(
        "branch_protection",
        status=404,
        error="HTTP 404: branch is governed by rulesets",
    )
    snapshot["rulesets"] = _api("rulesets", [_branch_ruleset()])

    report = _audit(snapshot)

    assert report["passed"] is True, report["failures"]
    assert _finding(report, "default_branch.protected")["evidence"][
        "matching_rulesets"
    ] == ["default-branch-governance"]


def test_immutable_tag_ruleset_can_satisfy_release_requirement(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    snapshot["releases"] = _api("releases", [])
    snapshot["rulesets"] = _api("rulesets", [_tag_ruleset()])

    report = _audit(snapshot)

    assert report["passed"] is True, report["failures"]
    release = _finding(report, "release.immutable_release_or_tag")
    assert release["evidence"]["immutable_tags"] == ["v1.0.0"]


@pytest.mark.parametrize(
    ("case", "expected_failure"),
    [
        ("no_protection", "default_branch.protected"),
        ("no_pr", "default_branch.pull_request_required"),
        ("missing_check", "default_branch.required_checks"),
        ("no_codeowners", "default_branch.codeowners_review"),
        ("codeowners_missing_arena", "codeowners.required_trust_surfaces"),
        ("admins_bypass", "default_branch.no_bypass_include_admins"),
        ("actor_bypass", "default_branch.no_bypass_include_admins"),
        (
            "pages_missing_branch_rule",
            "pages.environment_deployment_branch_policy",
        ),
        (
            "pages_protected_branches_mode",
            "pages.environment_deployment_branch_policy",
        ),
        ("pages_wrong_branch", "pages.environment_deployment_branch_policy"),
        (
            "pages_policy_incomplete",
            "pages.environment_deployment_branch_policy",
        ),
        ("actions_all", "actions.allowed_policy"),
        ("actions_wildcard", "actions.allowed_policy"),
        ("sha_pinning_off", "actions.sha_pinning"),
        ("sha_pinning_missing", "actions.sha_pinning"),
        ("unexpected_active_workflow", "workflows.reviewed_source_invariant"),
        ("workflow_catalog_incomplete", "workflows.reviewed_source_invariant"),
        ("workflow_source_drift", "workflows.reviewed_source_invariant"),
        ("missing_workflow_source", "workflows.reviewed_source_invariant"),
        ("no_immutable_release", "release.immutable_release_or_tag"),
    ],
)
def test_required_control_drift_fails_closed(
    compliant_snapshot,
    case,
    expected_failure,
):
    snapshot = copy.deepcopy(compliant_snapshot)
    protection = snapshot["branch_protection"]["data"]

    if case == "no_protection":
        snapshot["branch_protection"] = _api(
            "branch_protection",
            status=404,
            error="HTTP 404",
        )
    elif case == "no_pr":
        protection["required_pull_request_reviews"] = None
    elif case == "missing_check":
        protection["required_status_checks"]["contexts"] = ["hermetic"]
    elif case == "no_codeowners":
        protection["required_pull_request_reviews"][
            "require_code_owner_reviews"
        ] = False
    elif case == "codeowners_missing_arena":
        source = snapshot["codeowners"]["data"]
        content = base64.b64decode(source["content"])
        changed = content.replace(
            b"/arena/                         @All-The-Vibes/league-maintainers\n",
            b"",
        )
        assert changed != content
        source["content"] = base64.b64encode(changed).decode("ascii")
        source["size"] = len(changed)
    elif case == "admins_bypass":
        protection["enforce_admins"]["enabled"] = False
    elif case == "actor_bypass":
        protection["required_pull_request_reviews"][
            "bypass_pull_request_allowances"
        ]["users"] = [{"login": "admin"}]
    elif case == "pages_missing_branch_rule":
        snapshot["pages_environment"]["data"]["protection_rules"] = []
    elif case == "pages_protected_branches_mode":
        policy = snapshot["pages_environment"]["data"][
            "deployment_branch_policy"
        ]
        policy["protected_branches"] = True
        policy["custom_branch_policies"] = False
    elif case == "pages_wrong_branch":
        snapshot["pages_branch_policies"]["data"]["branch_policies"][0][
            "name"
        ] = "release"
    elif case == "pages_policy_incomplete":
        snapshot["pages_branch_policies"]["data"]["total_count"] = 2
    elif case == "actions_all":
        snapshot["actions_permissions"]["data"]["allowed_actions"] = "all"
    elif case == "actions_wildcard":
        snapshot["selected_actions"]["data"]["patterns_allowed"] = [
            "third-party/action@*"
        ]
    elif case == "sha_pinning_off":
        snapshot["actions_permissions"]["data"]["sha_pinning_required"] = False
    elif case == "sha_pinning_missing":
        del snapshot["actions_permissions"]["data"]["sha_pinning_required"]
    elif case == "unexpected_active_workflow":
        row = {
            "id": 9999,
            "name": "league",
            "path": ".github/workflows/league.yml",
            "state": "active",
        }
        snapshot["workflows"]["data"]["workflows"].append(row)
        snapshot["workflows"]["data"]["total_count"] += 1
        snapshot["workflow_sources"]["data"].append(
            {
                "id": row["id"],
                "path": row["path"],
                "state": row["state"],
                "source": _api(
                    "league-source",
                    {
                        "type": "file",
                        "path": row["path"],
                        "sha": "b" * 40,
                        "size": len(b"name: x\n"),
                        "encoding": "base64",
                        "content": base64.b64encode(b"name: x\n").decode("ascii"),
                    },
                ),
            }
        )
    elif case == "workflow_catalog_incomplete":
        snapshot["workflows"]["data"]["total_count"] += 1
    elif case == "workflow_source_drift":
        source = snapshot["workflow_sources"]["data"][0]["source"]["data"]
        changed = base64.b64decode(source["content"]) + b"# changed\n"
        source["content"] = base64.b64encode(changed).decode("ascii")
        source["size"] = len(changed)
    elif case == "missing_workflow_source":
        snapshot["workflow_sources"]["data"].pop()
    elif case == "no_immutable_release":
        snapshot["releases"]["data"][0]["immutable"] = False
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    report = _audit(snapshot)

    assert report["passed"] is False
    assert expected_failure in report["failures"]


def test_missing_endpoint_is_an_explicit_failure(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    del snapshot["pages_environment"]

    report = _audit(snapshot)

    assert "api.pages_environment" in report["failures"]
    api_finding = _finding(report, "api.pages_environment")
    assert "missing required endpoint" in api_finding["evidence"]["error"]


def test_completely_missing_snapshot_returns_failures_not_an_exception():
    report = _audit({})

    assert report["passed"] is False
    assert report["failure_count"] > 0
    assert "api.repository" in report["failures"]
    assert "default_branch.protected" in report["failures"]


def test_default_branch_must_be_main(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    snapshot["repository"]["data"]["default_branch"] = "release"
    snapshot["pages_branch_policies"]["data"]["branch_policies"][0][
        "name"
    ] = "release"

    report = _audit(snapshot)

    assert "repository.identity_and_default_branch" in report["failures"]
    assert "pages.environment_deployment_branch_policy" in report["failures"]


def test_malformed_workflow_source_fails_closed(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    snapshot["workflow_sources"]["data"][0]["source"]["data"]["content"] = "***"

    report = _audit(snapshot)

    assert "workflows.reviewed_source_invariant" in report["failures"]
    errors = _finding(
        report,
        "workflows.reviewed_source_invariant",
    )["evidence"]["errors"]
    assert any("invalid base64" in error for error in errors)


def test_api_denial_is_an_explicit_failure(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    snapshot["rulesets"] = _api(
        "rulesets",
        status=403,
        error="HTTP 403: Resource not accessible by integration",
    )

    report = _audit(snapshot)

    assert "api.rulesets" in report["failures"]
    assert "rulesets.complete_and_unambiguous" in report["failures"]
    assert "403" in _finding(report, "api.rulesets")["summary"]


def test_ambiguous_missing_policy_field_is_a_failure(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    del snapshot["branch_protection"]["data"]["required_pull_request_reviews"][
        "required_approving_review_count"
    ]

    report = _audit(snapshot)

    assert "default_branch.pull_request_required" in report["failures"]
    errors = _finding(
        report,
        "default_branch.pull_request_required",
    )["evidence"]["errors"]
    assert any("review_count" in error for error in errors)


def test_classic_api_omitted_empty_bypass_allowances_means_no_bypass(
    compliant_snapshot,
):
    snapshot = copy.deepcopy(compliant_snapshot)
    del snapshot["branch_protection"]["data"]["required_pull_request_reviews"][
        "bypass_pull_request_allowances"
    ]

    report = _audit(snapshot)

    assert report["passed"] is True, report["failures"]


def test_malformed_ruleset_is_reported_instead_of_crashing(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    malformed = _branch_ruleset()
    del malformed["conditions"]
    snapshot["rulesets"] = _api("rulesets", [malformed])

    report = _audit(snapshot)

    assert report["passed"] is False
    assert "rulesets.complete_and_unambiguous" in report["failures"]
    errors = _finding(
        report,
        "rulesets.complete_and_unambiguous",
    )["evidence"]["errors"]
    assert any("conditions" in error for error in errors)


def _load_script_module():
    spec = importlib.util.spec_from_file_location("audit_github_governance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gh_client_structures_denied_and_malformed_responses():
    module = _load_script_module()

    def denied(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )

    denied_result = module.GhApiClient(runner=denied).get("repos/o/r")
    assert denied_result["status"] == 403
    assert "Resource not accessible" in denied_result["error"]

    def malformed(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="{not json",
            stderr="",
        )

    malformed_result = module.GhApiClient(runner=malformed).get("repos/o/r")
    assert malformed_result["status"] == 200
    assert "malformed JSON" in malformed_result["error"]


def test_collector_uses_get_only_and_fetches_ruleset_details():
    module = _load_script_module()

    class FixtureClient:
        def __init__(self):
            self.calls = []

        def get(self, endpoint, *, paginate=False):
            self.calls.append((endpoint, paginate))
            if endpoint == "repos/o/r":
                return _api(
                    "repository",
                    {"full_name": "o/r", "default_branch": "release/1"},
                )
            if endpoint.endswith("rulesets?includes_parents=true&per_page=100"):
                return _api("rulesets", [{"id": 77}])
            if endpoint.endswith("rulesets/77"):
                return _api("ruleset_detail", _branch_ruleset())
            if endpoint.endswith(
                "contents/.github/CODEOWNERS?ref=release%2F1"
            ):
                return _api("codeowners", _codeowners_contents())
            if endpoint.endswith("actions/workflows?per_page=100"):
                rows = _workflow_catalog((WORKFLOW_PATHS[0],))
                return _api(
                    "workflows",
                    {"total_count": 1, "workflows": rows},
                )
            if "/contents/.github/workflows/ci.yml?ref=release%2F1" in endpoint:
                return _api("workflow_source", _workflow_contents(WORKFLOW_PATHS[0]))
            return _api(endpoint, {})

    client = FixtureClient()
    snapshot = module.collect_snapshot("o/r", client=client)

    assert (
        "repos/o/r/branches/release%2F1/protection",
        False,
    ) in client.calls
    assert (
        "repos/o/r/contents/.github/CODEOWNERS?ref=release%2F1",
        False,
    ) in client.calls
    assert ("repos/o/r/rulesets/77", False) in client.calls
    assert snapshot["rulesets"]["data"] == [_branch_ruleset()]
    assert (
        "repos/o/r/environments/github-pages",
        False,
    ) in client.calls
    assert (
        "repos/o/r/environments/github-pages/deployment-branch-policies"
        "?per_page=100",
        False,
    ) in client.calls
    assert (
        "repos/o/r/contents/.github/workflows/ci.yml?ref=release%2F1",
        False,
    ) in client.calls
    assert snapshot["workflow_sources"]["error"] is None

    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--method",\n            "GET"' in source
    assert "league-match" not in source
    assert "run-match" not in source
    for write_method in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert write_method not in source


def test_invalid_repository_still_writes_a_machine_failure_report(tmp_path):
    module = _load_script_module()
    output = tmp_path / "report.json"

    exit_code = module.main(["--repo", "not-a-repository", "--out", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["passed"] is False
    assert report["failures"] == ["audit.fatal"]
