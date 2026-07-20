"""Smoke-only evaluation pipeline from task audit through descriptive estimate."""
from __future__ import annotations

import json
from pathlib import Path

from atv_bench.eval.bundle import ContentAddressedStore, TrialBundle
from atv_bench.eval.grader import (
    ControllerAssertedLifecycleReceipt,
    FileAssertionsGrader,
)
from atv_bench.eval.scheduler import build_paired_schedule
from atv_bench.eval.stats import (
    Decision,
    EvaluationQualityEvidence,
    TrialObservation,
    analyze_paired,
)
from atv_bench.eval.tasks import (
    TaskGate,
    TaskPackageValidator,
    load_task_suite,
)
from atv_bench.eval.trial import (
    Budget,
    BudgetProfile,
    HarnessRef,
    HarnessStatus,
    InfrastructureStatus,
    ModelPolicyRef,
    TrialOutcome,
)


ROOT = Path(__file__).resolve().parents[1]


def test_smoke_evaluation_pipeline_is_reproducible_but_not_publishable(tmp_path):
    packages = load_task_suite(sorted((ROOT / "tasks" / "smoke").iterdir()))
    package_by_id = {package.id: package for package in packages}

    # Task eligibility is established before descriptive analysis.
    audits = []
    for package in packages:
        audit = TaskPackageValidator().validate(
            package, FileAssertionsGrader.from_task(package)
        )
        assert audit.eligible is True
        audits.append(audit)

    schedule = build_paired_schedule(
        benchmark_release="ATV-2026.07-smoke",
        protocol_version="atv.trial/v1",
        tasks=tuple(package.task_ref for package in packages),
        harnesses=(
            HarnessRef("A", "1.0.0", "a" * 64),
            HarnessRef("B", "1.0.0", "b" * 64),
        ),
        model_policies=(
            ModelPolicyRef("fixed-model", "2026-07-19", "c" * 64),
        ),
        budget_profiles=(
            BudgetProfile(
                "equal-cost",
                Budget(
                    wall_time_seconds=60,
                    max_model_tokens=10_000,
                    max_model_calls=20,
                    max_cost_microusd=500_000,
                ),
            ),
        ),
        repetitions=5,
        seed=20260719,
        workers=("worker-0", "worker-1"),
    )
    assert len(schedule) == 20
    assert len({item.attempt.workspace_id for item in schedule}) == 20

    store = ContentAddressedStore(tmp_path / "evidence")
    bundle_digests: list[str] = []
    observations: list[TrialObservation] = []
    lifecycle_receipt = ControllerAssertedLifecycleReceipt.completed(
        controller_id="smoke-test-controller"
    )
    for assignment in schedule:
        package = package_by_id[assignment.spec.task.id]
        cases = {
            gate: path
            for gate, _, path, _ in package.validation_cases()
        }
        output_tree = (
            cases[TaskGate.ORACLE]
            if assignment.spec.harness.id == "A"
            else cases[TaskGate.MUTATION]
        )
        grade = FileAssertionsGrader.from_task(package).grade(
            package,
            output_tree,
            lifecycle_receipt=lifecycle_receipt,
        )
        assert grade.official_verified is False
        outcome = TrialOutcome(
            trial_id=assignment.spec.trial_id,
            attempt_id=assignment.attempt.attempt_id,
            infrastructure_status=InfrastructureStatus.OK,
            harness_status=HarnessStatus.COMPLETED,
            score=grade.score,
        )
        event = {
            "type": "result",
            "trial_id": assignment.spec.trial_id,
            "output_tree_digest": grade.output_tree_digest,
        }
        bundle = TrialBundle.create(
            store,
            spec=assignment.spec,
            attempt=assignment.attempt,
            outcome=outcome,
            grade=grade,
            output_tree=output_tree,
            artifacts={
                "logs/events.jsonl": (
                    json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()
            },
            runner_metadata={
                "worker_id": assignment.worker_id,
                "order_index": assignment.order_index,
            },
        )
        bundle.verify()
        assert TrialBundle.load(store, bundle.digest).digest == bundle.digest
        bundle_digests.append(bundle.digest)
        observations.append(TrialObservation.from_trial(assignment.spec, outcome))

    # Repeated artifacts do not collapse independent trial evidence because each
    # manifest binds the fresh trial, attempt, schedule position, and status.
    assert len(set(bundle_digests)) == len(bundle_digests)

    quality_evidence = EvaluationQualityEvidence.from_task_reports_and_schedule(
        audits,
        schedule,
        harness_a="A",
        harness_b="B",
    )
    analysis = analyze_paired(
        observations,
        harness_a="A",
        harness_b="B",
        equivalence_margin=0.10,
        confidence=0.95,
        bootstrap_samples=5_000,
        seed=20260719,
        quality_evidence=quality_evidence,
    )
    assert analysis.task_count == 2
    assert analysis.rankable_trial_count == 20
    assert analysis.infrastructure_exclusions == ()
    assert analysis.ci_low > 0.10
    assert analysis.descriptive_decision is Decision.A_BETTER
    assert analysis.publication_decision is Decision.INCONCLUSIVE
    assert analysis.decision is Decision.INCONCLUSIVE
    assert analysis.publication_eligible is False
    assert {
        failure.code for failure in analysis.quality_gate_failures
    } == {"minimum_eligible_tasks"}

    # The report itself is deterministic for a fixed accepted trial set and seed.
    repeated = analyze_paired(
        reversed(observations),
        harness_a="A",
        harness_b="B",
        equivalence_margin=0.10,
        confidence=0.95,
        bootstrap_samples=5_000,
        seed=20260719,
        quality_evidence=quality_evidence,
    )
    assert repeated.to_dict() == analysis.to_dict()
