#!/usr/bin/env python3
"""Task-clustered analysis for preregistered Phoenix-versus-hve-core trials.

Each input JSON document represents one benchmark task and uses
``atv.phoenix-hve-task-trial/v1``. The canonical shape is:

.. code-block:: json

    {
      "schema": "atv.phoenix-hve-task-trial/v1",
      "task_id": "task-001",
      "category": "debugging",
      "eligible": true,
      "attempts": [
        {
          "attempt_id": "task-001-attempt-0",
          "repetition": 0,
          "infrastructure_valid": true,
          "phoenix": {"score": 0.8, "reliable": true},
          "hve": {"score": 0.6, "reliable": true}
        }
      ]
    }

Formal analysis requires an explicit separately frozen sealed preregistration.
The binding freezes exactly twenty task IDs, categories, digests, one
experiment digest, one model, and one budget. Every accepted task document,
attempt, and harness receipt must have a valid canonical-JSON seal.

There must be exactly five fresh infrastructure-valid paired attempts per task.
Attempts are nested measurements. The task mean is the analysis observation,
tasks receive equal macro weight, and bootstrap resampling is over tasks only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from atv_bench.eval._canonical import sha256_json

INPUT_SCHEMA = "atv.phoenix-hve-task-trial/v1"
OUTPUT_SCHEMA = "atv.phoenix-hve-task-analysis/v1"
PREREGISTRATION_SCHEMA = "atv.phoenix-hve-task-preregistration/v1"
EXPERIMENT_SCHEMA = "atv.phoenix-hve-task-experiment/v1"
RUN_SUMMARY_SCHEMA = "atv.phoenix-hve-task-run-summary/v1"
DEFAULT_MARGIN = 0.05
ATTEMPTS_PER_TASK = 5
FORMAL_TASK_COUNT = 20
FORMAL_CATEGORY_COUNT = 5
FORMAL_TASKS_PER_CATEGORY = 4
MINIMUM_ELIGIBLE_TASKS = FORMAL_TASK_COUNT
MINIMUM_CATEGORIES = FORMAL_CATEGORY_COUNT
SUPERIORITY_MIN_AT_LEAST_ONE_RELIABLE_SUITE_RATE = 0.90
SUPERIORITY_MIN_AT_LEAST_ONE_RELIABLE_PER_TASK = 4
EQUIVALENCE_MIN_BOTH_RELIABLE_SUITE_RATE = 0.90
EQUIVALENCE_MIN_BOTH_RELIABLE_PER_TASK = 4
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_BOOTSTRAP_SEED = 20_260_721
DEFAULT_RELIABILITY_ALPHA = 0.05
DEFAULT_JSON_NAME = "aggregate-tasks.json"
DEFAULT_MARKDOWN_NAME = "TASK_RESULTS.md"

PHOENIX = "phoenix"
HVE = "hve"


class TaskAnalysisError(ValueError):
    """The requested task analysis cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class FrozenTask:
    task_id: str
    category: str
    task_digest: str


@dataclass(frozen=True, slots=True)
class ExperimentBinding:
    source: str
    schema: str
    binding_sha256: str
    experiment_digest: str
    model: str
    max_ai_credits: int
    timeout_seconds: int
    tasks: tuple[FrozenTask, ...]
    margin: float
    confidence: float
    bootstrap_samples: int
    bootstrap_seed: int
    reliability_alpha: float
    superiority_min_suite_rate: float
    superiority_min_per_task: int
    equivalence_min_suite_rate: float
    equivalence_min_per_task: int

    @property
    def budget(self) -> dict[str, int]:
        return {
            "max_ai_credits": self.max_ai_credits,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def tasks_by_id(self) -> dict[str, FrozenTask]:
        return {task.task_id: task for task in self.tasks}


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    repetition: int
    phoenix_score: float
    hve_score: float
    phoenix_reliable: bool
    hve_reliable: bool


@dataclass(frozen=True, slots=True)
class TaskTrial:
    source: str
    task_id: str
    category: str
    attempts: tuple[Attempt, ...]
    task_digest: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapSummary:
    mean: float
    ci_low: float
    ci_high: float
    samples: int
    observation_count: int
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean": _rounded(self.mean),
            "ci": {
                "low": _rounded(self.ci_low),
                "high": _rounded(self.ci_high),
                "confidence": self.confidence,
            },
            "samples": self.samples,
            "observation_count": self.observation_count,
            "resampling_unit": "task",
        }


