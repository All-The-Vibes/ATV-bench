"""Repository invariant: GitHub Actions never performs benchmark execution.

Actions may run ordinary code/security tests and build/deploy the static GitHub Pages
site. They must never execute submitted bots, harnesses, model calls, arenas, trials, or
benchmark/evaluation commands. Local and official evaluation implementations remain
available outside GitHub Actions.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
DEPLOY_WORKFLOW = WORKFLOWS / "league-deploy.yml"
ALLOWED_WORKFLOW_FILES = {"ci.yml", "league-deploy.yml"}
RETIRED_EXECUTION_WORKFLOWS = {"league.yml", "league-publish.yml"}

FORBIDDEN_EXECUTION_PATTERNS = {
    "container execution": re.compile(r"\b(?:docker|podman|nerdctl)\s+(?:run|build|compose)\b", re.I),
    "submitted bot path": re.compile(r"\bleague/submissions\b|\bsubmission/main\.py\b", re.I),
    "harness execution": re.compile(r"\bharness[-_ ]?run\b|\bcodeclash\b", re.I),
    "benchmark CLI": re.compile(
        r"\batv-bench\s+(?:benchmark|harness-run|eval|trial|play|run)\b", re.I
    ),
    "benchmark module": re.compile(
        r"python(?:3)?\s+-m\s+atv_bench\.(?:benchmark_cli|runner|play|arena|eval|"
        r"control_plane|sandbox)",
        re.I,
    ),
    "model provider": re.compile(
        r"\b(?:OPENAI|ANTHROPIC|AZURE_OPENAI|LITELLM)_[A-Z0-9_]*\b|\blitellm\b",
        re.I,
    ),
}


def _load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path.name} must contain a YAML mapping"
    return value


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on") or workflow.get(True)
    assert isinstance(value, dict), "workflow trigger must be a mapping"
    return value


def _steps(workflow: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    for job_name, job in jobs.items():
        assert isinstance(job, dict)
        for step in job.get("steps", []):
            assert isinstance(step, dict)
            yield str(job_name), step


def _code(step: dict[str, Any]) -> str:
    """Return executable shell with comments removed."""
    lines = []
    for line in str(step.get("run", "")).splitlines():
        code = line.split("#", 1)[0]
        if code.strip():
            lines.append(code)
    return "\n".join(lines)


def _workflow_executable_surface(path: Path) -> str:
    workflow = _load(path)
    chunks: list[str] = []
    for _job_name, step in _steps(workflow):
        chunks.append(_code(step))
        chunks.append(str(step.get("uses", "")))
        chunks.append(yaml.safe_dump(step.get("with", {}), sort_keys=True))
        chunks.append(yaml.safe_dump(step.get("env", {}), sort_keys=True))
    for job in workflow.get("jobs", {}).values():
        chunks.append(yaml.safe_dump(job.get("container", {}), sort_keys=True))
        chunks.append(yaml.safe_dump(job.get("services", {}), sort_keys=True))
    return "\n".join(chunks)


def test_only_test_and_pages_workflows_exist():
    actual = {path.name for path in WORKFLOWS.glob("*.y*ml")}
    assert actual == ALLOWED_WORKFLOW_FILES
    assert not (actual & RETIRED_EXECUTION_WORKFLOWS)


def test_no_privileged_event_chaining_or_manual_execution_triggers():
    forbidden = {
        "workflow_run",
        "pull_request_target",
        "workflow_dispatch",
        "workflow_call",
        "repository_dispatch",
        "issue_comment",
        "schedule",
    }
    for path in WORKFLOWS.glob("*.y*ml"):
        triggers = set(_on(_load(path)))
        assert not (triggers & forbidden), (
            f"{path.name} uses execution-capable trigger(s): {sorted(triggers & forbidden)}"
        )


def test_no_action_executes_bots_harnesses_models_or_benchmarks():
    for path in WORKFLOWS.glob("*.y*ml"):
        surface = _workflow_executable_surface(path)
        for label, pattern in FORBIDDEN_EXECUTION_PATTERNS.items():
            assert not pattern.search(surface), (
                f"{path.name} violates the Actions-never-evaluate invariant: {label}"
            )


def test_actions_do_not_receive_repository_or_model_secrets():
    for path in WORKFLOWS.glob("*.y*ml"):
        text = path.read_text(encoding="utf-8")
        assert "${{ secrets." not in text, f"{path.name} must not consume repository secrets"


def test_ci_is_read_only_and_runs_ordinary_tests_only():
    workflow = _load(CI_WORKFLOW)
    assert set(_on(workflow)) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    body = _workflow_executable_surface(CI_WORKFLOW)
    assert "pytest " in body
    assert "atv_bench.publish build" not in body
    assert "actions/deploy-pages" not in body
    for job in workflow["jobs"].values():
        permissions = job.get("permissions", {})
        assert permissions.get("contents") != "write"
        assert "pages" not in permissions
        assert "id-token" not in permissions


def test_pages_workflow_is_push_only_and_pages_only():
    workflow = _load(DEPLOY_WORKFLOW)
    assert set(_on(workflow)) == {"push"}
    job = workflow["jobs"]["deploy"]
    assert job["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    surface = _workflow_executable_surface(DEPLOY_WORKFLOW)
    assert "python -m atv_bench.publish build" in surface
    assert "actions/upload-pages-artifact" in surface
    assert "actions/deploy-pages" in surface


def test_ci_has_always_on_pr_path_guard():
    workflow = _load(CI_WORKFLOW)
    guard = workflow["jobs"]["pr-path-guard"]
    assert "pull_request" in str(guard.get("if", ""))
    body = yaml.safe_dump(guard, width=10**9)
    assert "validate-pr-paths" in body
    assert "--name-status" in body


def test_codeowners_protects_all_trust_critical_surfaces():
    text = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    required = (
        "/.github/",
        "/league/matches.jsonl",
        "/src/",
        "/pyproject.toml",
        "/leaderboard/schema.json",
        "/arena/",
        "/tests/",
        "/uv.lock",
    )
    for path in required:
        assert path in text, f"CODEOWNERS must protect {path}"
    assert "@All-The-Vibes/league-maintainers" in text
