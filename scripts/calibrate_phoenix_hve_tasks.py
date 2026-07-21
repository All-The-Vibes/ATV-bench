#!/usr/bin/env python3
"""Run non-scored budget calibration for the Phoenix-versus-hve task study.

Every candidate budget is one calibration cell. Each cell contains exactly one
fresh paired Phoenix/hve attempt for every explicitly selected calibration task.
The task runner owns workspace creation, harness execution, grading, receipts,
and evidence preservation; this module only checkpoints calibration progress and
applies the preregistered completion-feasibility gate.

Calibration is public, local/self-attested, non-scored, and non-rankable. Task
quality scores and pass/fail outcomes are deliberately omitted from the
calibration summary and never participate in budget selection.
"""

from __future__ import annotations

import argparse
import json
import platform
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from atv_bench.comparison import sha256_file

try:
    from scripts import run_phoenix_hve_task_trials as task_runner
except ImportError:  # Direct ``python scripts/...py`` execution.
    import run_phoenix_hve_task_trials as task_runner  # type: ignore[no-redef]


REPO_ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_SCHEMA = "atv.phoenix-hve-task-calibration/v1"
PLAN_SCHEMA = "atv.phoenix-hve-task-calibration-plan/v1"

# Frozen 20-task analysis portfolio. Calibration is rejected if any of these
# task IDs are requested, regardless of CLI/configuration intent.
SELECTED_BENCHMARK_TASK_DIRECTORIES = (
    "greenfield_02_product_factors",
    "greenfield_05_temperature_range",
    "greenfield_08_join_segments",
    "greenfield_09_weighted_total",
    "repair_01_service_status",
    "repair_02_request_timeout",
    "repair_04_feature_enabled",
    "repair_10_output_format",
    "debugging_02_stale_cache",
    "debugging_03_inverted_flag",
    "debugging_07_path_normalization",
    "debugging_10_premature_rounding",
    "recovery_02_ledger_balance",
    "recovery_03_queue_order",
    "recovery_06_retry_state",
    "recovery_10_restored_mode",
    "context_retrieval_01_service_owner",
    "context_retrieval_03_retention_days",
    "context_retrieval_09_runbook_command",
    "context_retrieval_10_artifact_format",
)
SELECTED_BENCHMARK_TASK_IDS = (
    "pilot.greenfield.02-product-factors",
    "pilot.greenfield.05-temperature-range",
    "pilot.greenfield.08-join-segments",
    "pilot.greenfield.09-weighted-total",
    "pilot.repair.01-service-status",
    "pilot.repair.02-request-timeout",
    "pilot.repair.04-feature-enabled",
    "pilot.repair.10-output-format",
    "pilot.debugging.02-stale-cache",
    "pilot.debugging.03-inverted-flag",
    "pilot.debugging.07-path-normalization",
    "pilot.debugging.10-premature-rounding",
    "pilot.recovery.02-ledger-balance",
    "pilot.recovery.03-queue-order",
    "pilot.recovery.06-retry-state",
    "pilot.recovery.10-restored-mode",
    "pilot.context-retrieval.01-service-owner",
    "pilot.context-retrieval.03-retention-days",
    "pilot.context-retrieval.09-runbook-command",
    "pilot.context-retrieval.10-artifact-format",
)
SELECTED_BENCHMARK_SELECTION_SHA256 = (
    "5b2fdc11722d266ebf6443975fabdd5867787b36ec33f5b6d1b8390df54b665a"
)

_LIMITATIONS = [
    "Calibration is public, synthetic, local, self-attested, and non-rankable.",
    "A selected budget establishes paired completion feasibility, not harness quality.",
    "Task scores and task pass/fail outcomes are preserved only in raw evidence and are not analyzed.",
    "Receipt seals validate JSON structure but do not rehash every referenced raw evidence file on resume.",
    "No network-isolation guarantee is added beyond the task runner's prompt and runtime controls.",
]


