"""Paired task-clustered inference for fresh harness trials."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

from ._canonical import require_sha256
from .trial import HarnessStatus, InfrastructureStatus, TrialOutcome, TrialSpec


class AnalysisError(ValueError):
    """Observations cannot support the requested controlled comparison."""


class ObservationUnit(str, Enum):
    TRIAL = "trial"
    GAME = "game"
    ROUND = "round"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class TrialObservation:
    """One independent fresh harness execution and trusted task score."""

    trial_id: str
    task_id: str
    harness_id: str
    model_policy_id: str
    budget_profile_id: str
    repetition: int
    infrastructure_status: InfrastructureStatus
    harness_status: HarnessStatus
    score: float | None
    unit: ObservationUnit = ObservationUnit.TRIAL

    def __post_init__(self) -> None:
        require_sha256(self.trial_id, field="trial id")
        for field in (
            "task_id",
            "harness_id",
            "model_policy_id",
            "budget_profile_id",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise AnalysisError(f"{field} must be non-empty")
        if not isinstance(self.repetition, int) or isinstance(self.repetition, bool):
            raise AnalysisError("repetition must be an integer")
        if self.repetition < 0:
            raise AnalysisError("repetition must be non-negative")
        if self.unit is not ObservationUnit.TRIAL:
            raise AnalysisError(
                "games, rounds, and tests are nested evidence, not harness observations"
            )
        if not isinstance(self.infrastructure_status, InfrastructureStatus):
            raise AnalysisError(
                "infrastructure_status must be InfrastructureStatus"
            )
        if not isinstance(self.harness_status, HarnessStatus):
            raise AnalysisError("harness_status must be HarnessStatus")
        if self.infrastructure_status is InfrastructureStatus.OK:
            if self.score is None:
                raise AnalysisError("rankable trials require a score")
            if self.harness_status is HarnessStatus.NOT_RUN:
                raise AnalysisError("infrastructure OK cannot leave the harness not_run")
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise AnalysisError("score must be numeric")
            if not math.isfinite(float(self.score)) or not 0.0 <= float(self.score) <= 1.0:
                raise AnalysisError("score must be finite and between 0 and 1")
            object.__setattr__(self, "score", float(self.score))
            if self.harness_status is not HarnessStatus.COMPLETED and self.score != 0.0:
                raise AnalysisError("harness failures must remain rankable zeros")
        elif self.score is not None:
            raise AnalysisError("infrastructure failures must be excluded, not scored")

    @property
    def rankable(self) -> bool:
        return self.infrastructure_status is InfrastructureStatus.OK

    @classmethod
    def from_trial(
        cls,
        spec: TrialSpec,
        outcome: TrialOutcome,
    ) -> "TrialObservation":
        if outcome.trial_id != spec.trial_id:
            raise AnalysisError("TrialSpec and TrialOutcome identifiers differ")
        return cls(
            trial_id=spec.trial_id,
            task_id=spec.task.id,
            harness_id=spec.harness.id,
            model_policy_id=spec.model_policy.analysis_id,
            budget_profile_id=spec.budget_profile.analysis_id,
            repetition=spec.repetition,
            infrastructure_status=outcome.infrastructure_status,
            harness_status=outcome.harness_status,
            score=outcome.score,
        )


@dataclass(frozen=True, slots=True)
class PairedTaskEffect:
    task_id: str
    repetitions: tuple[int, ...]
    mean_a: float
    mean_b: float
    difference: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repetitions": list(self.repetitions),
            "mean_a": self.mean_a,
            "mean_b": self.mean_b,
            "difference": self.difference,
        }


class Decision(str, Enum):
    A_BETTER = "a_better"
    B_BETTER = "b_better"
    EQUIVALENT = "equivalent"
    INCONCLUSIVE = "inconclusive"


class AnalysisMode(str, Enum):
    OFFICIAL = "official"
    SIMULATION = "simulation"


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    """Preregistered gates for an official comparison claim."""

    mode: AnalysisMode = AnalysisMode.OFFICIAL
    min_eligible_tasks: int = 50
    min_paired_trials_per_cell: int = 5
    max_infrastructure_error_rate: float = 0.02
    max_grader_nondeterminism_rate: float = 0.001
    require_schedule_balance: bool = True
    require_all_tasks_validated: bool = True

    def __post_init__(self) -> None:
        if self.min_eligible_tasks <= 0:
            raise AnalysisError("min_eligible_tasks must be positive")
        if self.min_paired_trials_per_cell <= 0:
            raise AnalysisError("min_paired_trials_per_cell must be positive")
        if not 0.0 < self.max_infrastructure_error_rate < 1.0:
            raise AnalysisError("max_infrastructure_error_rate must be in (0, 1)")
        if not 0.0 < self.max_grader_nondeterminism_rate < 1.0:
            raise AnalysisError(
                "max_grader_nondeterminism_rate must be in (0, 1)"
            )

    @classmethod
    def official(cls) -> "PublicationPolicy":
        return cls()

    @classmethod
    def simulation(cls) -> "PublicationPolicy":
        """Non-official policy for synthetic calibration only."""

        return cls(
            mode=AnalysisMode.SIMULATION,
            min_eligible_tasks=1,
            min_paired_trials_per_cell=1,
            require_schedule_balance=False,
            require_all_tasks_validated=False,
        )


@dataclass(frozen=True, slots=True)
class EvaluationQualityEvidence:
    """Controller-observed evidence consumed by official quality gates."""

    task_eligibility: tuple[tuple[str, bool], ...]
    schedule_balanced: bool | None
    grader_replay_count: int | None
    grader_nondeterministic_count: int | None

    def __post_init__(self) -> None:
        task_ids = [task_id for task_id, _ in self.task_eligibility]
        if len(set(task_ids)) != len(task_ids):
            raise AnalysisError("quality evidence contains duplicate task ids")
        if self.grader_replay_count is not None and self.grader_replay_count < 0:
            raise AnalysisError("grader_replay_count must be non-negative")
        if (
            self.grader_nondeterministic_count is not None
            and self.grader_nondeterministic_count < 0
        ):
            raise AnalysisError(
                "grader_nondeterministic_count must be non-negative"
            )
        if (
            self.grader_replay_count is not None
            and self.grader_nondeterministic_count is not None
            and self.grader_nondeterministic_count > self.grader_replay_count
        ):
            raise AnalysisError(
                "grader_nondeterministic_count exceeds grader_replay_count"
            )

    @classmethod
    def from_task_reports(
        cls,
        reports: Iterable[Any],
        *,
        schedule_balanced: bool,
    ) -> "EvaluationQualityEvidence":
        materialized = tuple(reports)
        return cls(
            task_eligibility=tuple(
                (str(report.task_id), bool(report.eligible))
                for report in materialized
            ),
            schedule_balanced=schedule_balanced,
            grader_replay_count=sum(
                int(report.grader_replay_count) for report in materialized
            ),
            grader_nondeterministic_count=sum(
                int(report.grader_nondeterministic_count)
                for report in materialized
            ),
        )

    @classmethod
    def from_task_reports_and_schedule(
        cls,
        reports: Iterable[Any],
        scheduled_trials: Iterable[Any],
        *,
        harness_a: str,
        harness_b: str,
    ) -> "EvaluationQualityEvidence":
        materialized = tuple(reports)
        return cls(
            task_eligibility=tuple(
                (str(report.task_id), bool(report.eligible))
                for report in materialized
            ),
            schedule_balanced=_paired_schedule_is_balanced(
                scheduled_trials,
                harness_a=harness_a,
                harness_b=harness_b,
            ),
            grader_replay_count=sum(
                int(report.grader_replay_count) for report in materialized
            ),
            grader_nondeterministic_count=sum(
                int(report.grader_nondeterministic_count)
                for report in materialized
            ),
        )

    @property
    def task_eligibility_map(self) -> dict[str, bool]:
        return dict(self.task_eligibility)


@dataclass(frozen=True, slots=True)
class QualityGateFailure:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _paired_schedule_is_balanced(
    scheduled_trials: Iterable[Any],
    *,
    harness_a: str,
    harness_b: str,
) -> bool:
    selected = tuple(
        item
        for item in scheduled_trials
        if item.spec.harness.id in {harness_a, harness_b}
    )
    if not selected:
        return False
    by_block: dict[str, list[Any]] = {}
    positions: dict[tuple[str, str], dict[int, int]] = {}
    for item in selected:
        by_block.setdefault(str(item.block_id), []).append(item)
        key = (str(item.spec.task.id), str(item.spec.harness.id))
        position_counts = positions.setdefault(key, {})
        position_counts[item.order_index] = (
            position_counts.get(item.order_index, 0) + 1
        )
    for block in by_block.values():
        if {item.spec.harness.id for item in block} != {harness_a, harness_b}:
            return False
        if len(block) != 2 or len({item.order_index for item in block}) != 2:
            return False
    for task_id in {item.spec.task.id for item in selected}:
        for harness_id in (harness_a, harness_b):
            counts = positions.get((task_id, harness_id))
            if not counts or set(counts) - {0, 1}:
                return False
            values = [counts.get(0, 0), counts.get(1, 0)]
            if max(values) - min(values) > 1:
                return False
    return True


@dataclass(frozen=True, slots=True)
class InfrastructureExclusion:
    trial_id: str
    task_id: str
    harness_id: str
    status: InfrastructureStatus

    def to_dict(self) -> dict[str, str]:
        return {
            "trial_id": self.trial_id,
            "task_id": self.task_id,
            "harness_id": self.harness_id,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class PairedAnalysis:
    harness_a: str
    harness_b: str
    model_policy_id: str
    budget_profile_id: str
    effects: tuple[PairedTaskEffect, ...]
    mean_difference: float
    confidence: float
    ci_low: float
    ci_high: float
    equivalence_margin: float
    descriptive_decision: Decision
    publication_decision: Decision
    publication_eligible: bool
    quality_gate_failures: tuple[QualityGateFailure, ...]
    analysis_mode: AnalysisMode
    paired_permutation_p_value: float
    direction_stability: float
    bootstrap_samples: int
    rankable_trial_count: int
    infrastructure_exclusions: tuple[InfrastructureExclusion, ...]

    @property
    def task_count(self) -> int:
        return len(self.effects)

    @property
    def decision(self) -> Decision:
        """Safe compatibility alias: never expose an ineligible winner."""

        return self.publication_decision

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.paired-analysis/v1",
            "harness_a": self.harness_a,
            "harness_b": self.harness_b,
            "model_policy_id": self.model_policy_id,
            "budget_profile_id": self.budget_profile_id,
            "task_count": self.task_count,
            "rankable_trial_count": self.rankable_trial_count,
            "infrastructure_exclusion_count": len(
                self.infrastructure_exclusions
            ),
            "effects": [effect.to_dict() for effect in self.effects],
            "mean_difference": self.mean_difference,
            "confidence": self.confidence,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "equivalence_margin": self.equivalence_margin,
            "descriptive_decision": self.descriptive_decision.value,
            "publication_decision": self.publication_decision.value,
            "publication_eligible": self.publication_eligible,
            "analysis_mode": self.analysis_mode.value,
            "quality_gate_failures": [
                failure.to_dict() for failure in self.quality_gate_failures
            ],
            "paired_permutation_p_value": self.paired_permutation_p_value,
            "direction_stability": self.direction_stability,
            "bootstrap_samples": self.bootstrap_samples,
            "infrastructure_exclusions": [
                item.to_dict() for item in self.infrastructure_exclusions
            ],
        }


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise AnalysisError("cannot compute a percentile of an empty sample")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(
        sorted_values[lower] * (1.0 - fraction)
        + sorted_values[upper] * fraction
    )


def _paired_permutation_p_value(
    differences: Sequence[float],
    *,
    seed: int,
    monte_carlo_samples: int = 50_000,
) -> float:
    observed = abs(sum(differences) / len(differences))
    tolerance = 1e-15
    count = len(differences)
    extreme = 0

    if count <= 20:
        total = 1 << count
        for bits in range(total):
            candidate = 0.0
            for index, difference in enumerate(differences):
                candidate += difference if bits & (1 << index) else -difference
            candidate = abs(candidate / count)
            if candidate + tolerance >= observed:
                extreme += 1
        return extreme / total

    rng = random.Random(seed)
    total = monte_carlo_samples
    for _ in range(total):
        candidate = sum(
            difference if rng.getrandbits(1) else -difference
            for difference in differences
        )
        if abs(candidate / count) + tolerance >= observed:
            extreme += 1
    return (extreme + 1) / (total + 1)


def _decision(ci_low: float, ci_high: float, margin: float) -> Decision:
    if ci_low > margin:
        return Decision.A_BETTER
    if ci_high < -margin:
        return Decision.B_BETTER
    if ci_low >= -margin and ci_high <= margin:
        return Decision.EQUIVALENT
    return Decision.INCONCLUSIVE


def _publication_gate_failures(
    *,
    policy: PublicationPolicy,
    quality_evidence: EvaluationQualityEvidence | None,
    effects: Sequence[PairedTaskEffect],
    selected_trial_count: int,
    infrastructure_exclusion_count: int,
) -> tuple[QualityGateFailure, ...]:
    if policy.mode is AnalysisMode.SIMULATION:
        return (
            QualityGateFailure(
                code="non_official_simulation_policy",
                message="simulation analyses are descriptive and never publishable",
            ),
        )

    failures: list[QualityGateFailure] = []
    task_ids = {effect.task_id for effect in effects}
    eligibility: dict[str, bool] = {}
    if quality_evidence is None:
        if policy.require_all_tasks_validated:
            failures.append(
                QualityGateFailure(
                    code="task_validation_evidence_missing",
                    message="official publication requires task-validation evidence",
                )
            )
        failures.append(
            QualityGateFailure(
                code="grader_nondeterminism_evidence_missing",
                message="official publication requires grader replay evidence",
            )
        )
        if policy.require_schedule_balance:
            failures.append(
                QualityGateFailure(
                    code="schedule_balance_evidence_missing",
                    message="official publication requires schedule-balance evidence",
                )
            )
    else:
        eligibility = quality_evidence.task_eligibility_map
        if policy.require_all_tasks_validated:
            missing = sorted(task_ids - eligibility.keys())
            failed = sorted(
                task_id
                for task_id in task_ids
                if eligibility.get(task_id) is False
            )
            if missing:
                failures.append(
                    QualityGateFailure(
                        code="task_validation_evidence_missing",
                        message="missing validation evidence for: " + ", ".join(missing),
                    )
                )
            if failed:
                failures.append(
                    QualityGateFailure(
                        code="task_validation_failed",
                        message="ineligible tasks present: " + ", ".join(failed),
                    )
                )
        if policy.require_schedule_balance:
            if quality_evidence.schedule_balanced is None:
                failures.append(
                    QualityGateFailure(
                        code="schedule_balance_evidence_missing",
                        message="schedule balance was not measured",
                    )
                )
            elif not quality_evidence.schedule_balanced:
                failures.append(
                    QualityGateFailure(
                        code="schedule_unbalanced",
                        message="paired execution order is not balanced",
                    )
                )
        replay_count = quality_evidence.grader_replay_count
        nondeterministic = quality_evidence.grader_nondeterministic_count
        if replay_count is None or nondeterministic is None or replay_count <= 0:
            failures.append(
                QualityGateFailure(
                    code="grader_nondeterminism_evidence_missing",
                    message="grader replay evidence is absent or empty",
                )
            )
        else:
            nondeterminism_rate = nondeterministic / replay_count
            if nondeterminism_rate >= policy.max_grader_nondeterminism_rate:
                failures.append(
                    QualityGateFailure(
                        code="grader_nondeterminism_rate",
                        message=(
                            f"grader nondeterminism rate {nondeterminism_rate:.6f} "
                            f"must be < {policy.max_grader_nondeterminism_rate:.6f}"
                        ),
                    )
                )

    eligible_task_count = sum(
        1
        for task_id in task_ids
        if eligibility.get(task_id, not policy.require_all_tasks_validated)
    )
    if eligible_task_count < policy.min_eligible_tasks:
        failures.append(
            QualityGateFailure(
                code="minimum_eligible_tasks",
                message=(
                    f"eligible task count {eligible_task_count} must be >= "
                    f"{policy.min_eligible_tasks}"
                ),
            )
        )

    undersampled = [
        effect.task_id
        for effect in effects
        if len(effect.repetitions) < policy.min_paired_trials_per_cell
    ]
    if undersampled:
        failures.append(
            QualityGateFailure(
                code="minimum_paired_trials_per_cell",
                message=(
                    f"{len(undersampled)} task cells have fewer than "
                    f"{policy.min_paired_trials_per_cell} paired fresh trials"
                ),
            )
        )

    infrastructure_rate = (
        infrastructure_exclusion_count / selected_trial_count
        if selected_trial_count
        else 1.0
    )
    if infrastructure_rate >= policy.max_infrastructure_error_rate:
        failures.append(
            QualityGateFailure(
                code="infrastructure_error_rate",
                message=(
                    f"infrastructure error rate {infrastructure_rate:.6f} "
                    f"must be < {policy.max_infrastructure_error_rate:.6f}"
                ),
            )
        )
    return tuple(failures)


def analyze_paired(
    observations: Iterable[TrialObservation],
    *,
    harness_a: str,
    harness_b: str,
    equivalence_margin: float,
    confidence: float = 0.95,
    bootstrap_samples: int = 10_000,
    seed: int = 0,
    publication_policy: PublicationPolicy | None = None,
    quality_evidence: EvaluationQualityEvidence | None = None,
) -> PairedAnalysis:
    """Analyze a controlled pair by resampling tasks, never nested outcomes."""

    if harness_a == harness_b:
        raise AnalysisError("harness_a and harness_b must differ")
    if not 0.0 < equivalence_margin < 1.0:
        raise AnalysisError("equivalence_margin must be in (0, 1)")
    if not 0.5 < confidence < 1.0:
        raise AnalysisError("confidence must be in (0.5, 1)")
    if (
        not isinstance(bootstrap_samples, int)
        or isinstance(bootstrap_samples, bool)
        or bootstrap_samples < 100
    ):
        raise AnalysisError("bootstrap_samples must be an integer of at least 100")

    rows = tuple(observations)
    if not rows:
        raise AnalysisError("at least one observation is required")
    trial_ids = [row.trial_id for row in rows]
    if len(set(trial_ids)) != len(trial_ids):
        raise AnalysisError("duplicate trial_id would double-count evidence")

    selected = tuple(
        row for row in rows if row.harness_id in {harness_a, harness_b}
    )
    if not selected:
        raise AnalysisError("no observations match the requested harnesses")

    cells = {
        (row.model_policy_id, row.budget_profile_id) for row in selected
    }
    if len(cells) != 1:
        raise AnalysisError(
            "controlled analysis cannot pool model policies or budget profiles"
        )
    model_policy_id, budget_profile_id = next(iter(cells))

    exclusions = tuple(
        InfrastructureExclusion(
            trial_id=row.trial_id,
            task_id=row.task_id,
            harness_id=row.harness_id,
            status=row.infrastructure_status,
        )
        for row in selected
        if not row.rankable
    )
    rankable = tuple(row for row in selected if row.rankable)
    if not rankable:
        raise AnalysisError("all matching trials were infrastructure failures")

    grouped: dict[str, dict[str, dict[int, float]]] = {}
    for row in rankable:
        task_group = grouped.setdefault(row.task_id, {})
        harness_group = task_group.setdefault(row.harness_id, {})
        if row.repetition in harness_group:
            raise AnalysisError(
                f"duplicate repetition for {row.task_id}/{row.harness_id}: "
                f"{row.repetition}"
            )
        assert row.score is not None
        harness_group[row.repetition] = row.score

    effects: list[PairedTaskEffect] = []
    for task_id in sorted(grouped):
        task_group = grouped[task_id]
        if set(task_group) != {harness_a, harness_b}:
            raise AnalysisError(
                f"task {task_id} lacks a rankable paired harness result"
            )
        repetitions_a = set(task_group[harness_a])
        repetitions_b = set(task_group[harness_b])
        if repetitions_a != repetitions_b:
            raise AnalysisError(
                f"task {task_id} has unpaired repetition sets after "
                "infrastructure exclusions"
            )
        repetitions = tuple(sorted(repetitions_a))
        scores_a = [task_group[harness_a][item] for item in repetitions]
        scores_b = [task_group[harness_b][item] for item in repetitions]
        mean_a = sum(scores_a) / len(scores_a)
        mean_b = sum(scores_b) / len(scores_b)
        effects.append(
            PairedTaskEffect(
                task_id=task_id,
                repetitions=repetitions,
                mean_a=mean_a,
                mean_b=mean_b,
                difference=mean_a - mean_b,
            )
        )

    differences = tuple(effect.difference for effect in effects)
    mean_difference = sum(differences) / len(differences)
    rng = random.Random(seed)
    bootstrap: list[float] = []
    for _ in range(bootstrap_samples):
        sampled = rng.choices(differences, k=len(differences))
        bootstrap.append(sum(sampled) / len(sampled))
    bootstrap.sort()
    alpha = 1.0 - confidence
    ci_low = _percentile(bootstrap, alpha / 2.0)
    ci_high = _percentile(bootstrap, 1.0 - alpha / 2.0)
    descriptive_decision = _decision(ci_low, ci_high, equivalence_margin)

    if descriptive_decision is Decision.A_BETTER:
        stable = sum(value > equivalence_margin for value in bootstrap)
    elif descriptive_decision is Decision.B_BETTER:
        stable = sum(value < -equivalence_margin for value in bootstrap)
    elif descriptive_decision is Decision.EQUIVALENT:
        stable = sum(
            -equivalence_margin <= value <= equivalence_margin
            for value in bootstrap
        )
    else:
        stable = max(
            sum(value > equivalence_margin for value in bootstrap),
            sum(value < -equivalence_margin for value in bootstrap),
            sum(
                -equivalence_margin <= value <= equivalence_margin
                for value in bootstrap
            ),
        )

    active_policy = publication_policy or PublicationPolicy.official()
    quality_gate_failures = _publication_gate_failures(
        policy=active_policy,
        quality_evidence=quality_evidence,
        effects=effects,
        selected_trial_count=len(selected),
        infrastructure_exclusion_count=len(exclusions),
    )
    publication_eligible = (
        active_policy.mode is AnalysisMode.OFFICIAL
        and not quality_gate_failures
    )
    publication_decision = (
        descriptive_decision
        if publication_eligible
        else Decision.INCONCLUSIVE
    )

    return PairedAnalysis(
        harness_a=harness_a,
        harness_b=harness_b,
        model_policy_id=model_policy_id,
        budget_profile_id=budget_profile_id,
        effects=tuple(effects),
        mean_difference=mean_difference,
        confidence=confidence,
        ci_low=ci_low,
        ci_high=ci_high,
        equivalence_margin=equivalence_margin,
        descriptive_decision=descriptive_decision,
        publication_decision=publication_decision,
        publication_eligible=publication_eligible,
        quality_gate_failures=quality_gate_failures,
        analysis_mode=active_policy.mode,
        paired_permutation_p_value=_paired_permutation_p_value(
            differences, seed=seed
        ),
        direction_stability=stable / bootstrap_samples,
        bootstrap_samples=bootstrap_samples,
        rankable_trial_count=len(rankable),
        infrastructure_exclusions=exclusions,
    )
