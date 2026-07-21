"""Focused tests for non-scored Phoenix-versus-hve task calibration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

import pytest

from atv_bench.eval.tasks import TaskGate, TaskPackage
from scripts import calibrate_phoenix_hve_tasks as calibration
from scripts import run_phoenix_hve_task_trials as runner


ROOT = Path(__file__).resolve().parents[1]
PILOT_ROOT = ROOT / "tasks" / "pilot"
CALIBRATION_TASK_DIRECTORIES = (
    "greenfield_01_sum_orders",
    "repair_03_listener_port",
    "debugging_01_off_by_one",
    "recovery_01_sequence_resume",
    "context_retrieval_02_request_route",
)
CALIBRATION_TASK_IDS = (
    "pilot.greenfield.01-sum-orders",
    "pilot.repair.03-listener-port",
    "pilot.debugging.01-off-by-one",
    "pilot.recovery.01-sequence-resume",
    "pilot.context-retrieval.02-request-route",
)
MODEL = "model-explicit"


def _packages(
    task_ids: tuple[str, ...] = CALIBRATION_TASK_IDS,
) -> tuple[TaskPackage, ...]:
    packages = {
        package.id: package
        for package in (
            TaskPackage.load(PILOT_ROOT / directory)
            for directory in CALIBRATION_TASK_DIRECTORIES
        )
    }
    return tuple(packages[task_id] for task_id in sorted(task_ids))


def _replace_workspace(workspace: Path, candidate: Path) -> None:
    for child in list(workspace.iterdir()):
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for source in candidate.iterdir():
        destination = workspace / source.name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


def _jsonl(model: str, session_id: str, *, mixed: bool = False) -> bytes:
    events = []
    if mixed:
        events.append(
            {
                "type": "assistant.message",
                "data": {"model": "wrong-model", "content": "starting"},
            }
        )
    events.extend(
        [
            {
                "type": "assistant.message",
                "data": {"model": model, "content": "done"},
            },
            {
                "type": "result",
                "exitCode": 0,
                "sessionId": session_id,
                "usage": {"totalTokens": 17},
            },
        ]
    )
    return b"".join(
        json.dumps(event, sort_keys=True).encode("utf-8") + b"\n" for event in events
    )


def _config(
    tmp_path: Path,
    *,
    task_ids: tuple[str, ...] = CALIBRATION_TASK_IDS,
    budgets: tuple[int, ...] = (10, 20, 30),
) -> calibration.CalibrationConfig:
    phoenix_repo = tmp_path / "phoenix"
    hve_repo = tmp_path / "hve"
    phoenix_repo.mkdir(exist_ok=True)
    (phoenix_repo / "Cargo.toml").write_text(
        "[package]\nname='fake'\n",
        encoding="utf-8",
    )
    (hve_repo / "plugins" / "hve-core").mkdir(parents=True, exist_ok=True)
    return calibration.CalibrationConfig(
        phoenix_repo=phoenix_repo,
        hve_repo=hve_repo,
        task_roots=(PILOT_ROOT,),
        calibration_task_ids=task_ids,
        model=MODEL,
        candidate_budgets=budgets,
        timeout_seconds=123,
        randomization_seed=20260721,
        ledger_dir=tmp_path / "calibration-ledger",
        evidence_root=tmp_path / "calibration-evidence",
        work_root=tmp_path / "work",
    )


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    packages: tuple[TaskPackage, ...],
    unreliable: Callable[[int, str, str], bool],
) -> tuple[list[dict[str, object]], list[str]]:
    prompts = {
        package.id: package.prompt_path.read_text(encoding="utf-8").rstrip()
        for package in packages
    }
    oracles = {
        package.id: next(
            path
            for gate, _, path, _ in package.validation_cases()
            if gate is TaskGate.ORACLE
        )
        for package in packages
    }
    events: list[dict[str, object]] = []
    workspaces: list[str] = []

    monkeypatch.setattr(
        runner,
        "_source_identity",
        lambda repo, *, repository: {
            "repository": repository,
            "commit": "a" * 40,
            "git_tree": "b" * 40,
            "tracked_tree_listing_sha256": "c" * 64,
            "worktree_status_sha256": "d" * 64,
            "tracked_diff_sha256": "e" * 64,
            "dirty": False,
        },
    )
    monkeypatch.setattr(
        runner,
        "_copilot_runtime_identity",
        lambda: (
            "node",
            "loader",
            {
                "copilot_cli": "fake-copilot 1.0",
                "node": "v24.0.0",
                "loader_sha256": "f" * 64,
            },
        ),
    )
    monkeypatch.setattr(runner, "_github_token", lambda: "secret-not-recorded")
    monkeypatch.setattr(runner, "_ambient_skill_names", lambda: ["ambient"])
    monkeypatch.setattr(runner, "_utc_now", lambda: "2026-07-21T00:00:00Z")
    monkeypatch.setattr(calibration, "_utc_now", lambda: "2026-07-21T00:00:00Z")

    def prepare_phoenix(repo, root, disabled, *, tool_compat_shim):
        copilot_home = root / "copilot-home"
        user_home = root / "user-home"
        copilot_home.mkdir(parents=True)
        user_home.mkdir(parents=True)
        binary = root / "phoenix-mcp.exe"
        binary.write_bytes(b"phoenix")
        return {
            "copilot_home": copilot_home,
            "user_home": user_home,
            "binary": binary,
            "tool_compatibility_shim": {"same": True},
        }

    def prepare_hve(repo, root, disabled, *, tool_compat_shim):
        copilot_home = root / "copilot-home"
        user_home = root / "user-home"
        plugin = root / "plugin"
        copilot_home.mkdir(parents=True)
        user_home.mkdir(parents=True)
        plugin.mkdir(parents=True)
        (plugin / "agent.md").write_text("fake", encoding="utf-8")
        return {
            "copilot_home": copilot_home,
            "user_home": user_home,
            "plugin": plugin,
            "resolved_pointers": 1,
            "tool_compatibility_shim": {"same": True},
        }

    monkeypatch.setattr(runner, "_prepare_phoenix", prepare_phoenix)
    monkeypatch.setattr(runner, "_prepare_hve", prepare_hve)

    def initialize_workspace(source, destination):
        shutil.copytree(source, destination)
        (destination / ".git").mkdir()

    monkeypatch.setattr(runner, "_initialize_workspace", initialize_workspace)

    def run_harness(command, *, workspace, env, timeout_seconds):
        agent = command[command.index("--agent") + 1]
        name = "phoenix" if agent == "phoenix" else "hve"
        budget = int(command[command.index("--max-ai-credits") + 1])
        goal = command[command.index("-p") + 1]
        task_id = next(
            task_id for task_id, prompt in prompts.items() if goal.startswith(prompt)
        )
        mixed = unreliable(budget, task_id, name)
        events.append(
            {
                "budget": budget,
                "task_id": task_id,
                "harness": name,
                "mixed": mixed,
            }
        )
        workspaces.append(str(workspace))
        assert workspace.is_dir()
        assert (workspace / ".git").exists()
        assert command[command.index("--model") + 1] == MODEL
        assert timeout_seconds == 123
        _replace_workspace(workspace, oracles[task_id])
        return runner.HarnessExecution(
            status="ok",
            exit_code=0,
            duration_seconds=0.01,
            stdout=_jsonl(
                MODEL,
                f"{budget}-{task_id}-{name}",
                mixed=mixed,
            ),
            stderr=b"",
            diff=f"diff for {budget}-{task_id}-{name}",
        )

    monkeypatch.setattr(runner, "_run_harness", run_harness)
    return events, workspaces


def _summary_file(config: calibration.CalibrationConfig) -> dict:
    path = config.ledger_dir / "calibration.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_selects_smallest_fully_reliable_budget_and_resumes(
    tmp_path,
    monkeypatch,
):
    packages = _packages()
    config = _config(tmp_path)
    events, workspaces = _install_fake_runtime(
        monkeypatch,
        packages,
        lambda budget, task_id, name: budget == 10 and name == "phoenix",
    )

    output = calibration.run_calibration(config)

    assert output["schema"] == calibration.CALIBRATION_SCHEMA
    assert output["rankable"] is False
    assert output["official"] is False
    assert output["scored"] is False
    assert output["decision"] == "selected"
    assert output["selected_max_ai_credits"] == 20
    assert output["checkpoint"] == {
        "completed_cells": 3,
        "required_cells": 3,
        "completed_task_attempts": len(packages) * 3,
        "required_task_attempts": len(packages) * 3,
        "complete": True,
    }
    assert [cell["status"] for cell in output["cells"]] == [
        "failed",
        "passed",
        "passed",
    ]
    assert len(events) == len(packages) * len(config.candidate_budgets) * 2
    assert len(workspaces) == len(packages) * len(config.candidate_budgets) * 2
    assert len(set(workspaces)) == len(packages) * len(config.candidate_budgets) * 2
    assert {
        (event["budget"], event["task_id"], event["harness"]) for event in events
    } == {
        (budget, package.id, harness)
        for budget in config.candidate_budgets
        for package in packages
        for harness in runner.HARNESSES
    }

    attempt_ids = set()
    for cell in output["cells"]:
        assert len(cell["tasks"]) == len(packages)
        for task in cell["tasks"]:
            assert set(task["scoring"]) == {
                "scored",
                "quality_outcome_used",
                "task_pass_fail_used",
                "scores_omitted_from_summary",
            }
            assert task["scoring"]["scored"] is False
            assert "score" not in task
            assert "artifact_score" not in task
            assert "passed" not in task
            attempt_ids.add(task["attempt_id"])
            evidence = config.evidence_root / task["evidence"]["relative_path"]
            attempt = json.loads(
                (evidence / "attempt.json").read_text(encoding="utf-8")
            )
            assert attempt["attempt_id"] == task["attempt_id"]
            for harness in runner.HARNESSES:
                assert attempt[harness]["receipt"]["receipt_sha256"]
                assert (evidence / f"raw/{harness}.stdout.bin").is_file()
                assert (evidence / f"artifacts/{harness}.manifest.json").is_file()
    assert len(attempt_ids) == len(packages) * len(config.candidate_budgets)
    assert _summary_file(config) == output

    calls_before = len(events)
    bytes_before = (config.ledger_dir / "calibration.json").read_bytes()
    resumed = calibration.run_calibration(config)
    assert resumed["selected_max_ai_credits"] == 20
    assert len(events) == calls_before
    assert (config.ledger_dir / "calibration.json").read_bytes() == bytes_before


def test_no_budget_when_any_task_fails_every_candidate(
    tmp_path,
    monkeypatch,
):
    packages = _packages()
    config = _config(tmp_path, budgets=(10, 20))
    events, _ = _install_fake_runtime(
        monkeypatch,
        packages,
        lambda budget, task_id, name: (
            task_id == CALIBRATION_TASK_IDS[0] and name == "hve"
        ),
    )

    output = calibration.run_calibration(config)

    assert output["decision"] == "no_budget"
    assert output["selected_max_ai_credits"] is None
    assert output["checkpoint"]["complete"] is True
    assert [cell["status"] for cell in output["cells"]] == ["failed", "failed"]
    assert len(events) == len(packages) * len(config.candidate_budgets) * 2
    for cell in output["cells"]:
        assert any(
            reason.endswith(":hve:not-reliable") for reason in cell["failure_reasons"]
        )


def test_committed_orphan_evidence_is_recovered_after_interruption(
    tmp_path,
    monkeypatch,
):
    packages = _packages()
    config = _config(tmp_path, budgets=(20,))
    events, _ = _install_fake_runtime(
        monkeypatch,
        packages,
        lambda budget, task_id, name: False,
    )
    original = runner._run_paired_attempt
    interrupted = False

    def commit_then_interrupt(*args, **kwargs):
        nonlocal interrupted
        attempt = original(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise runner.TaskTrialRunnerError("simulated checkpoint interruption")
        return attempt

    monkeypatch.setattr(runner, "_run_paired_attempt", commit_then_interrupt)
    with pytest.raises(
        calibration.TaskCalibrationError,
        match="simulated checkpoint interruption",
    ):
        calibration.run_calibration(config)

    partial = _summary_file(config)
    assert partial["decision"] == "incomplete"
    assert partial["selected_max_ai_credits"] is None
    assert partial["last_error"]["task_id"] == packages[0].id
    assert partial["cells"][0]["tasks"] == []
    assert len(list(config.evidence_root.rglob("attempt.json"))) == 1
    assert len(events) == 2

    monkeypatch.setattr(runner, "_run_paired_attempt", original)
    output = calibration.run_calibration(config)

    assert output["decision"] == "selected"
    assert output["selected_max_ai_credits"] == 20
    assert output["last_error"] is None
    assert len(output["cells"][0]["tasks"]) == len(packages)
    assert len(events) == len(packages) * 2
    assert len(list(config.evidence_root.rglob("attempt.json"))) == len(packages)


def test_selected_analysis_task_is_rejected_before_execution(tmp_path, monkeypatch):
    selected = calibration.SELECTED_BENCHMARK_TASK_IDS[0]
    config = _config(tmp_path, task_ids=(selected,), budgets=(20,))
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("execution must not start")

    monkeypatch.setattr(runner, "_source_identity", unexpected)

    with pytest.raises(
        calibration.TaskCalibrationError,
        match="overlap the frozen 20-task",
    ):
        calibration.run_calibration(config)
    assert called is False
    assert not config.ledger_dir.exists()
    assert not config.evidence_root.exists()


@pytest.mark.parametrize("budgets", [(20, 10), (10, 10)])
def test_candidate_budgets_must_be_strictly_increasing(
    tmp_path,
    monkeypatch,
    budgets,
):
    config = _config(tmp_path, budgets=budgets)
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("runtime discovery must not start")

    monkeypatch.setattr(runner, "_source_identity", unexpected)

    with pytest.raises(
        calibration.TaskCalibrationError,
        match="strictly increasing",
    ):
        calibration.run_calibration(config)
    assert called is False