class TaskCalibrationError(RuntimeError):
    """Calibration cannot continue without weakening its fail-closed contract."""


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    phoenix_repo: Path
    hve_repo: Path
    task_roots: tuple[Path, ...]
    calibration_task_ids: tuple[str, ...]
    model: str
    candidate_budgets: tuple[int, ...]
    timeout_seconds: int
    randomization_seed: int
    ledger_dir: Path
    evidence_root: Path
    expected_phoenix_commit: str | None = None
    expected_hve_commit: str | None = None
    execution_backend: str = "process"
    oci_copilot_package: Path | None = None
    oci_runtime_base_image: str | None = None
    oci_rust_builder_image: str | None = None
    oci_image_evidence_dir: Path | None = None
    oci_docker: str = "docker"
    tool_compat_shim: bool = True
    work_root: Path | None = None


def _utc_now() -> str:
    return task_runner._utc_now()


def _summary_path(config: CalibrationConfig) -> Path:
    return config.ledger_dir / "calibration.json"


def _selection_policy() -> dict[str, Any]:
    return {
        "candidate_order": "strictly_increasing_max_ai_credits",
        "attempts_per_task_budget_cell": 1,
        "gate": (
            "both harnesses must be reliable and have valid artifacts on every "
            "calibration task"
        ),
        "selection": "first passing candidate in ascending order",
        "scores_used": False,
        "task_pass_fail_used": False,
        "fail_closed": True,
    }


def _selected_suite() -> dict[str, Any]:
    return {
        "task_count": len(SELECTED_BENCHMARK_TASK_IDS),
        "task_ids": list(SELECTED_BENCHMARK_TASK_IDS),
        "task_directories": list(SELECTED_BENCHMARK_TASK_DIRECTORIES),
        "selection_sha256": SELECTED_BENCHMARK_SELECTION_SHA256,
        "calibration_overlap_allowed": False,
    }


def _base_runner_config(
    config: CalibrationConfig,
    *,
    max_ai_credits: int,
) -> task_runner.RunnerConfig:
    return task_runner.RunnerConfig(
        phoenix_repo=config.phoenix_repo,
        hve_repo=config.hve_repo,
        task_roots=config.task_roots,
        model=config.model,
        max_ai_credits=max_ai_credits,
        timeout_seconds=config.timeout_seconds,
        randomization_seed=config.randomization_seed,
        ledger_dir=config.ledger_dir,
        evidence_root=config.evidence_root,
        task_ids=(),
        expected_phoenix_commit=config.expected_phoenix_commit,
        expected_hve_commit=config.expected_hve_commit,
        execution_backend=config.execution_backend,
        oci_copilot_package=config.oci_copilot_package,
        oci_runtime_base_image=config.oci_runtime_base_image,
        oci_rust_builder_image=config.oci_rust_builder_image,
        oci_image_evidence_dir=config.oci_image_evidence_dir,
        oci_docker=config.oci_docker,
        tool_compat_shim=config.tool_compat_shim,
        work_root=config.work_root,
    )


def _cell_randomization_seed(
    base_seed: int,
    candidate_index: int,
    max_ai_credits: int,
) -> int:
    digest = task_runner.sha256_json(
        {
            "algorithm": "phoenix-hve-calibration-cell-seed-v1",
            "base_seed": base_seed,
            "candidate_index": candidate_index,
            "max_ai_credits": max_ai_credits,
        }
    )
    return int(digest[:16], 16)


def _cell_runner_config(
    config: CalibrationConfig,
    *,
    candidate_index: int,
    max_ai_credits: int,
) -> task_runner.RunnerConfig:
    base = _base_runner_config(config, max_ai_credits=max_ai_credits)
    return replace(
        base,
        randomization_seed=_cell_randomization_seed(
            config.randomization_seed,
            candidate_index,
            max_ai_credits,
        ),
    )


