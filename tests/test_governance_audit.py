"""Offline, fixture-driven tests for the dynamic GitHub governance audit."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from atv_bench.governance import audit_governance

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "audit_github_governance.py"


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


@pytest.fixture
def compliant_snapshot():
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
        "rulesets": _api("rulesets", []),
        "environment": _api(
            "environment",
            {
                "name": "league-match",
                "can_admins_bypass": False,
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": True,
                        "reviewers": [
                            {
                                "type": "Team",
                                "reviewer": {"slug": "benchmark-maintainers"},
                            }
                        ],
                    }
                ],
            },
        ),
        "label": _api("label", {"name": "run-match"}),
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
        ("admins_bypass", "default_branch.no_bypass_include_admins"),
        ("actor_bypass", "default_branch.no_bypass_include_admins"),
        ("no_environment_reviewers", "environment.league_match_reviewers"),
        ("environment_admin_bypass", "environment.league_match_reviewers"),
        ("environment_self_review", "environment.league_match_reviewers"),
        ("no_label", "label.run_match"),
        ("actions_all", "actions.allowed_policy"),
        ("actions_wildcard", "actions.allowed_policy"),
        ("sha_pinning_off", "actions.sha_pinning"),
        ("sha_pinning_missing", "actions.sha_pinning"),
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
    elif case == "admins_bypass":
        protection["enforce_admins"]["enabled"] = False
    elif case == "actor_bypass":
        protection["required_pull_request_reviews"][
            "bypass_pull_request_allowances"
        ]["users"] = [{"login": "admin"}]
    elif case == "no_environment_reviewers":
        snapshot["environment"]["data"]["protection_rules"] = []
    elif case == "environment_admin_bypass":
        snapshot["environment"]["data"]["can_admins_bypass"] = True
    elif case == "environment_self_review":
        snapshot["environment"]["data"]["protection_rules"][0][
            "prevent_self_review"
        ] = False
    elif case == "no_label":
        snapshot["label"] = _api("label", status=404, error="HTTP 404")
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
    elif case == "no_immutable_release":
        snapshot["releases"]["data"][0]["immutable"] = False
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    report = _audit(snapshot)

    assert report["passed"] is False
    assert expected_failure in report["failures"]


def test_missing_endpoint_is_an_explicit_failure(compliant_snapshot):
    snapshot = copy.deepcopy(compliant_snapshot)
    del snapshot["environment"]

    report = _audit(snapshot)

    assert "api.environment" in report["failures"]
    api_finding = _finding(report, "api.environment")
    assert "missing required endpoint" in api_finding["evidence"]["error"]


def test_completely_missing_snapshot_returns_failures_not_an_exception():
    report = _audit({})

    assert report["passed"] is False
    assert report["failure_count"] > 0
    assert "api.repository" in report["failures"]
    assert "default_branch.protected" in report["failures"]


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
            return _api(endpoint, {})

    client = FixtureClient()
    snapshot = module.collect_snapshot("o/r", client=client)

    assert (
        "repos/o/r/branches/release%2F1/protection",
        False,
    ) in client.calls
    assert ("repos/o/r/rulesets/77", False) in client.calls
    assert snapshot["rulesets"]["data"] == [_branch_ruleset()]

    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--method",\n            "GET"' in source
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
