"""Trusted post-run grader and immutable bundle integrity tests."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

import atv_bench.eval._canonical as eval_canonical
from atv_bench.eval.bundle import (
    BundleIntegrityError,
    ContentAddressedStore,
    TrialBundle,
)
from atv_bench.eval._canonical import TreeLimits, canonical_json_bytes
from atv_bench.eval.grader import (
    ControllerAssertedLifecycleReceipt,
    FileAssertionsGrader,
    GraderError,
    GradingStateError,
    GradingTrustTier,
)
from atv_bench.eval.tasks import TaskGate, TaskPackage
from atv_bench.eval.trial import (
    Budget,
    BudgetProfile,
    HarnessRef,
    HarnessStatus,
    InfrastructureStatus,
    ModelPolicyRef,
    TrialAttempt,
    TrialOutcome,
    TrialSpec,
)


SMOKE_ROOT = Path(__file__).resolve().parents[1] / "tasks" / "smoke"


def _package() -> TaskPackage:
    return TaskPackage.load(SMOKE_ROOT / "repair_config")


def _candidate(package: TaskPackage, gate: TaskGate) -> Path:
    return next(
        path
        for observed_gate, _, path, _ in package.validation_cases()
        if observed_gate is gate
    )


def _receipt(*, complete: bool = True) -> ControllerAssertedLifecycleReceipt:
    factory = (
        ControllerAssertedLifecycleReceipt.completed
        if complete
        else ControllerAssertedLifecycleReceipt.incomplete
    )
    return factory(controller_id="test-controller")


def _grade(package: TaskPackage, output: Path):
    return FileAssertionsGrader.from_task(package).grade(
        package,
        output,
        lifecycle_receipt=_receipt(),
    )


def _trial(package: TaskPackage, *, harness_id: str = "harness-a"):
    spec = TrialSpec(
        benchmark_release="ATV-2026.07-smoke",
        protocol_version="atv.trial/v1",
        schedule_id="1" * 64,
        task=package.task_ref,
        harness=HarnessRef(harness_id, "1.0.0", "2" * 64),
        model_policy=ModelPolicyRef("model", "2026-07-19", "3" * 64),
        budget_profile=BudgetProfile(
            "small",
            Budget(
                wall_time_seconds=60,
                max_model_tokens=10_000,
                max_model_calls=20,
                max_cost_microusd=500_000,
            ),
        ),
        repetition=0,
        schedule_seed=9,
    )
    attempt = TrialAttempt(spec=spec, attempt_number=1, fresh_nonce="4" * 64)
    return spec, attempt


def test_grader_requires_completed_controller_lifecycle_receipt():
    package = _package()
    grader = FileAssertionsGrader.from_task(package)
    oracle = _candidate(package, TaskGate.ORACLE)
    with pytest.raises(GradingStateError, match="not asserted"):
        grader.grade(
            package,
            oracle,
            lifecycle_receipt=_receipt(complete=False),
        )


def test_file_assertion_grader_is_deterministic_and_does_not_execute_output():
    package = _package()
    grader = FileAssertionsGrader.from_task(package)
    oracle = _candidate(package, TaskGate.ORACLE)
    receipt = _receipt()
    first = grader.grade(package, oracle, lifecycle_receipt=receipt)
    second = grader.grade(package, oracle, lifecycle_receipt=receipt)
    assert first.passed is True
    assert first.score == 1.0
    assert first.to_dict() == second.to_dict()
    assert first.result_digest == second.result_digest
    assert first.trust_tier is GradingTrustTier.LOCAL_SELF_ATTESTED
    assert first.official_verified is False


def test_plain_boolean_cannot_create_official_or_verified_grade():
    package = _package()
    grader = FileAssertionsGrader.from_task(package)
    oracle = _candidate(package, TaskGate.ORACLE)
    with pytest.raises(GradingStateError, match="RunnerLifecycleReceipt"):
        grader.grade(
            package,
            oracle,
            lifecycle_receipt=True,  # type: ignore[arg-type]
        )
    local = ControllerAssertedLifecycleReceipt.completed(
        controller_id="caller-controlled"
    )
    assert local.official_verified is False
    assert local.trust_tier is GradingTrustTier.LOCAL_SELF_ATTESTED


def test_grader_returns_partial_evidence_but_does_not_pass_wrong_output():
    package = _package()
    mutation = _candidate(package, TaskGate.MUTATION)
    result = _grade(package, mutation)
    assert result.passed is False
    assert 0.0 < result.score < 1.0
    assert any(assertion.passed is False for assertion in result.assertions)


def test_grader_rejects_unknown_or_ambiguous_spec_fields():
    with pytest.raises(GraderError, match="exactly"):
        FileAssertionsGrader(
            {
                "schema": "atv.grader.file-assertions/v1",
                "pass_score": 1.0,
                "assertions": [],
                "verified": True,
            }
        )


def test_grader_rejects_symlinked_output_tree_content(tmp_path):
    package = _package()
    candidate = tmp_path / "candidate"
    shutil.copytree(
        _candidate(package, TaskGate.ORACLE),
        candidate,
    )
    try:
        os.symlink(tmp_path / "outside", candidate / "leak")
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(GraderError, match="symlink|junction"):
        FileAssertionsGrader.from_task(package).grade(
            package,
            candidate,
            lifecycle_receipt=_receipt(),
        )


def test_grader_rejects_hardlinked_output_tree_content(tmp_path):
    package = _package()
    candidate = tmp_path / "candidate"
    shutil.copytree(_candidate(package, TaskGate.ORACLE), candidate)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        os.link(outside, candidate / "hardlinked.txt")
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on this worker: {exc}")
    with pytest.raises(GraderError, match="hardlink"):
        FileAssertionsGrader.from_task(package).grade(
            package,
            candidate,
            lifecycle_receipt=_receipt(),
        )


def test_grader_rejects_same_size_content_change_during_capture(
    tmp_path,
    monkeypatch,
):
    package = _package()
    candidate = tmp_path / "candidate"
    shutil.copytree(_candidate(package, TaskGate.ORACLE), candidate)
    grader = FileAssertionsGrader.from_task(package)
    original_reader = eval_canonical.capture_module.read_confined_regular_file
    changed = False

    def racing_reader(root, relpath, *, max_bytes):
        nonlocal changed
        data = original_reader(root, relpath, max_bytes=max_bytes)
        if not changed and str(relpath).replace("\\", "/") == "config.json":
            target = Path(root) / "config.json"
            target.write_bytes(target.read_bytes().replace(b"ready", b"other"))
            changed = True
        return data

    monkeypatch.setattr(
        eval_canonical.capture_module,
        "read_confined_regular_file",
        racing_reader,
    )
    with pytest.raises(GraderError, match="changed"):
        grader.grade(
            package,
            candidate,
            lifecycle_receipt=_receipt(),
        )


def test_grader_rejects_oversized_output_tree(tmp_path):
    package = _package()
    candidate = tmp_path / "candidate"
    shutil.copytree(_candidate(package, TaskGate.ORACLE), candidate)
    (candidate / "oversized.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(GraderError, match="limit"):
        FileAssertionsGrader.from_task(package).grade(
            package,
            candidate,
            lifecycle_receipt=_receipt(),
        )


def test_completed_trial_bundle_round_trips_and_verifies_all_objects(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    store = ContentAddressedStore(tmp_path / "store")
    bundle = TrialBundle.create(
        store,
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=grade,
        output_tree=output,
        artifacts={"logs/stderr.txt": b"trusted controller log\n"},
        runner_metadata={"runner": "fake-v1", "worker_class": "test"},
    )
    assert bundle.manifest_path.is_file()
    assert bundle.manifest["trial"]["trial_id"] == spec.trial_id
    assert bundle.manifest["outcome"]["rankable"] is True
    assert any(
        item["path"] == "output/config.json"
        for item in bundle.manifest["artifacts"]
    )
    loaded = TrialBundle.load(store, bundle.digest)
    assert loaded.manifest == bundle.manifest
    loaded.verify()


def test_same_evidence_produces_same_bundle_digest(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    first = TrialBundle.create(
        ContentAddressedStore(tmp_path / "one"),
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=grade,
        output_tree=output,
        artifacts={"logs/events.jsonl": b'{"type":"result"}\n'},
    )
    second = TrialBundle.create(
        ContentAddressedStore(tmp_path / "two"),
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=grade,
        output_tree=output,
        artifacts={"logs/events.jsonl": b'{"type":"result"}\n'},
    )
    assert first.digest == second.digest


def test_tampering_with_an_artifact_is_detected(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    store = ContentAddressedStore(tmp_path / "store")
    bundle = TrialBundle.create(
        store,
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=grade,
        output_tree=output,
    )
    record = next(
        item
        for item in bundle.manifest["artifacts"]
        if item["path"] == "output/config.json"
    )
    object_path = store.object_path(record["sha256"])
    os.chmod(object_path, stat.S_IWRITE | stat.S_IREAD)
    object_path.write_bytes(b"tampered")
    with pytest.raises(BundleIntegrityError, match="tampered"):
        bundle.verify()


def test_bundle_rejects_path_escape_and_score_mismatch(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    wrong = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=0.5,
    )
    store = ContentAddressedStore(tmp_path / "store")
    with pytest.raises(BundleIntegrityError, match="score"):
        TrialBundle.create(
            store,
            spec=spec,
            attempt=attempt,
            outcome=wrong,
            grade=grade,
            output_tree=output,
        )
    valid = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    with pytest.raises(BundleIntegrityError, match="unsafe|relative"):
        TrialBundle.create(
            store,
            spec=spec,
            attempt=attempt,
            outcome=valid,
            grade=grade,
            output_tree=output,
            artifacts={"../../escape": b"no"},
        )


def test_bundle_rejects_caller_supplied_official_or_verified_metadata(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    with pytest.raises(BundleIntegrityError, match="reserved trust claim"):
        TrialBundle.create(
            ContentAddressedStore(tmp_path / "store"),
            spec=spec,
            attempt=attempt,
            outcome=outcome,
            grade=grade,
            output_tree=output,
            runner_metadata={"official_verified": True},
        )


def test_bundle_binds_exact_fresh_attempt_and_detects_manifest_rewrite(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    store = ContentAddressedStore(tmp_path / "store")
    other_attempt = TrialAttempt(
        spec=spec,
        attempt_number=2,
        fresh_nonce="5" * 64,
    )
    with pytest.raises(BundleIntegrityError, match="attempt_id"):
        TrialBundle.create(
            store,
            spec=spec,
            attempt=other_attempt,
            outcome=outcome,
            grade=grade,
            output_tree=output,
        )

    bundle = TrialBundle.create(
        store,
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=grade,
        output_tree=output,
    )
    rewritten = bundle.manifest
    rewritten["attempt"]["fresh_nonce"] = "5" * 64
    rewritten_digest = store.put_bytes(canonical_json_bytes(rewritten))
    with pytest.raises(BundleIntegrityError, match="attempt identity"):
        TrialBundle.load(store, rewritten_digest)


def test_bundle_rejects_rewritten_local_grade_as_official(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    store = ContentAddressedStore(tmp_path / "store")
    bundle = TrialBundle.create(
        store,
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=grade,
        output_tree=output,
    )
    rewritten = bundle.manifest
    rewritten["grade"]["official_verified"] = True
    grade_payload = {
        key: value
        for key, value in rewritten["grade"].items()
        if key != "result_digest"
    }
    rewritten["grade"]["result_digest"] = eval_canonical.sha256_json(grade_payload)
    rewritten_digest = store.put_bytes(canonical_json_bytes(rewritten))
    with pytest.raises(BundleIntegrityError, match="trust tier"):
        TrialBundle.load(store, rewritten_digest)


def test_infrastructure_failure_bundle_remains_unrankable(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.RUNNER_FAILED,
        harness_status=HarnessStatus.NOT_RUN,
        score=None,
        reason_code="worker_lost",
    )
    bundle = TrialBundle.create(
        ContentAddressedStore(tmp_path / "store"),
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=None,
        output_tree=None,
        artifacts={"logs/controller.json": b'{"error":"worker_lost"}'},
    )
    bundle.verify()
    assert bundle.manifest["outcome"]["rankable"] is False
    assert bundle.manifest["grade"] is None


def test_harness_failure_bundle_is_rankable_zero_not_infrastructure(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.CRASHED,
        score=0.0,
        reason_code="exit_17",
    )
    bundle = TrialBundle.create(
        ContentAddressedStore(tmp_path / "store"),
        spec=spec,
        attempt=attempt,
        outcome=outcome,
        grade=None,
        output_tree=None,
        artifacts={"logs/stderr.txt": b"crash\n"},
    )
    bundle.verify()
    assert bundle.manifest["outcome"]["rankable"] is True
    assert bundle.manifest["outcome"]["score"] == 0.0
    assert bundle.manifest["grade"] is None


def test_completed_bundle_requires_trusted_grade_and_output(tmp_path):
    package = _package()
    spec, attempt = _trial(package)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=1.0,
    )
    with pytest.raises(BundleIntegrityError, match="require"):
        TrialBundle.create(
            ContentAddressedStore(tmp_path / "store"),
            spec=spec,
            attempt=attempt,
            outcome=outcome,
            grade=None,
            output_tree=None,
        )


def test_store_refuses_to_bless_tampered_existing_object(tmp_path):
    store = ContentAddressedStore(tmp_path / "store")
    digest = store.put_bytes(b"original")
    path = store.object_path(digest)
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(b"tampered")
    with pytest.raises(BundleIntegrityError, match="tampered|does not match"):
        store.put_bytes(b"original")


def test_store_rejects_hardlinked_existing_object(tmp_path):
    store = ContentAddressedStore(tmp_path / "store")
    data = b"hardlink-object"
    digest = store.put_bytes(data)
    object_path = store.object_path(digest)
    os.chmod(object_path, stat.S_IWRITE | stat.S_IREAD)
    object_path.unlink()
    outside = tmp_path / "outside-object"
    outside.write_bytes(data)
    try:
        os.link(outside, object_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on this worker: {exc}")
    with pytest.raises(BundleIntegrityError, match="hardlink"):
        store.read_bytes(digest)
    with pytest.raises(BundleIntegrityError, match="hardlink"):
        store.put_bytes(data)


def test_store_rejects_symlinked_object_prefix(tmp_path):
    store = ContentAddressedStore(tmp_path / "store")
    data = b"symlink-prefix"
    digest = eval_canonical.sha256_bytes(data)
    prefix = store.objects / digest[:2]
    outside = tmp_path / "outside-prefix"
    outside.mkdir()
    try:
        prefix.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable on this worker: {exc}")
    with pytest.raises(BundleIntegrityError, match="link"):
        store.put_bytes(data)


def test_store_rejects_replaced_root_directory(tmp_path):
    root = tmp_path / "store"
    store = ContentAddressedStore(root)
    displaced = tmp_path / "displaced-store"
    root.rename(displaced)
    root.mkdir()
    with pytest.raises(BundleIntegrityError, match="replaced|changed"):
        store.put_bytes(b"must-not-follow-replacement")


def test_store_rejects_same_size_object_change_during_read(tmp_path, monkeypatch):
    store = ContentAddressedStore(tmp_path / "store")
    original = b"original-object"
    digest = store.put_bytes(original)
    object_path = store.object_path(digest)
    os.chmod(object_path, stat.S_IWRITE | stat.S_IREAD)
    original_reader = eval_canonical.capture_module.read_confined_regular_file
    changed = False

    def racing_reader(root, relpath, *, max_bytes):
        nonlocal changed
        data = original_reader(root, relpath, max_bytes=max_bytes)
        if not changed and str(relpath).replace("\\", "/").endswith(digest[2:]):
            object_path.write_bytes(b"modified-object")
            changed = True
        return data

    monkeypatch.setattr(
        eval_canonical.capture_module,
        "read_confined_regular_file",
        racing_reader,
    )
    with pytest.raises(BundleIntegrityError, match="changed"):
        store.read_bytes(digest)


def test_canonical_tree_bounds_empty_directories_and_depth(tmp_path):
    flood = tmp_path / "flood"
    flood.mkdir()
    for index in range(8):
        (flood / f"d{index}").mkdir()
    with pytest.raises(eval_canonical.UnsafePathError, match="entry|directory"):
        eval_canonical.snapshot_regular_tree(
            flood,
            limits=TreeLimits(
                max_files=8,
                max_total_bytes=1024,
                max_file_bytes=1024,
                max_entries=6,
                max_directories=6,
            ),
        )

    deep = tmp_path / "deep"
    deep.mkdir()
    current = deep
    for index in range(5):
        current = current / f"d{index}"
        current.mkdir()
    with pytest.raises(eval_canonical.UnsafePathError, match="depth"):
        eval_canonical.snapshot_regular_tree(
            deep,
            limits=TreeLimits(
                max_files=8,
                max_total_bytes=1024,
                max_file_bytes=1024,
                max_depth=2,
            ),
        )


def test_store_and_bundle_enforce_object_and_tree_limits(tmp_path):
    store = ContentAddressedStore(tmp_path / "store", max_object_bytes=4)
    with pytest.raises(BundleIntegrityError, match="exceeds limit"):
        store.put_bytes(b"12345")

    package = _package()
    spec, attempt = _trial(package)
    output = _candidate(package, TaskGate.ORACLE)
    grade = _grade(package, output)
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    with pytest.raises(BundleIntegrityError, match="file limit"):
        TrialBundle.create(
            ContentAddressedStore(tmp_path / "bundle-store"),
            spec=spec,
            attempt=attempt,
            outcome=outcome,
            grade=grade,
            output_tree=output,
            tree_limits=TreeLimits(
                max_files=0,
                max_total_bytes=1024,
                max_file_bytes=1024,
            ),
        )
