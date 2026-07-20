"""Fresh-trial identity and paired scheduler credibility tests."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import FrozenInstanceError

import pytest

from atv_bench.eval.scheduler import build_paired_schedule
from atv_bench.eval.trial import (
    Budget,
    BudgetProfile,
    HarnessRef,
    HarnessStatus,
    InfrastructureStatus,
    ModelPolicyRef,
    TaskRef,
    TrialAttempt,
    TrialOutcome,
    TrialSpec,
)


def _digest(char: str) -> str:
    return char * 64


def _refs():
    tasks = (
        TaskRef("task-a", "1.0.0", _digest("a")),
        TaskRef("task-b", "1.0.0", _digest("b")),
    )
    harnesses = (
        HarnessRef("harness-a", "1.0.0", _digest("c")),
        HarnessRef("harness-b", "1.0.0", _digest("d")),
        HarnessRef("harness-c", "1.0.0", _digest("e")),
    )
    models = (ModelPolicyRef("model", "2026-07-19", _digest("f")),)
    budgets = (
        BudgetProfile(
            "small",
            Budget(
                wall_time_seconds=60,
                max_model_tokens=10_000,
                max_model_calls=20,
                max_cost_microusd=500_000,
            ),
        ),
    )
    return tasks, harnesses, models, budgets


def _schedule(*, seed: int = 7, repetitions: int = 6, workers=("w0", "w1")):
    tasks, harnesses, models, budgets = _refs()
    return build_paired_schedule(
        benchmark_release="ATV-2026.07-smoke",
        protocol_version="atv.trial/v1",
        tasks=tasks,
        harnesses=harnesses,
        model_policies=models,
        budget_profiles=budgets,
        repetitions=repetitions,
        seed=seed,
        workers=workers,
    )


def test_budget_and_trial_spec_are_immutable_and_content_addressed():
    tasks, harnesses, models, budgets = _refs()
    spec = TrialSpec(
        benchmark_release="ATV-2026.07-smoke",
        protocol_version="atv.trial/v1",
        schedule_id=_digest("1"),
        task=tasks[0],
        harness=harnesses[0],
        model_policy=models[0],
        budget_profile=budgets[0],
        repetition=0,
        schedule_seed=42,
    )
    assert len(spec.trial_id) == 64
    assert spec.trial_id == spec.trial_id
    with pytest.raises(FrozenInstanceError):
        spec.repetition = 1  # type: ignore[misc]
    changed = TrialSpec(
        benchmark_release=spec.benchmark_release,
        protocol_version=spec.protocol_version,
        schedule_id=spec.schedule_id,
        task=spec.task,
        harness=spec.harness,
        model_policy=spec.model_policy,
        budget_profile=spec.budget_profile,
        repetition=1,
        schedule_seed=spec.schedule_seed,
    )
    assert changed.trial_id != spec.trial_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wall_time_seconds": 0},
        {"max_model_tokens": 0},
        {"max_model_calls": 0},
        {"max_cost_microusd": 0},
    ],
)
def test_budget_rejects_unenforceable_nonpositive_limits(kwargs):
    values = {
        "wall_time_seconds": 1,
        "max_model_tokens": 1,
        "max_model_calls": 1,
        "max_cost_microusd": 1,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        Budget(**values)


def test_attempt_identity_binds_fresh_workspace_and_attempt_number():
    scheduled = _schedule(repetitions=1)[0]
    original = scheduled.attempt
    retry = TrialAttempt(
        spec=original.spec,
        attempt_number=2,
        fresh_nonce=_digest("9"),
    )
    assert retry.spec.trial_id == original.spec.trial_id
    assert retry.attempt_id != original.attempt_id
    assert retry.workspace_id != original.workspace_id


def test_retry_preserves_assignment_but_requires_new_attempt_and_nonce():
    scheduled = _schedule(repetitions=1)[0]
    retried = scheduled.retry(attempt_number=2, fresh_nonce=_digest("9"))
    assert retried.block_id == scheduled.block_id
    assert retried.order_index == scheduled.order_index
    assert retried.worker_id == scheduled.worker_id
    assert retried.spec == scheduled.spec
    assert retried.attempt.attempt_id != scheduled.attempt.attempt_id
    with pytest.raises(ValueError, match="increase"):
        scheduled.retry(attempt_number=1, fresh_nonce=_digest("8"))
    with pytest.raises(ValueError, match="new fresh_nonce"):
        scheduled.retry(
            attempt_number=2,
            fresh_nonce=scheduled.attempt.fresh_nonce,
        )


def test_outcome_separates_infrastructure_failures_from_harness_losses():
    attempt = _schedule(repetitions=1)[0].attempt
    infra = TrialOutcome(
        trial_id=attempt.spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.GRADER_FAILED,
        harness_status=HarnessStatus.COMPLETED,
        score=None,
        reason_code="grader_process_crashed",
    )
    assert infra.rankable is False
    assert infra.retryable_infrastructure_failure is True

    harness_failure = TrialOutcome(
        trial_id=attempt.spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.CRASHED,
        score=0.0,
        reason_code="exit_17",
    )
    assert harness_failure.rankable is True
    assert harness_failure.retryable_infrastructure_failure is False


def test_outcome_invariants_prevent_accidental_infrastructure_losses():
    attempt = _schedule(repetitions=1)[0].attempt
    with pytest.raises(ValueError, match="must not carry"):
        TrialOutcome(
            trial_id=attempt.spec.trial_id,
            attempt_id=attempt.attempt_id,
            infrastructure_status=InfrastructureStatus.RUNNER_FAILED,
            harness_status=HarnessStatus.NOT_RUN,
            score=0.0,
            reason_code="worker_lost",
        )
    with pytest.raises(ValueError, match="rankable harness failures"):
        TrialOutcome(
            trial_id=attempt.spec.trial_id,
            attempt_id=attempt.attempt_id,
            infrastructure_status=InfrastructureStatus.OK,
            harness_status=HarnessStatus.TIMED_OUT,
            score=0.25,
            reason_code="timeout",
        )
    with pytest.raises(ValueError, match="trusted grade score"):
        TrialOutcome(
            trial_id=attempt.spec.trial_id,
            attempt_id=attempt.attempt_id,
            infrastructure_status=InfrastructureStatus.OK,
            harness_status=HarnessStatus.COMPLETED,
            score=None,
        )


def test_schedule_is_fully_crossed_and_paired_by_block():
    schedule = _schedule(repetitions=4)
    # 2 tasks x 1 model x 1 budget x 4 repetitions x 3 harnesses.
    assert len(schedule) == 24
    by_block = defaultdict(list)
    for item in schedule:
        by_block[item.block_id].append(item)
    assert len(by_block) == 8
    expected = {"harness-a", "harness-b", "harness-c"}
    for block in by_block.values():
        assert {item.spec.harness.id for item in block} == expected
        assert sorted(item.order_index for item in block) == [0, 1, 2]
        assert len({item.spec.task.id for item in block}) == 1
        assert len({item.spec.repetition for item in block}) == 1


def test_schedule_is_deterministic_for_a_fixed_seed():
    first = [item.to_dict() for item in _schedule(seed=91)]
    second = [item.to_dict() for item in _schedule(seed=91)]
    assert first == second


def test_schedule_changes_with_seed():
    first = [item.spec.trial_id for item in _schedule(seed=91)]
    second = [item.spec.trial_id for item in _schedule(seed=92)]
    assert first != second


def test_rotation_balances_every_harness_across_every_order_position():
    schedule = _schedule(repetitions=6)
    positions = defaultdict(Counter)
    for item in schedule:
        key = item.spec.task.id
        positions[key][(item.spec.harness.id, item.order_index)] += 1
    for counts in positions.values():
        assert set(counts.values()) == {2}


def test_worker_assignments_are_deterministic_and_balanced():
    schedule = _schedule(repetitions=5, workers=("w0", "w1", "w2", "w3"))
    counts = Counter(item.worker_id for item in schedule)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert [item.worker_id for item in schedule] == [
        item.worker_id for item in _schedule(repetitions=5, workers=("w0", "w1", "w2", "w3"))
    ]


def test_every_planned_trial_has_a_distinct_trial_attempt_and_workspace():
    schedule = _schedule(repetitions=8)
    assert len({item.spec.trial_id for item in schedule}) == len(schedule)
    assert len({item.attempt.attempt_id for item in schedule}) == len(schedule)
    assert len({item.attempt.workspace_id for item in schedule}) == len(schedule)


def test_duplicate_entities_are_rejected_before_scheduling():
    tasks, harnesses, models, budgets = _refs()
    with pytest.raises(ValueError, match="duplicate harnesses"):
        build_paired_schedule(
            benchmark_release="ATV-2026.07-smoke",
            protocol_version="atv.trial/v1",
            tasks=tasks,
            harnesses=(harnesses[0], harnesses[0]),
            model_policies=models,
            budget_profiles=budgets,
            repetitions=1,
            seed=1,
            workers=("w0",),
        )
