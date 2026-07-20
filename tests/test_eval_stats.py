"""Simulation calibration for task-clustered paired harness inference."""
from __future__ import annotations

import hashlib

import pytest

from atv_bench.eval.stats import (
    AnalysisError,
    Decision,
    EvaluationQualityEvidence,
    ObservationUnit,
    PublicationPolicy,
    TrialObservation,
    analyze_paired,
)
from atv_bench.eval.trial import (
    Budget,
    BudgetProfile,
    HarnessRef,
    HarnessStatus,
    InfrastructureStatus,
    ModelPolicyRef,
    TaskRef,
    TrialOutcome,
    TrialSpec,
)


def _trial_id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def _observation(
    task: str,
    harness: str,
    repetition: int,
    score: float | None,
    *,
    infrastructure: InfrastructureStatus = InfrastructureStatus.OK,
    harness_status: HarnessStatus = HarnessStatus.COMPLETED,
    model: str = "model-snapshot",
    budget: str = "equal-cost",
) -> TrialObservation:
    return TrialObservation(
        trial_id=_trial_id(
            task,
            harness,
            repetition,
            infrastructure.value,
            harness_status.value,
            model,
            budget,
        ),
        task_id=task,
        harness_id=harness,
        model_policy_id=model,
        budget_profile_id=budget,
        repetition=repetition,
        infrastructure_status=infrastructure,
        harness_status=harness_status,
        score=score,
    )


def _calibration_rows(differences: list[float], repetitions: int = 5):
    rows = []
    for task_index, difference in enumerate(differences):
        task = f"task-{task_index:03d}"
        midpoint = 0.5
        for repetition in range(repetitions):
            rows.append(
                _observation(
                    task,
                    "A",
                    repetition,
                    max(0.0, min(1.0, midpoint + difference / 2)),
                )
            )
            rows.append(
                _observation(
                    task,
                    "B",
                    repetition,
                    max(0.0, min(1.0, midpoint - difference / 2)),
                )
            )
    return rows


def _analyze(rows, *, margin=0.05, seed=7):
    return analyze_paired(
        rows,
        harness_a="A",
        harness_b="B",
        equivalence_margin=margin,
        confidence=0.95,
        bootstrap_samples=2_000,
        seed=seed,
        publication_policy=PublicationPolicy.simulation(),
    )


def _spec_observation(
    *,
    policy_digest: str = "3" * 64,
    max_cost_microusd: int = 500_000,
) -> TrialObservation:
    spec = TrialSpec(
        benchmark_release="ATV-2026.09",
        protocol_version="1",
        schedule_id="0" * 64,
        task=TaskRef("task-a", "1.0.0", "1" * 64),
        harness=HarnessRef("A", "1.0.0", "2" * 64),
        model_policy=ModelPolicyRef("policy", "1.0.0", policy_digest),
        budget_profile=BudgetProfile(
            "equal-cost",
            Budget(60, 20_000, 20, max_cost_microusd),
        ),
        repetition=0,
        schedule_seed=42,
    )
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id="4" * 64,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=1.0,
    )
    return TrialObservation.from_trial(spec, outcome)


def test_from_trial_uses_full_policy_and_budget_identities():
    baseline = _spec_observation()
    changed_policy = _spec_observation(policy_digest="5" * 64)
    changed_budget = _spec_observation(max_cost_microusd=600_000)

    assert baseline.model_policy_id.endswith("3" * 64)
    assert baseline.model_policy_id != changed_policy.model_policy_id
    assert baseline.budget_profile_id != changed_budget.budget_profile_id
    assert len(baseline.budget_profile_id.rsplit(":", 1)[1]) == 64


def test_calibration_recovers_known_positive_effect():
    result = _analyze(_calibration_rows([0.30] * 40))
    assert result.descriptive_decision is Decision.A_BETTER
    assert result.publication_decision is Decision.INCONCLUSIVE
    assert result.publication_eligible is False
    assert result.quality_gate_failures[0].code == "non_official_simulation_policy"
    assert result.mean_difference == pytest.approx(0.30)
    assert result.ci_low > 0.05
    assert result.direction_stability == 1.0
    assert result.paired_permutation_p_value < 0.01


def test_calibration_recovers_known_negative_effect():
    result = _analyze(_calibration_rows([-0.25] * 40))
    assert result.descriptive_decision is Decision.B_BETTER
    assert result.publication_decision is Decision.INCONCLUSIVE
    assert result.mean_difference == pytest.approx(-0.25)
    assert result.ci_high < -0.05
    assert result.direction_stability == 1.0


