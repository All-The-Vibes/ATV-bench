"""GitHub Pages workflow topology and freshness tripwires."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "league-deploy.yml"


@pytest.fixture(scope="module")
def deploy_wf() -> dict[str, Any]:
    assert WORKFLOW.exists()
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on") or workflow.get(True)
    assert isinstance(value, dict)
    return value


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow["jobs"]["deploy"]["steps"]


def _uses(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).startswith(action)


def _code(step: dict[str, Any]) -> str:
    return "\n".join(
        line.split("#", 1)[0]
        for line in str(step.get("run", "")).splitlines()
        if line.split("#", 1)[0].strip()
    )


def test_deploy_is_push_only_on_the_default_branch(deploy_wf):
    on = _on(deploy_wf)
    assert set(on) == {"push"}
    assert set(on["push"]["branches"]) == {"main", "master"}


def test_deploy_watches_every_real_board_input(deploy_wf):
    paths = set(_on(deploy_wf)["push"]["paths"])
    required = {
        "league/**",
        "leaderboard/**",
        "src/atv_bench/publish.py",
        "src/atv_bench/leaderboard.py",
        "src/atv_bench/store.py",
        "src/atv_bench/elo.py",
        "src/atv_bench/fingerprint/**",
        "src/atv_bench/view/index.html",
        ".github/workflows/league-deploy.yml",
    }
    assert required <= paths


def test_deploy_uses_newest_wins_pages_concurrency(deploy_wf):
    concurrency = deploy_wf["concurrency"]
    assert concurrency["group"] == "pages"
    assert concurrency["cancel-in-progress"] is True


def test_deploy_checks_out_the_triggering_commit_without_credentials(deploy_wf):
    checkout = next(step for step in _steps(deploy_wf) if _uses(step, "actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    assert "ref" not in checkout["with"], (
        "push-only deploy must build the immutable triggering SHA, not a later branch head"
    )


def test_deploy_only_builds_and_publishes_static_pages(deploy_wf):
    steps = _steps(deploy_wf)
    build = next(step for step in steps if "atv_bench.publish build" in _code(step))
    assert "--out ./site" in _code(build)
    assert "--store league" in _code(build)
    assert any(_uses(step, "actions/upload-pages-artifact") for step in steps)
    assert any(_uses(step, "actions/deploy-pages") for step in steps)

    body = "\n".join(_code(step) for step in steps).lower()
    for forbidden in (
        "docker run",
        "docker build",
        "harness-run",
        "atv-bench benchmark",
        "atv-bench eval",
        "atv-bench trial",
        "codeclash",
        "league/submissions",
        "pip install -e",
    ):
        assert forbidden not in body