def _validate_and_load_tasks(
    config: CalibrationConfig,
) -> tuple[task_runner.TaskPackage, ...]:
    if len(SELECTED_BENCHMARK_TASK_IDS) != 20:
        raise TaskCalibrationError(
            "the frozen analysis portfolio must contain 20 tasks"
        )
    if not config.calibration_task_ids:
        raise TaskCalibrationError("at least one calibration task ID is required")
    if len(set(config.calibration_task_ids)) != len(config.calibration_task_ids):
        raise TaskCalibrationError("calibration task IDs must be unique")
    overlap = sorted(
        set(config.calibration_task_ids).intersection(
            {
                *SELECTED_BENCHMARK_TASK_IDS,
                *SELECTED_BENCHMARK_TASK_DIRECTORIES,
            }
        )
    )
    if overlap:
        raise TaskCalibrationError(
            "calibration tasks overlap the frozen 20-task analysis portfolio: "
            f"{overlap}"
        )
    budgets = config.candidate_budgets
    if not budgets:
        raise TaskCalibrationError("at least one candidate budget is required")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in budgets
    ):
        raise TaskCalibrationError("candidate budgets must be positive integers")
    if tuple(sorted(set(budgets))) != budgets:
        raise TaskCalibrationError(
            "candidate budgets must be unique and strictly increasing"
        )
    if (
        isinstance(config.timeout_seconds, bool)
        or not isinstance(config.timeout_seconds, int)
        or config.timeout_seconds <= 0
    ):
        raise TaskCalibrationError("timeout_seconds must be positive")
    if isinstance(config.randomization_seed, bool) or not isinstance(
        config.randomization_seed,
        int,
    ):
        raise TaskCalibrationError("randomization_seed must be an integer")
    if config.ledger_dir.resolve() == config.evidence_root.resolve():
        raise TaskCalibrationError("ledger_dir and evidence_root must differ")

    runner_config = _base_runner_config(
        config,
        max_ai_credits=config.candidate_budgets[0],
    )
    try:
        task_runner._validate_config(runner_config)
        available = task_runner.load_task_suite(
            task_runner._discover_task_roots(config.task_roots)
        )
    except (
        OSError,
        task_runner.TaskPackageError,
        task_runner.TaskTrialRunnerError,
    ) as exc:
        raise TaskCalibrationError(str(exc)) from exc

    by_reference: dict[str, task_runner.TaskPackage] = {}
    ambiguous: set[str] = set()
    for package in available:
        for reference in (package.id, package.root.name):
            previous = by_reference.get(reference)
            if previous is not None and previous.digest != package.digest:
                ambiguous.add(reference)
            else:
                by_reference[reference] = package
    if ambiguous.intersection(config.calibration_task_ids):
        raise TaskCalibrationError(
            "calibration task references are ambiguous: "
            f"{sorted(ambiguous.intersection(config.calibration_task_ids))}"
        )
    missing = [
        reference
        for reference in config.calibration_task_ids
        if reference not in by_reference
    ]
    if missing:
        raise TaskCalibrationError(f"unknown calibration task references: {missing}")
    selected = [by_reference[reference] for reference in config.calibration_task_ids]
    if len({package.digest for package in selected}) != len(selected):
        raise TaskCalibrationError(
            "calibration task references must identify distinct task packages"
        )
    selected_overlap = sorted(
        package.id
        for package in selected
        if package.id in SELECTED_BENCHMARK_TASK_IDS
        or package.root.name in SELECTED_BENCHMARK_TASK_DIRECTORIES
    )
    if selected_overlap:
        raise TaskCalibrationError(
            "calibration tasks overlap the frozen 20-task analysis portfolio: "
            f"{selected_overlap}"
        )
    packages = tuple(sorted(selected, key=lambda package: (package.id, package.digest)))
    if len(packages) != len(config.calibration_task_ids):
        raise TaskCalibrationError(
            "loaded calibration task IDs do not exactly match the requested set"
        )
    if len(packages) != 5:
        raise TaskCalibrationError(
            "formal calibration requires exactly five held-out tasks"
        )
    category_counts: dict[str, int] = {}
    for package in packages:
        category_counts[package.category] = category_counts.get(package.category, 0) + 1
    expected_categories = {
        "greenfield",
        "repair",
        "debugging",
        "recovery",
        "context-retrieval",
    }
    if set(category_counts) != expected_categories or set(category_counts.values()) != {
        1
    }:
        raise TaskCalibrationError(
            "formal calibration requires one held-out task from each category"
        )
    return packages


