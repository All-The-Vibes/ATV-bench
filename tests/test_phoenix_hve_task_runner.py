"""Focused tests for resumable Phoenix-versus-hve task execution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from atv_bench.eval.tasks import TaskGate, TaskPackage
from scripts import run_phoenix_hve_task_trials as runner


ROOT = Path(__file__).resolve().parents[1]
PILOT_TASK = ROOT / "tasks" / "pilot" / "context_retrieval_01_service_owner"
MODEL = "model-explicit"


@pytest.fixture
def package() -> TaskPackage:
    return TaskPackage.load(PILOT_TASK)


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
    package: TaskPackage,
) -> runner.RunnerConfig:
    phoenix_repo = tmp_path / "phoenix"
    hve_repo = tmp_path / "hve"
    phoenix_repo.mkdir()
    (phoenix_repo / "Cargo.toml").write_text("[package]\nname='fake'\n")
    (hve_repo / "plugins" / "hve-core").mkdir(parents=True)
    return runner.RunnerConfig(
        phoenix_repo=phoenix_repo,
        hve_repo=hve_repo,
        task_roots=(package.root,),
        model=MODEL,
        max_ai_credits=41,
        timeout_seconds=123,
        randomization_seed=20260721,
        ledger_dir=tmp_path / "task-ledger",
        evidence_root=tmp_path / "task-evidence",
        work_root=tmp_path / "work",
    )


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    package: TaskPackage,
    events: list[tuple[str, str]],
    *,
    mixed_phoenix_repetitions: set[int] | None = None,
) -> list[Path]:
    cases = {gate: path for gate, _, path, _ in package.validation_cases()}
    oracle = cases[TaskGate.ORACLE]
    mutation = cases[TaskGate.MUTATION]
    mixed_repetitions = mixed_phoenix_repetitions or set()
    workspaces: list[Path] = []
    run_count = 0

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
        (plugin / "agent.md").write_text("fake")
        return {
            "copilot_home": copilot_home,
            "user_home": user_home,
            "plugin": plugin,
            "resolved_pointers": 1,
            "tool_compatibility_shim": {"same": True},
        }

    monkeypatch.setattr(runner, "_prepare_phoenix", prepare_phoenix)
    monkeypatch.setattr(runner, "_prepare_hve", prepare_hve)

    def run_harness(command, *, workspace, env, timeout_seconds):
        nonlocal run_count
        agent = command[command.index("--agent") + 1]
        name = "phoenix" if agent == "phoenix" else "hve"
        repetition = run_count // 2
        run_count += 1
        events.append(("run", name))
        workspaces.append(workspace)
        assert workspace.is_dir()
        assert (workspace / ".git").exists()
        assert not (workspace / "trusted").exists()
        assert str(package.root) not in command[command.index("-p") + 1]
        assert command[command.index("--model") + 1] == MODEL
        assert command[command.index("--max-ai-credits") + 1] == "41"
        assert timeout_seconds == 123
        _replace_workspace(
            workspace,
            oracle if name == "phoenix" else mutation,
        )
        return runner.HarnessExecution(
            status="ok",
            exit_code=0,
            duration_seconds=0.01,
            stdout=_jsonl(
                MODEL,
                f"{repetition}-{name}",
                mixed=(name == "phoenix" and repetition in mixed_repetitions),
            ),
            stderr=b"",
            diff=f"diff for {name}",
        )

    monkeypatch.setattr(runner, "_run_harness", run_harness)
    original_load_grader = runner._load_hidden_grader

    def load_grader(task):
        events.append(("grade", task.id))
        return original_load_grader(task)

    monkeypatch.setattr(runner, "_load_hidden_grader", load_grader)
    return workspaces


def _task_document(config: runner.RunnerConfig) -> dict:
    paths = sorted(config.ledger_dir.glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_writes_exact_five_attempt_task_document_and_resumes(
    tmp_path,
    monkeypatch,
    package,
):
    config = _config(tmp_path, package)
    events: list[tuple[str, str]] = []
    workspaces = _install_fake_runtime(monkeypatch, package, events)

    summary = runner.run_experiment(config)
    document = _task_document(config)

    assert summary["executed_this_run"] == 5
    assert document["schema"] == runner.TRIAL_SCHEMA
    assert document["task_id"] == package.id
    assert document["category"] == package.category
    assert document["task_digest"] == package.digest
    assert document["eligible"] is True
    assert document["rankable"] is False
    assert len(document["attempts"]) == 5
    assert [attempt["repetition"] for attempt in document["attempts"]] == list(range(5))
    assert len({attempt["attempt_id"] for attempt in document["attempts"]}) == 5
    for attempt in document["attempts"]:
        assert attempt["infrastructure_valid"] is True
        assert sorted(attempt["randomized_order"]) == ["hve", "phoenix"]
        assert attempt["phoenix"]["reliable"] is True
        assert attempt["hve"]["reliable"] is True
        assert attempt["phoenix"]["score"] == 1.0
        assert 0.0 <= attempt["hve"]["score"] <= 1.0
        assert attempt["paired_score_difference_phoenix_minus_hve"] == round(
            attempt["phoenix"]["score"] - attempt["hve"]["score"],
            12,
        )

    # Every attempt runs both harnesses before the hidden grader is loaded.
    assert [kind for kind, _ in events] == [
        item for _ in range(5) for item in ("run", "run", "grade")
    ]
    assert len(workspaces) == 10
    assert len({str(path) for path in workspaces}) == 10

    before = next(config.ledger_dir.glob("*.json")).read_bytes()
    calls_before = len(workspaces)
    resumed = runner.run_experiment(config)
    assert resumed["executed_this_run"] == 0
    assert resumed["resumed_from_checkpoints"] == 5
    assert len(workspaces) == calls_before
    assert next(config.ledger_dir.glob("*.json")).read_bytes() == before


def test_partial_checkpoint_resumes_only_missing_repetitions(
    tmp_path,
    monkeypatch,
    package,
):
    config = _config(tmp_path, package)
    events: list[tuple[str, str]] = []
    workspaces = _install_fake_runtime(monkeypatch, package, events)
    original = runner._run_paired_attempt

    def interrupt(config, package, repetition, **kwargs):
        if repetition == 2:
            raise runner.TaskTrialRunnerError("simulated interruption")
        return original(config, package, repetition, **kwargs)

    monkeypatch.setattr(runner, "_run_paired_attempt", interrupt)
    with pytest.raises(runner.TaskTrialRunnerError, match="simulated interruption"):
        runner.run_experiment(config)

    partial = _task_document(config)
    assert partial["eligible"] is False
    assert partial["checkpoint"]["completed_attempts"] == 2
    assert [row["repetition"] for row in partial["attempts"]] == [0, 1]
    assert len(workspaces) == 4

    monkeypatch.setattr(runner, "_run_paired_attempt", original)
    summary = runner.run_experiment(config)
    complete = _task_document(config)
    assert summary["resumed_from_checkpoints"] == 2
    assert summary["executed_this_run"] == 3
    assert complete["eligible"] is True
    assert len(complete["attempts"]) == 5
    assert len(workspaces) == 10


def test_mixed_model_receipt_keeps_artifact_score_but_zeros_analysis_score(
    tmp_path,
    monkeypatch,
    package,
):
    config = _config(tmp_path, package)
    events: list[tuple[str, str]] = []
    _install_fake_runtime(
        monkeypatch,
        package,
        events,
        mixed_phoenix_repetitions={0},
    )

    runner.run_experiment(config)
    document = _task_document(config)
    first = document["attempts"][0]
    phoenix = first["phoenix"]

    assert document["eligible"] is False
    assert first["infrastructure_valid"] is False
    assert "phoenix:model-attestation-fail" in first["infrastructure_reasons"]
    assert phoenix["reliable"] is False
    assert phoenix["score"] == 0.0
    assert phoenix["artifact_score"] == 1.0
    assert phoenix["receipt"]["execution"]["valid"] is True
    assert phoenix["receipt"]["artifact"]["valid"] is True
    assert phoenix["receipt"]["model_attestation"]["status"] == "fail"
    assert "mixed-model-evidence" in phoenix["receipt"]["model_attestation"]["reasons"]


def test_corrupted_atomic_checkpoint_fails_closed(
    tmp_path,
    monkeypatch,
    package,
):
    config = _config(tmp_path, package)
    events: list[tuple[str, str]] = []
    workspaces = _install_fake_runtime(monkeypatch, package, events)
    runner.run_experiment(config)
    path = next(config.ledger_dir.glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["category"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")
    calls_before = len(workspaces)

    with pytest.raises(runner.TaskTrialRunnerError, match="document_sha256"):
        runner.run_experiment(config)
    assert len(workspaces) == calls_before


def test_missing_referenced_evidence_fails_closed_on_resume(
    tmp_path,
    monkeypatch,
    package,
):
    config = _config(tmp_path, package)
    events: list[tuple[str, str]] = []
    workspaces = _install_fake_runtime(monkeypatch, package, events)
    runner.run_experiment(config)
    document = _task_document(config)
    first = document["attempts"][0]
    relative = first["evidence"]["relative_path"]
    stdout = first["phoenix"]["receipt"]["execution"]["stdout"]["path"]
    (config.evidence_root / relative / stdout).unlink()
    calls_before = len(workspaces)

    with pytest.raises(
        runner.TaskTrialRunnerError,
        match="phoenix.stdout evidence is unavailable",
    ):
        runner.run_experiment(config)
    assert len(workspaces) == calls_before


def test_resealed_score_tampering_is_rejected_against_grade_evidence(
    tmp_path,
    monkeypatch,
    package,
):
    config = _config(tmp_path, package)
    events: list[tuple[str, str]] = []
    workspaces = _install_fake_runtime(monkeypatch, package, events)
    runner.run_experiment(config)
    document_path = next(config.ledger_dir.glob("*.json"))
    document = json.loads(document_path.read_text(encoding="utf-8"))
    attempt = document["attempts"][0]
    attempt["phoenix"]["artifact_score"] = 0.25
    attempt["attempt_sha256"] = runner.sha256_json(
        {key: value for key, value in attempt.items() if key != "attempt_sha256"}
    )
    evidence_attempt = (
        config.evidence_root / attempt["evidence"]["relative_path"] / "attempt.json"
    )
    evidence_attempt.write_text(
        json.dumps(attempt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    document["document_sha256"] = runner.sha256_json(
        {key: value for key, value in document.items() if key != "document_sha256"}
    )
    document_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    calls_before = len(workspaces)

    with pytest.raises(
        runner.TaskTrialRunnerError,
        match="grade-derived result fields are inconsistent",
    ):
        runner.run_experiment(config)
    assert len(workspaces) == calls_before


def test_frozen_selection_and_schedule_are_exact_and_balanced(tmp_path):
    selection_path = (
        ROOT / "docs" / "proof" / "phoenix-hve-task-v1" / "task-selection.json"
    )
    config = runner.RunnerConfig(
        phoenix_repo=tmp_path / "unused-phoenix",
        hve_repo=tmp_path / "unused-hve",
        task_roots=(ROOT / "tasks" / "pilot",),
        model=MODEL,
        max_ai_credits=60,
        timeout_seconds=1200,
        randomization_seed=20260721,
        ledger_dir=tmp_path / "ledger",
        evidence_root=tmp_path / "evidence",
        selection_file=selection_path,
        expected_phoenix_commit="1" * 40,
        expected_hve_commit="2" * 40,
    )

    selection = runner._load_selection(selection_path)
    assert selection is not None
    assert (
        selection["digest"]
        == "5b2fdc11722d266ebf6443975fabdd5867787b36ec33f5b6d1b8390df54b665a"
    )
    packages = runner._load_tasks(config, selection=selection)
    assert len(packages) == 20
    assert {package.category for package in packages} == {
        "context-retrieval",
        "debugging",
        "greenfield",
        "recovery",
        "repair",
    }

    schedule = runner._schedule_plan(packages, seed=config.randomization_seed)
    assert len(schedule) == 100
    assert sum(row["randomized_order"][0] == "phoenix" for row in schedule) == 50
    assert sum(row["randomized_order"][0] == "hve" for row in schedule) == 50
    for package in packages:
        rows = [row for row in schedule if row["task_digest"] == package.digest]
        assert [row["repetition"] for row in rows] == list(range(5))
        first_counts = {
            name: sum(row["randomized_order"][0] == name for row in rows)
            for name in runner.HARNESSES
        }
        assert sorted(first_counts.values()) == [2, 3]
    for category in {package.category for package in packages}:
        repetition_zero = [
            row
            for row in schedule
            if row["category"] == category and row["repetition"] == 0
        ]
        assert (
            sum(row["randomized_order"][0] == "phoenix" for row in repetition_zero) == 2
        )
        assert sum(row["randomized_order"][0] == "hve" for row in repetition_zero) == 2
    assert all(
        left["category"] != right["category"]
        for left, right in zip(schedule, schedule[1:])
    )


def test_directory_publish_retries_transient_windows_lock(tmp_path, monkeypatch):
    temporary = tmp_path / "temporary"
    final = tmp_path / "final"
    temporary.mkdir()
    (temporary / "attempt.json").write_text("{}\n", encoding="utf-8")
    real_replace = runner.os.replace
    calls = 0

    def transient_replace(source, target):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise PermissionError("transient scanner lock")
        return real_replace(source, target)

    monkeypatch.setattr(runner.os, "replace", transient_replace)
    runner._publish_directory(temporary, final)

    assert calls == 4
    assert not temporary.exists()
    assert (final / "attempt.json").is_file()
