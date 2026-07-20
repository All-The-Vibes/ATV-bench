"""Task-package acceptance gates and adversarial fixture tests."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from atv_bench.eval.grader import (
    ControllerAssertedLifecycleReceipt,
    FileAssertionsGrader,
)
from atv_bench.eval.tasks import (
    ReviewLevel,
    ReviewerRole,
    ReviewSuiteStatus,
    TaskGate,
    TaskPackage,
    TaskPackageError,
    TaskPackageValidator,
    TrustedReviewerApproval,
    TrustedReviewerRegistry,
    load_task_suite,
)
from atv_bench.protocol.schemas import SchemaKind, default_schema_store


SMOKE_ROOT = Path(__file__).resolve().parents[1] / "tasks" / "smoke"


def _packages():
    return load_task_suite(sorted(path for path in SMOKE_ROOT.iterdir() if path.is_dir()))


def _rewrite_validation_document(
    root: Path,
    manifest: dict,
    descriptor: dict,
    document: dict,
) -> None:
    path = root / descriptor["path"]
    data = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(data)
    descriptor["size_bytes"] = len(data)
    descriptor["digest"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(data).hexdigest(),
    }
    (root / "task.json").write_text(json.dumps(manifest), encoding="utf-8")


def _rewrite_review_document(root: Path, review: dict) -> None:
    manifest_path = root / "task.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = manifest["validation_evidence"]["independent_review"]
    path = root / descriptor["path"]
    data = (
        json.dumps(review, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    path.write_bytes(data)
    descriptor["schema"] = review.get("schema", descriptor["schema"])
    descriptor["size_bytes"] = len(data)
    descriptor["digest"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(data).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _review(root: Path) -> dict:
    manifest = json.loads((root / "task.json").read_text(encoding="utf-8"))
    descriptor = manifest["validation_evidence"]["independent_review"]
    return json.loads((root / descriptor["path"]).read_text(encoding="utf-8"))


def _human_reviewer(reviewer_id: str, *, conflict: bool = False) -> dict:
    return {
        "reviewer_id": reviewer_id,
        "reviewer_kind": "human",
        "independent": True,
        "conflict_disclosure": {
            "status": "declared" if conflict else "none",
            "details": "Task authorship conflict disclosed." if conflict else "",
        },
    }


def _completed_human_review(root: Path) -> dict:
    review = _review(root)
    review.update(
        {
            "review_level": "human-independent",
            "suite_status": "human-independent-reviewed",
            "official_review_eligible": True,
            "reviewers": [
                _human_reviewer("human.example-reviewer-a"),
                _human_reviewer("human.example-reviewer-b"),
            ],
        }
    )
    _rewrite_review_document(root, review)
    return review


def _trusted_approval(
    review: dict,
    reviewer_id: str,
    *,
    role: ReviewerRole = ReviewerRole.TASK_REVIEWER,
) -> TrustedReviewerApproval:
    subject = review["subject"]
    return TrustedReviewerApproval(
        reviewer_id=reviewer_id,
        role=role,
        task_id=subject["task_id"],
        task_version=subject["task_version"],
        manifest_core_digest=subject["manifest_core_digest"]["value"],
        reviewed_at=review["reviewed_at"],
    )


def test_smoke_suite_has_multiple_unique_versioned_tasks():
    packages = _packages()
    assert len(packages) == 2
    assert {package.id for package in packages} == {
        "smoke.cross-file-total",
        "smoke.repair-config",
    }
    assert all(package.version == "1.0.0" for package in packages)
    assert len({package.digest for package in packages}) == 2
    assert all(
        package.review_level is ReviewLevel.MACHINE_FIXTURE
        for package in packages
    )
    assert all(
        package.suite_status is ReviewSuiteStatus.INTERNAL_MACHINE_REVIEWED
        for package in packages
    )


def test_smoke_manifests_conform_to_authoritative_protocol_task_schema():
    store = default_schema_store()
    for package in _packages():
        store.validate(package.manifest, SchemaKind.TASK)


@pytest.mark.parametrize("package", _packages(), ids=lambda package: package.id)
def test_every_smoke_task_passes_all_machine_acceptance_gates(package):
    report = TaskPackageValidator().validate(
        package, FileAssertionsGrader.from_task(package)
    )
    assert report.eligible is True
    assert report.machine_eligible is True
    assert report.review_level is ReviewLevel.MACHINE_FIXTURE
    assert report.suite_status is ReviewSuiteStatus.INTERNAL_MACHINE_REVIEWED
    assert report.official_review_eligible is False
    assert report.official_eligible is False
    assert report.human_reviewer_count == 0
    gates = {result.gate for result in report.gates}
    assert gates == {
        TaskGate.ORACLE,
        TaskGate.NO_OP,
        TaskGate.ALTERNATIVE,
        TaskGate.EXPLOIT,
        TaskGate.MUTATION,
        TaskGate.DETERMINISM,
    }
    assert all(result.passed for result in report.gates)


def test_machine_review_cannot_open_the_official_launch_gate():
    for package in _packages():
        report = TaskPackageValidator().validate(
            package,
            FileAssertionsGrader.from_task(package),
        )
        payload = report.to_dict()
        assert payload["eligible"] is True
        assert payload["machine_eligible"] is True
        assert payload["suite_status"] == "internal-machine-reviewed"
        assert payload["official_review_eligible"] is False
        assert payload["official_eligible"] is False


def test_self_declared_human_reviewers_cannot_open_the_official_review_gate(
    tmp_path,
):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    review = _review(copy)
    review.update(
        {
            "review_level": "human-independent",
            "suite_status": "human-review-incomplete",
            "official_review_eligible": False,
            "reviewers": [_human_reviewer("human.example-reviewer-a")],
        }
    )
    _rewrite_review_document(copy, review)
    one_reviewer = TaskPackage.load(copy)
    assert one_reviewer.official_review_eligible is False

    review.update(
        {
            "suite_status": "human-independent-reviewed",
            "official_review_eligible": True,
            "reviewers": [
                _human_reviewer("human.example-reviewer-a"),
                _human_reviewer("human.example-reviewer-b"),
            ],
        }
    )
    _rewrite_review_document(copy, review)
    two_reviewers = TaskPackage.load(copy)
    report = TaskPackageValidator().validate(
        two_reviewers,
        FileAssertionsGrader.from_task(two_reviewers),
    )
    assert report.machine_eligible is True
    assert report.official_review_eligible is False
    assert report.official_eligible is False


def test_role_scoped_trusted_reviewer_registry_can_open_official_review_gate(
    tmp_path,
):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    review = _completed_human_review(copy)
    reviewer_ids = tuple(item["reviewer_id"] for item in review["reviewers"])

    wrong_role = TrustedReviewerRegistry(
        tuple(
            _trusted_approval(
                review,
                reviewer_id,
                role=ReviewerRole.SECURITY_REVIEWER,
            )
            for reviewer_id in reviewer_ids
        )
    )
    wrong_role_package = TaskPackage.load(copy, reviewer_trust=wrong_role)
    assert wrong_role_package.official_review_eligible is False

    trusted = TrustedReviewerRegistry(
        tuple(_trusted_approval(review, reviewer_id) for reviewer_id in reviewer_ids)
    )
    package = TaskPackage.load(copy, reviewer_trust=trusted)
    report = TaskPackageValidator().validate(
        package,
        FileAssertionsGrader.from_task(package),
    )
    assert report.official_review_eligible is True
    assert report.official_eligible is True


def test_disclosed_human_conflict_keeps_official_review_gate_closed(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    review = _review(copy)
    review.update(
        {
            "review_level": "human-independent",
            "suite_status": "human-review-incomplete",
            "official_review_eligible": False,
            "reviewers": [
                _human_reviewer("human.example-reviewer-a"),
                _human_reviewer("human.example-reviewer-b", conflict=True),
            ],
        }
    )
    _rewrite_review_document(copy, review)
    package = TaskPackage.load(copy)
    assert package.official_review_eligible is False


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda review: review.update({"reviewed_at": "2999-01-01T00:00:00Z"}),
            "future",
        ),
        (
            lambda review: review.update({"reviewed_at": "2026-02-30T00:00:00Z"}),
            "real UTC",
        ),
        (
            lambda review: review["reviewers"][0].pop("conflict_disclosure"),
            "unexpected fields",
        ),
        (
            lambda review: review.update({"reviewed_by_humans": True}),
            "unexpected fields",
        ),
    ],
)
def test_review_timestamp_identity_and_conflict_evidence_fail_closed(
    tmp_path,
    mutate,
    match,
):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    review = _review(copy)
    mutate(review)
    _rewrite_review_document(copy, review)
    with pytest.raises(TaskPackageError, match=match):
        TaskPackage.load(copy)


def test_duplicate_reviewer_identities_are_rejected(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    review = _review(copy)
    duplicate = dict(review["reviewers"][0])
    duplicate["conflict_disclosure"] = dict(duplicate["conflict_disclosure"])
    review.update(
        {
            "review_level": "machine-dual-review",
            "reviewers": [review["reviewers"][0], duplicate],
        }
    )
    _rewrite_review_document(copy, review)
    with pytest.raises(TaskPackageError, match="distinct"):
        TaskPackage.load(copy)


def test_reviewer_kind_must_match_typed_identity(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    review = _review(copy)
    review["reviewers"][0]["reviewer_kind"] = "human"
    _rewrite_review_document(copy, review)
    with pytest.raises(TaskPackageError, match="does not match"):
        TaskPackage.load(copy)


def test_review_evidence_cannot_be_replayed_onto_a_different_task(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    replayed = _review(SMOKE_ROOT / "cross_file_total")
    _rewrite_review_document(copy, replayed)
    with pytest.raises(TaskPackageError, match="not bound"):
        TaskPackage.load(copy)


def test_task_manifest_view_cannot_mutate_loaded_package():
    package = _packages()[0]
    first = package.manifest
    first["id"] = "tampered"
    first["validation_evidence"]["oracle"]["path"] = "../../outside"
    assert package.id != "tampered"
    assert package.manifest["validation_evidence"]["oracle"]["path"] != "../../outside"


def test_package_digest_changes_when_any_fixture_byte_changes(tmp_path):
    source = SMOKE_ROOT / "repair_config"
    copy = tmp_path / "task"
    shutil.copytree(source, copy)
    before = TaskPackage.load(copy)
    candidate = copy / "trusted" / "candidates" / "oracle" / "config.json"
    candidate.write_text('{"label":"ATV Bench","status":"ready","note":"changed"}\n')
    after = TaskPackage.load(copy)
    assert before.digest != after.digest


def test_path_traversal_in_manifest_is_rejected(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    manifest_path = copy / "task.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["prompt"]["path"] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TaskPackageError, match="atv.task/v1"):
        TaskPackage.load(copy)


def test_missing_required_gate_fixture_is_rejected(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    manifest_path = copy / "task.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["validation_evidence"]["alternative_solutions"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TaskPackageError, match="atv.task/v1"):
        TaskPackage.load(copy)


def test_trusted_fixtures_must_remain_under_trusted_boundary(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    manifest = json.loads((copy / "task.json").read_text())
    descriptor = manifest["validation_evidence"]["oracle"]
    _rewrite_validation_document(
        copy,
        manifest,
        descriptor,
        {
            "schema": "atv.validation-case/v1",
            "candidate": "public/workspace",
            "expected": "pass",
        },
    )
    with pytest.raises(TaskPackageError, match="must remain under trusted"):
        TaskPackage.load(copy)


def test_no_op_must_be_the_unchanged_public_workspace(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    manifest = json.loads((copy / "task.json").read_text())
    descriptor = manifest["validation_evidence"]["noop"]
    _rewrite_validation_document(
        copy,
        manifest,
        descriptor,
        {
            "schema": "atv.validation-case/v1",
            "candidate": "trusted/candidates/mutation",
            "expected": "fail",
        },
    )
    with pytest.raises(TaskPackageError, match="no-op validation"):
        TaskPackage.load(copy)


def test_unknown_manifest_fields_fail_closed(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    manifest_path = copy / "task.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["verified"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TaskPackageError, match="unknown fields"):
        TaskPackage.load(copy)


def test_duplicate_task_ids_are_rejected_at_suite_boundary(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    shutil.copytree(SMOKE_ROOT / "repair_config", first)
    shutil.copytree(SMOKE_ROOT / "repair_config", second)
    with pytest.raises(TaskPackageError, match="duplicate task id"):
        load_task_suite((first, second))


def test_symlink_anywhere_in_package_is_rejected(tmp_path):
    copy = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT / "repair_config", copy)
    link = copy / "trusted" / "candidates" / "oracle" / "outside-link"
    try:
        os.symlink(tmp_path / "outside", link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(TaskPackageError, match="symlink|junction"):
        TaskPackage.load(copy)


@dataclass
class _FakeGrade:
    passed: bool
    score: float
    result_digest: str


class _NondeterministicGrader:
    def __init__(self):
        self.counter = 0

    def grade(self, task, output_tree, *, lifecycle_receipt):
        lifecycle_receipt.validate_for_grading()
        self.counter += 1
        return _FakeGrade(
            passed=True,
            score=1.0,
            result_digest=f"{self.counter:064x}",
        )


def test_nondeterministic_grader_makes_task_ineligible():
    package = _packages()[0]
    report = TaskPackageValidator().validate(package, _NondeterministicGrader())
    assert report.eligible is False
    determinism = [
        result for result in report.gates if result.gate is TaskGate.DETERMINISM
    ]
    assert len(determinism) == 1
    assert determinism[0].passed is False


class _CrashingGrader:
    def grade(self, task, output_tree, *, lifecycle_receipt):
        raise RuntimeError("grader crashed")


def test_validator_records_grader_errors_instead_of_silently_skipping_gates():
    package = _packages()[0]
    report = TaskPackageValidator().validate(package, _CrashingGrader())
    assert report.eligible is False
    assert all(result.passed is False for result in report.gates)
    assert any("grader crashed" in result.message for result in report.gates)


def test_no_op_and_adversarial_candidates_really_fail_the_grader():
    for package in _packages():
        grader = FileAssertionsGrader.from_task(package)
        receipt = ControllerAssertedLifecycleReceipt.completed(
            controller_id="test-task-validator"
        )
        results = {
            case: grader.grade(
                package,
                path,
                lifecycle_receipt=receipt,
            )
            for _, case, path, _ in package.validation_cases()
        }
        assert results["oracle"].passed is True
        assert results["alternative_solutions[0]"].passed is True
        assert results["no_op"].passed is False
        assert results["exploit_cases[0]"].passed is False
        assert results["mutation_cases[0]"].passed is False
