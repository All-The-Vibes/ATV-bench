"""Focused tests for the preregistered task-cluster precision analysis."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scripts import phoenix_hve_task_precision as precision


@pytest.fixture(scope="module")
def report() -> dict:
    return precision.build_report(simulation_repetitions=2_000)


def test_task_is_the_independent_unit_and_attempts_remain_nested(report):
    assumptions = report["assumptions_frozen_before_results"]

    assert assumptions["task_count"] == 20
    assert assumptions["attempts_per_task"] == 5
    assert assumptions["paired_attempt_count"] == 100
    assert assumptions["independent_unit"] == "task"
    assert assumptions["independent_cluster_count"] == 20
    assert assumptions["attempts_are_nested"] is True
    assert (
        assumptions["portfolio_binding"]["selection_sha256"]
        == precision.PORTFOLIO_SELECTION_SHA256
    )
    assert assumptions["portfolio_binding"]["category_counts"] == {
        "greenfield": 4,
        "repair": 4,
        "debugging": 4,
        "recovery": 4,
        "context-retrieval": 4,
    }
    assert report["uses_observed_task_results"] is False
    assert report["rankable"] is False
    assert report["official"] is False


def test_normal_half_width_and_required_task_formulas():
    z_value = precision.z_critical()
    expected_half_width = z_value * 0.10 / math.sqrt(20)

    assert z_value == pytest.approx(1.9599639845)
    assert precision.standard_error(0.10, 20) == pytest.approx(0.10 / math.sqrt(20))
    assert precision.ci_half_width(0.10, 20) == pytest.approx(expected_half_width)
    assert precision.minimum_tasks_for_half_width(0.10, 0.05) == 16
    assert precision.minimum_tasks_for_half_width(0.10, 0.025) == 62


def test_analytic_probabilities_honor_margin_boundaries_and_symmetry():
    at_positive_margin = precision.analytic_decision_probabilities(
        precision.MARGIN,
        0.10,
    )
    positive = precision.analytic_decision_probabilities(0.10, 0.15)
    negative = precision.analytic_decision_probabilities(-0.10, 0.15)
    zero_low_sd = precision.analytic_decision_probabilities(0.0, 0.10)
    zero_high_sd = precision.analytic_decision_probabilities(0.0, 0.15)

    assert at_positive_margin["phoenix_superior"] == pytest.approx(0.025)
    assert positive["phoenix_superior"] == pytest.approx(negative["hve_superior"])
    assert positive["hve_superior"] == pytest.approx(negative["phoenix_superior"])
    assert sum(positive.values()) == pytest.approx(1.0)
    assert zero_low_sd["practically_equivalent"] == pytest.approx(
        0.217532,
        abs=1e-6,
    )
    assert zero_high_sd["practically_equivalent"] == 0.0


def test_mde_and_power_task_counts_show_20_is_not_a_small_effect_design():
    mde_80 = precision.minimum_true_effect_for_superiority(
        0.15,
        power=0.80,
    )

    assert mde_80 == pytest.approx(0.143968, abs=1e-6)
    assert mde_80 > 2.8 * precision.MARGIN
    assert (
        precision.minimum_tasks_for_superiority(
            0.15,
            0.10,
            power=0.80,
        )
        == 71
    )
    assert (
        precision.minimum_tasks_for_equivalence_at_zero(
            0.15,
            probability=0.80,
        )
        == 95
    )
    assert (
        precision.minimum_tasks_for_superiority(
            0.15,
            precision.MARGIN,
            power=0.80,
        )
        is None
    )


def test_simulation_is_deterministic_and_tracks_the_analytic_scenario():
    first = precision.simulate_decision_probabilities(
        0.10,
        0.10,
        repetitions=10_000,
        seed=73,
    )
    second = precision.simulate_decision_probabilities(
        0.10,
        0.10,
        repetitions=10_000,
        seed=73,
    )
    analytic = precision.analytic_decision_probabilities(0.10, 0.10)

    assert first == second
    assert first["repetitions"] == 10_000
    assert sum(first["counts"].values()) == 10_000
    assert sum(first["probabilities"].values()) == pytest.approx(1.0)
    assert first["probabilities"]["phoenix_superior"] == pytest.approx(
        analytic["phoenix_superior"],
        abs=0.04,
    )
    assert first["maximum_possible_monte_carlo_standard_error"] == (
        pytest.approx(0.005)
    )


def test_report_covers_all_frozen_sd_and_effect_scenarios(report):
    assumptions = report["assumptions_frozen_before_results"]
    rows = report["results"]["decision_scenarios_at_20_tasks"]

    assert len(rows) == (
        len(precision.TASK_EFFECT_SDS) * len(precision.TRUE_EFFECT_MARGIN_UNITS)
    )
    assert {row["task_effect_sd"] for row in rows} == set(precision.TASK_EFFECT_SDS)
    assert {row["true_effect_in_margin_units"] for row in rows} == set(
        precision.TRUE_EFFECT_MARGIN_UNITS
    )
    assert "purposively selected" in assumptions["sampling_frame"]
    assert "not a random sample" in assumptions["sampling_frame"]
    assert (
        report["interpretation"]["primary_conclusion"] == precision.TWENTY_CLUSTER_CLAIM
    )
    assert report["interpretation"]["generalization"] == precision.GENERALIZATION_CLAIM


def test_nested_attempt_illustration_preserves_clusters_and_favors_more_tasks(
    report,
):
    illustration = report["results"]["nested_attempt_illustration"]
    rows = illustration["rows"]
    row_20x10 = next(
        row
        for row in rows
        if row["task_count"] == 20 and row["attempts_per_task"] == 10
    )
    row_40x5 = next(
        row for row in rows if row["task_count"] == 40 and row["attempts_per_task"] == 5
    )

    assert row_20x10["paired_attempts"] == 200
    assert row_20x10["independent_clusters"] == 20
    assert row_40x5["paired_attempts"] == 200
    assert row_40x5["independent_clusters"] == 40
    assert row_40x5["ci_half_width"] < row_20x10["ci_half_width"]
    assert (
        illustration["doubling_tasks_relative_half_width_reduction"]
        > illustration["doubling_attempts_relative_half_width_reduction"]
    )


def test_markdown_freezes_assumptions_before_results_and_states_claim_limits(
    report,
):
    markdown = precision.render_markdown(report)

    assert markdown.index("## Frozen assumptions") < markdown.index(
        "## Sensitivity results"
    )
    assert precision.TWENTY_CLUSTER_CLAIM in markdown
    assert precision.GENERALIZATION_CLAIM in markdown
    assert precision.EXACT_SUITE_CLAIM in markdown
    assert "do not turn 20 tasks into 100 independent observations" in markdown
    assert "overall harness richness" in markdown


def test_json_and_markdown_outputs_are_deterministic(tmp_path: Path, report):
    first_json = tmp_path / "first.json"
    first_markdown = tmp_path / "first.md"
    second_json = tmp_path / "second.json"
    second_markdown = tmp_path / "second.md"

    precision.write_outputs(
        report,
        json_output=first_json,
        markdown_output=first_markdown,
    )
    precision.write_outputs(
        report,
        json_output=second_json,
        markdown_output=second_markdown,
    )

    assert first_json.read_bytes() == second_json.read_bytes()
    assert first_markdown.read_bytes() == second_markdown.read_bytes()
    assert json.loads(first_json.read_text(encoding="utf-8")) == report
    assert first_markdown.read_text(encoding="utf-8") == precision.render_markdown(
        report
    )


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: precision.ci_half_width(0.0), "task_effect_sd"),
        (
            lambda: precision.minimum_tasks_for_half_width(0.1, 0.0),
            "target_half_width",
        ),
        (
            lambda: precision.simulate_decision_probabilities(
                0.0,
                0.1,
                repetitions=0,
            ),
            "simulation_repetitions",
        ),
    ],
)
def test_invalid_sensitivity_inputs_fail_closed(call, message):
    with pytest.raises(precision.PrecisionError, match=message):
        call()
