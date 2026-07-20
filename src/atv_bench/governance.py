"""Fail-closed evaluation of GitHub repository governance state.

The auditor consumes a snapshot of GitHub REST responses rather than making
network calls itself. Each snapshot entry has this machine-readable shape:

    {
        "endpoint": "repos/owner/repo",
        "status": 200,
        "data": {...},
        "error": null,
    }

Missing entries, denied endpoints, malformed JSON shapes, and ambiguous policy
fields become explicit failed findings. The companion script gathers the
snapshot with read-only ``gh api`` GET requests.
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_CHECKS = ("hermetic", "pr-path-guard")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
PINNED_ACTION_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}$"
)

SNAPSHOT_KEYS = (
    "repository",
    "branch_protection",
    "rulesets",
    "environment",
    "label",
    "actions_permissions",
    "selected_actions",
    "releases",
    "tags",
)


@dataclass(frozen=True)
class EndpointResult:
    key: str
    endpoint: str
    status: int | None
    data: Any
    error: str | None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any], key: str) -> "EndpointResult":
        if key not in snapshot:
            return cls(
                key=key,
                endpoint=key,
                status=None,
                data=None,
                error=f"snapshot is missing required endpoint result {key!r}",
            )
        raw = snapshot[key]
        if not isinstance(raw, Mapping):
            return cls(
                key=key,
                endpoint=key,
                status=None,
                data=None,
                error=f"endpoint result {key!r} is not an object",
            )
        endpoint = str(raw.get("endpoint") or key)
        status = raw.get("status")
        if isinstance(status, bool) or (status is not None and not isinstance(status, int)):
            return cls(
                key=key,
                endpoint=endpoint,
                status=None,
                data=raw.get("data"),
                error=f"endpoint result {key!r} has an invalid HTTP status",
            )
        error = raw.get("error")
        if error is not None and not isinstance(error, str):
            error = f"endpoint result {key!r} has a non-string error"
        return cls(
            key=key,
            endpoint=endpoint,
            status=status,
            data=raw.get("data"),
            error=error,
        )


@dataclass(frozen=True)
class Finding:
    id: str
    passed: bool
    summary: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finding(
    finding_id: str,
    passed: bool,
    summary: str,
    **evidence: Any,
) -> Finding:
    return Finding(finding_id, bool(passed), summary, evidence)


def _endpoint_evidence(result: EndpointResult) -> dict[str, Any]:
    return {
        "endpoint": result.endpoint,
        "status": result.status,
        "error": result.error,
    }


def _api_finding(
    result: EndpointResult,
    *,
    accepted_statuses: set[int] | None = None,
) -> Finding:
    accepted = accepted_statuses or {200}
    passed = result.status in accepted and (
        not result.error or result.status == 404
    )
    if passed:
        if result.status == 404:
            summary = (
                f"{result.key} returned 404; an alternative governance mechanism "
                "must satisfy the policy"
            )
        else:
            summary = f"{result.key} endpoint was readable"
    else:
        summary = (
            f"{result.key} endpoint is not verifiable"
            f" (status={result.status!r}, error={result.error!r})"
        )
    return _finding(
        f"api.{result.key}",
        passed,
        summary,
        **_endpoint_evidence(result),
    )


def _as_mapping(value: Any, label: str, errors: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{label} must be an object")
        return None
    return value


def _as_list(value: Any, label: str, errors: list[str]) -> list[Any] | None:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return None
    return value


def _pattern_matches(
    pattern: str,
    *,
    target: str,
    short_name: str,
    default_branch: str,
) -> bool:
    full_ref = f"refs/heads/{short_name}" if target == "branch" else f"refs/tags/{short_name}"
    if pattern == "~ALL":
        return True
    if pattern == "~DEFAULT_BRANCH":
        return target == "branch" and short_name == default_branch
    return fnmatch.fnmatchcase(full_ref, pattern) or fnmatch.fnmatchcase(short_name, pattern)


def _ruleset_matches(
    ruleset: Mapping[str, Any],
    *,
    target: str,
    short_name: str,
    default_branch: str,
) -> bool:
    if ruleset.get("target") != target or ruleset.get("enforcement") != "active":
        return False
    conditions = ruleset.get("conditions")
    if not isinstance(conditions, Mapping):
        return False
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, Mapping):
        return False
    includes = ref_name.get("include")
    excludes = ref_name.get("exclude")
    if not isinstance(includes, list) or not isinstance(excludes, list):
        return False
    included = any(
        _pattern_matches(
            str(pattern),
            target=target,
            short_name=short_name,
            default_branch=default_branch,
        )
        for pattern in includes
    )
    excluded = any(
        _pattern_matches(
            str(pattern),
            target=target,
            short_name=short_name,
            default_branch=default_branch,
        )
        for pattern in excludes
    )
    return included and not excluded


def _validated_rulesets(result: EndpointResult) -> tuple[list[Mapping[str, Any]], list[str]]:
    errors: list[str] = []
    if not result.ok:
        return [], [f"rulesets endpoint is unavailable: {result.error or result.status}"]
    rows = _as_list(result.data, "rulesets response", errors)
    if rows is None:
        return [], errors

    validated: list[Mapping[str, Any]] = []
    for index, raw in enumerate(rows):
        error_count = len(errors)
        item = _as_mapping(raw, f"rulesets[{index}]", errors)
        if item is None:
            continue
        if item.get("id") is None:
            errors.append(f"rulesets[{index}] is missing id")
        if not isinstance(item.get("name"), str) or not item.get("name"):
            errors.append(f"rulesets[{index}] is missing name")
        enforcement = item.get("enforcement")
        if enforcement not in {"active", "disabled", "evaluate"}:
            errors.append(f"rulesets[{index}] has unknown enforcement {enforcement!r}")
        target = item.get("target")
        if not isinstance(target, str):
            errors.append(f"rulesets[{index}] is missing target")
            continue
        if enforcement == "active" and target in {"branch", "tag"}:
            bypass = _as_list(
                item.get("bypass_actors"),
                f"rulesets[{index}].bypass_actors",
                errors,
            )
            rules = _as_list(item.get("rules"), f"rulesets[{index}].rules", errors)
            conditions = _as_mapping(
                item.get("conditions"),
                f"rulesets[{index}].conditions",
                errors,
            )
            if conditions is not None:
                ref_name = _as_mapping(
                    conditions.get("ref_name"),
                    f"rulesets[{index}].conditions.ref_name",
                    errors,
                )
                if ref_name is not None:
                    _as_list(
                        ref_name.get("include"),
                        f"rulesets[{index}].conditions.ref_name.include",
                        errors,
                    )
                    _as_list(
                        ref_name.get("exclude"),
                        f"rulesets[{index}].conditions.ref_name.exclude",
                        errors,
                    )
            if rules is not None:
                for rule_index, rule in enumerate(rules):
                    rule_obj = _as_mapping(
                        rule,
                        f"rulesets[{index}].rules[{rule_index}]",
                        errors,
                    )
                    if rule_obj is not None and not isinstance(rule_obj.get("type"), str):
                        errors.append(
                            f"rulesets[{index}].rules[{rule_index}] is missing type"
                        )
            if bypass is None:
                continue
        if len(errors) == error_count:
            validated.append(item)
    return validated, errors


def _classic_protection(
    result: EndpointResult,
) -> tuple[Mapping[str, Any] | None, list[str]]:
    if result.status == 404:
        return None, []
    if not result.ok:
        return None, [
            f"classic branch protection endpoint is unavailable: "
            f"{result.error or result.status}"
        ]
    errors: list[str] = []
    data = _as_mapping(result.data, "branch protection response", errors)
    return data, errors


def _classic_pull_request_policy(
    classic: Mapping[str, Any] | None,
) -> tuple[bool, bool, list[str], list[str]]:
    """Return (pr_required, codeowners, bypass_clear, errors)."""
    if classic is None:
        return False, False, [], []
    errors: list[str] = []
    if "required_pull_request_reviews" not in classic:
        errors.append("branch protection is missing required_pull_request_reviews")
        return False, False, [], errors
    policy = classic.get("required_pull_request_reviews")
    if policy is None:
        return False, False, [], errors
    policy_obj = _as_mapping(policy, "required_pull_request_reviews", errors)
    if policy_obj is None:
        return False, False, [], errors

    count = policy_obj.get("required_approving_review_count")
    if isinstance(count, bool) or not isinstance(count, int):
        errors.append("required_approving_review_count is missing or invalid")
        count = 0
    codeowners = policy_obj.get("require_code_owner_reviews")
    if not isinstance(codeowners, bool):
        errors.append("require_code_owner_reviews is missing or invalid")
        codeowners = False

    bypass_clear: list[str] = []
    # GitHub omits bypass_pull_request_allowances from the GET response when
    # all three actor lists are empty. Absence in this specific successful
    # response therefore means "no bypass actors", not "unknown policy".
    raw_bypass = policy_obj.get("bypass_pull_request_allowances", {})
    bypass = _as_mapping(
        raw_bypass,
        "bypass_pull_request_allowances",
        errors,
    )
    if bypass is not None:
        for actor_type in ("users", "teams", "apps"):
            actors = _as_list(
                # GitHub also omits individual empty actor arrays.
                bypass.get(actor_type, []),
                f"bypass_pull_request_allowances.{actor_type}",
                errors,
            )
            if actors:
                bypass_clear.extend(
                    f"{actor_type}:{_actor_name(actor)}" for actor in actors
                )
    return count >= 1, bool(codeowners), bypass_clear, errors


def _actor_name(actor: Any) -> str:
    if isinstance(actor, Mapping):
        return str(actor.get("login") or actor.get("name") or actor.get("slug") or actor)
    return str(actor)


def _classic_checks(
    classic: Mapping[str, Any] | None,
) -> tuple[set[str], list[str]]:
    if classic is None:
        return set(), []
    errors: list[str] = []
    if "required_status_checks" not in classic:
        errors.append("branch protection is missing required_status_checks")
        return set(), errors
    policy = classic.get("required_status_checks")
    if policy is None:
        return set(), errors
    policy_obj = _as_mapping(policy, "required_status_checks", errors)
    if policy_obj is None:
        return set(), errors
    contexts: set[str] = set()
    raw_contexts = policy_obj.get("contexts")
    raw_checks = policy_obj.get("checks")
    if raw_contexts is None and raw_checks is None:
        errors.append("required_status_checks has neither contexts nor checks")
        return contexts, errors
    if raw_contexts is not None:
        items = _as_list(raw_contexts, "required_status_checks.contexts", errors)
        if items is not None:
            for item in items:
                if not isinstance(item, str) or not item.strip():
                    errors.append("required_status_checks.contexts contains an invalid name")
                else:
                    contexts.add(item.strip())
    if raw_checks is not None:
        items = _as_list(raw_checks, "required_status_checks.checks", errors)
        if items is not None:
            for item in items:
                check = _as_mapping(item, "required_status_checks.checks[]", errors)
                context = check.get("context") if check is not None else None
                if not isinstance(context, str) or not context.strip():
                    errors.append("required_status_checks.checks[] is missing context")
                else:
                    contexts.add(context.strip())
    return contexts, errors


def _ruleset_policy(
    rulesets: list[Mapping[str, Any]],
    *,
    target: str,
    short_name: str,
    default_branch: str,
) -> dict[str, Any]:
    matched = [
        item
        for item in rulesets
        if _ruleset_matches(
            item,
            target=target,
            short_name=short_name,
            default_branch=default_branch,
        )
    ]
    result: dict[str, Any] = {
        "matched": matched,
        "checks": set(),
        "pr_required": False,
        "codeowners": False,
        "bypass": [],
        "errors": [],
    }
    for item in matched:
        bypass = item.get("bypass_actors")
        if bypass:
            result["bypass"].extend(
                f"{item.get('name')}:{_actor_name(actor)}" for actor in bypass
            )
        for rule in item.get("rules", []):
            rule_type = rule.get("type")
            parameters = rule.get("parameters")
            if rule_type == "pull_request":
                if not isinstance(parameters, Mapping):
                    result["errors"].append(
                        f"ruleset {item.get('name')!r} pull_request rule lacks parameters"
                    )
                    continue
                count = parameters.get("required_approving_review_count")
                if isinstance(count, bool) or not isinstance(count, int):
                    result["errors"].append(
                        f"ruleset {item.get('name')!r} has invalid approving review count"
                    )
                elif count >= 1:
                    result["pr_required"] = True
                codeowners = parameters.get("require_code_owner_review")
                if not isinstance(codeowners, bool):
                    result["errors"].append(
                        f"ruleset {item.get('name')!r} lacks require_code_owner_review"
                    )
                elif codeowners:
                    result["codeowners"] = True
            elif rule_type == "required_status_checks":
                if not isinstance(parameters, Mapping):
                    result["errors"].append(
                        f"ruleset {item.get('name')!r} status-check rule lacks parameters"
                    )
                    continue
                checks = parameters.get("required_status_checks")
                if not isinstance(checks, list):
                    result["errors"].append(
                        f"ruleset {item.get('name')!r} status checks are missing"
                    )
                    continue
                for check in checks:
                    context = check.get("context") if isinstance(check, Mapping) else None
                    if not isinstance(context, str) or not context.strip():
                        result["errors"].append(
                            f"ruleset {item.get('name')!r} contains an invalid check"
                        )
                    else:
                        result["checks"].add(context.strip())
    return result


def _check_is_required(check: str, configured: set[str]) -> bool:
    wanted = check.casefold()
    for name in configured:
        normalized = name.strip().casefold()
        if normalized == wanted:
            return True
        if normalized.rsplit("/", 1)[-1].strip() == wanted:
            return True
    return False


def _environment_finding(result: EndpointResult) -> Finding:
    errors: list[str] = []
    data = _as_mapping(result.data, "environment response", errors) if result.ok else None
    reviewers: list[str] = []
    can_admins_bypass: bool | None = None
    prevent_self_review: bool | None = None
    if data is not None:
        can_admins_bypass = data.get("can_admins_bypass")
        if not isinstance(can_admins_bypass, bool):
            errors.append("environment response is missing boolean can_admins_bypass")
        rules = _as_list(data.get("protection_rules"), "protection_rules", errors)
        if rules is not None:
            for raw_rule in rules:
                rule = _as_mapping(raw_rule, "protection_rules[]", errors)
                if rule is None or rule.get("type") != "required_reviewers":
                    continue
                prevent_self_review = rule.get("prevent_self_review")
                if not isinstance(prevent_self_review, bool):
                    errors.append(
                        "required_reviewers is missing boolean prevent_self_review"
                    )
                raw_reviewers = _as_list(
                    rule.get("reviewers"),
                    "required_reviewers.reviewers",
                    errors,
                )
                if raw_reviewers is not None:
                    for raw in raw_reviewers:
                        reviewer = raw.get("reviewer") if isinstance(raw, Mapping) else None
                        reviewers.append(_actor_name(reviewer or raw))
    passed = (
        result.ok
        and not errors
        and bool(reviewers)
        and can_admins_bypass is False
        and prevent_self_review is True
    )
    return _finding(
        "environment.league_match_reviewers",
        passed,
        (
            "league-match requires independent reviewer approval with no admin bypass"
            if passed
            else "league-match lacks independent review or permits an unsafe bypass"
        ),
        reviewers=reviewers,
        can_admins_bypass=can_admins_bypass,
        prevent_self_review=prevent_self_review,
        errors=errors,
        **_endpoint_evidence(result),
    )


def _label_finding(result: EndpointResult) -> Finding:
    data = result.data if isinstance(result.data, Mapping) else {}
    name = data.get("name")
    passed = result.ok and isinstance(name, str) and name.casefold() == "run-match"
    return _finding(
        "label.run_match",
        passed,
        "run-match label exists" if passed else "run-match label is missing or ambiguous",
        observed=name,
        **_endpoint_evidence(result),
    )


def _actions_findings(
    permissions: EndpointResult,
    selected: EndpointResult,
) -> tuple[Finding, Finding]:
    errors: list[str] = []
    data = (
        _as_mapping(permissions.data, "actions permissions response", errors)
        if permissions.ok
        else None
    )
    selected_data = (
        _as_mapping(selected.data, "selected actions response", errors)
        if selected.ok
        else None
    )

    enabled = data.get("enabled") if data is not None else None
    allowed = data.get("allowed_actions") if data is not None else None
    sha_pinning = data.get("sha_pinning_required") if data is not None else None
    if not isinstance(enabled, bool):
        errors.append("actions permissions is missing boolean enabled")
    if not isinstance(allowed, str):
        errors.append("actions permissions is missing allowed_actions")
    if not isinstance(sha_pinning, bool):
        errors.append("actions permissions is missing boolean sha_pinning_required")

    github_owned = selected_data.get("github_owned_allowed") if selected_data else None
    verified = selected_data.get("verified_allowed") if selected_data else None
    patterns = selected_data.get("patterns_allowed") if selected_data else None
    if not isinstance(github_owned, bool):
        errors.append("selected actions policy is missing github_owned_allowed")
    if not isinstance(verified, bool):
        errors.append("selected actions policy is missing verified_allowed")
    if not isinstance(patterns, list):
        errors.append("selected actions policy is missing patterns_allowed")
        patterns = []
    invalid_patterns = [
        str(pattern)
        for pattern in patterns
        if not isinstance(pattern, str) or not PINNED_ACTION_PATTERN.fullmatch(pattern)
    ]

    policy_passed = (
        permissions.ok
        and selected.ok
        and not errors
        and enabled is True
        and allowed == "selected"
        and github_owned is True
        and verified is False
        and not invalid_patterns
    )
    policy = _finding(
        "actions.allowed_policy",
        policy_passed,
        (
            "Actions is enabled with a selected, SHA-specific allow policy"
            if policy_passed
            else "Actions allow policy is broad, missing, denied, or ambiguous"
        ),
        enabled=enabled,
        allowed_actions=allowed,
        github_owned_allowed=github_owned,
        verified_allowed=verified,
        patterns_allowed=patterns,
        invalid_patterns=invalid_patterns,
        errors=errors,
    )
    pinning_passed = permissions.ok and isinstance(sha_pinning, bool) and sha_pinning
    pinning = _finding(
        "actions.sha_pinning",
        pinning_passed,
        (
            "repository policy requires full-length action commit SHAs"
            if pinning_passed
            else "full-length action SHA policy is disabled, missing, or not exposed"
        ),
        sha_pinning_required=sha_pinning,
        **_endpoint_evidence(permissions),
    )
    return policy, pinning


def _immutable_release_or_tag(
    releases: EndpointResult,
    tags: EndpointResult,
    rulesets: list[Mapping[str, Any]],
    *,
    default_branch: str,
) -> Finding:
    errors: list[str] = []
    release_rows = _as_list(releases.data, "releases response", errors) if releases.ok else None
    tag_rows = _as_list(tags.data, "tags response", errors) if tags.ok else None
    immutable_releases: list[str] = []
    immutable_tags: list[str] = []

    if release_rows is not None:
        for index, raw in enumerate(release_rows):
            release = _as_mapping(raw, f"releases[{index}]", errors)
            if release is None:
                continue
            tag_name = release.get("tag_name")
            immutable = release.get("immutable")
            draft = release.get("draft")
            if not isinstance(immutable, bool):
                errors.append(f"releases[{index}] is missing boolean immutable")
                continue
            if not isinstance(draft, bool):
                errors.append(f"releases[{index}] is missing boolean draft")
                continue
            if immutable and not draft and isinstance(tag_name, str) and tag_name:
                immutable_releases.append(tag_name)

    if tag_rows is not None:
        for index, raw in enumerate(tag_rows):
            tag = _as_mapping(raw, f"tags[{index}]", errors)
            if tag is None:
                continue
            name = tag.get("name")
            commit = tag.get("commit")
            sha = commit.get("sha") if isinstance(commit, Mapping) else None
            if not isinstance(name, str) or not name:
                errors.append(f"tags[{index}] is missing name")
                continue
            if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
                errors.append(f"tags[{index}] is missing an immutable commit SHA")
                continue
            if tag.get("immutable") is True:
                immutable_tags.append(name)
                continue
            matched = [
                item
                for item in rulesets
                if _ruleset_matches(
                    item,
                    target="tag",
                    short_name=name,
                    default_branch=default_branch,
                )
            ]
            for item in matched:
                rule_types = {
                    rule.get("type")
                    for rule in item.get("rules", [])
                    if isinstance(rule, Mapping)
                }
                if (
                    {"deletion", "update"}.issubset(rule_types)
                    and item.get("bypass_actors") == []
                ):
                    immutable_tags.append(name)
                    break

    passed = (
        releases.ok
        and tags.ok
        and not errors
        and bool(immutable_releases or immutable_tags)
    )
    return _finding(
        "release.immutable_release_or_tag",
        passed,
        (
            "at least one immutable release or protected immutable tag exists"
            if passed
            else "no verifiable immutable release or immutable protected tag exists"
        ),
        immutable_releases=immutable_releases,
        immutable_tags=immutable_tags,
        errors=errors,
    )


def audit_governance(
    snapshot: Mapping[str, Any],
    *,
    repository: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic machine report for a GitHub governance snapshot."""
    endpoints = {
        key: EndpointResult.from_snapshot(snapshot, key) for key in SNAPSHOT_KEYS
    }
    findings: list[Finding] = []

    findings.append(_api_finding(endpoints["repository"]))
    findings.append(
        _api_finding(
            endpoints["branch_protection"],
            accepted_statuses={200, 404},
        )
    )
    for key in (
        "rulesets",
        "environment",
        "label",
        "actions_permissions",
        "selected_actions",
        "releases",
        "tags",
    ):
        findings.append(_api_finding(endpoints[key]))

    repo_errors: list[str] = []
    repo_data = (
        _as_mapping(endpoints["repository"].data, "repository response", repo_errors)
        if endpoints["repository"].ok
        else None
    )
    observed_repo = repo_data.get("full_name") if repo_data is not None else None
    default_branch = repo_data.get("default_branch") if repo_data is not None else None
    if not isinstance(observed_repo, str) or not observed_repo:
        repo_errors.append("repository response is missing full_name")
    if not isinstance(default_branch, str) or not default_branch:
        repo_errors.append("repository response is missing default_branch")
        default_branch = ""
    identity_passed = (
        endpoints["repository"].ok
        and not repo_errors
        and str(observed_repo).casefold() == repository.casefold()
    )
    findings.append(
        _finding(
            "repository.identity_and_default_branch",
            identity_passed,
            (
                f"resolved {repository} default branch {default_branch!r}"
                if identity_passed
                else "repository identity or default branch is missing or ambiguous"
            ),
            expected_repository=repository,
            observed_repository=observed_repo,
            default_branch=default_branch or None,
            errors=repo_errors,
        )
    )

    rulesets, ruleset_errors = _validated_rulesets(endpoints["rulesets"])
    if ruleset_errors:
        findings.append(
            _finding(
                "rulesets.complete_and_unambiguous",
                False,
                "ruleset data is incomplete, denied, or ambiguous",
                errors=ruleset_errors,
            )
        )
    else:
        findings.append(
            _finding(
                "rulesets.complete_and_unambiguous",
                True,
                "ruleset data is complete and unambiguous",
                count=len(rulesets),
            )
        )

    classic, classic_errors = _classic_protection(endpoints["branch_protection"])
    branch_rules = (
        _ruleset_policy(
            rulesets,
            target="branch",
            short_name=default_branch,
            default_branch=default_branch,
        )
        if default_branch
        else {
            "matched": [],
            "checks": set(),
            "pr_required": False,
            "codeowners": False,
            "bypass": [],
            "errors": ["default branch is unresolved"],
        }
    )
    branch_rule_names = [str(item.get("name")) for item in branch_rules["matched"]]
    protection_passed = (
        bool(classic is not None or branch_rules["matched"])
        and not classic_errors
        and not ruleset_errors
        and not branch_rules["errors"]
    )
    findings.append(
        _finding(
            "default_branch.protected",
            protection_passed,
            (
                "default branch is protected by classic protection and/or an active ruleset"
                if protection_passed
                else "default branch has no complete, verifiable protection"
            ),
            classic_protection=classic is not None,
            matching_rulesets=branch_rule_names,
            errors=classic_errors + branch_rules["errors"],
        )
    )

    classic_pr, classic_codeowners, classic_bypass, classic_pr_errors = (
        _classic_pull_request_policy(classic)
    )
    pr_passed = (
        (classic_pr or branch_rules["pr_required"])
        and not classic_pr_errors
        and not branch_rules["errors"]
    )
    findings.append(
        _finding(
            "default_branch.pull_request_required",
            pr_passed,
            "pull requests with approval are required" if pr_passed else
            "pull-request approval is missing or ambiguous",
            classic=classic_pr,
            ruleset=branch_rules["pr_required"],
            errors=classic_pr_errors + branch_rules["errors"],
        )
    )

    classic_checks, classic_check_errors = _classic_checks(classic)
    configured_checks = set(classic_checks) | set(branch_rules["checks"])
    missing_checks = [
        check for check in REQUIRED_CHECKS if not _check_is_required(check, configured_checks)
    ]
    checks_passed = (
        not missing_checks
        and not classic_check_errors
        and not branch_rules["errors"]
    )
    findings.append(
        _finding(
            "default_branch.required_checks",
            checks_passed,
            (
                "hermetic and pr-path-guard are required checks"
                if checks_passed
                else "required status checks are missing or ambiguous"
            ),
            configured=sorted(configured_checks),
            required=list(REQUIRED_CHECKS),
            missing=missing_checks,
            errors=classic_check_errors + branch_rules["errors"],
        )
    )

    codeowners_passed = (
        (classic_codeowners or branch_rules["codeowners"])
        and not classic_pr_errors
        and not branch_rules["errors"]
    )
    findings.append(
        _finding(
            "default_branch.codeowners_review",
            codeowners_passed,
            (
                "CODEOWNERS review is required"
                if codeowners_passed
                else "CODEOWNERS review is disabled, missing, or ambiguous"
            ),
            classic=classic_codeowners,
            ruleset=branch_rules["codeowners"],
            errors=classic_pr_errors + branch_rules["errors"],
        )
    )

    bypass_errors: list[str] = []
    enforce_admins = None
    if classic is not None:
        enforce = _as_mapping(classic.get("enforce_admins"), "enforce_admins", bypass_errors)
        enforce_admins = enforce.get("enabled") if enforce is not None else None
        if not isinstance(enforce_admins, bool):
            bypass_errors.append("enforce_admins.enabled is missing or invalid")
    bypasses = list(classic_bypass) + list(branch_rules["bypass"])
    no_bypass_passed = (
        protection_passed
        and not bypass_errors
        and not classic_pr_errors
        and not branch_rules["errors"]
        and not bypasses
        and (classic is None or enforce_admins is True)
    )
    findings.append(
        _finding(
            "default_branch.no_bypass_include_admins",
            no_bypass_passed,
            (
                "admins are included and no bypass actors are configured"
                if no_bypass_passed
                else "admin enforcement or bypass policy is missing, denied, or unsafe"
            ),
            enforce_admins=enforce_admins,
            bypasses=bypasses,
            errors=bypass_errors + classic_pr_errors + branch_rules["errors"],
        )
    )

    findings.append(_environment_finding(endpoints["environment"]))
    findings.append(_label_finding(endpoints["label"]))
    actions_policy, action_pinning = _actions_findings(
        endpoints["actions_permissions"],
        endpoints["selected_actions"],
    )
    findings.extend((actions_policy, action_pinning))
    findings.append(
        _immutable_release_or_tag(
            endpoints["releases"],
            endpoints["tags"],
            rulesets,
            default_branch=default_branch,
        )
    )

    serialized = [finding.to_dict() for finding in findings]
    failures = [finding["id"] for finding in serialized if not finding["passed"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "github-rest-via-gh",
        "repository": repository,
        "default_branch": default_branch or None,
        "generated_at": generated_at,
        "passed": not failures,
        "failure_count": len(failures),
        "failures": failures,
        "findings": serialized,
    }
