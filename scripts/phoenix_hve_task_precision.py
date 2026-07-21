#!/usr/bin/env python3
"""Preregistered precision sensitivity for the Phoenix-versus-hve task study.

This module is a design-stage tool. It does not read trial outcomes. The
independent observation is one task-level Phoenix-minus-hve score difference,
formed by averaging five paired attempts within that task. The 100 paired
attempts therefore remain nested inside 20 independent task clusters.

The primary calculations use a transparent paired-task normal approximation.
A deterministic Monte Carlo check samples normally distributed task effects,
estimates their sample standard deviation, and applies the same z-interval
decision rule. These scenarios describe the exact purposively selected
synthetic suite only; they do not create a random-population sampling frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Any, Sequence

SCHEMA = "atv.phoenix-hve-task-precision/v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKDOWN_OUTPUT = (
    REPO_ROOT / "docs" / "proof" / "phoenix-hve-task-v1" / "SAMPLE_SIZE_SENSITIVITY.md"
)

TASK_COUNT = 20
ATTEMPTS_PER_TASK = 5
PAIRED_ATTEMPT_COUNT = TASK_COUNT * ATTEMPTS_PER_TASK
MARGIN = 0.05
CONFIDENCE = 0.95
SIMULATION_REPETITIONS = 50_000
SIMULATION_SEED = 20_260_721

TASK_EFFECT_SDS = (0.05, 0.10, 0.15, 0.20, 0.30)
TRUE_EFFECT_MARGIN_UNITS = (-3, -2, -1, 0, 1, 2, 3)
POWER_TARGETS = (0.80, 0.90)
PORTFOLIO_SELECTION_SHA256 = (
    "5b2fdc11722d266ebf6443975fabdd5867787b36ec33f5b6d1b8390df54b665a"
)
PORTFOLIO_CATEGORY_COUNTS = {
    "greenfield": 4,
    "repair": 4,
    "debugging": 4,
    "recovery": 4,
    "context-retrieval": 4,
}

ILLUSTRATIVE_BETWEEN_TASK_SD = 0.15
ILLUSTRATIVE_WITHIN_ATTEMPT_SD = 0.10
ILLUSTRATIVE_TASK_COUNTS = (20, 40)
ILLUSTRATIVE_ATTEMPT_COUNTS = (1, 5, 10)

EXACT_SUITE_CLAIM = (
    "The scenarios apply only to the exact purposively selected public synthetic "
    "suite; they do not support random-population inference."
)
TWENTY_CLUSTER_CLAIM = (
    "20 clusters can support only large, consistent exact-suite effects."
)
GENERALIZATION_CLAIM = "More task families—not more repetitions—improve generalization."

_NORMAL = NormalDist()


class PrecisionError(ValueError):
    """The sensitivity analysis inputs violate the frozen design contract."""


def _require_probability(value: float, *, name: str) -> float:
    numeric = float(value)
    if not 0.0 < numeric < 1.0:
        raise PrecisionError(f"{name} must be strictly between zero and one")
    return numeric


def _require_positive(value: float, *, name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise PrecisionError(f"{name} must be finite and positive")
    return numeric


def _require_task_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise PrecisionError("task_count must be an integer of at least two")
    return value


def _require_repetitions(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PrecisionError("simulation_repetitions must be a positive integer")
    return value


def _rounded(value: float, digits: int = 6) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == -0.0 else rounded


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def z_critical(confidence: float = CONFIDENCE) -> float:
    """Return the two-sided normal critical value for ``confidence``."""

    confidence = _require_probability(confidence, name="confidence")
    return _NORMAL.inv_cdf(0.5 + confidence / 2.0)


def standard_error(task_effect_sd: float, task_count: int = TASK_COUNT) -> float:
    """Standard error of the macro mean over independent task effects."""

    task_effect_sd = _require_positive(task_effect_sd, name="task_effect_sd")
    task_count = _require_task_count(task_count)
    return task_effect_sd / math.sqrt(task_count)


def ci_half_width(
    task_effect_sd: float,
    task_count: int = TASK_COUNT,
    confidence: float = CONFIDENCE,
) -> float:
    """Normal-approximation half-width for the paired-task macro mean."""

    return z_critical(confidence) * standard_error(task_effect_sd, task_count)


def minimum_tasks_for_half_width(
    task_effect_sd: float,
    target_half_width: float,
    confidence: float = CONFIDENCE,
) -> int:
    """Smallest task count whose normal CI half-width is at most the target."""

    task_effect_sd = _require_positive(task_effect_sd, name="task_effect_sd")
    target_half_width = _require_positive(
        target_half_width,
        name="target_half_width",
    )
    raw = (z_critical(confidence) * task_effect_sd / target_half_width) ** 2
    return max(2, math.ceil(raw - 1e-12))


def minimum_true_effect_for_superiority(
    task_effect_sd: float,
    *,
    task_count: int = TASK_COUNT,
    margin: float = MARGIN,
    confidence: float = CONFIDENCE,
    power: float = 0.80,
) -> float:
    """True positive effect needed for the requested superiority probability."""

    margin = _require_positive(margin, name="margin")
    power = _require_probability(power, name="power")
    return margin + (z_critical(confidence) + _NORMAL.inv_cdf(power)) * standard_error(
        task_effect_sd, task_count
    )


def minimum_tasks_for_superiority(
    task_effect_sd: float,
    true_effect: float,
    *,
    margin: float = MARGIN,
    confidence: float = CONFIDENCE,
    power: float = 0.80,
) -> int | None:
    """Smallest cluster count for positive superiority under the approximation.

    ``None`` means the stipulated true effect does not exceed the practical
    margin, so increasing sample size cannot reach the requested power for a
    superiority decision.
    """

    task_effect_sd = _require_positive(task_effect_sd, name="task_effect_sd")
    margin = _require_positive(margin, name="margin")
    power = _require_probability(power, name="power")
    gap = float(true_effect) - margin
    if not math.isfinite(gap):
        raise PrecisionError("true_effect must be finite")
    if gap <= 0.0:
        return None
    numerator = task_effect_sd * (z_critical(confidence) + _NORMAL.inv_cdf(power))
    return max(2, math.ceil((numerator / gap) ** 2 - 1e-12))


def minimum_tasks_for_equivalence_at_zero(
    task_effect_sd: float,
    *,
    margin: float = MARGIN,
    confidence: float = CONFIDENCE,
    probability: float = 0.80,
) -> int:
    """Smallest task count for an equivalence decision when the true effect is 0."""

    task_effect_sd = _require_positive(task_effect_sd, name="task_effect_sd")
    margin = _require_positive(margin, name="margin")
    probability = _require_probability(probability, name="probability")
    central_quantile = _NORMAL.inv_cdf((1.0 + probability) / 2.0)
    numerator = task_effect_sd * (z_critical(confidence) + central_quantile)
    return max(2, math.ceil((numerator / margin) ** 2 - 1e-12))


def classify_interval(
    estimate: float,
    half_width: float,
    *,
    margin: float = MARGIN,
) -> str:
    """Apply the preregistered task-analysis decision boundaries."""

    margin = _require_positive(margin, name="margin")
    half_width = float(half_width)
    estimate = float(estimate)
    if not math.isfinite(estimate):
        raise PrecisionError("estimate must be finite")
    if not math.isfinite(half_width) or half_width < 0.0:
        raise PrecisionError("half_width must be finite and non-negative")
    low = estimate - half_width
    high = estimate + half_width
    if low > margin:
        return "phoenix_superior"
    if high < -margin:
        return "hve_superior"
    if low >= -margin and high <= margin:
        return "practically_equivalent"
    return "inconclusive"


def analytic_decision_probabilities(
    true_effect: float,
    task_effect_sd: float,
    *,
    task_count: int = TASK_COUNT,
    margin: float = MARGIN,
    confidence: float = CONFIDENCE,
) -> dict[str, float]:
    """Decision probabilities when the scenario SD is treated as known."""

    task_count = _require_task_count(task_count)
    task_effect_sd = _require_positive(task_effect_sd, name="task_effect_sd")
    margin = _require_positive(margin, name="margin")
    true_effect = float(true_effect)
    if not math.isfinite(true_effect):
        raise PrecisionError("true_effect must be finite")

    se = standard_error(task_effect_sd, task_count)
    z_value = z_critical(confidence)
    half_width = z_value * se

    phoenix = _NORMAL.cdf((true_effect - margin) / se - z_value)
    hve = _NORMAL.cdf((-margin - true_effect) / se - z_value)
    equivalence = 0.0
    if half_width < margin:
        lower_standardized = (-margin + half_width - true_effect) / se
        upper_standardized = (margin - half_width - true_effect) / se
        equivalence = max(
            0.0,
            _NORMAL.cdf(upper_standardized) - _NORMAL.cdf(lower_standardized),
        )

    decisive_total = phoenix + hve + equivalence
    inconclusive = max(0.0, 1.0 - decisive_total)
    total = phoenix + hve + equivalence + inconclusive
    if total <= 0.0:
        raise PrecisionError("decision probabilities did not form a distribution")
    return {
        "phoenix_superior": phoenix / total,
        "hve_superior": hve / total,
        "practically_equivalent": equivalence / total,
        "inconclusive": inconclusive / total,
    }


def _scenario_seed(
    base_seed: int,
    *,
    true_effect: float,
    task_effect_sd: float,
    task_count: int,
) -> int:
    payload = {
        "algorithm": "phoenix-hve-task-precision-scenario-seed-v1",
        "base_seed": int(base_seed),
        "task_count": task_count,
        "task_effect_sd": float(task_effect_sd),
        "true_effect": float(true_effect),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def simulate_decision_probabilities(
    true_effect: float,
    task_effect_sd: float,
    *,
    task_count: int = TASK_COUNT,
    margin: float = MARGIN,
    confidence: float = CONFIDENCE,
    repetitions: int = SIMULATION_REPETITIONS,
    seed: int = SIMULATION_SEED,
) -> dict[str, Any]:
    """Deterministically simulate z intervals using an estimated task SD.

    Under normal task effects, the sample mean and sample variance are
    independent. Sampling one standard normal and one chi-square variate per
    repetition is exactly equivalent to materializing every task effect, while
    being much faster and preserving the task-level inferential unit.
    """

    task_count = _require_task_count(task_count)
    task_effect_sd = _require_positive(task_effect_sd, name="task_effect_sd")
    margin = _require_positive(margin, name="margin")
    repetitions = _require_repetitions(repetitions)
    true_effect = float(true_effect)
    if not math.isfinite(true_effect):
        raise PrecisionError("true_effect must be finite")

    scenario_seed = _scenario_seed(
        seed,
        true_effect=true_effect,
        task_effect_sd=task_effect_sd,
        task_count=task_count,
    )
    rng = random.Random(scenario_seed)
    z_value = z_critical(confidence)
    true_se = task_effect_sd / math.sqrt(task_count)
    degrees_of_freedom = task_count - 1
    counts = {
        "phoenix_superior": 0,
        "hve_superior": 0,
        "practically_equivalent": 0,
        "inconclusive": 0,
    }

    for _ in range(repetitions):
        estimate = true_effect + true_se * rng.normalvariate(0.0, 1.0)
        chi_square = rng.gammavariate(degrees_of_freedom / 2.0, 2.0)
        estimated_sd = task_effect_sd * math.sqrt(chi_square / degrees_of_freedom)
        half_width = z_value * estimated_sd / math.sqrt(task_count)
        counts[classify_interval(estimate, half_width, margin=margin)] += 1

    probabilities = {key: value / repetitions for key, value in counts.items()}
    monte_carlo_se = {
        key: math.sqrt(probability * (1.0 - probability) / repetitions)
        for key, probability in probabilities.items()
    }
    return {
        "scenario_seed": scenario_seed,
        "repetitions": repetitions,
        "counts": counts,
        "probabilities": probabilities,
        "monte_carlo_standard_errors": monte_carlo_se,
        "maximum_possible_monte_carlo_standard_error": (0.5 / math.sqrt(repetitions)),
    }


def frozen_assumptions(
    *,
    simulation_repetitions: int = SIMULATION_REPETITIONS,
) -> dict[str, Any]:
    """Return assumptions that are serialized before any sensitivity results."""

    simulation_repetitions = _require_repetitions(simulation_repetitions)
    return {
        "analysis_stage": (
            "design-stage preregistration before Phoenix-vs-hve task outcomes"
        ),
        "uses_observed_task_results": False,
        "task_count": TASK_COUNT,
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "paired_attempt_count": PAIRED_ATTEMPT_COUNT,
        "independent_unit": "task",
        "independent_cluster_count": TASK_COUNT,
        "attempts_are_nested": True,
        "task_effect_definition": (
            "Phoenix-minus-hve score difference averaged over five paired "
            "attempts within each task"
        ),
        "task_effect_sd_definition": (
            "Across-task standard deviation of the five-attempt task means; "
            "it already includes any residual within-task noise after averaging"
        ),
        "portfolio_binding": {
            "selection_sha256": PORTFOLIO_SELECTION_SHA256,
            "category_counts": PORTFOLIO_CATEGORY_COUNTS,
            "category_count": len(PORTFOLIO_CATEGORY_COUNTS),
            "tasks_per_category": 4,
        },
        "practical_margin": {
            "lower": -MARGIN,
            "upper": MARGIN,
            "absolute": MARGIN,
        },
        "confidence": CONFIDENCE,
        "interval": (
            "two-sided paired-task normal z interval around the equal-weight macro mean"
        ),
        "decision_rule": {
            "phoenix_superior": "lower interval bound > +0.05",
            "hve_superior": "upper interval bound < -0.05",
            "practically_equivalent": ("entire interval within [-0.05, +0.05]"),
            "otherwise": "inconclusive",
        },
        "task_effect_sd_scenarios": list(TASK_EFFECT_SDS),
        "true_effect_scenarios": [
            {
                "margin_units": units,
                "phoenix_minus_hve": _rounded(units * MARGIN),
            }
            for units in TRUE_EFFECT_MARGIN_UNITS
        ],
        "power_targets": list(POWER_TARGETS),
        "simulation": {
            "repetitions_per_scenario": simulation_repetitions,
            "base_seed": SIMULATION_SEED,
            "task_effect_distribution": "normal",
            "interval_sd": "estimated from the 20 simulated task effects",
            "sampling_method": (
                "independent normal sample-mean and chi-square sample-variance draws"
            ),
        },
        "nested_attempt_illustration": {
            "between_task_sd": ILLUSTRATIVE_BETWEEN_TASK_SD,
            "within_attempt_sd": ILLUSTRATIVE_WITHIN_ATTEMPT_SD,
            "task_counts": list(ILLUSTRATIVE_TASK_COUNTS),
            "attempt_counts": list(ILLUSTRATIVE_ATTEMPT_COUNTS),
            "variance_model": "between_task_sd^2 + within_attempt_sd^2 / attempts",
            "purpose": (
                "illustrate measurement stabilization only, not broadened "
                "task-family generalization"
            ),
        },
        "sampling_frame": (
            "exact purposively selected public synthetic 20-task suite; not a "
            "random sample of coding tasks or harness workloads"
        ),
        "claim_boundary": EXACT_SUITE_CLAIM,
    }


def _formula_ledger() -> dict[str, str]:
    return {
        "standard_error": "SE = task_effect_SD / sqrt(number_of_tasks)",
        "ci_half_width": "h = z_0.975 * SE",
        "phoenix_superiority_probability": (
            "Phi((true_effect - margin) / SE - z_0.975)"
        ),
        "hve_superiority_probability": ("Phi((-margin - true_effect) / SE - z_0.975)"),
        "equivalence_probability": (
            "Phi((margin - h - true_effect) / SE) - "
            "Phi((-margin + h - true_effect) / SE), when h < margin; "
            "otherwise 0"
        ),
        "superiority_mde": (
            "margin + (z_0.975 + z_power) * task_effect_SD / sqrt(tasks)"
        ),
        "required_tasks_for_half_width": (
            "ceil((z_0.975 * task_effect_SD / target_half_width)^2)"
        ),
        "required_tasks_for_superiority": (
            "ceil((task_effect_SD * (z_0.975 + z_power) / (true_effect - margin))^2)"
        ),
        "required_tasks_for_zero_equivalence": (
            "ceil((task_effect_SD * (z_0.975 + "
            "z_((1 + target_probability) / 2)) / margin)^2)"
        ),
    }


def _precision_rows() -> list[dict[str, Any]]:
    rows = []
    for task_sd in TASK_EFFECT_SDS:
        se = standard_error(task_sd)
        half_width = ci_half_width(task_sd)
        rows.append(
            {
                "task_effect_sd": task_sd,
                "sd_in_margin_units": _rounded(task_sd / MARGIN),
                "standard_error": _rounded(se),
                "ci_half_width": _rounded(half_width),
                "ci_full_width": _rounded(2.0 * half_width),
                "equivalence_has_positive_probability": half_width < MARGIN,
                "minimum_true_effect_for_80pct_superiority": _rounded(
                    minimum_true_effect_for_superiority(
                        task_sd,
                        power=0.80,
                    )
                ),
                "minimum_true_effect_for_90pct_superiority": _rounded(
                    minimum_true_effect_for_superiority(
                        task_sd,
                        power=0.90,
                    )
                ),
            }
        )
    return rows


def _required_task_rows() -> list[dict[str, Any]]:
    rows = []
    for task_sd in TASK_EFFECT_SDS:
        rows.append(
            {
                "task_effect_sd": task_sd,
                "half_width_at_most_margin": minimum_tasks_for_half_width(
                    task_sd,
                    MARGIN,
                ),
                "half_width_at_most_half_margin": minimum_tasks_for_half_width(
                    task_sd,
                    MARGIN / 2.0,
                ),
                "superiority_at_true_effect_2m": {
                    "true_effect": 2.0 * MARGIN,
                    "tasks_for_80pct": minimum_tasks_for_superiority(
                        task_sd,
                        2.0 * MARGIN,
                        power=0.80,
                    ),
                    "tasks_for_90pct": minimum_tasks_for_superiority(
                        task_sd,
                        2.0 * MARGIN,
                        power=0.90,
                    ),
                },
                "superiority_at_true_effect_3m": {
                    "true_effect": 3.0 * MARGIN,
                    "tasks_for_80pct": minimum_tasks_for_superiority(
                        task_sd,
                        3.0 * MARGIN,
                        power=0.80,
                    ),
                    "tasks_for_90pct": minimum_tasks_for_superiority(
                        task_sd,
                        3.0 * MARGIN,
                        power=0.90,
                    ),
                },
                "equivalence_at_true_zero": {
                    "tasks_for_80pct": minimum_tasks_for_equivalence_at_zero(
                        task_sd,
                        probability=0.80,
                    ),
                    "tasks_for_90pct": minimum_tasks_for_equivalence_at_zero(
                        task_sd,
                        probability=0.90,
                    ),
                },
            }
        )
    return rows


def _decision_scenario_rows(
    *,
    simulation_repetitions: int,
) -> list[dict[str, Any]]:
    rows = []
    for task_sd in TASK_EFFECT_SDS:
        for margin_units in TRUE_EFFECT_MARGIN_UNITS:
            true_effect = margin_units * MARGIN
            analytic = analytic_decision_probabilities(
                true_effect,
                task_sd,
            )
            simulation = simulate_decision_probabilities(
                true_effect,
                task_sd,
                repetitions=simulation_repetitions,
            )
            rows.append(
                {
                    "task_effect_sd": task_sd,
                    "true_effect": _rounded(true_effect),
                    "true_effect_in_margin_units": margin_units,
                    "analytic_known_sd": {
                        key: _rounded(value) for key, value in analytic.items()
                    },
                    "simulation_estimated_sd": {
                        "scenario_seed": simulation["scenario_seed"],
                        "repetitions": simulation["repetitions"],
                        "probabilities": {
                            key: _rounded(value)
                            for key, value in simulation["probabilities"].items()
                        },
                        "monte_carlo_standard_errors": {
                            key: _rounded(value)
                            for key, value in simulation[
                                "monte_carlo_standard_errors"
                            ].items()
                        },
                        "maximum_possible_monte_carlo_standard_error": _rounded(
                            simulation["maximum_possible_monte_carlo_standard_error"]
                        ),
                    },
                }
            )
    return rows


def _nested_attempt_rows() -> list[dict[str, Any]]:
    rows = []
    for task_count in ILLUSTRATIVE_TASK_COUNTS:
        for attempt_count in ILLUSTRATIVE_ATTEMPT_COUNTS:
            task_mean_sd = math.sqrt(
                ILLUSTRATIVE_BETWEEN_TASK_SD**2
                + ILLUSTRATIVE_WITHIN_ATTEMPT_SD**2 / attempt_count
            )
            rows.append(
                {
                    "task_count": task_count,
                    "attempts_per_task": attempt_count,
                    "paired_attempts": task_count * attempt_count,
                    "independent_clusters": task_count,
                    "task_mean_sd": _rounded(task_mean_sd),
                    "ci_half_width": _rounded(ci_half_width(task_mean_sd, task_count)),
                }
            )
    return rows


def _find_scenario(
    rows: Sequence[dict[str, Any]],
    *,
    task_sd: float,
    margin_units: int,
) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row["task_effect_sd"] == task_sd
        and row["true_effect_in_margin_units"] == margin_units
    )


def build_report(
    *,
    simulation_repetitions: int = SIMULATION_REPETITIONS,
) -> dict[str, Any]:
    """Build the deterministic sensitivity artifact without outcome data."""

    simulation_repetitions = _require_repetitions(simulation_repetitions)
    assumptions = frozen_assumptions(simulation_repetitions=simulation_repetitions)
    decision_rows = _decision_scenario_rows(
        simulation_repetitions=simulation_repetitions
    )
    zero_sd_010 = _find_scenario(
        decision_rows,
        task_sd=0.10,
        margin_units=0,
    )
    positive_2m_sd_010 = _find_scenario(
        decision_rows,
        task_sd=0.10,
        margin_units=2,
    )
    positive_2m_sd_015 = _find_scenario(
        decision_rows,
        task_sd=0.15,
        margin_units=2,
    )
    positive_2m_sd_020 = _find_scenario(
        decision_rows,
        task_sd=0.20,
        margin_units=2,
    )
    precision_rows = _precision_rows()
    nested_rows = _nested_attempt_rows()
    baseline_nested = next(
        row
        for row in nested_rows
        if row["task_count"] == 20 and row["attempts_per_task"] == 5
    )
    doubled_attempts = next(
        row
        for row in nested_rows
        if row["task_count"] == 20 and row["attempts_per_task"] == 10
    )
    doubled_tasks = next(
        row
        for row in nested_rows
        if row["task_count"] == 40 and row["attempts_per_task"] == 5
    )

    return {
        "schema": SCHEMA,
        "rankable": False,
        "official": False,
        "uses_observed_task_results": False,
        "assumptions_sha256": _canonical_sha256(assumptions),
        "assumptions_frozen_before_results": assumptions,
        "formulas": _formula_ledger(),
        "results": {
            "z_critical": _rounded(z_critical()),
            "precision_at_20_tasks": precision_rows,
            "required_task_counts": _required_task_rows(),
            "decision_scenarios_at_20_tasks": decision_rows,
            "nested_attempt_illustration": {
                "rows": nested_rows,
                "baseline_20_tasks_x_5_attempts_half_width": baseline_nested[
                    "ci_half_width"
                ],
                "doubling_attempts_to_10_half_width": doubled_attempts["ci_half_width"],
                "doubling_tasks_to_40_half_width": doubled_tasks["ci_half_width"],
                "doubling_attempts_relative_half_width_reduction": _rounded(
                    1.0
                    - doubled_attempts["ci_half_width"]
                    / baseline_nested["ci_half_width"]
                ),
                "doubling_tasks_relative_half_width_reduction": _rounded(
                    1.0
                    - doubled_tasks["ci_half_width"] / baseline_nested["ci_half_width"]
                ),
            },
        },
        "interpretation": {
            "scope": EXACT_SUITE_CLAIM,
            "primary_conclusion": TWENTY_CLUSTER_CLAIM,
            "generalization": GENERALIZATION_CLAIM,
            "quantitative_checks": [
                (
                    "At task-effect SD 0.15, the 20-task 95% half-width is "
                    f"{next(row for row in precision_rows if row['task_effect_sd'] == 0.15)['ci_half_width']:.6f}, "
                    "which is wider than the 0.05 equivalence margin."
                ),
                (
                    "At true effect 0 and task-effect SD 0.10, analytic "
                    "equivalence probability is "
                    f"{zero_sd_010['analytic_known_sd']['practically_equivalent']:.6f}."
                ),
                (
                    "At true effect +0.10, analytic Phoenix-superiority "
                    "probability falls from "
                    f"{positive_2m_sd_010['analytic_known_sd']['phoenix_superior']:.6f} "
                    "at SD 0.10 to "
                    f"{positive_2m_sd_015['analytic_known_sd']['phoenix_superior']:.6f} "
                    "at SD 0.15 and "
                    f"{positive_2m_sd_020['analytic_known_sd']['phoenix_superior']:.6f} "
                    "at SD 0.20."
                ),
            ],
            "attempts_statement": (
                "Five attempts may reduce within-task run noise, but they remain "
                "nested and do not turn 20 tasks into 100 independent observations."
            ),
            "normal_approximation_limit": (
                "These are planning scenarios under a normal approximation, not "
                "guarantees about the later task bootstrap or observed effects."
            ),
        },
    }


def _percent(value: float) -> str:
    return f"{100.0 * float(value):.1f}%"


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    """Render a human-readable companion with assumptions before results."""

    assumptions = report["assumptions_frozen_before_results"]
    results = report["results"]
    precision_rows = results["precision_at_20_tasks"]
    required_rows = results["required_task_counts"]
    scenario_rows = results["decision_scenarios_at_20_tasks"]
    nested = results["nested_attempt_illustration"]

    assumption_rows = [
        ("Stage", assumptions["analysis_stage"]),
        ("Observed outcomes used", "No"),
        ("Independent unit", "Task"),
        ("Independent clusters", assumptions["independent_cluster_count"]),
        ("Nested attempts per task", assumptions["attempts_per_task"]),
        ("Total paired attempts", assumptions["paired_attempt_count"]),
        (
            "Portfolio selection SHA-256",
            assumptions["portfolio_binding"]["selection_sha256"],
        ),
        (
            "Category structure",
            (
                f"{assumptions['portfolio_binding']['category_count']} "
                "categories × "
                f"{assumptions['portfolio_binding']['tasks_per_category']} "
                "tasks"
            ),
        ),
        ("Practical margin", "[-0.05, +0.05]"),
        ("Confidence interval", f"{assumptions['confidence']:.0%} paired-task z"),
        (
            "Task-effect SD scenarios",
            ", ".join(f"{value:.2f}" for value in TASK_EFFECT_SDS),
        ),
        (
            "True-effect scenarios",
            ", ".join(
                f"{units:+d}M" if units else "0" for units in TRUE_EFFECT_MARGIN_UNITS
            ),
        ),
        (
            "Simulation",
            (
                f"{assumptions['simulation']['repetitions_per_scenario']:,} "
                f"repetitions/scenario; seed "
                f"{assumptions['simulation']['base_seed']}"
            ),
        ),
        ("Sampling frame", assumptions["sampling_frame"]),
    ]

    precision_table = _markdown_table(
        (
            "Task SD",
            "SD / M",
            "SE",
            "95% half-width",
            "Equivalence possible",
            "80% superiority MDE",
            "90% superiority MDE",
        ),
        [
            (
                f"{row['task_effect_sd']:.2f}",
                f"{row['sd_in_margin_units']:.1f}",
                f"{row['standard_error']:.4f}",
                f"{row['ci_half_width']:.4f}",
                "yes" if row["equivalence_has_positive_probability"] else "no",
                f"{row['minimum_true_effect_for_80pct_superiority']:.3f}",
                f"{row['minimum_true_effect_for_90pct_superiority']:.3f}",
            )
            for row in precision_rows
        ],
    )

    required_table = _markdown_table(
        (
            "Task SD",
            "h <= M",
            "h <= M/2",
            "80% sup. at +2M",
            "90% sup. at +2M",
            "80% equiv. at 0",
            "90% equiv. at 0",
        ),
        [
            (
                f"{row['task_effect_sd']:.2f}",
                row["half_width_at_most_margin"],
                row["half_width_at_most_half_margin"],
                row["superiority_at_true_effect_2m"]["tasks_for_80pct"],
                row["superiority_at_true_effect_2m"]["tasks_for_90pct"],
                row["equivalence_at_true_zero"]["tasks_for_80pct"],
                row["equivalence_at_true_zero"]["tasks_for_90pct"],
            )
            for row in required_rows
        ],
    )

    analytic_table = _markdown_table(
        (
            "Task SD",
            "True effect",
            "P(Phoenix)",
            "P(hve)",
            "P(equivalent)",
            "P(inconclusive)",
        ),
        [
            (
                f"{row['task_effect_sd']:.2f}",
                (
                    f"{row['true_effect']:+.2f} "
                    f"({row['true_effect_in_margin_units']:+d}M)"
                    if row["true_effect_in_margin_units"]
                    else "0.00 (0M)"
                ),
                _percent(row["analytic_known_sd"]["phoenix_superior"]),
                _percent(row["analytic_known_sd"]["hve_superior"]),
                _percent(row["analytic_known_sd"]["practically_equivalent"]),
                _percent(row["analytic_known_sd"]["inconclusive"]),
            )
            for row in scenario_rows
        ],
    )

    simulation_focus = [
        row for row in scenario_rows if row["true_effect_in_margin_units"] in (0, 2, 3)
    ]
    simulation_table = _markdown_table(
        (
            "Task SD",
            "True effect",
            "Target decision",
            "Analytic",
            "Simulation",
            "Max MC SE",
        ),
        [
            (
                f"{row['task_effect_sd']:.2f}",
                (
                    "0M"
                    if row["true_effect_in_margin_units"] == 0
                    else f"+{row['true_effect_in_margin_units']}M"
                ),
                (
                    "equivalent"
                    if row["true_effect_in_margin_units"] == 0
                    else "Phoenix"
                ),
                _percent(
                    row["analytic_known_sd"][
                        (
                            "practically_equivalent"
                            if row["true_effect_in_margin_units"] == 0
                            else "phoenix_superior"
                        )
                    ]
                ),
                _percent(
                    row["simulation_estimated_sd"]["probabilities"][
                        (
                            "practically_equivalent"
                            if row["true_effect_in_margin_units"] == 0
                            else "phoenix_superior"
                        )
                    ]
                ),
                _percent(
                    row["simulation_estimated_sd"][
                        "maximum_possible_monte_carlo_standard_error"
                    ]
                ),
            )
            for row in simulation_focus
        ],
    )

    nested_table = _markdown_table(
        (
            "Tasks",
            "Attempts/task",
            "Paired attempts",
            "Independent clusters",
            "Illustrative task-mean SD",
            "95% half-width",
        ),
        [
            (
                row["task_count"],
                row["attempts_per_task"],
                row["paired_attempts"],
                row["independent_clusters"],
                f"{row['task_mean_sd']:.4f}",
                f"{row['ci_half_width']:.4f}",
            )
            for row in nested["rows"]
        ],
    )

    formulas = report["formulas"]
    lines = [
        "# Phoenix vs hve-core task-study sample-size sensitivity",
        "",
        "**Status:** preregistered design analysis; no Phoenix-vs-hve task "
        "outcomes are read or used.",
        "",
        "This file is deterministically generated by "
        "`scripts/phoenix_hve_task_precision.py`. It evaluates the planned "
        "20-task × 5-attempt design without treating the 100 paired attempts "
        "as independent.",
        "",
        "## Frozen assumptions (before results)",
        "",
        _markdown_table(("Assumption", "Frozen value"), assumption_rows),
        "",
        f"Assumption seal (SHA-256): `{report['assumptions_sha256']}`.",
        "",
        EXACT_SUITE_CLAIM,
        "",
        "The task-effect SD is the across-task SD of the five-attempt task "
        "means. The five attempts can stabilize each task estimate, but the "
        "inferential cluster count remains 20.",
        "",
        "## Formulas",
        "",
        f"- Standard error: `{formulas['standard_error']}`.",
        f"- 95% half-width: `{formulas['ci_half_width']}`.",
        "- Phoenix superiority probability: "
        f"`{formulas['phoenix_superiority_probability']}`.",
        f"- Equivalence probability: `{formulas['equivalence_probability']}`.",
        f"- Superiority MDE: `{formulas['superiority_mde']}`.",
        "",
        "The analytic table treats each scenario SD as known. The simulation "
        "draws a sample mean and an estimated sample SD for 20 normally "
        "distributed task effects, then applies the same z-interval rule. "
        "The normal model is a planning approximation, not a claim that the "
        "bounded task effects are literally normal.",
        "",
        "## Sensitivity results",
        "",
        "### Precision and minimum detectable superiority effect at 20 tasks",
        "",
        precision_table,
        "",
        "`M` is the 0.05 practical margin. The MDE columns are the positive "
        "true effects needed for 80% or 90% probability that the lower 95% "
        "bound clears +0.05.",
        "",
        "### Required independent task counts",
        "",
        required_table,
        "",
        "These counts add task clusters. Adding attempts to an existing task "
        "does not satisfy them.",
        "",
        "### Analytic decision probabilities at 20 tasks",
        "",
        analytic_table,
        "",
        "Negative and positive scenarios are shown explicitly. The symmetry "
        "is a property of the planning model, not evidence that the observed "
        "suite will be symmetric.",
        "",
        "### Deterministic simulation check",
        "",
        simulation_table,
        "",
        "The simulation uses an estimated task SD, so modest differences from "
        "the known-SD analytic approximation are expected. In particular, a "
        "finite sample can underestimate the scenario SD and rarely fit inside "
        "the equivalence margin even when the known-SD analytic half-width "
        "exceeds it.",
        "",
        "### Why nested attempts do not replace task families",
        "",
        "The following is a frozen illustration with between-task SD 0.15 and "
        "within-attempt SD 0.10. It is not an estimate from trial outcomes.",
        "",
        nested_table,
        "",
        "Under this illustration, doubling attempts from 5 to 10 while keeping "
        f"20 tasks reduces half-width by {_percent(nested['doubling_attempts_relative_half_width_reduction'])}; "
        "doubling task families from 20 to 40 at five attempts reduces it by "
        f"{_percent(nested['doubling_tasks_relative_half_width_reduction'])}.",
        "",
        "## What 20 tasks can and cannot support",
        "",
        f"- **{TWENTY_CLUSTER_CLAIM}**",
        "- Under the known-SD analytic scenarios, task-effect SD 0.15 or "
        "larger makes the 20-task 95% half-width exceed 0.05, so an "
        "equivalence decision has zero probability under that analytic rule.",
        "- A true +0.10 effect has only about 61% analytic Phoenix-superiority "
        "probability at SD 0.10, about 32% at SD 0.15, and about 20% at SD "
        "0.20.",
        "- Twenty tasks can be persuasive when effects are well beyond the "
        "margin and consistent across tasks. They are weak for near-margin, "
        "heterogeneous, or equivalence claims.",
        "- Five attempts may reduce within-task execution noise, but they do "
        "not turn 20 tasks into 100 independent observations.",
        f"- **{GENERALIZATION_CLAIM}** Broader independent task families "
        "increase both cluster count and coverage; extra repetitions only "
        "stabilize measurements inside the existing suite.",
        "- No result from this design establishes overall harness richness, "
        "production sophistication, or performance on a random population of "
        "coding tasks.",
        "",
        "## Claim boundary",
        "",
        "A later result can support, at most, a statement about completion-"
        "adjusted macro performance under pinned commits, model, budget, and "
        "these exact 20 public synthetic tasks. The task bootstrap remains the "
        "primary analysis; this normal approximation is its preregistered "
        "sample-size and precision sensitivity check.",
        "",
    ]
    return "\n".join(lines)


def _json_text(report: dict[str, Any]) -> str:
    return (
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def _write_text(destination: str | Path, text: str) -> None:
    if str(destination) == "-":
        sys.stdout.write(text)
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_outputs(
    report: dict[str, Any],
    *,
    json_output: str | Path,
    markdown_output: str | Path,
) -> None:
    """Write deterministic JSON and Markdown representations."""

    if str(json_output) == "-" and str(markdown_output) == "-":
        raise PrecisionError(
            "json_output and markdown_output cannot both target stdout"
        )
    _write_text(json_output, _json_text(report))
    _write_text(markdown_output, render_markdown(report))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the frozen 20-task x 5-attempt Phoenix-vs-hve "
            "sample-size sensitivity analysis."
        )
    )
    parser.add_argument(
        "--json-output",
        default="-",
        help="JSON destination, or '-' for stdout (default: stdout).",
    )
    parser.add_argument(
        "--markdown-output",
        default=str(DEFAULT_MARKDOWN_OUTPUT),
        help="Markdown destination (default: committed proof document).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_report()
    write_outputs(
        report,
        json_output=args.json_output,
        markdown_output=args.markdown_output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