def _rounded(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == -0.0 else rounded


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _seal_reason(
    value: Any,
    *,
    field: str,
    label: str,
) -> str | None:
    if not isinstance(value, dict):
        return f"{label} must be an object"
    observed = value.get(field)
    if not _is_sha256(observed):
        return f"{label} {field} is missing or is not lowercase sha256"
    unsigned = {key: item for key, item in value.items() if key != field}
    try:
        expected = sha256_json(unsigned)
    except (TypeError, ValueError) as exc:
        return f"{label} cannot be canonically hashed: {exc}"
    if observed != expected:
        return f"{label} {field} is invalid"
    return None


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskAnalysisError(f"{field} must be a positive integer")
    return value


def _requested_model(value: Any, *, field: str) -> str:
    if not isinstance(value, dict):
        raise TaskAnalysisError(f"{field} must be an object")
    requested = _clean_string(value.get("requested"))
    if requested is None:
        raise TaskAnalysisError(f"{field}.requested must be a non-empty string")
    return requested


def _budget_values(value: Any, *, field: str) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise TaskAnalysisError(f"{field} must be an object")
    return (
        _positive_integer(
            value.get("max_ai_credits"),
            field=f"{field}.max_ai_credits",
        ),
        _positive_integer(
            value.get("timeout_seconds"),
            field=f"{field}.timeout_seconds",
        ),
    )


def _analysis_policy_values(value: Any) -> dict[str, Any]:
    expected_fields = {
        "superiority_equivalence_margin",
        "confidence",
        "bootstrap_samples",
        "bootstrap_seed",
        "reliability_alpha",
        "superiority_min_at_least_one_reliable_suite_rate",
        "superiority_min_at_least_one_reliable_per_task",
        "equivalence_min_both_reliable_suite_rate",
        "equivalence_min_both_reliable_per_task",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise TaskAnalysisError("preregistration analysis_policy has unexpected fields")

    def finite_number(field: str) -> float:
        observed = value[field]
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise TaskAnalysisError(f"analysis_policy.{field} must be numeric")
        parsed = float(observed)
        if not math.isfinite(parsed):
            raise TaskAnalysisError(f"analysis_policy.{field} must be finite")
        return parsed

    margin = finite_number("superiority_equivalence_margin")
    confidence = finite_number("confidence")
    reliability_alpha = finite_number("reliability_alpha")
    superiority_rate = finite_number("superiority_min_at_least_one_reliable_suite_rate")
    equivalence_rate = finite_number("equivalence_min_both_reliable_suite_rate")
    if margin <= 0.0:
        raise TaskAnalysisError("analysis_policy margin must be positive")
    if not 0.5 < confidence < 1.0:
        raise TaskAnalysisError("analysis_policy confidence must be in (0.5, 1)")
    if not 0.0 < reliability_alpha < 1.0:
        raise TaskAnalysisError("analysis_policy reliability_alpha must be in (0, 1)")
    if not 0.0 < superiority_rate <= 1.0:
        raise TaskAnalysisError(
            "analysis_policy superiority suite rate must be in (0, 1]"
        )
    if not 0.0 < equivalence_rate <= 1.0:
        raise TaskAnalysisError(
            "analysis_policy equivalence suite rate must be in (0, 1]"
        )
    bootstrap_samples = _positive_integer(
        value["bootstrap_samples"],
        field="analysis_policy.bootstrap_samples",
    )
    if bootstrap_samples < 100:
        raise TaskAnalysisError(
            "analysis_policy.bootstrap_samples must be at least 100"
        )
    bootstrap_seed = value["bootstrap_seed"]
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise TaskAnalysisError("analysis_policy.bootstrap_seed must be an integer")
    superiority_per_task = _positive_integer(
        value["superiority_min_at_least_one_reliable_per_task"],
        field="analysis_policy.superiority_min_at_least_one_reliable_per_task",
    )
    equivalence_per_task = _positive_integer(
        value["equivalence_min_both_reliable_per_task"],
        field="analysis_policy.equivalence_min_both_reliable_per_task",
    )
    if superiority_per_task > ATTEMPTS_PER_TASK:
        raise TaskAnalysisError(
            "analysis_policy superiority per-task coverage exceeds attempts"
        )
    if equivalence_per_task > ATTEMPTS_PER_TASK:
        raise TaskAnalysisError(
            "analysis_policy equivalence per-task coverage exceeds attempts"
        )
    return {
        "margin": margin,
        "confidence": confidence,
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": bootstrap_seed,
        "reliability_alpha": reliability_alpha,
        "superiority_min_suite_rate": superiority_rate,
        "superiority_min_per_task": superiority_per_task,
        "equivalence_min_suite_rate": equivalence_rate,
        "equivalence_min_per_task": equivalence_per_task,
    }


def _parse_frozen_tasks(value: Any) -> tuple[FrozenTask, ...]:
    if not isinstance(value, list):
        raise TaskAnalysisError("preregistration tasks must be a list")
    if len(value) != FORMAL_TASK_COUNT:
        raise TaskAnalysisError(
            "formal preregistration must freeze exactly "
            f"{FORMAL_TASK_COUNT} tasks; observed {len(value)}"
        )
    tasks: list[FrozenTask] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise TaskAnalysisError(f"preregistration task {index} must be an object")
        task_id = _clean_string(row.get("task_id"))
        category = _clean_string(row.get("category"))
        task_digest = row.get("task_digest")
        if task_id is None:
            raise TaskAnalysisError(
                f"preregistration task {index}.task_id must be non-empty"
            )
        if category is None:
            raise TaskAnalysisError(
                f"preregistration task {index}.category must be non-empty"
            )
        if not _is_sha256(task_digest):
            raise TaskAnalysisError(
                f"preregistration task {index}.task_digest must be lowercase sha256"
            )
        tasks.append(
            FrozenTask(
                task_id=task_id,
                category=category,
                task_digest=task_digest,
            )
        )

    task_ids = [task.task_id for task in tasks]
    task_digests = [task.task_digest for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise TaskAnalysisError("preregistration task IDs must be unique")
    if len(set(task_digests)) != len(task_digests):
        raise TaskAnalysisError("preregistration task digests must be unique")
    category_counts = Counter(task.category for task in tasks)
    if len(category_counts) != FORMAL_CATEGORY_COUNT:
        raise TaskAnalysisError(
            "formal preregistration must contain exactly "
            f"{FORMAL_CATEGORY_COUNT} categories; observed {len(category_counts)}"
        )
    if set(category_counts.values()) != {FORMAL_TASKS_PER_CATEGORY}:
        raise TaskAnalysisError(
            "formal preregistration must contain exactly "
            f"{FORMAL_TASKS_PER_CATEGORY} tasks in each category; observed "
            f"{dict(sorted(category_counts.items()))}"
        )
    return tuple(tasks)


def _load_preregistration(
    preregistration: Mapping[str, Any] | str | Path,
) -> ExperimentBinding:
    if isinstance(preregistration, (str, Path)):
        path = Path(preregistration).resolve()
        try:
            document = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskAnalysisError(
                f"cannot read preregistration {path}: {type(exc).__name__}: {exc}"
            ) from exc
        source = str(path)
    elif isinstance(preregistration, Mapping):
        document = dict(preregistration)
        source = "<in-memory-preregistration>"
    else:
        raise TaskAnalysisError("preregistration must be an object or a JSON file path")
    if not isinstance(document, dict):
        raise TaskAnalysisError("preregistration document must be a JSON object")

    schema = document.get("schema")
    if schema == PREREGISTRATION_SCHEMA:
        reason = _seal_reason(
            document,
            field="preregistration_sha256",
            label="preregistration",
        )
        if reason:
            raise TaskAnalysisError(reason)
        binding_sha256 = str(document["preregistration_sha256"])
        descriptor = document
    else:
        raise TaskAnalysisError(
            f"preregistration schema must equal {PREREGISTRATION_SCHEMA}"
        )

    experiment_digest = document.get("experiment_digest")
    if not _is_sha256(experiment_digest):
        raise TaskAnalysisError(
            "preregistration experiment_digest must be lowercase sha256"
        )
    if descriptor.get("attempts_per_task") != ATTEMPTS_PER_TASK:
        raise TaskAnalysisError(
            f"preregistration attempts_per_task must equal {ATTEMPTS_PER_TASK}"
        )
    model = _requested_model(descriptor.get("model"), field="preregistration model")
    max_ai_credits, timeout_seconds = _budget_values(
        descriptor.get("budget"),
        field="preregistration budget",
    )
    analysis_policy = _analysis_policy_values(descriptor.get("analysis_policy"))
    tasks = _parse_frozen_tasks(descriptor.get("tasks"))
    return ExperimentBinding(
        source=source,
        schema=str(schema),
        binding_sha256=binding_sha256,
        experiment_digest=str(experiment_digest),
        model=model,
        max_ai_credits=max_ai_credits,
        timeout_seconds=timeout_seconds,
        tasks=tasks,
        **analysis_policy,
    )


def _serialize_binding(binding: ExperimentBinding) -> dict[str, Any]:
    return {
        "source": binding.source,
        "schema": binding.schema,
        "binding_sha256": binding.binding_sha256,
        "experiment_digest": binding.experiment_digest,
        "model": {"requested": binding.model},
        "budget": binding.budget,
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "analysis_policy": {
            "superiority_equivalence_margin": binding.margin,
            "confidence": binding.confidence,
            "bootstrap_samples": binding.bootstrap_samples,
            "bootstrap_seed": binding.bootstrap_seed,
            "reliability_alpha": binding.reliability_alpha,
            "superiority_min_at_least_one_reliable_suite_rate": (
                binding.superiority_min_suite_rate
            ),
            "superiority_min_at_least_one_reliable_per_task": (
                binding.superiority_min_per_task
            ),
            "equivalence_min_both_reliable_suite_rate": (
                binding.equivalence_min_suite_rate
            ),
            "equivalence_min_both_reliable_per_task": (
                binding.equivalence_min_per_task
            ),
        },
        "task_count": len(binding.tasks),
        "category_count": len({task.category for task in binding.tasks}),
        "tasks": [
            {
                "task_id": task.task_id,
                "category": task.category,
                "task_digest": task.task_digest,
            }
            for task in binding.tasks
        ],
    }


def _unique_reasons(reasons: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(reason for reason in reasons if reason))


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise TaskAnalysisError("cannot compute a percentile of an empty sample")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(
        sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction
    )


def _bootstrap_tasks(
    values: Sequence[float],
    *,
    samples: int,
    confidence: float,
    seed: int,
) -> BootstrapSummary | None:
    if not values:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(samples):
        sampled = rng.choices(values, k=len(values))
        draws.append(sum(sampled) / len(sampled))
    draws.sort()
    alpha = 1.0 - confidence
    return BootstrapSummary(
        mean=sum(values) / len(values),
        ci_low=_percentile(draws, alpha / 2.0),
        ci_high=_percentile(draws, 1.0 - alpha / 2.0),
        samples=samples,
        observation_count=len(values),
        confidence=confidence,
    )


def _score_decision(
    bootstrap: BootstrapSummary | None,
    *,
    margin: float,
) -> str:
    if bootstrap is None:
        return "inconclusive"
    if bootstrap.ci_low > margin:
        return "phoenix_superior"
    if bootstrap.ci_high < -margin:
        return "hve_superior"
    if bootstrap.ci_low >= -margin and bootstrap.ci_high <= margin:
        return "practically_equivalent"
    return "inconclusive"


def _exact_two_sided_sign_test(
    positive: int,
    negative: int,
) -> tuple[float, dict[str, Any]]:
    decisive = positive + negative
    if decisive == 0:
        p_value = 1.0
    else:
        smaller = min(positive, negative)
        tail = sum(math.comb(decisive, index) for index in range(smaller + 1))
        p_value = min(1.0, 2.0 * tail / (2**decisive))
    return p_value, {
        "positive": positive,
        "negative": negative,
        "ties_omitted": True,
        "decisive_units": decisive,
        "two_sided_p_value": _rounded(p_value),
        "null": "equal probability of either harness winning a decisive pair",
    }


def _identity_field(
    document: dict[str, Any],
    nested_task: dict[str, Any],
    *,
    top_level: str,
    nested: str,
    label: str,
    reasons: list[str],
) -> str | None:
    top_value = _clean_string(document.get(top_level))
    nested_value = _clean_string(nested_task.get(nested))
    if top_value and nested_value and top_value != nested_value:
        reasons.append(f"conflicting {label} values")
        return top_value
    value = top_value or nested_value
    if value is None:
        reasons.append(f"{label} is missing or blank")
    return value


def _repetition_value(
    attempt: dict[str, Any],
    *,
    position: int,
) -> tuple[int | None, str | None]:
    found: list[int] = []
    for key in ("repetition", "attempt_index", "index", "attempt"):
        if key not in attempt:
            continue
        value = attempt[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, f"{key} must be a non-negative integer"
        found.append(value)
    if len(set(found)) > 1:
        return None, "attempt repetition fields conflict"
    return (found[0] if found else position), None


def _score_value(
    attempt: dict[str, Any],
    harness: str,
) -> tuple[float | None, str | None]:
    candidates: list[Any] = []
    direct = attempt.get(harness)
    if isinstance(direct, dict) and "score" in direct:
        candidates.append(direct["score"])
    harnesses = attempt.get("harnesses")
    if isinstance(harnesses, dict):
        row = harnesses.get(harness)
        if isinstance(row, dict) and "score" in row:
            candidates.append(row["score"])
    scores = attempt.get("scores")
    if isinstance(scores, dict) and harness in scores:
        candidates.append(scores[harness])
    if not candidates:
        return None, f"{harness} score is missing"
    parsed: list[float] = []
    for value in candidates:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"{harness} score must be numeric"
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return None, f"{harness} score must be finite and between 0 and 1"
        parsed.append(number)
    if len(set(parsed)) != 1:
        return None, f"{harness} score fields conflict"
    return parsed[0], None


def _status_reliability(value: Any) -> bool | None:
    status = _clean_string(value)
    if status is None:
        return None
    normalized = status.casefold().replace("-", "_")
    if normalized in {"completed", "ok", "success", "valid"}:
        return True
    if normalized in {
        "crashed",
        "failed",
        "invalid",
        "no_artifact",
        "timeout",
        "timed_out",
    }:
        return False
    raise TaskAnalysisError(f"unrecognized harness status {status!r}")


def _reliability_value(
    attempt: dict[str, Any],
    harness: str,
) -> tuple[bool | None, str | None]:
    candidates: list[bool] = []
    containers: list[dict[str, Any]] = []
    direct = attempt.get(harness)
    if isinstance(direct, dict):
        containers.append(direct)
    harnesses = attempt.get("harnesses")
    if isinstance(harnesses, dict) and isinstance(harnesses.get(harness), dict):
        containers.append(harnesses[harness])
    for container in containers:
        for key in ("reliable", "artifact_valid", "completed"):
            if key not in container:
                continue
            value = container[key]
            if not isinstance(value, bool):
                return None, f"{harness} {key} must be boolean"
            candidates.append(value)
        if "status" in container:
            try:
                status_value = _status_reliability(container["status"])
            except TaskAnalysisError as exc:
                return None, f"{harness} {exc}"
            if status_value is not None:
                candidates.append(status_value)
    reliability = attempt.get("reliability")
    if isinstance(reliability, dict) and harness in reliability:
        value = reliability[harness]
        if not isinstance(value, bool):
            return None, f"{harness} reliability must be boolean"
        candidates.append(value)
    if not candidates:
        return None, f"{harness} reliability is missing"
    if len(set(candidates)) != 1:
        return None, f"{harness} reliability fields conflict"
    return candidates[0], None


def _parse_attempt(
    attempt: Any,
    *,
    task_id: str,
    position: int,
) -> tuple[Attempt | None, list[str]]:
    if not isinstance(attempt, dict):
        return None, ["attempt must be an object"]
    reasons: list[str] = []
    for key in ("eligible", "infrastructure_valid", "pair_valid"):
        if key not in attempt:
            continue
        value = attempt[key]
        if not isinstance(value, bool):
            reasons.append(f"{key} must be boolean")
        elif not value:
            reasons.append(f"{key} is false")
    infrastructure_status = attempt.get("infrastructure_status")
    if infrastructure_status is not None:
        status = _clean_string(infrastructure_status)
        if status is None:
            reasons.append("infrastructure_status must be a non-empty string")
        elif status.casefold() != "ok":
            reasons.append(f"infrastructure_status is {status!r}, not 'ok'")

    repetition, repetition_error = _repetition_value(attempt, position=position)
    if repetition_error:
        reasons.append(repetition_error)

    explicit_ids = [
        value
        for key in ("attempt_id", "id")
        if (value := _clean_string(attempt.get(key))) is not None
    ]
    if len(set(explicit_ids)) > 1:
        reasons.append("attempt identifier fields conflict")
    if repetition is None:
        attempt_id = explicit_ids[0] if explicit_ids else ""
    else:
        attempt_id = explicit_ids[0] if explicit_ids else f"{task_id}:{repetition}"
    if not attempt_id:
        reasons.append("attempt_id is missing or blank")

    phoenix_score, error = _score_value(attempt, PHOENIX)
    if error:
        reasons.append(error)
    hve_score, error = _score_value(attempt, HVE)
    if error:
        reasons.append(error)
    phoenix_reliable, error = _reliability_value(attempt, PHOENIX)
    if error:
        reasons.append(error)
    hve_reliable, error = _reliability_value(attempt, HVE)
    if error:
        reasons.append(error)

    if phoenix_reliable is False and phoenix_score not in {None, 0.0}:
        reasons.append("unreliable phoenix attempt must have score 0")
    if hve_reliable is False and hve_score not in {None, 0.0}:
        reasons.append("unreliable hve attempt must have score 0")
    if reasons:
        return None, _unique_reasons(reasons)
    assert repetition is not None
    assert phoenix_score is not None
    assert hve_score is not None
    assert phoenix_reliable is not None
    assert hve_reliable is not None
    return (
        Attempt(
            attempt_id=attempt_id,
            repetition=repetition,
            phoenix_score=phoenix_score,
            hve_score=hve_score,
            phoenix_reliable=phoenix_reliable,
            hve_reliable=hve_reliable,
        ),
        [],
    )


def _document_exclusion_reasons(document: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    eligibility = document.get("eligibility")
    eligibility_object = eligibility if isinstance(eligibility, dict) else {}
    flags: list[bool] = []
    for value in (document.get("eligible"), eligibility_object.get("eligible")):
        if value is None:
            continue
        if not isinstance(value, bool):
            reasons.append("eligible must be boolean")
        else:
            flags.append(value)
    if len(set(flags)) > 1:
        reasons.append("eligibility flags conflict")
    if False in flags:
        reasons.append("task is marked ineligible")
    supplied_reasons: list[Any] = []
    if "exclusion_reasons" in document:
        value = document["exclusion_reasons"]
        if isinstance(value, list):
            supplied_reasons.extend(value)
        else:
            reasons.append("exclusion_reasons must be a list")
    if "reasons" in eligibility_object:
        value = eligibility_object["reasons"]
        if isinstance(value, list):
            supplied_reasons.extend(value)
        else:
            reasons.append("eligibility.reasons must be a list")
    for value in supplied_reasons:
        reason = _clean_string(value)
        if reason is None:
            reasons.append("exclusion reasons must be non-empty strings")
        else:
            reasons.append(f"declared exclusion: {reason}")
    return _unique_reasons(reasons)


def _strict_score(value: Any, *, field: str) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{field} must be numeric"
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return None, f"{field} must be finite and between 0 and 1"
    return score, None


def _formal_model_reasons(
    value: Any,
    *,
    field: str,
    binding: ExperimentBinding,
) -> list[str]:
    try:
        observed = _requested_model(value, field=field)
    except TaskAnalysisError as exc:
        return [str(exc)]
    if observed != binding.model:
        return [f"{field}.requested is {observed!r}; preregistered {binding.model!r}"]
    return []


def _formal_budget_reasons(
    value: Any,
    *,
    field: str,
    binding: ExperimentBinding,
) -> list[str]:
    try:
        observed = _budget_values(value, field=field)
    except TaskAnalysisError as exc:
        return [str(exc)]
    expected = (binding.max_ai_credits, binding.timeout_seconds)
    if observed != expected:
        return [
            f"{field} is max_ai_credits={observed[0]}, "
            f"timeout_seconds={observed[1]}; preregistered "
            f"max_ai_credits={expected[0]}, timeout_seconds={expected[1]}"
        ]
    return []


def _formal_harness_reasons(
    row: Any,
    *,
    harness: str,
    binding: ExperimentBinding,
) -> list[str]:
    if not isinstance(row, dict):
        return [f"{harness} result must be an object"]
    reasons: list[str] = []
    score, score_error = _strict_score(row.get("score"), field=f"{harness}.score")
    if score_error:
        reasons.append(score_error)
    reliable = row.get("reliable")
    if not isinstance(reliable, bool):
        reasons.append(f"{harness}.reliable must be boolean")
        reliable = None

    receipt = row.get("receipt")
    seal_error = _seal_reason(
        receipt,
        field="receipt_sha256",
        label=f"{harness} receipt",
    )
    if seal_error:
        reasons.append(seal_error)
    if not isinstance(receipt, dict):
        return _unique_reasons(reasons)

    execution = receipt.get("execution")
    execution_valid = execution.get("valid") if isinstance(execution, dict) else None
    if not isinstance(execution_valid, bool):
        reasons.append(f"{harness} receipt execution.valid must be boolean")

    attestation = receipt.get("model_attestation")
    if not isinstance(attestation, dict):
        reasons.append(f"{harness} receipt model_attestation must be an object")
        attestation_valid = None
    else:
        attestation_valid = attestation.get("status") == "pass"
        if attestation.get("status") not in {"pass", "fail"}:
            reasons.append(
                f"{harness} receipt model_attestation.status must be pass or fail"
            )
        if attestation.get("requested_model") != binding.model:
            reasons.append(
                f"{harness} receipt requested_model does not match preregistration"
            )

    artifact = receipt.get("artifact")
    artifact_valid = artifact.get("valid") if isinstance(artifact, dict) else None
    if not isinstance(artifact_valid, bool):
        reasons.append(f"{harness} receipt artifact.valid must be boolean")

    receipt_reliability = receipt.get("reliability")
    if not isinstance(receipt_reliability, dict):
        reasons.append(f"{harness} receipt reliability must be an object")
    else:
        for field_name, observed in (
            ("reliable", receipt_reliability.get("reliable")),
            ("execution_valid", receipt_reliability.get("execution_valid")),
            (
                "model_attestation_valid",
                receipt_reliability.get("model_attestation_valid"),
            ),
            ("artifact_valid", receipt_reliability.get("artifact_valid")),
        ):
            if not isinstance(observed, bool):
                reasons.append(
                    f"{harness} receipt reliability.{field_name} must be boolean"
                )
        if (
            isinstance(execution_valid, bool)
            and receipt_reliability.get("execution_valid") != execution_valid
        ):
            reasons.append(
                f"{harness} receipt execution validity fields are inconsistent"
            )
        if (
            isinstance(attestation_valid, bool)
            and receipt_reliability.get("model_attestation_valid") != attestation_valid
        ):
            reasons.append(
                f"{harness} receipt model-attestation validity fields are inconsistent"
            )
        if (
            isinstance(artifact_valid, bool)
            and receipt_reliability.get("artifact_valid") != artifact_valid
        ):
            reasons.append(
                f"{harness} receipt artifact validity fields are inconsistent"
            )
        component_values = (
            receipt_reliability.get("execution_valid"),
            receipt_reliability.get("model_attestation_valid"),
            receipt_reliability.get("artifact_valid"),
        )
        if all(isinstance(value, bool) for value in component_values):
            expected_reliable = all(component_values)
            if receipt_reliability.get("reliable") is not expected_reliable:
                reasons.append(
                    f"{harness} receipt reliable flag is inconsistent with components"
                )
        if (
            isinstance(reliable, bool)
            and isinstance(receipt_reliability.get("reliable"), bool)
            and reliable is not receipt_reliability["reliable"]
        ):
            reasons.append(f"{harness}.reliable conflicts with its sealed receipt")

    if reliable is False and score not in {None, 0.0}:
        reasons.append(f"unreliable {harness} result must have analysis score 0")

    artifact_score = row.get("artifact_score")
    receipt_raw_score = (
        artifact.get("raw_score") if isinstance(artifact, dict) else None
    )
    if artifact_score != receipt_raw_score:
        reasons.append(f"{harness}.artifact_score conflicts with its sealed receipt")
    if reliable is True:
        raw_score, raw_error = _strict_score(
            receipt_raw_score,
            field=f"{harness} receipt artifact.raw_score",
        )
        if raw_error:
            reasons.append(raw_error)
        elif score is not None and raw_score != score:
            reasons.append(
                f"{harness}.score conflicts with reliable artifact.raw_score"
            )
    return _unique_reasons(reasons)


def _formal_attempt_reasons(
    attempt: Any,
    *,
    position: int,
    frozen_task: FrozenTask,
    binding: ExperimentBinding,
) -> tuple[int | None, list[str]]:
    if not isinstance(attempt, dict):
        return None, ["attempt must be an object"]
    reasons: list[str] = []
    seal_error = _seal_reason(
        attempt,
        field="attempt_sha256",
        label="attempt",
    )
    if seal_error:
        reasons.append(seal_error)

    repetition = attempt.get("repetition")
    if (
        isinstance(repetition, bool)
        or not isinstance(repetition, int)
        or not 0 <= repetition < ATTEMPTS_PER_TASK
    ):
        reasons.append(
            f"repetition must be an integer from 0 to {ATTEMPTS_PER_TASK - 1}"
        )
        repetition_value = None
    else:
        repetition_value = repetition
        expected_attempt_id = sha256_json(
            {
                "experiment_digest": binding.experiment_digest,
                "task_digest": frozen_task.task_digest,
                "repetition": repetition,
            }
        )
        if attempt.get("attempt_id") != expected_attempt_id:
            reasons.append(
                "attempt_id does not match experiment, task digest, and repetition"
            )

    if attempt.get("infrastructure_valid") is not True:
        reasons.append("infrastructure_valid must be true")
    randomized_order = attempt.get("randomized_order")
    if (
        not isinstance(randomized_order, list)
        or len(randomized_order) != 2
        or set(randomized_order) != {PHOENIX, HVE}
    ):
        reasons.append("randomized_order must contain phoenix and hve exactly once")
    reasons.extend(
        _formal_model_reasons(
            attempt.get("model"),
            field="attempt model",
            binding=binding,
        )
    )
    reasons.extend(
        _formal_budget_reasons(
            attempt.get("budget"),
            field="attempt budget",
            binding=binding,
        )
    )
    for harness in (PHOENIX, HVE):
        reasons.extend(
            f"{harness}: {reason}"
            for reason in _formal_harness_reasons(
                attempt.get(harness),
                harness=harness,
                binding=binding,
            )
        )

    phoenix = attempt.get(PHOENIX)
    hve = attempt.get(HVE)
    if isinstance(phoenix, dict) and isinstance(hve, dict):
        phoenix_score, phoenix_error = _strict_score(
            phoenix.get("score"),
            field="phoenix.score",
        )
        hve_score, hve_error = _strict_score(hve.get("score"), field="hve.score")
        paired = attempt.get("paired_score_difference_phoenix_minus_hve")
        if (
            phoenix_error is None
            and hve_error is None
            and phoenix_score is not None
            and hve_score is not None
        ):
            if isinstance(paired, bool) or not isinstance(paired, (int, float)):
                reasons.append(
                    "paired_score_difference_phoenix_minus_hve must be numeric"
                )
            elif not math.isclose(
                float(paired),
                phoenix_score - hve_score,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                reasons.append(
                    "paired_score_difference_phoenix_minus_hve is inconsistent"
                )
    return repetition_value, _unique_reasons(reasons)


def _formal_document_reasons(
    source: str,
    document: Any,
    *,
    binding: ExperimentBinding,
) -> tuple[str | None, list[str]]:
    if not isinstance(document, dict):
        return None, ["document must be a JSON object"]
    reasons: list[str] = []
    seal_error = _seal_reason(
        document,
        field="document_sha256",
        label="task document",
    )
    if seal_error:
        reasons.append(seal_error)
    if document.get("schema") != INPUT_SCHEMA:
        reasons.append(f"schema must equal {INPUT_SCHEMA}")

    task_id = _clean_string(document.get("task_id"))
    if task_id is None:
        reasons.append("task_id is missing or blank")
        frozen_task = None
    else:
        frozen_task = binding.tasks_by_id.get(task_id)
        if frozen_task is None:
            reasons.append(f"unexpected task_id {task_id!r}")

    if frozen_task is not None:
        if document.get("category") != frozen_task.category:
            reasons.append(f"category does not match frozen task {frozen_task.task_id}")
        if document.get("task_digest") != frozen_task.task_digest:
            reasons.append(
                f"task_digest does not match frozen task {frozen_task.task_id}"
            )
    if document.get("eligible") is not True:
        reasons.append("eligible must be true")
    if document.get("rankable") is not False:
        reasons.append("rankable must be false")
    if document.get("official") is not False:
        reasons.append("official must be false")
    if document.get("experiment_digest") != binding.experiment_digest:
        reasons.append("experiment_digest does not match preregistration")
    if (
        binding.schema == PREREGISTRATION_SCHEMA
        and document.get("preregistration_sha256") != binding.binding_sha256
    ):
        reasons.append("preregistration_sha256 does not match preregistration")
    reasons.extend(
        _formal_model_reasons(
            document.get("model"),
            field="task document model",
            binding=binding,
        )
    )
    reasons.extend(
        _formal_budget_reasons(
            document.get("budget"),
            field="task document budget",
            binding=binding,
        )
    )

    attempts = document.get("attempts")
    repetitions: list[int] = []
    if not isinstance(attempts, list):
        reasons.append("attempts must be a list")
    elif len(attempts) != ATTEMPTS_PER_TASK:
        reasons.append(
            f"task must contain exactly {ATTEMPTS_PER_TASK} paired attempts; "
            f"observed {len(attempts)}"
        )
    elif frozen_task is not None:
        for position, attempt in enumerate(attempts):
            repetition, attempt_reasons = _formal_attempt_reasons(
                attempt,
                position=position,
                frozen_task=frozen_task,
                binding=binding,
            )
            if repetition is not None:
                repetitions.append(repetition)
            reasons.extend(
                f"attempt {position}: {reason}" for reason in attempt_reasons
            )
    if repetitions and sorted(repetitions) != list(range(ATTEMPTS_PER_TASK)):
        reasons.append(f"repetitions must be exactly {list(range(ATTEMPTS_PER_TASK))}")
    return task_id, [f"{source}: {reason}" for reason in _unique_reasons(reasons)]


def _prepare_formal_documents(
    documents: Iterable[tuple[str, Any]],
    *,
    binding: ExperimentBinding,
) -> list[tuple[str, Any]]:
    trial_documents: list[tuple[str, Any]] = []
    task_ids: list[str] = []
    errors: list[str] = []
    observed_experiments: set[str] = set()
    observed_models: set[str] = set()
    observed_budgets: set[tuple[int, int]] = set()
    ignored_schemas = {
        OUTPUT_SCHEMA,
        PREREGISTRATION_SCHEMA,
        EXPERIMENT_SCHEMA,
        RUN_SUMMARY_SCHEMA,
    }
    for source, document in documents:
        schema = document.get("schema") if isinstance(document, dict) else None
        if schema in ignored_schemas:
            continue
        trial_documents.append((source, document))
        task_id, document_errors = _formal_document_reasons(
            source,
            document,
            binding=binding,
        )
        if task_id is not None:
            task_ids.append(task_id)
        errors.extend(document_errors)
        if isinstance(document, dict):
            experiment = document.get("experiment_digest")
            if isinstance(experiment, str):
                observed_experiments.add(experiment)
            try:
                observed_models.add(
                    _requested_model(
                        document.get("model"),
                        field="task document model",
                    )
                )
            except TaskAnalysisError:
                pass
            try:
                observed_budgets.add(
                    _budget_values(
                        document.get("budget"),
                        field="task document budget",
                    )
                )
            except TaskAnalysisError:
                pass

    expected_ids = {task.task_id for task in binding.tasks}
    observed_ids = set(task_ids)
    duplicates = sorted(
        task_id for task_id, count in Counter(task_ids).items() if count > 1
    )
    missing = sorted(expected_ids - observed_ids)
    extra = sorted(observed_ids - expected_ids)
    if len(trial_documents) != FORMAL_TASK_COUNT:
        errors.append(
            "formal suite must contain exactly "
            f"{FORMAL_TASK_COUNT} task documents; observed {len(trial_documents)}"
        )
    if duplicates:
        errors.append("duplicate task documents: " + ", ".join(duplicates))
    if missing:
        errors.append("missing preregistered tasks: " + ", ".join(missing))
    if extra:
        errors.append("extra or substituted tasks: " + ", ".join(extra))
    if observed_experiments != {binding.experiment_digest}:
        errors.append(
            "task documents do not share the one preregistered experiment digest"
        )
    if observed_models != {binding.model}:
        errors.append("task documents do not share the one preregistered model")
    expected_budget = (binding.max_ai_credits, binding.timeout_seconds)
    if observed_budgets != {expected_budget}:
        errors.append("task documents do not share the one preregistered budget")
    if errors:
        raise TaskAnalysisError(
            "formal analysis rejected:\n- " + "\n- ".join(_unique_reasons(errors))
        )
    return trial_documents


def _parse_task_document(
    source: str,
    document: Any,
) -> tuple[TaskTrial | None, dict[str, Any] | None]:
    if not isinstance(document, dict):
        return None, {
            "source": source,
            "task_id": None,
            "category": None,
            "reasons": ["document must be a JSON object"],
        }
    if document.get("schema") in {
        OUTPUT_SCHEMA,
        PREREGISTRATION_SCHEMA,
        EXPERIMENT_SCHEMA,
        RUN_SUMMARY_SCHEMA,
    }:
        return None, None

    reasons: list[str] = []
    if document.get("schema") != INPUT_SCHEMA:
        reasons.append(f"schema must equal {INPUT_SCHEMA}")
    nested_task = document.get("task")
    if nested_task is None:
        nested_task = {}
    elif not isinstance(nested_task, dict):
        reasons.append("task must be an object")
        nested_task = {}
    task_id = _identity_field(
        document,
        nested_task,
        top_level="task_id",
        nested="id",
        label="task_id",
        reasons=reasons,
    )
    category = _identity_field(
        document,
        nested_task,
        top_level="category",
        nested="category",
        label="category",
        reasons=reasons,
    )
    reasons.extend(_document_exclusion_reasons(document))

    attempts_value = document.get("attempts")
    parsed_attempts: list[Attempt] = []
    if not isinstance(attempts_value, list):
        reasons.append("attempts must be a list")
    else:
        if len(attempts_value) != ATTEMPTS_PER_TASK:
            reasons.append(
                f"task must contain exactly {ATTEMPTS_PER_TASK} paired attempts; "
                f"observed {len(attempts_value)}"
            )
        if task_id is not None:
            for position, attempt_value in enumerate(attempts_value):
                attempt, attempt_reasons = _parse_attempt(
                    attempt_value,
                    task_id=task_id,
                    position=position,
                )
                if attempt_reasons:
                    reasons.extend(
                        f"attempt {position}: {reason}" for reason in attempt_reasons
                    )
                elif attempt is not None:
                    parsed_attempts.append(attempt)

    if parsed_attempts:
        attempt_ids = [attempt.attempt_id for attempt in parsed_attempts]
        repetitions = [attempt.repetition for attempt in parsed_attempts]
        if len(set(attempt_ids)) != len(attempt_ids):
            reasons.append("attempt_id values must be unique within a task")
        if len(set(repetitions)) != len(repetitions):
            reasons.append("repetition values must be unique within a task")
    if reasons or task_id is None or category is None:
        return None, {
            "source": source,
            "task_id": task_id,
            "category": category,
            "reasons": _unique_reasons(reasons),
        }
    return (
        TaskTrial(
            source=source,
            task_id=task_id,
            category=category,
            attempts=tuple(parsed_attempts),
            task_digest=(
                document.get("task_digest")
                if _is_sha256(document.get("task_digest"))
                else None
            ),
        ),
        None,
    )


def _resolve_duplicates(
    candidates: Sequence[TaskTrial],
    excluded: list[dict[str, Any]],
) -> list[TaskTrial]:
    task_sources: dict[str, list[str]] = defaultdict(list)
    attempt_sources: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for task in candidates:
        task_sources[task.task_id].append(task.source)
        for attempt in task.attempts:
            attempt_sources[attempt.attempt_id].append((task.task_id, task.source))
    duplicate_tasks = {
        task_id: sources
        for task_id, sources in task_sources.items()
        if len(sources) > 1
    }
    duplicate_attempts = {
        attempt_id: locations
        for attempt_id, locations in attempt_sources.items()
        if len(locations) > 1
    }
    accepted: list[TaskTrial] = []
    for task in candidates:
        reasons: list[str] = []
        if task.task_id in duplicate_tasks:
            sources = ", ".join(sorted(duplicate_tasks[task.task_id]))
            reasons.append(f"duplicate task_id appears in: {sources}")
        task_duplicate_attempts = sorted(
            attempt.attempt_id
            for attempt in task.attempts
            if attempt.attempt_id in duplicate_attempts
        )
        if task_duplicate_attempts:
            reasons.append(
                "attempt_id is reused across task documents: "
                + ", ".join(task_duplicate_attempts)
            )
        if reasons:
            excluded.append(
                {
                    "source": task.source,
                    "task_id": task.task_id,
                    "category": task.category,
                    "reasons": reasons,
                }
            )
        else:
            accepted.append(task)
    return sorted(accepted, key=lambda task: (task.task_id, task.source))


def _task_effect(task: TaskTrial) -> dict[str, Any]:
    phoenix_mean = sum(attempt.phoenix_score for attempt in task.attempts) / len(
        task.attempts
    )
    hve_mean = sum(attempt.hve_score for attempt in task.attempts) / len(task.attempts)
    phoenix_reliability = sum(
        attempt.phoenix_reliable for attempt in task.attempts
    ) / len(task.attempts)
    hve_reliability = sum(attempt.hve_reliable for attempt in task.attempts) / len(
        task.attempts
    )
    both_reliable = [
        attempt
        for attempt in task.attempts
        if attempt.phoenix_reliable and attempt.hve_reliable
    ]
    if both_reliable:
        conditional_phoenix = sum(
            attempt.phoenix_score for attempt in both_reliable
        ) / len(both_reliable)
        conditional_hve = sum(attempt.hve_score for attempt in both_reliable) / len(
            both_reliable
        )
        conditional_difference = conditional_phoenix - conditional_hve
    else:
        conditional_phoenix = None
        conditional_hve = None
        conditional_difference = None
    return {
        "task_id": task.task_id,
        "category": task.category,
        "source": task.source,
        "task_digest": task.task_digest,
        "attempt_count": len(task.attempts),
        "phoenix_mean_score": phoenix_mean,
        "hve_mean_score": hve_mean,
        "score_difference": phoenix_mean - hve_mean,
        "phoenix_reliability_rate": phoenix_reliability,
        "hve_reliability_rate": hve_reliability,
        "reliability_difference": phoenix_reliability - hve_reliability,
        "both_reliable_attempt_count": len(both_reliable),
        "conditional_both_reliable_phoenix_mean": conditional_phoenix,
        "conditional_both_reliable_hve_mean": conditional_hve,
        "conditional_both_reliable_difference": conditional_difference,
    }


def _serialize_task_effect(effect: dict[str, Any]) -> dict[str, Any]:
    conditional_fields = {
        field: (_rounded(effect[field]) if effect[field] is not None else None)
        for field in (
            "conditional_both_reliable_phoenix_mean",
            "conditional_both_reliable_hve_mean",
            "conditional_both_reliable_difference",
        )
    }
    return {
        **effect,
        "phoenix_mean_score": _rounded(effect["phoenix_mean_score"]),
        "hve_mean_score": _rounded(effect["hve_mean_score"]),
        "score_difference": _rounded(effect["score_difference"]),
        "phoenix_reliability_rate": _rounded(effect["phoenix_reliability_rate"]),
        "hve_reliability_rate": _rounded(effect["hve_reliability_rate"]),
        "reliability_difference": _rounded(effect["reliability_difference"]),
        **conditional_fields,
    }


def _reliability_analysis(
    tasks: Sequence[TaskTrial],
    *,
    alpha: float,
) -> dict[str, Any]:
    task_signs: Counter[str] = Counter()
    nested_pairs: Counter[str] = Counter()
    phoenix_task_rates: list[float] = []
    hve_task_rates: list[float] = []
    task_rows: list[dict[str, Any]] = []
    for task in tasks:
        phoenix_count = sum(attempt.phoenix_reliable for attempt in task.attempts)
        hve_count = sum(attempt.hve_reliable for attempt in task.attempts)
        phoenix_rate = phoenix_count / len(task.attempts)
        hve_rate = hve_count / len(task.attempts)
        phoenix_task_rates.append(phoenix_rate)
        hve_task_rates.append(hve_rate)
        if phoenix_count > hve_count:
            task_signs["phoenix"] += 1
        elif hve_count > phoenix_count:
            task_signs["hve"] += 1
        else:
            task_signs["tie"] += 1
        task_rows.append(
            {
                "task_id": task.task_id,
                "phoenix_reliable_attempts": phoenix_count,
                "hve_reliable_attempts": hve_count,
                "attempts": len(task.attempts),
            }
        )
        for attempt in task.attempts:
            pair = (attempt.phoenix_reliable, attempt.hve_reliable)
            if pair == (True, True):
                nested_pairs["both_reliable"] += 1
            elif pair == (True, False):
                nested_pairs["phoenix_only_reliable"] += 1
            elif pair == (False, True):
                nested_pairs["hve_only_reliable"] += 1
            else:
                nested_pairs["neither_reliable"] += 1

    task_p, task_exact = _exact_two_sided_sign_test(
        task_signs["phoenix"],
        task_signs["hve"],
    )
    task_exact.update(
        {
            "unit": "task",
            "phoenix_better_tasks": task_signs["phoenix"],
            "hve_better_tasks": task_signs["hve"],
            "tied_tasks": task_signs["tie"],
        }
    )
    if task_exact["decisive_units"] and task_p < alpha:
        reliability_winner = (
            PHOENIX if task_signs["phoenix"] > task_signs["hve"] else HVE
        )
    else:
        reliability_winner = None

    return {
        "primary_unit": "task",
        "attempts_are_nested": True,
        "alpha": alpha,
        "phoenix_macro_reliability_rate": (
            _rounded(sum(phoenix_task_rates) / len(phoenix_task_rates))
            if phoenix_task_rates
            else None
        ),
        "hve_macro_reliability_rate": (
            _rounded(sum(hve_task_rates) / len(hve_task_rates))
            if hve_task_rates
            else None
        ),
        "exact_task_level_sign_test": task_exact,
        "task_level_reliability_winner": reliability_winner,
        "task_rows": task_rows,
        "nested_attempt_pairs_descriptive_only": {
            "unit": "attempt",
            "nested_descriptive_only": True,
            "p_value_omitted": (
                "attempts are nested within tasks and are not independent "
                "inferential units"
            ),
            "paired_outcomes": {
                key: nested_pairs[key]
                for key in (
                    "both_reliable",
                    "phoenix_only_reliable",
                    "hve_only_reliable",
                    "neither_reliable",
                )
            },
        },
        "_task_p_value": task_p,
    }


def _conditional_both_reliable_quality(
    effects: Sequence[dict[str, Any]],
    *,
    bootstrap_samples: int,
    confidence: float,
    seed: int,
) -> dict[str, Any]:
    rows = [
        effect
        for effect in effects
        if effect["conditional_both_reliable_difference"] is not None
    ]
    phoenix_values = [
        float(effect["conditional_both_reliable_phoenix_mean"]) for effect in rows
    ]
    hve_values = [
        float(effect["conditional_both_reliable_hve_mean"]) for effect in rows
    ]
    differences = [
        float(effect["conditional_both_reliable_difference"]) for effect in rows
    ]
    bootstrap = _bootstrap_tasks(
        differences,
        samples=bootstrap_samples,
        confidence=confidence,
        seed=seed + 20_000,
    )
    task_count = len(rows)
    return {
        "role": "descriptive_only",
        "estimand": "quality_conditional_on_both_harnesses_being_reliable",
        "cluster_unit": "task",
        "weighting": "equal_weight_per_task_with_at_least_one_both-reliable_pair",
        "selection_warning": (
            "This conditions on a post-execution event and is not used to name "
            "a winner or declare equivalence."
        ),
        "task_count": task_count,
        "omitted_task_count": len(effects) - task_count,
        "both_reliable_attempt_count": sum(
            int(effect["both_reliable_attempt_count"]) for effect in rows
        ),
        "macro_average": {
            "phoenix_mean_score": (
                _rounded(sum(phoenix_values) / task_count) if task_count else None
            ),
            "hve_mean_score": (
                _rounded(sum(hve_values) / task_count) if task_count else None
            ),
            "phoenix_minus_hve": (
                _rounded(sum(differences) / task_count) if task_count else None
            ),
        },
        "paired_task_bootstrap": (
            bootstrap.to_dict() if bootstrap is not None else None
        ),
        "task_effects": [
            {
                "task_id": effect["task_id"],
                "category": effect["category"],
                "both_reliable_attempt_count": effect["both_reliable_attempt_count"],
                "phoenix_mean_score": _rounded(
                    effect["conditional_both_reliable_phoenix_mean"]
                ),
                "hve_mean_score": _rounded(
                    effect["conditional_both_reliable_hve_mean"]
                ),
                "phoenix_minus_hve": _rounded(
                    effect["conditional_both_reliable_difference"]
                ),
            }
            for effect in rows
        ],
    }


def _informative_coverage(
    tasks: Sequence[TaskTrial],
    *,
    candidate_decision: str,
    superiority_min_suite_rate: float,
    superiority_min_per_task: int,
    equivalence_min_suite_rate: float,
    equivalence_min_per_task: int,
) -> dict[str, Any]:
    task_rows: list[dict[str, Any]] = []
    at_least_one_total = 0
    both_total = 0
    for task in tasks:
        at_least_one = sum(
            attempt.phoenix_reliable or attempt.hve_reliable
            for attempt in task.attempts
        )
        both = sum(
            attempt.phoenix_reliable and attempt.hve_reliable
            for attempt in task.attempts
        )
        at_least_one_total += at_least_one
        both_total += both
        task_rows.append(
            {
                "task_id": task.task_id,
                "at_least_one_reliable_pairs": at_least_one,
                "both_reliable_pairs": both,
                "paired_attempts": len(task.attempts),
            }
        )

    total_pairs = sum(len(task.attempts) for task in tasks)
    at_least_one_rate = at_least_one_total / total_pairs if total_pairs else 0.0
    both_rate = both_total / total_pairs if total_pairs else 0.0
    if candidate_decision in {"phoenix_superior", "hve_superior"}:
        requirement = "at_least_one_harness_reliable"
        minimum_suite_rate = superiority_min_suite_rate
        minimum_per_task = superiority_min_per_task
        observed_suite_rate = at_least_one_rate
        below = [
            row["task_id"]
            for row in task_rows
            if row["at_least_one_reliable_pairs"] < minimum_per_task
        ]
        passed = observed_suite_rate >= minimum_suite_rate and not below
    elif candidate_decision == "practically_equivalent":
        requirement = "both_harnesses_reliable"
        minimum_suite_rate = equivalence_min_suite_rate
        minimum_per_task = equivalence_min_per_task
        observed_suite_rate = both_rate
        below = [
            row["task_id"]
            for row in task_rows
            if row["both_reliable_pairs"] < minimum_per_task
        ]
        passed = observed_suite_rate >= minimum_suite_rate and not below
    else:
        requirement = "not_applicable_until_score_interval_is_decisive"
        minimum_suite_rate = None
        minimum_per_task = None
        observed_suite_rate = None
        below = []
        passed = True
    return {
        "passed": passed,
        "candidate_decision": candidate_decision,
        "required_coverage": requirement,
        "minimum_suite_rate": minimum_suite_rate,
        "minimum_pairs_per_task": minimum_per_task,
        "observed_suite_rate": (
            _rounded(observed_suite_rate) if observed_suite_rate is not None else None
        ),
        "tasks_below_minimum": below,
        "suite": {
            "paired_attempts": total_pairs,
            "at_least_one_reliable_pairs": at_least_one_total,
            "at_least_one_reliable_rate": _rounded(at_least_one_rate),
            "both_reliable_pairs": both_total,
            "both_reliable_rate": _rounded(both_rate),
        },
        "task_rows": task_rows,
    }


def _category_sensitivity(
    effects: Sequence[dict[str, Any]],
    *,
    candidate_decision: str,
    margin: float,
    bootstrap_samples: int,
    confidence: float,
    seed: int,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for effect in effects:
        grouped[str(effect["category"])].append(effect)
    categories: list[dict[str, Any]] = []
    for category in sorted(grouped):
        rows = grouped[category]
        differences = [float(row["score_difference"]) for row in rows]
        phoenix_scores = [float(row["phoenix_mean_score"]) for row in rows]
        hve_scores = [float(row["hve_mean_score"]) for row in rows]
        categories.append(
            {
                "category": category,
                "task_count": len(rows),
                "task_ids": sorted(str(row["task_id"]) for row in rows),
                "phoenix_macro_mean": _rounded(
                    sum(phoenix_scores) / len(phoenix_scores)
                ),
                "hve_macro_mean": _rounded(sum(hve_scores) / len(hve_scores)),
                "score_difference": _rounded(sum(differences) / len(differences)),
            }
        )

    leave_one_out: list[dict[str, Any]] = []
    for index, category in enumerate(sorted(grouped)):
        remaining = [
            float(effect["score_difference"])
            for effect in effects
            if effect["category"] != category
        ]
        bootstrap = _bootstrap_tasks(
            remaining,
            samples=bootstrap_samples,
            confidence=confidence,
            seed=seed + 10_000 + index,
        )
        leave_one_out.append(
            {
                "excluded_category": category,
                "remaining_task_count": len(remaining),
                "paired_task_bootstrap": (
                    bootstrap.to_dict() if bootstrap is not None else None
                ),
                "score_decision": _score_decision(
                    bootstrap,
                    margin=margin,
                ),
            }
        )

    opposing_categories: list[str] = []
    unstable_omissions: list[str] = []
    if candidate_decision == "phoenix_superior":
        opposing_categories = [
            row["category"]
            for row in categories
            if float(row["score_difference"]) < -margin
        ]
        unstable_omissions = [
            row["excluded_category"]
            for row in leave_one_out
            if row["score_decision"] != candidate_decision
        ]
        passed = not opposing_categories and not unstable_omissions
        status = "pass" if passed else "fail"
    elif candidate_decision == "hve_superior":
        opposing_categories = [
            row["category"]
            for row in categories
            if float(row["score_difference"]) > margin
        ]
        unstable_omissions = [
            row["excluded_category"]
            for row in leave_one_out
            if row["score_decision"] != candidate_decision
        ]
        passed = not opposing_categories and not unstable_omissions
        status = "pass" if passed else "fail"
    elif candidate_decision == "practically_equivalent":
        opposing_categories = [
            row["category"]
            for row in categories
            if abs(float(row["score_difference"])) > margin
        ]
        unstable_omissions = [
            row["excluded_category"]
            for row in leave_one_out
            if row["score_decision"] != candidate_decision
        ]
        passed = not opposing_categories and not unstable_omissions
        status = "pass" if passed else "fail"
    else:
        passed = True
        status = "not_applicable_until_score_interval_is_decisive"

    return {
        "status": status,
        "passed": passed,
        "rule": (
            "No category may cross the opposite practical margin, and deleting "
            "any one category must preserve the same task-bootstrap decision."
        ),
        "categories": categories,
        "opposing_or_out_of_margin_categories": opposing_categories,
        "decision_changing_category_omissions": unstable_omissions,
        "leave_one_category_out": leave_one_out,
    }


def _gate(
    code: str,
    passed: bool,
    evidence: str,
) -> dict[str, Any]:
    return {"code": code, "passed": passed, "evidence": evidence}


def _reliability_consistency_gate(
    candidate_decision: str,
    reliability_winner: str | None,
) -> tuple[bool, str]:
    if candidate_decision == "phoenix_superior" and reliability_winner == HVE:
        return False, "exact task-level reliability favors hve-core"
    if candidate_decision == "hve_superior" and reliability_winner == PHOENIX:
        return False, "exact task-level reliability favors Phoenix"
    if (
        candidate_decision == "practically_equivalent"
        and reliability_winner is not None
    ):
        return (
            False,
            "score equivalence conflicts with an exact task-level reliability "
            f"winner ({reliability_winner})",
        )
    return True, "no statistically significant reliability result contradicts score"


def summarize_task_documents(
    documents: Iterable[tuple[str, Any]],
    *,
    preregistration: Mapping[str, Any] | str | Path | None = None,
    minimum_tasks: int = MINIMUM_ELIGIBLE_TASKS,
    minimum_categories: int = MINIMUM_CATEGORIES,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    reliability_alpha: float = DEFAULT_RELIABILITY_ALPHA,
) -> dict[str, Any]:
    """Run formal task-clustered analysis against an explicit frozen binding."""

    if preregistration is None:
        raise TaskAnalysisError(
            "formal analysis requires an explicit preregistration object or path"
        )
    if minimum_tasks != FORMAL_TASK_COUNT:
        raise TaskAnalysisError(
            f"formal minimum_tasks must equal exactly {FORMAL_TASK_COUNT}"
        )
    if minimum_categories != FORMAL_CATEGORY_COUNT:
        raise TaskAnalysisError(
            f"formal minimum_categories must equal exactly {FORMAL_CATEGORY_COUNT}"
        )
    if (
        not isinstance(bootstrap_samples, int)
        or isinstance(bootstrap_samples, bool)
        or bootstrap_samples < 100
    ):
        raise TaskAnalysisError("bootstrap_samples must be an integer of at least 100")
    if not 0.5 < confidence < 1.0:
        raise TaskAnalysisError("confidence must be in (0.5, 1)")
    if not 0.0 < reliability_alpha < 1.0:
        raise TaskAnalysisError("reliability_alpha must be in (0, 1)")

    binding = _load_preregistration(preregistration)
    if bootstrap_samples != binding.bootstrap_samples:
        raise TaskAnalysisError(
            "bootstrap_samples does not match the sealed preregistration"
        )
    if not math.isclose(
        confidence,
        binding.confidence,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise TaskAnalysisError("confidence does not match the sealed preregistration")
    if seed != binding.bootstrap_seed:
        raise TaskAnalysisError(
            "bootstrap seed does not match the sealed preregistration"
        )
    if not math.isclose(
        reliability_alpha,
        binding.reliability_alpha,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise TaskAnalysisError(
            "reliability_alpha does not match the sealed preregistration"
        )
    margin = binding.margin
    supplied_documents = list(documents)
    if not supplied_documents:
        raise TaskAnalysisError("no JSON documents were provided")
    formal_documents = _prepare_formal_documents(
        supplied_documents,
        binding=binding,
    )

    candidates: list[TaskTrial] = []
    excluded: list[dict[str, Any]] = []
    for source, document in formal_documents:
        candidate, exclusion = _parse_task_document(source, document)
        if candidate is not None:
            candidates.append(candidate)
        elif exclusion is not None:
            excluded.append(exclusion)
    accepted = _resolve_duplicates(candidates, excluded)
    if excluded or len(accepted) != FORMAL_TASK_COUNT:
        details = [
            f"{row.get('source')}: {'; '.join(row.get('reasons', []))}"
            for row in excluded
        ]
        raise TaskAnalysisError(
            "formal analysis rejected after strict parsing"
            + (":\n- " + "\n- ".join(details) if details else "")
        )

    effects = [_task_effect(task) for task in accepted]
    differences = [float(effect["score_difference"]) for effect in effects]
    bootstrap = _bootstrap_tasks(
        differences,
        samples=bootstrap_samples,
        confidence=confidence,
        seed=seed,
    )
    candidate_decision = _score_decision(bootstrap, margin=margin)
    category_sensitivity = _category_sensitivity(
        effects,
        candidate_decision=candidate_decision,
        margin=margin,
        bootstrap_samples=bootstrap_samples,
        confidence=confidence,
        seed=seed,
    )
    reliability = _reliability_analysis(
        accepted,
        alpha=reliability_alpha,
    )
    reliability_winner = reliability["task_level_reliability_winner"]
    reliability_gate_passed, reliability_gate_evidence = _reliability_consistency_gate(
        candidate_decision,
        reliability_winner,
    )
    informative_coverage = _informative_coverage(
        accepted,
        candidate_decision=candidate_decision,
        superiority_min_suite_rate=binding.superiority_min_suite_rate,
        superiority_min_per_task=binding.superiority_min_per_task,
        equivalence_min_suite_rate=binding.equivalence_min_suite_rate,
        equivalence_min_per_task=binding.equivalence_min_per_task,
    )
    conditional_quality = _conditional_both_reliable_quality(
        effects,
        bootstrap_samples=bootstrap_samples,
        confidence=confidence,
        seed=seed,
    )

    category_counts = Counter(task.category for task in accepted)
    eligible_task_count = len(accepted)
    category_count = len(category_counts)
    expected_identity = {
        (task.task_id, task.category, task.task_digest) for task in binding.tasks
    }
    accepted_identity = {
        (task.task_id, task.category, task.task_digest) for task in accepted
    }
    exact_portfolio = accepted_identity == expected_identity
    coverage_minimum = informative_coverage["minimum_suite_rate"]
    coverage_observed = informative_coverage["observed_suite_rate"]
    coverage_per_task = informative_coverage["minimum_pairs_per_task"]
    coverage_below = informative_coverage["tasks_below_minimum"]
    gates = [
        _gate(
            "exact_preregistered_task_portfolio",
            exact_portfolio,
            (
                f"observed exactly {eligible_task_count} frozen tasks across "
                f"{category_count} categories; required exactly "
                f"{FORMAL_TASK_COUNT} tasks and {FORMAL_CATEGORY_COUNT} categories"
            ),
        ),
        _gate(
            "sealed_common_experiment_binding",
            True,
            (
                "all task documents, attempts, and receipts passed seals and "
                f"match experiment {binding.experiment_digest}, model "
                f"{binding.model}, and one budget"
            ),
        ),
        _gate(
            "five_infrastructure_valid_attempts_per_task",
            bool(accepted)
            and all(len(task.attempts) == ATTEMPTS_PER_TASK for task in accepted),
            (
                f"all {eligible_task_count} accepted tasks contain exactly "
                f"{ATTEMPTS_PER_TASK} sealed infrastructure-valid paired attempts"
            ),
        ),
        _gate(
            "paired_task_bootstrap_decision",
            candidate_decision != "inconclusive",
            (
                f"pre-gate score decision is {candidate_decision}; interval must "
                "clear a superiority boundary or fit inside equivalence"
            ),
        ),
        _gate(
            "informative_coverage",
            bool(informative_coverage["passed"]),
            (
                f"requirement={informative_coverage['required_coverage']}; "
                f"observed suite rate={coverage_observed}; minimum suite "
                f"rate={coverage_minimum}; minimum pairs per task="
                f"{coverage_per_task}; tasks below minimum={coverage_below}"
            ),
        ),
        _gate(
            "category_sensitivity",
            bool(category_sensitivity["passed"]),
            (
                f"status={category_sensitivity['status']}; "
                "leave-one-category-out decision and category direction checked"
            ),
        ),
        _gate(
            "score_reliability_consistency",
            reliability_gate_passed,
            reliability_gate_evidence,
        ),
    ]
    all_gates_passed = all(bool(gate["passed"]) for gate in gates)
    final_decision = candidate_decision if all_gates_passed else "inconclusive"
    winner = {
        "phoenix_superior": PHOENIX,
        "hve_superior": HVE,
    }.get(final_decision)
    failed_gate_codes = [str(gate["code"]) for gate in gates if not gate["passed"]]
    if failed_gate_codes:
        decision_reason = (
            "No winner: one or more preregistered gates failed: "
            + ", ".join(failed_gate_codes)
        )
    elif final_decision == "phoenix_superior":
        decision_reason = (
            "Phoenix clears the +margin task-bootstrap boundary and all gates."
        )
    elif final_decision == "hve_superior":
        decision_reason = (
            "hve-core clears the -margin task-bootstrap boundary and all gates."
        )
    else:
        decision_reason = (
            "The entire task-bootstrap interval is inside the configured "
            "equivalence region and all gates pass."
        )

    phoenix_macro = sum(
        float(effect["phoenix_mean_score"]) for effect in effects
    ) / len(effects)
    hve_macro = sum(float(effect["hve_mean_score"]) for effect in effects) / len(
        effects
    )
    accepted_ledger = [
        {
            "source": task.source,
            "task_id": task.task_id,
            "category": task.category,
            "task_digest": task.task_digest,
            "experiment_digest": binding.experiment_digest,
            "attempt_count": len(task.attempts),
            "status": "accepted",
        }
        for task in accepted
    ]

    reliability.pop("_task_p_value", None)
    return {
        "schema": OUTPUT_SCHEMA,
        "input_schema": INPUT_SCHEMA,
        "formal_analysis": True,
        "rankable": False,
        "official": False,
        "global_harness_winner": None,
        "scope": "exact_preregistered_phoenix_hve_task_suite_only",
        "cluster_unit": "task",
        "attempts_are_nested": True,
        "experiment_binding": _serialize_binding(binding),
        "policy": {
            "attempts_per_task": ATTEMPTS_PER_TASK,
            "formal_task_count": FORMAL_TASK_COUNT,
            "formal_category_count": FORMAL_CATEGORY_COUNT,
            "formal_tasks_per_category": FORMAL_TASKS_PER_CATEGORY,
            "superiority_equivalence_margin": margin,
            "confidence": confidence,
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_seed": seed,
            "reliability_alpha": reliability_alpha,
            "informative_coverage": {
                "superiority": {
                    "pair_definition": "at_least_one_harness_reliable",
                    "minimum_suite_rate": binding.superiority_min_suite_rate,
                    "minimum_pairs_per_task": binding.superiority_min_per_task,
                },
                "equivalence": {
                    "pair_definition": "both_harnesses_reliable",
                    "minimum_suite_rate": binding.equivalence_min_suite_rate,
                    "minimum_pairs_per_task": binding.equivalence_min_per_task,
                },
            },
        },
        "ledger": {
            "accepted_count": len(accepted_ledger),
            "excluded_count": 0,
            "accepted": accepted_ledger,
            "excluded": [],
        },
        "eligible_task_count": eligible_task_count,
        "category_count": category_count,
        "category_task_counts": dict(sorted(category_counts.items())),
        "task_effects": [_serialize_task_effect(effect) for effect in effects],
        "macro_average": {
            "estimand": "end_to_end_completion_adjusted_quality",
            "primary": True,
            "unreliable_attempt_score": 0.0,
            "weighting": "equal_weight_per_task",
            "phoenix_mean_score": _rounded(phoenix_macro),
            "hve_mean_score": _rounded(hve_macro),
            "phoenix_minus_hve": _rounded(phoenix_macro - hve_macro),
        },
        "paired_task_bootstrap": (
            bootstrap.to_dict() if bootstrap is not None else None
        ),
        "conditional_both_reliable_quality": conditional_quality,
        "score_decision_before_gates": candidate_decision,
        "informative_coverage": informative_coverage,
        "reliability": reliability,
        "category_sensitivity": category_sensitivity,
        "nested_attempt_totals_descriptive_only": {
            "paired_attempts": eligible_task_count * ATTEMPTS_PER_TASK,
            "harness_observations": eligible_task_count * ATTEMPTS_PER_TASK * 2,
        },
        "gates": {
            "all_passed": all_gates_passed,
            "items": gates,
            "failed_codes": failed_gate_codes,
        },
        "decision": final_decision,
        "winner": winner,
        "decision_reason": decision_reason,
        "limitations": [
            "The output is explicitly non-rankable and unofficial.",
            "Five attempts are nested within each task and never bootstrapped as independent observations.",
            "The primary macro estimand gives every frozen task equal weight and assigns zero to unreliable attempts.",
            "The both-reliable quality estimand is descriptive only because it conditions on a post-execution event.",
            "Reliability inference uses task-level paired signs; attempt-level paired counts are descriptive only.",
            "Canonical JSON seals detect mutation but are local self-attestations, not trusted signatures.",
            "The analyzer does not rehash every raw evidence file referenced by a receipt.",
            "The analyzer does not establish process, filesystem, credential, or network isolation.",
            "Twenty public synthetic tasks can support only an exact-suite result, not broad external validity.",
            "A task-suite result is not a global harness sophistication or production-readiness ranking.",
        ],
    }


def _read_task_documents(root: Path) -> list[tuple[str, Any]]:
    if root.is_file():
        paths = [root]
        base = root.parent
    elif root.is_dir():
        paths = sorted(path for path in root.rglob("*.json") if path.is_file())
        base = root
    else:
        raise TaskAnalysisError(f"input path does not exist: {root}")
    if not paths:
        raise TaskAnalysisError(f"no JSON documents found under {root}")

    documents: list[tuple[str, Any]] = []
    for path in paths:
        source = path.relative_to(base).as_posix()
        try:
            document = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            document = {
                "schema": None,
                "task_id": None,
                "category": None,
                "attempts": None,
                "exclusion_reasons": [
                    f"JSON document is unreadable: {type(exc).__name__}: {exc}"
                ],
            }
        documents.append((source, document))
    return documents


def summarize_root(
    root: str | Path,
    *,
    preregistration: Mapping[str, Any] | str | Path,
    minimum_tasks: int = MINIMUM_ELIGIBLE_TASKS,
    minimum_categories: int = MINIMUM_CATEGORIES,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    reliability_alpha: float = DEFAULT_RELIABILITY_ALPHA,
) -> dict[str, Any]:
    path = Path(root).resolve()
    return summarize_task_documents(
        _read_task_documents(path),
        preregistration=preregistration,
        minimum_tasks=minimum_tasks,
        minimum_categories=minimum_categories,
        bootstrap_samples=bootstrap_samples,
        confidence=confidence,
        seed=seed,
        reliability_alpha=reliability_alpha,
    )


def _markdown_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_number(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.6f}"


def render_markdown(output: dict[str, Any]) -> str:
    bootstrap = output["paired_task_bootstrap"]
    if bootstrap is None:
        interval = "n/a"
    else:
        interval = f"[{bootstrap['ci']['low']:.6f}, {bootstrap['ci']['high']:.6f}]"
    conditional = output["conditional_both_reliable_quality"]
    conditional_bootstrap = conditional["paired_task_bootstrap"]
    if conditional_bootstrap is None:
        conditional_interval = "n/a"
    else:
        conditional_interval = (
            f"[{conditional_bootstrap['ci']['low']:.6f}, "
            f"{conditional_bootstrap['ci']['high']:.6f}]"
        )
    winner = output["winner"] or "none"
    lines = [
        "# NON-RANKABLE Phoenix vs hve-core task-clustered analysis",
        "",
        f"Decision: **{output['decision']}**.",
        "",
        f"Task-suite winner: **{winner}**.",
        "",
        output["decision_reason"] + ".",
        "",
        "**This aggregate is unofficial and non-rankable. It does not name a global harness winner.**",
        "",
        "Five paired attempts are nested within each task. The task is the cluster and macro-average unit.",
        "",
        (
            "Experiment binding: `"
            f"{output['experiment_binding']['experiment_digest']}`; model `"
            f"{output['experiment_binding']['model']['requested']}`."
        ),
        "",
        "## Task-clustered result",
        "",
        f"- Accepted eligible tasks: **{output['eligible_task_count']}**.",
        f"- Categories: **{output['category_count']}**.",
        (
            "- Phoenix macro mean: **"
            f"{_format_number(output['macro_average']['phoenix_mean_score'])}**."
        ),
        (
            "- hve-core macro mean: **"
            f"{_format_number(output['macro_average']['hve_mean_score'])}**."
        ),
        (
            "- Phoenix minus hve-core macro difference: **"
            f"{_format_number(output['macro_average']['phoenix_minus_hve'])}**."
        ),
        f"- Paired task-bootstrap interval: **{interval}**.",
        (
            "- Configured superiority/equivalence region: **"
            f"±{output['policy']['superiority_equivalence_margin']:.2f}**."
        ),
        (
            "- Informative coverage requirement: **"
            f"{output['informative_coverage']['required_coverage']}**, observed "
            f"**{_format_number(output['informative_coverage']['observed_suite_rate'])}**."
        ),
        "",
        "## Conditional both-reliable quality (descriptive only)",
        "",
        (
            "- Tasks with at least one both-reliable pair: **"
            f"{conditional['task_count']}**."
        ),
        (
            "- Phoenix conditional macro mean: **"
            f"{_format_number(conditional['macro_average']['phoenix_mean_score'])}**."
        ),
        (
            "- hve-core conditional macro mean: **"
            f"{_format_number(conditional['macro_average']['hve_mean_score'])}**."
        ),
        (
            "- Conditional Phoenix minus hve-core: **"
            f"{_format_number(conditional['macro_average']['phoenix_minus_hve'])}**."
        ),
        f"- Conditional task-bootstrap interval: **{conditional_interval}**.",
        "",
        conditional["selection_warning"],
        "",
        "## Gates",
        "",
        "| Gate | Pass | Evidence |",
        "|---|---:|---|",
    ]
    for gate in output["gates"]["items"]:
        lines.append(
            f"| {_markdown_text(gate['code'])} | "
            f"{'yes' if gate['passed'] else 'no'} | "
            f"{_markdown_text(gate['evidence'])} |"
        )

    reliability = output["reliability"]
    exact = reliability["exact_task_level_sign_test"]
    lines.extend(
        [
            "",
            "## Exact reliability analysis",
            "",
            (
                "- Phoenix macro reliability: **"
                f"{_format_number(reliability['phoenix_macro_reliability_rate'])}**."
            ),
            (
                "- hve-core macro reliability: **"
                f"{_format_number(reliability['hve_macro_reliability_rate'])}**."
            ),
            (
                "- Task-level signs: Phoenix **"
                f"{exact['phoenix_better_tasks']}**, hve-core "
                f"**{exact['hve_better_tasks']}**, ties "
                f"**{exact['tied_tasks']}**."
            ),
            (
                "- Exact two-sided task-level sign-test p-value: **"
                f"{exact['two_sided_p_value']:.6f}**."
            ),
            "- Nested attempt-level reliability pairs are counts only; no p-value "
            "is computed because attempts are not independent.",
            "",
            "## Category sensitivity",
            "",
            "| Category | Tasks | Phoenix macro | hve-core macro | Difference |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in output["category_sensitivity"]["categories"]:
        lines.append(
            f"| {_markdown_text(row['category'])} | {row['task_count']} | "
            f"{row['phoenix_macro_mean']:.6f} | "
            f"{row['hve_macro_mean']:.6f} | "
            f"{row['score_difference']:.6f} |"
        )
    lines.extend(
        [
            "",
            "Leave-one-category-out status: "
            f"**{output['category_sensitivity']['status']}**.",
            "",
            "## Accepted task ledger",
            "",
            "| Task | Category | Digest | Attempts | Source |",
            "|---|---|---|---:|---|",
        ]
    )
    for row in output["ledger"]["accepted"]:
        lines.append(
            f"| {_markdown_text(row['task_id'])} | "
            f"{_markdown_text(row['category'])} | "
            f"`{_markdown_text(row['task_digest'])}` | "
            f"{row['attempt_count']} | {_markdown_text(row['source'])} |"
        )
    lines.extend(
        [
            "",
            "## Excluded task ledger",
            "",
            "| Task | Category | Source | Reasons |",
            "|---|---|---|---|",
        ]
    )
    if output["ledger"]["excluded"]:
        for row in output["ledger"]["excluded"]:
            lines.append(
                f"| {_markdown_text(row.get('task_id') or 'unknown')} | "
                f"{_markdown_text(row.get('category') or 'unknown')} | "
                f"{_markdown_text(row.get('source') or 'unknown')} | "
                f"{_markdown_text('; '.join(row['reasons']))} |"
            )
    else:
        lines.append("| none | none | none | none |")
    lines.extend(
        [
            "",
            "## Claim boundary",
            "",
            "No overall harness richness, sophistication, or production-readiness ranking is inferred.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze atv.phoenix-hve-task-trial/v1 documents with tasks as "
            "the paired bootstrap clusters."
        )
    )
    parser.add_argument("root", help="JSON file or directory of task-trial JSON")
    parser.add_argument(
        "--preregistration",
        required=True,
        help=(
            "Separately frozen and sealed "
            "atv.phoenix-hve-task-preregistration/v1 binding."
        ),
    )
    parser.add_argument("--json-output")
    parser.add_argument("--markdown-output")
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
    )
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE)
    parser.add_argument("--seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    parser.add_argument(
        "--reliability-alpha",
        type=float,
        default=DEFAULT_RELIABILITY_ALPHA,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        output = summarize_root(
            root,
            preregistration=Path(args.preregistration).resolve(),
            bootstrap_samples=args.bootstrap_samples,
            confidence=args.confidence,
            seed=args.seed,
            reliability_alpha=args.reliability_alpha,
        )
    except TaskAnalysisError as exc:
        raise SystemExit(str(exc)) from exc

    base = root if root.is_dir() else root.parent
    json_output = (
        Path(args.json_output).resolve()
        if args.json_output
        else base / DEFAULT_JSON_NAME
    )
    markdown_output = (
        Path(args.markdown_output).resolve()
        if args.markdown_output
        else base / DEFAULT_MARKDOWN_NAME
    )
    _write_text(
        json_output,
        json.dumps(output, indent=2, sort_keys=True) + "\n",
    )
    _write_text(markdown_output, render_markdown(output))
    print(json_output)
    print(markdown_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
