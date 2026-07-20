#!/usr/bin/env python3
"""Collect and audit GitHub governance settings using read-only REST requests."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from atv_bench.governance import audit_governance

HTTP_STATUS = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _result(
    endpoint: str,
    *,
    status: int | None,
    data: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "status": status,
        "data": data,
        "error": error,
    }


class GhApiClient:
    """Small injectable wrapper around ``gh api --method GET``."""

    def __init__(self, binary: str = "gh", runner=subprocess.run):
        self.binary = binary
        self.runner = runner

    def get(self, endpoint: str, *, paginate: bool = False) -> dict[str, Any]:
        command = [
            self.binary,
            "api",
            "--method",
            "GET",
            endpoint,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        if paginate:
            command.extend(("--paginate", "--slurp"))
        try:
            process = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            return _result(
                endpoint,
                status=None,
                error=f"could not execute {self.binary!r}: {exc}",
            )
        if process.returncode != 0:
            diagnostic = (process.stderr or process.stdout or "").strip()
            match = HTTP_STATUS.search(diagnostic)
            status = int(match.group(1)) if match else None
            return _result(
                endpoint,
                status=status,
                error=diagnostic or f"gh api exited {process.returncode}",
            )
        payload = process.stdout.strip()
        if not payload:
            return _result(
                endpoint,
                status=200,
                error="gh api returned an empty response body",
            )
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return _result(
                endpoint,
                status=200,
                error=f"gh api returned malformed JSON: {exc}",
            )
        if paginate:
            if not isinstance(data, list):
                return _result(
                    endpoint,
                    status=200,
                    error="paginated gh api response is not an array",
                )
            if data and all(isinstance(page, list) for page in data):
                data = [item for page in data for item in page]
        return _result(endpoint, status=200, data=data)


def _unresolved(endpoint: str, reason: str) -> dict[str, Any]:
    return _result(endpoint, status=None, error=reason)


def _workflow_sources(
    client: GhApiClient,
    repository: str,
    default_branch: str | None,
    workflows: dict[str, Any],
) -> dict[str, Any]:
    """Fetch default-branch source for every workflow GitHub reports as active."""
    endpoint = f"repos/{repository}/contents/.github/workflows/<active>"
    if workflows.get("status") != 200 or workflows.get("error"):
        return _unresolved(
            endpoint,
            "active workflow source cannot be resolved because the workflow "
            "catalog is unavailable",
        )
    if not isinstance(default_branch, str) or not default_branch:
        return _unresolved(
            endpoint,
            "active workflow source cannot be resolved because the default "
            "branch is unavailable",
        )

    catalog = workflows.get("data")
    if not isinstance(catalog, dict):
        return _result(
            endpoint,
            status=200,
            error="workflow catalog response is not an object",
        )
    rows = catalog.get("workflows")
    if not isinstance(rows, list):
        return _result(
            endpoint,
            status=200,
            error="workflow catalog is missing the workflows array",
        )

    encoded_ref = quote(default_branch, safe="")
    sources: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            failures.append(f"workflows[{index}] is not an object")
            continue
        state = row.get("state")
        if not isinstance(state, str):
            failures.append(f"workflows[{index}] is missing state")
            continue
        if state != "active":
            continue
        path = row.get("path")
        workflow_id = row.get("id")
        if not isinstance(path, str) or not path:
            failures.append(f"workflows[{index}] is missing path")
            continue
        if isinstance(workflow_id, bool) or not isinstance(workflow_id, int):
            failures.append(f"workflows[{index}] is missing integer id")
            continue
        content_endpoint = (
            f"repos/{repository}/contents/{quote(path, safe='/')}"
            f"?ref={encoded_ref}"
        )
        source = client.get(content_endpoint)
        sources.append(
            {
                "id": workflow_id,
                "path": path,
                "state": state,
                "source": source,
            }
        )
        if source.get("status") != 200 or source.get("error"):
            failures.append(
                f"{content_endpoint}: status={source.get('status')!r}, "
                f"error={source.get('error')!r}"
            )
    return _result(
        endpoint,
        status=200,
        data=sources,
        error="; ".join(failures) if failures else None,
    )


def _ruleset_details(
    client: GhApiClient,
    repository: str,
    listed: dict[str, Any],
) -> dict[str, Any]:
    endpoint = f"repos/{repository}/rulesets?includes_parents=true&per_page=100"
    if listed.get("status") != 200 or listed.get("error"):
        return listed
    rows = listed.get("data")
    if not isinstance(rows, list):
        return _result(
            endpoint,
            status=200,
            error="ruleset list response is not an array",
        )
    details: list[Any] = []
    failures: list[str] = []
    for index, row in enumerate(rows):
        ruleset_id = row.get("id") if isinstance(row, dict) else None
        if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int):
            failures.append(f"rulesets[{index}] has no integer id")
            continue
        detail_endpoint = f"repos/{repository}/rulesets/{ruleset_id}"
        detail = client.get(detail_endpoint)
        if detail.get("status") != 200 or detail.get("error"):
            failures.append(
                f"{detail_endpoint}: status={detail.get('status')!r}, "
                f"error={detail.get('error')!r}"
            )
            continue
        details.append(detail.get("data"))
    return _result(
        endpoint,
        status=200,
        data=details,
        error="; ".join(failures) if failures else None,
    )


def collect_snapshot(
    repository: str,
    *,
    client: GhApiClient | None = None,
) -> dict[str, Any]:
    """Collect every endpoint needed by the fail-closed policy evaluator."""
    client = client or GhApiClient()
    base = f"repos/{repository}"
    snapshot: dict[str, Any] = {}

    snapshot["repository"] = client.get(base)
    repo_data = snapshot["repository"].get("data")
    default_branch = (
        repo_data.get("default_branch") if isinstance(repo_data, dict) else None
    )
    if isinstance(default_branch, str) and default_branch:
        encoded_branch = quote(default_branch, safe="")
        snapshot["branch_protection"] = client.get(
            f"{base}/branches/{encoded_branch}/protection"
        )
        snapshot["codeowners"] = client.get(
            f"{base}/contents/.github/CODEOWNERS?ref={encoded_branch}"
        )
    else:
        snapshot["branch_protection"] = _unresolved(
            f"{base}/branches/<unresolved>/protection",
            "default branch could not be resolved from repository metadata",
        )
        snapshot["codeowners"] = _unresolved(
            f"{base}/contents/.github/CODEOWNERS?ref=<unresolved>",
            "CODEOWNERS source could not be resolved from repository metadata",
        )

    listed_rulesets = client.get(
        f"{base}/rulesets?includes_parents=true&per_page=100",
        paginate=True,
    )
    snapshot["rulesets"] = _ruleset_details(
        client,
        repository,
        listed_rulesets,
    )
    pages_environment = quote("github-pages", safe="")
    snapshot["pages_environment"] = client.get(
        f"{base}/environments/{pages_environment}"
    )
    snapshot["pages_branch_policies"] = client.get(
        f"{base}/environments/{pages_environment}/deployment-branch-policies"
        "?per_page=100"
    )
    snapshot["actions_permissions"] = client.get(f"{base}/actions/permissions")
    snapshot["selected_actions"] = client.get(
        f"{base}/actions/permissions/selected-actions"
    )
    snapshot["workflows"] = client.get(
        f"{base}/actions/workflows?per_page=100"
    )
    snapshot["workflow_sources"] = _workflow_sources(
        client,
        repository,
        default_branch,
        snapshot["workflows"],
    )
    snapshot["releases"] = client.get(
        f"{base}/releases?per_page=100",
        paginate=True,
    )
    snapshot["tags"] = client.get(
        f"{base}/tags?per_page=100",
        paginate=True,
    )
    return snapshot


def _fatal_report(repository: str, generated_at: str, error: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "github-rest-via-gh",
        "repository": repository,
        "default_branch": None,
        "generated_at": generated_at,
        "passed": False,
        "failure_count": 1,
        "failures": ["audit.fatal"],
        "findings": [
            {
                "id": "audit.fatal",
                "passed": False,
                "summary": "governance audit could not complete",
                "evidence": {"error": error},
            }
        ],
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _print_summary(report: dict[str, Any]) -> None:
    for finding in report.get("findings", []):
        marker = "PASS" if finding.get("passed") else "FAIL"
        print(f"[{marker}] {finding.get('id')}: {finding.get('summary')}")
    print(
        f"governance audit: passed={report.get('passed')} "
        f"failures={report.get('failure_count')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit GitHub governance without mutating repository settings."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repository in owner/name form (default: GITHUB_REPOSITORY).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("governance-audit.json"),
        help="Machine-readable JSON report path.",
    )
    parser.add_argument("--gh", default="gh", help="gh executable path.")
    args = parser.parse_args(argv)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    repository = args.repo.strip()
    if not REPOSITORY.fullmatch(repository):
        report = _fatal_report(
            repository,
            generated_at,
            "--repo must be a non-empty owner/name repository identifier",
        )
    else:
        try:
            snapshot = collect_snapshot(
                repository,
                client=GhApiClient(binary=args.gh),
            )
            report = audit_governance(
                snapshot,
                repository=repository,
                generated_at=generated_at,
            )
        except Exception as exc:  # fail closed while still emitting the artifact
            report = _fatal_report(repository, generated_at, repr(exc))

    _write_report(args.out, report)
    _print_summary(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