def _calibration_plan(
    config: CalibrationConfig,
    packages: Sequence[task_runner.TaskPackage],
    *,
    sources: dict[str, Any],
    runtime: dict[str, Any],
    oci_images: Mapping[str, task_runner.OciImage] | None,
) -> dict[str, Any]:
    code = task_runner._code_identity()
    code["calibrator"] = sha256_file(Path(__file__))
    descriptor = {
        "schema": PLAN_SCHEMA,
        "rankable": False,
        "official": False,
        "scored": False,
        "phase": "budget_calibration",
        "selected_suite": _selected_suite(),
        "calibration_tasks": [
            task_runner._task_identity(package) for package in packages
        ],
        "candidate_max_ai_credits": list(config.candidate_budgets),
        "model": {
            "requested": config.model,
            "selection_source": "explicit_cli",
            "provider_signed": False,
        },
        "timeout_seconds": config.timeout_seconds,
        "randomization": {
            "base_seed": config.randomization_seed,
            "cell_seed_algorithm": "phoenix-hve-calibration-cell-seed-v1",
            "harness_order_algorithm": "sha256-parity-v1",
        },
        "tool_compatibility_shim": config.tool_compat_shim,
        "execution_backend": config.execution_backend,
        "oci_images": (
            {name: image.evidence() for name, image in sorted(oci_images.items())}
            if oci_images is not None
            else None
        ),
        "sources": sources,
        "runtime": {
            **runtime,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "code": code,
        "selection_policy": _selection_policy(),
        "methodology": {
            "fresh_workspace_per_harness_attempt": True,
            "fresh_copilot_home_per_harness_attempt": True,
            "hidden_grader_loaded_after_both_harnesses_exit": True,
            "exact_jsonl_model_attestation": True,
            "all_candidate_cells_executed": True,
            "quality_outcomes_discarded": True,
            "separate_oci_container_per_harness": (config.execution_backend == "oci"),
        },
    }
    return {
        "schema": PLAN_SCHEMA,
        "calibration_plan_digest": task_runner.sha256_json(descriptor),
        "descriptor": descriptor,
    }


def _cell_experiment_digest(
    calibration_plan_digest: str,
    candidate_index: int,
    max_ai_credits: int,
) -> str:
    return task_runner.sha256_json(
        {
            "schema": "atv.phoenix-hve-task-calibration-cell/v1",
            "calibration_plan_digest": calibration_plan_digest,
            "candidate_index": candidate_index,
            "max_ai_credits": max_ai_credits,
        }
    )


def _cell_skeleton(
    config: CalibrationConfig,
    *,
    calibration_plan_digest: str,
    candidate_index: int,
    max_ai_credits: int,
    required_tasks: int,
) -> dict[str, Any]:
    return {
        "candidate_index": candidate_index,
        "max_ai_credits": max_ai_credits,
        "experiment_digest": _cell_experiment_digest(
            calibration_plan_digest,
            candidate_index,
            max_ai_credits,
        ),
        "randomization_seed": _cell_randomization_seed(
            config.randomization_seed,
            candidate_index,
            max_ai_credits,
        ),
        "required_tasks": required_tasks,
        "completed_tasks": 0,
        "status": "pending",
        "passed": False,
        "failure_reasons": [],
        "tasks": [],
    }


def _harness_gate(attempt: dict[str, Any], name: str) -> dict[str, Any]:
    harness = attempt[name]
    receipt = harness["receipt"]
    return {
        "reliable": harness["reliable"] is True,
        "execution_valid": receipt["execution"]["valid"] is True,
        "model_attestation_valid": receipt["model_attestation"]["status"] == "pass",
        "artifact_valid": receipt["artifact"]["valid"] is True,
        "receipt_sha256": receipt["receipt_sha256"],
    }


def _task_result_row(
    package: task_runner.TaskPackage,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": package.id,
        "category": package.category,
        "task_digest": package.digest,
        "attempt_id": attempt["attempt_id"],
        "attempt_sha256": attempt["attempt_sha256"],
        "randomized_order": list(attempt["randomized_order"]),
        "phoenix": _harness_gate(attempt, "phoenix"),
        "hve": _harness_gate(attempt, "hve"),
        "evidence": dict(attempt["evidence"]),
        "scoring": {
            "scored": False,
            "quality_outcome_used": False,
            "task_pass_fail_used": False,
            "scores_omitted_from_summary": True,
        },
    }


def _cell_state(cell: dict[str, Any]) -> dict[str, Any]:
    tasks = cell["tasks"]
    complete = len(tasks) == cell["required_tasks"]
    failures: list[str] = []
    if complete:
        for task in tasks:
            for name in task_runner.HARNESSES:
                gate = task[name]
                if gate["reliable"] is not True:
                    failures.append(f"{task['task_id']}:{name}:not-reliable")
                if gate["artifact_valid"] is not True:
                    failures.append(f"{task['task_id']}:{name}:artifact-invalid")
    passed = complete and not failures
    return {
        "completed_tasks": len(tasks),
        "status": "passed" if passed else ("failed" if complete else "pending"),
        "passed": passed,
        "failure_reasons": failures,
    }


def _derived_summary_state(cells: Sequence[dict[str, Any]]) -> dict[str, Any]:
    cell_states = [_cell_state(cell) for cell in cells]
    complete = all(state["status"] in {"passed", "failed"} for state in cell_states)
    selected = None
    if complete:
        selected = next(
            (
                cell["max_ai_credits"]
                for cell, state in zip(cells, cell_states, strict=True)
                if state["passed"]
            ),
            None,
        )
    return {
        "cell_states": cell_states,
        "decision": (
            "selected"
            if selected is not None
            else ("no_budget" if complete else "incomplete")
        ),
        "selected_max_ai_credits": selected,
        "checkpoint": {
            "completed_cells": sum(
                state["status"] in {"passed", "failed"} for state in cell_states
            ),
            "required_cells": len(cells),
            "completed_task_attempts": sum(
                state["completed_tasks"] for state in cell_states
            ),
            "required_task_attempts": sum(cell["required_tasks"] for cell in cells),
            "complete": complete,
        },
    }


def _refresh_summary(
    summary: dict[str, Any],
    *,
    last_error: dict[str, Any] | None,
) -> dict[str, Any]:
    unsigned = {key: value for key, value in summary.items() if key != "summary_sha256"}
    derived = _derived_summary_state(unsigned["cells"])
    for cell, state in zip(
        unsigned["cells"],
        derived["cell_states"],
        strict=True,
    ):
        cell.update(state)
    unsigned["decision"] = derived["decision"]
    unsigned["selected_max_ai_credits"] = derived["selected_max_ai_credits"]
    unsigned["checkpoint"] = derived["checkpoint"]
    unsigned["last_error"] = last_error
    unsigned["updated_at"] = _utc_now()
    if last_error is not None:
        unsigned["decision"] = "incomplete"
        unsigned["selected_max_ai_credits"] = None
        unsigned["checkpoint"]["complete"] = False
    return task_runner._seal(unsigned, field="summary_sha256")


def _initial_summary(
    config: CalibrationConfig,
    packages: Sequence[task_runner.TaskPackage],
    plan: dict[str, Any],
) -> dict[str, Any]:
    now = _utc_now()
    digest = plan["calibration_plan_digest"]
    summary = {
        "schema": CALIBRATION_SCHEMA,
        "rankable": False,
        "official": False,
        "scored": False,
        "phase": "budget_calibration",
        "calibration_plan_digest": digest,
        "selected_suite": _selected_suite(),
        "calibration_tasks": [
            task_runner._task_identity(package) for package in packages
        ],
        "candidate_max_ai_credits": list(config.candidate_budgets),
        "model": {
            "requested": config.model,
            "selection_source": "explicit_cli",
            "provider_signed": False,
        },
        "timeout_seconds": config.timeout_seconds,
        "randomization": {
            "base_seed": config.randomization_seed,
            "cell_seed_algorithm": "phoenix-hve-calibration-cell-seed-v1",
        },
        "selection_policy": _selection_policy(),
        "cells": [
            _cell_skeleton(
                config,
                calibration_plan_digest=digest,
                candidate_index=index,
                max_ai_credits=budget,
                required_tasks=len(packages),
            )
            for index, budget in enumerate(config.candidate_budgets)
        ],
        "decision": "incomplete",
        "selected_max_ai_credits": None,
        "checkpoint": {},
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "limitations": list(_LIMITATIONS),
    }
    return _refresh_summary(summary, last_error=None)


def _verify_summary(
    summary: Any,
    *,
    config: CalibrationConfig,
    packages: Sequence[task_runner.TaskPackage],
    plan: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(summary, dict):
        raise TaskCalibrationError("calibration checkpoint must be an object")
    try:
        task_runner._verify_seal(
            summary,
            field="summary_sha256",
            label="calibration checkpoint",
        )
    except task_runner.TaskTrialRunnerError as exc:
        raise TaskCalibrationError(str(exc)) from exc

    expected_static = {
        "schema": CALIBRATION_SCHEMA,
        "rankable": False,
        "official": False,
        "scored": False,
        "phase": "budget_calibration",
        "calibration_plan_digest": plan["calibration_plan_digest"],
        "selected_suite": _selected_suite(),
        "calibration_tasks": [
            task_runner._task_identity(package) for package in packages
        ],
        "candidate_max_ai_credits": list(config.candidate_budgets),
        "model": {
            "requested": config.model,
            "selection_source": "explicit_cli",
            "provider_signed": False,
        },
        "timeout_seconds": config.timeout_seconds,
        "randomization": {
            "base_seed": config.randomization_seed,
            "cell_seed_algorithm": "phoenix-hve-calibration-cell-seed-v1",
        },
        "selection_policy": _selection_policy(),
        "limitations": list(_LIMITATIONS),
    }
    for field, expected in expected_static.items():
        if summary.get(field) != expected:
            raise TaskCalibrationError(
                f"calibration checkpoint {field} does not match the plan"
            )
    for field in ("created_at", "updated_at"):
        if not isinstance(summary.get(field), str) or not summary[field]:
            raise TaskCalibrationError(
                f"calibration checkpoint {field} is missing or invalid"
            )
    if summary.get("last_error") is not None and not isinstance(
        summary["last_error"],
        dict,
    ):
        raise TaskCalibrationError("calibration checkpoint last_error is invalid")

    cells = summary.get("cells")
    if not isinstance(cells, list) or len(cells) != len(config.candidate_budgets):
        raise TaskCalibrationError("calibration checkpoint cells are invalid")
    packages_by_id = {package.id: package for package in packages}
    for index, (budget, cell) in enumerate(
        zip(config.candidate_budgets, cells, strict=True)
    ):
        if not isinstance(cell, dict):
            raise TaskCalibrationError("calibration checkpoint cell must be an object")
        expected_digest = _cell_experiment_digest(
            plan["calibration_plan_digest"],
            index,
            budget,
        )
        expected_seed = _cell_randomization_seed(
            config.randomization_seed,
            index,
            budget,
        )
        for field, expected in (
            ("candidate_index", index),
            ("max_ai_credits", budget),
            ("experiment_digest", expected_digest),
            ("randomization_seed", expected_seed),
            ("required_tasks", len(packages)),
        ):
            if cell.get(field) != expected:
                raise TaskCalibrationError(
                    f"calibration checkpoint cell {index} {field} is invalid"
                )
        task_rows = cell.get("tasks")
        if not isinstance(task_rows, list) or len(task_rows) > len(packages):
            raise TaskCalibrationError(
                f"calibration checkpoint cell {index} tasks are invalid"
            )
        task_ids = [row.get("task_id") for row in task_rows if isinstance(row, dict)]
        if len(task_ids) != len(task_rows) or len(set(task_ids)) != len(task_ids):
            raise TaskCalibrationError(
                f"calibration checkpoint cell {index} task IDs are invalid"
            )
        if any(task_id not in packages_by_id for task_id in task_ids):
            raise TaskCalibrationError(
                f"calibration checkpoint cell {index} contains an unknown task"
            )
        cell_config = _cell_runner_config(
            config,
            candidate_index=index,
            max_ai_credits=budget,
        )
        for row in task_rows:
            package = packages_by_id[row["task_id"]]
            try:
                attempt = task_runner._load_orphan_attempt(
                    cell_config,
                    package,
                    0,
                    experiment_digest=expected_digest,
                )
            except task_runner.TaskTrialRunnerError as exc:
                raise TaskCalibrationError(
                    f"calibration evidence for {package.id} is invalid: {exc}"
                ) from exc
            if attempt is None:
                raise TaskCalibrationError(
                    f"calibration evidence for {package.id} is missing"
                )
            if row != _task_result_row(package, attempt):
                raise TaskCalibrationError(
                    f"calibration checkpoint row for {package.id} does not match evidence"
                )
        state = _cell_state(cell)
        for field, expected in state.items():
            if cell.get(field) != expected:
                raise TaskCalibrationError(
                    f"calibration checkpoint cell {index} {field} is inconsistent"
                )

    derived = _derived_summary_state(cells)
    for field in ("decision", "selected_max_ai_credits", "checkpoint"):
        expected = derived[field]
        if summary.get(field) != expected:
            if summary.get("last_error") is not None and field in {
                "decision",
                "selected_max_ai_credits",
            }:
                expected = "incomplete" if field == "decision" else None
            elif summary.get("last_error") is not None and field == "checkpoint":
                expected = dict(expected)
                expected["complete"] = False
            if summary.get(field) != expected:
                raise TaskCalibrationError(
                    f"calibration checkpoint {field} is inconsistent"
                )
    return summary


def _load_summary(
    config: CalibrationConfig,
    packages: Sequence[task_runner.TaskPackage],
    plan: dict[str, Any],
) -> dict[str, Any]:
    path = _summary_path(config)
    if not path.exists():
        summary = _initial_summary(config, packages, plan)
        task_runner._atomic_write_json(path, summary)
        return summary
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskCalibrationError(
            f"calibration checkpoint is unreadable: {exc}"
        ) from exc
    return _verify_summary(
        observed,
        config=config,
        packages=packages,
        plan=plan,
    )


def run_calibration(
    config: CalibrationConfig,
    *,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Run all missing task/budget cells and return the sealed calibration summary."""

    packages = _validate_and_load_tasks(config)
    try:
        sources = {
            "atv_phoenix": task_runner._source_identity(
                config.phoenix_repo,
                repository="All-The-Vibes/ATV-Phoenix",
            ),
            "hve_core": task_runner._source_identity(
                config.hve_repo,
                repository="microsoft/hve-core",
            ),
        }
        task_runner._validate_source_cell(
            _base_runner_config(
                config,
                max_ai_credits=config.candidate_budgets[0],
            ),
            sources,
        )
        node, loader, runtime = task_runner._copilot_runtime_identity()
        oci_images = (
            task_runner._build_oci_images(
                _base_runner_config(
                    config,
                    max_ai_credits=config.candidate_budgets[0],
                )
            )
            if config.execution_backend == "oci"
            else None
        )
    except task_runner.TaskTrialRunnerError as exc:
        raise TaskCalibrationError(str(exc)) from exc
    plan = _calibration_plan(
        config,
        packages,
        sources=sources,
        runtime=runtime,
        oci_images=oci_images,
    )
    try:
        task_runner._ensure_experiment_file(config.evidence_root, plan)
    except task_runner.TaskTrialRunnerError as exc:
        raise TaskCalibrationError(str(exc)) from exc
    if plan_only:
        return {
            "schema": CALIBRATION_SCHEMA,
            "rankable": False,
            "official": False,
            "scored": False,
            "plan_only": True,
            "calibration_plan_digest": plan["calibration_plan_digest"],
            "plan_path": str((config.evidence_root / "experiment.json").resolve()),
            "calibration_tasks": len(packages),
            "candidate_budgets": list(config.candidate_budgets),
            "executed_cells": 0,
        }
    config.ledger_dir.mkdir(parents=True, exist_ok=True)

    with task_runner._ledger_lock(config.ledger_dir):
        summary = _load_summary(config, packages, plan)
        pending = any(
            len(cell["tasks"]) < cell["required_tasks"] for cell in summary["cells"]
        )
        token = task_runner._github_token() if pending else ""
        disabled_skills = task_runner._ambient_skill_names() if pending else []

        for index, (budget, cell) in enumerate(
            zip(config.candidate_budgets, summary["cells"], strict=True)
        ):
            cell_config = _cell_runner_config(
                config,
                candidate_index=index,
                max_ai_credits=budget,
            )
            experiment_digest = cell["experiment_digest"]
            completed = {row["task_id"] for row in cell["tasks"]}
            for package in packages:
                if package.id in completed:
                    continue
                try:
                    attempt = task_runner._load_orphan_attempt(
                        cell_config,
                        package,
                        0,
                        experiment_digest=experiment_digest,
                    )
                    if attempt is None:
                        attempt = task_runner._run_paired_attempt(
                            cell_config,
                            package,
                            0,
                            experiment_digest=experiment_digest,
                            node=node,
                            loader=loader,
                            token=token,
                            disabled_skills=disabled_skills,
                            oci_images=oci_images,
                        )
                    cell["tasks"].append(_task_result_row(package, attempt))
                    completed.add(package.id)
                    summary = _refresh_summary(summary, last_error=None)
                    task_runner._atomic_write_json(_summary_path(config), summary)
                    cell = summary["cells"][index]
                except Exception as exc:
                    failure = {
                        "candidate_index": index,
                        "max_ai_credits": budget,
                        "task_id": package.id,
                        "error": task_runner._error(exc),
                    }
                    summary = _refresh_summary(summary, last_error=failure)
                    task_runner._atomic_write_json(_summary_path(config), summary)
                    raise TaskCalibrationError(
                        "calibration stopped fail-closed at "
                        f"budget {budget}, task {package.id}: {exc}"
                    ) from exc

        # Recompute once more after every candidate cell is complete. Selection
        # is impossible until all lower and higher preregistered cells exist.
        summary = _refresh_summary(summary, last_error=None)
        task_runner._atomic_write_json(_summary_path(config), summary)
        return _verify_summary(
            summary,
            config=config,
            packages=packages,
            plan=plan,
        )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run non-scored Phoenix/hve completion calibration over held-out tasks."
        )
    )
    parser.add_argument("--phoenix-repo", required=True)
    parser.add_argument("--hve-repo", required=True)
    parser.add_argument("--tasks-root", action="append", default=[])
    parser.add_argument(
        "--calibration-task-id",
        action="append",
        required=True,
        help="Held-out task ID; repeat for every calibration task.",
    )
    parser.add_argument(
        "--candidate-budget",
        action="append",
        required=True,
        type=_positive_int,
        help="Ascending max-ai-credits candidate; repeat in strict order.",
    )
    parser.add_argument("--model", required=True, type=task_runner._model_identifier)
    parser.add_argument("--timeout-seconds", required=True, type=_positive_int)
    parser.add_argument("--randomization-seed", required=True, type=int)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--evidence-root")
    parser.add_argument("--work-root")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--expected-phoenix-commit")
    parser.add_argument("--expected-hve-commit")
    parser.add_argument(
        "--execution-backend",
        choices=("process", "oci"),
        default="process",
    )
    parser.add_argument("--oci-copilot-package")
    parser.add_argument("--oci-runtime-base-image")
    parser.add_argument("--oci-rust-builder-image")
    parser.add_argument("--oci-image-evidence-dir")
    parser.add_argument("--oci-docker", default="docker")
    parser.add_argument(
        "--tool-compat-shim",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    roots = tuple(
        Path(value).resolve()
        for value in (args.tasks_root or [str(REPO_ROOT / "tasks" / "pilot")])
    )
    ledger_dir = Path(args.ledger_dir).resolve()
    evidence_root = (
        Path(args.evidence_root).resolve()
        if args.evidence_root
        else ledger_dir.parent / f"{ledger_dir.name}-evidence"
    )
    config = CalibrationConfig(
        phoenix_repo=Path(args.phoenix_repo).resolve(),
        hve_repo=Path(args.hve_repo).resolve(),
        task_roots=roots,
        calibration_task_ids=tuple(args.calibration_task_id),
        model=args.model,
        candidate_budgets=tuple(args.candidate_budget),
        timeout_seconds=args.timeout_seconds,
        randomization_seed=args.randomization_seed,
        ledger_dir=ledger_dir,
        evidence_root=evidence_root.resolve(),
        expected_phoenix_commit=args.expected_phoenix_commit,
        expected_hve_commit=args.expected_hve_commit,
        execution_backend=args.execution_backend,
        oci_copilot_package=(
            Path(args.oci_copilot_package).resolve()
            if args.oci_copilot_package
            else None
        ),
        oci_runtime_base_image=args.oci_runtime_base_image,
        oci_rust_builder_image=args.oci_rust_builder_image,
        oci_image_evidence_dir=(
            Path(args.oci_image_evidence_dir).resolve()
            if args.oci_image_evidence_dir
            else None
        ),
        oci_docker=args.oci_docker,
        tool_compat_shim=bool(args.tool_compat_shim),
        work_root=Path(args.work_root).resolve() if args.work_root else None,
    )
    try:
        output = run_calibration(config, plan_only=bool(args.plan_only))
    except TaskCalibrationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