def test_calibration_declares_tight_null_effect_equivalent():
    result = _analyze(_calibration_rows([0.0] * 30))
    assert result.descriptive_decision is Decision.EQUIVALENT
    assert result.ci_low == 0.0
    assert result.ci_high == 0.0
    assert result.paired_permutation_p_value == 1.0


def test_underpowered_mixed_effect_is_inconclusive_not_a_winner():
    result = _analyze(_calibration_rows([0.30, -0.20, 0.30, -0.20]))
    assert result.descriptive_decision is Decision.INCONCLUSIVE
    assert result.ci_low < -0.05
    assert result.ci_high > 0.05


def test_bootstrap_is_deterministic_under_fixed_seed():
    rows = _calibration_rows([0.15, 0.20, 0.10, 0.25, 0.05] * 3)
    first = _analyze(rows, seed=911)
    second = _analyze(rows, seed=911)
    assert first.to_dict() == second.to_dict()


def test_tasks_not_trials_are_the_bootstrap_and_macro_average_unit():
    rows = []
    # One task has 100 repetitions and favors A.
    for repetition in range(100):
        rows.extend(
            (
                _observation("many-reps", "A", repetition, 1.0),
                _observation("many-reps", "B", repetition, 0.0),
            )
        )
    # One task has one repetition and favors B.
    rows.extend(
        (
            _observation("one-rep", "A", 0, 0.0),
            _observation("one-rep", "B", 0, 1.0),
        )
    )
    result = _analyze(rows)
    assert result.task_count == 2
    assert result.rankable_trial_count == 202
    assert result.mean_difference == pytest.approx(0.0)
    assert {effect.difference for effect in result.effects} == {-1.0, 1.0}


def test_infrastructure_failures_are_reported_but_never_scored_as_losses():
    rows = _calibration_rows([0.20] * 10)
    rows.extend(
        (
            _observation(
                "infra-only",
                "A",
                0,
                None,
                infrastructure=InfrastructureStatus.RUNNER_FAILED,
                harness_status=HarnessStatus.NOT_RUN,
            ),
            _observation(
                "infra-only",
                "B",
                0,
                None,
                infrastructure=InfrastructureStatus.GRADER_FAILED,
                harness_status=HarnessStatus.COMPLETED,
            ),
        )
    )
    result = _analyze(rows)
    assert result.descriptive_decision is Decision.A_BETTER
    assert result.task_count == 10
    assert len(result.infrastructure_exclusions) == 2
    assert result.rankable_trial_count == 100


def test_harness_crash_is_a_rankable_zero_not_infrastructure():
    rows = []
    for task_index in range(10):
        task = f"crash-{task_index}"
        rows.extend(
            (
                _observation(
                    task,
                    "A",
                    0,
                    0.0,
                    harness_status=HarnessStatus.CRASHED,
                ),
                _observation(task, "B", 0, 1.0),
            )
        )
    result = _analyze(rows)
    assert result.descriptive_decision is Decision.B_BETTER
    assert result.mean_difference == -1.0
    assert result.infrastructure_exclusions == ()


def test_duplicate_trial_ids_are_rejected():
    rows = _calibration_rows([0.1])
    rows.append(rows[0])
    with pytest.raises(AnalysisError, match="duplicate trial_id"):
        _analyze(rows)


def test_unpaired_harness_or_repetition_data_is_rejected():
    with pytest.raises(AnalysisError, match="lacks a rankable paired"):
        _analyze([_observation("task", "A", 0, 1.0)])

    rows = [
        _observation("task", "A", 0, 1.0),
        _observation("task", "A", 1, 1.0),
        _observation("task", "B", 0, 0.0),
    ]
    with pytest.raises(AnalysisError, match="unpaired repetition"):
        _analyze(rows)


def test_controlled_analysis_refuses_to_pool_models_or_budgets():
    rows = _calibration_rows([0.1])
    rows.extend(
        (
            _observation("other", "A", 0, 1.0, model="other-model"),
            _observation("other", "B", 0, 0.0, model="other-model"),
        )
    )
    with pytest.raises(AnalysisError, match="cannot pool"):
        _analyze(rows)


def test_nested_game_or_round_cannot_be_constructed_as_trial_evidence():
    with pytest.raises(AnalysisError, match="nested evidence"):
        TrialObservation(
            trial_id=_trial_id("game"),
            task_id="task",
            harness_id="A",
            model_policy_id="model",
            budget_profile_id="budget",
            repetition=0,
            infrastructure_status=InfrastructureStatus.OK,
            harness_status=HarnessStatus.COMPLETED,
            score=1.0,
            unit=ObservationUnit.GAME,
        )


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_scores_are_rejected(score):
    with pytest.raises(AnalysisError, match="score"):
        _observation("task", "A", 0, score)


def test_infrastructure_observation_cannot_carry_a_score():
    with pytest.raises(AnalysisError, match="excluded"):
        _observation(
            "task",
            "A",
            0,
            0.0,
            infrastructure=InfrastructureStatus.SETUP_FAILED,
            harness_status=HarnessStatus.NOT_RUN,
        )


def test_rankable_observation_cannot_claim_harness_never_ran():
    with pytest.raises(AnalysisError, match="not_run"):
        _observation(
            "task",
            "A",
            0,
            0.0,
            infrastructure=InfrastructureStatus.OK,
            harness_status=HarnessStatus.NOT_RUN,
        )


def _quality_evidence(
    task_count: int,
    *,
    failed_task: str | None = None,
    schedule_balanced: bool = True,
    replay_count: int = 10_000,
    nondeterministic_count: int = 0,
) -> EvaluationQualityEvidence:
    return EvaluationQualityEvidence(
        task_eligibility=tuple(
            (
                f"task-{index:03d}",
                f"task-{index:03d}" != failed_task,
            )
            for index in range(task_count)
        ),
        schedule_balanced=schedule_balanced,
        grader_replay_count=replay_count,
        grader_nondeterministic_count=nondeterministic_count,
    )


def test_default_official_policy_refuses_smoke_winner_but_keeps_estimate():
    rows = _calibration_rows([0.30, 0.30], repetitions=5)
    result = analyze_paired(
        rows,
        harness_a="A",
        harness_b="B",
        equivalence_margin=0.05,
        bootstrap_samples=1_000,
        seed=1,
    )
    assert result.descriptive_decision is Decision.A_BETTER
    assert result.publication_decision is Decision.INCONCLUSIVE
    assert result.decision is Decision.INCONCLUSIVE
    assert result.publication_eligible is False
    codes = {failure.code for failure in result.quality_gate_failures}
    assert "minimum_eligible_tasks" in codes
    assert "task_validation_evidence_missing" in codes
    assert "schedule_balance_evidence_missing" in codes
    assert "grader_nondeterminism_evidence_missing" in codes
    payload = result.to_dict()
    assert payload["publication_eligible"] is False
    assert payload["quality_gate_failures"]


def test_official_policy_can_publish_only_after_all_default_gates_pass():
    rows = _calibration_rows([0.20] * 50, repetitions=5)
    result = analyze_paired(
        rows,
        harness_a="A",
        harness_b="B",
        equivalence_margin=0.05,
        bootstrap_samples=2_000,
        seed=2,
        quality_evidence=_quality_evidence(50),
    )
    assert result.descriptive_decision is Decision.A_BETTER
    assert result.publication_decision is Decision.A_BETTER
    assert result.publication_eligible is True
    assert result.quality_gate_failures == ()


def test_official_quality_gates_report_every_failed_publication_condition():
    rows = _calibration_rows([0.20] * 50, repetitions=4)
    for index in range(10):
        rows.extend(
            (
                _observation(
                    f"infra-{index}",
                    "A",
                    0,
                    None,
                    infrastructure=InfrastructureStatus.RUNNER_FAILED,
                    harness_status=HarnessStatus.NOT_RUN,
                ),
                _observation(
                    f"infra-{index}",
                    "B",
                    0,
                    None,
                    infrastructure=InfrastructureStatus.GRADER_FAILED,
                    harness_status=HarnessStatus.COMPLETED,
                ),
            )
        )
    result = analyze_paired(
        rows,
        harness_a="A",
        harness_b="B",
        equivalence_margin=0.05,
        bootstrap_samples=1_000,
        seed=3,
        quality_evidence=_quality_evidence(
            50,
            failed_task="task-000",
            schedule_balanced=False,
            replay_count=1_000,
            nondeterministic_count=1,
        ),
    )
    assert result.publication_eligible is False
    assert result.publication_decision is Decision.INCONCLUSIVE
    codes = {failure.code for failure in result.quality_gate_failures}
    assert {
        "task_validation_failed",
        "minimum_eligible_tasks",
        "minimum_paired_trials_per_cell",
        "schedule_unbalanced",
        "grader_nondeterminism_rate",
        "infrastructure_error_rate",
    } <= codes
