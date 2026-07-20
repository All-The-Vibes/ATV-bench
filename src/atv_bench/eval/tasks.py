"""Versioned task packages and credibility-gate validation."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Sequence

from ._canonical import (
    UnsafePathError,
    assert_safe_tree,
    canonical_json_bytes,
    confined_path,
    read_stable_confined_regular_file,
    safe_relative_path,
    sha256_bytes,
    tree_digest,
)
from .grader import ControllerAssertedLifecycleReceipt, RunnerLifecycleReceipt
from .trial import TaskRef


class TaskPackageError(ValueError):
    """A task package is malformed or unsafe."""


class ReviewLevel(str, Enum):
    """Credibility level asserted by the task's review evidence."""

    HUMAN_INDEPENDENT = "human-independent"
    MACHINE_FIXTURE = "machine-fixture"
    MACHINE_DUAL_REVIEW = "machine-dual-review"


class ReviewSuiteStatus(str, Enum):
    """Public status derived from review evidence, not machine acceptance gates."""

    INTERNAL_MACHINE_REVIEWED = "internal-machine-reviewed"
    HUMAN_REVIEW_INCOMPLETE = "human-review-incomplete"
    HUMAN_INDEPENDENT_REVIEWED = "human-independent-reviewed"


class ReviewerRole(str, Enum):
    """Governance role authorized by externally trusted reviewer evidence."""

    TASK_REVIEWER = "task-reviewer"
    SECURITY_REVIEWER = "security-reviewer"
    STATISTICS_REVIEWER = "statistics-reviewer"


@dataclass(frozen=True, slots=True)
class ReviewerEvidence:
    reviewer_id: str
    reviewer_kind: str
    independent: bool
    conflict_status: str
    conflict_details: str


@dataclass(frozen=True, slots=True)
class IndependentReviewEvidence:
    review_level: ReviewLevel
    suite_status: ReviewSuiteStatus
    reviewed_at: str
    reviewers: tuple[ReviewerEvidence, ...]
    official_review_eligible: bool

    @property
    def reviewer_ids(self) -> tuple[str, ...]:
        return tuple(reviewer.reviewer_id for reviewer in self.reviewers)

    @property
    def human_reviewer_count(self) -> int:
        return sum(
            reviewer.reviewer_kind == "human" for reviewer in self.reviewers
        )


class GradeLike(Protocol):
    passed: bool
    score: float
    result_digest: str


class TaskGrader(Protocol):
    def grade(
        self,
        task: "TaskPackage",
        output_tree: Path,
        *,
        lifecycle_receipt: RunnerLifecycleReceipt,
    ) -> GradeLike: ...


def _descriptor_file(
    root: Path,
    descriptor: dict[str, Any],
    *,
    context: str,
) -> Path:
    try:
        relative = descriptor["path"]
        safe_relative_path(relative, field=f"{context}.path")
        path = confined_path(root, relative, field=f"{context}.path")
        data = read_stable_confined_regular_file(
            root,
            relative,
            max_bytes=4 * 1024 * 1024,
        )
        if len(data) != descriptor["size_bytes"]:
            raise TaskPackageError(f"{context} size_bytes does not match its file")
        digest = descriptor["digest"]
        if digest["algorithm"] != "sha256":
            raise TaskPackageError(f"{context} uses an unsupported digest")
        if sha256_bytes(data) != digest["value"]:
            raise TaskPackageError(f"{context} digest does not match its file")
        return path
    except (KeyError, TypeError, UnsafePathError, OSError) as exc:
        if isinstance(exc, TaskPackageError):
            raise
        raise TaskPackageError(f"{context} descriptor is invalid: {exc}") from exc


def _digest_matches_file(path: Path, digest: dict[str, Any], *, context: str) -> None:
    if digest.get("algorithm") != "sha256":
        raise TaskPackageError(f"{context} must use sha256")
    try:
        data = read_stable_confined_regular_file(
            path.parent,
            path.name,
            max_bytes=4 * 1024 * 1024,
        )
    except UnsafePathError as exc:
        raise TaskPackageError(str(exc)) from exc
    if sha256_bytes(data) != digest.get("value"):
        raise TaskPackageError(f"{context} does not match {path.name}")


def _load_json_object(path: Path, *, context: str) -> dict[str, Any]:
    try:
        data = read_stable_confined_regular_file(
            path.parent,
            path.name,
            max_bytes=4 * 1024 * 1024,
        )
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnsafePathError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskPackageError(f"{context} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskPackageError(f"{context} must contain a JSON object")
    return value


_REVIEWER_ID = re.compile(
    r"^(?:human|machine)\.[a-z0-9](?:[a-z0-9._@:/-]{0,125}[a-z0-9])?$"
)
_REVIEW_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)


class ReviewerTrustVerifier(Protocol):
    """External trust boundary for role- and task-bound reviewer approvals."""

    def verifies(
        self,
        *,
        reviewer_id: str,
        role: ReviewerRole,
        task_id: str,
        task_version: str,
        manifest_core_digest: str,
        reviewed_at: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class TrustedReviewerApproval:
    """One approval supplied by a caller-trusted external registry."""

    reviewer_id: str
    role: ReviewerRole
    task_id: str
    task_version: str
    manifest_core_digest: str
    reviewed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, ReviewerRole):
            object.__setattr__(self, "role", ReviewerRole(self.role))
        if (
            not isinstance(self.reviewer_id, str)
            or not self.reviewer_id.startswith("human.")
            or _REVIEWER_ID.fullmatch(self.reviewer_id) is None
        ):
            raise ValueError("trusted reviewer approval requires a human reviewer_id")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("trusted reviewer approval requires task_id")
        if not isinstance(self.task_version, str) or not self.task_version:
            raise ValueError("trusted reviewer approval requires task_version")
        if (
            not isinstance(self.manifest_core_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.manifest_core_digest) is None
        ):
            raise ValueError(
                "trusted reviewer approval manifest_core_digest must be sha256"
            )
        if (
            not isinstance(self.reviewed_at, str)
            or _REVIEW_TIMESTAMP.fullmatch(self.reviewed_at) is None
        ):
            raise ValueError(
                "trusted reviewer approval reviewed_at must be canonical UTC"
            )
        try:
            parsed = datetime.strptime(
                self.reviewed_at,
                "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(
                "trusted reviewer approval reviewed_at is not a real UTC date-time"
            ) from exc
        if parsed > datetime.now(timezone.utc):
            raise ValueError(
                "trusted reviewer approval reviewed_at cannot be in the future"
            )


class TrustedReviewerRegistry:
    """Exact trusted approvals keyed by role, task subject, and review time."""

    def __init__(self, approvals: Sequence[TrustedReviewerApproval]) -> None:
        normalized = tuple(approvals)
        if any(
            not isinstance(approval, TrustedReviewerApproval)
            for approval in normalized
        ):
            raise TypeError(
                "trusted reviewer registry accepts TrustedReviewerApproval values"
            )
        keys = tuple(self._key(approval) for approval in normalized)
        if len(set(keys)) != len(keys):
            raise ValueError("trusted reviewer registry approvals must be distinct")
        self._approvals = frozenset(keys)

    @staticmethod
    def _key(
        approval: TrustedReviewerApproval,
    ) -> tuple[str, ReviewerRole, str, str, str, str]:
        return (
            approval.reviewer_id,
            approval.role,
            approval.task_id,
            approval.task_version,
            approval.manifest_core_digest,
            approval.reviewed_at,
        )

    def verifies(
        self,
        *,
        reviewer_id: str,
        role: ReviewerRole,
        task_id: str,
        task_version: str,
        manifest_core_digest: str,
        reviewed_at: str,
    ) -> bool:
        try:
            normalized_role = ReviewerRole(role)
        except (TypeError, ValueError):
            return False
        return (
            reviewer_id,
            normalized_role,
            task_id,
            task_version,
            manifest_core_digest,
            reviewed_at,
        ) in self._approvals


def _parse_review_evidence(
    review: dict[str, Any],
    *,
    descriptor_schema: str,
    manifest: dict[str, Any],
    reviewer_trust: ReviewerTrustVerifier | None,
) -> IndependentReviewEvidence:
    expected_fields = {
        "schema",
        "subject",
        "review_level",
        "suite_status",
        "reviewed",
        "spec_grader_aligned",
        "reviewed_at",
        "official_review_eligible",
        "reviewers",
    }
    if set(review) != expected_fields:
        raise TaskPackageError("independent review document has unexpected fields")
    if (
        review["schema"] != "atv.independent-review/v2"
        or descriptor_schema != review["schema"]
    ):
        raise TaskPackageError(
            "independent review must use atv.independent-review/v2"
        )

    subject = review["subject"]
    if not isinstance(subject, dict) or set(subject) != {
        "task_id",
        "task_version",
        "manifest_core_digest",
    }:
        raise TaskPackageError("independent review subject has unexpected fields")
    subject_digest = subject["manifest_core_digest"]
    if not isinstance(subject_digest, dict) or set(subject_digest) != {
        "algorithm",
        "value",
    }:
        raise TaskPackageError(
            "independent review subject manifest_core_digest is invalid"
        )
    manifest_core = dict(manifest)
    manifest_core_evidence = dict(manifest["validation_evidence"])
    manifest_core_evidence.pop("independent_review", None)
    manifest_core["validation_evidence"] = manifest_core_evidence
    expected_subject_digest = sha256_bytes(canonical_json_bytes(manifest_core))
    if (
        subject["task_id"] != manifest["id"]
        or subject["task_version"] != manifest["version"]
        or subject_digest["algorithm"] != "sha256"
        or subject_digest["value"] != expected_subject_digest
    ):
        raise TaskPackageError(
            "independent review subject is not bound to this task manifest"
        )
    if review["reviewed"] is not True:
        raise TaskPackageError("independent review must record completed review")
    if review["spec_grader_aligned"] is not True:
        raise TaskPackageError(
            "independent review must approve specification/grader alignment"
        )

    reviewed_at = review["reviewed_at"]
    if (
        not isinstance(reviewed_at, str)
        or _REVIEW_TIMESTAMP.fullmatch(reviewed_at) is None
    ):
        raise TaskPackageError(
            "independent review reviewed_at must be canonical UTC date-time"
        )
    try:
        parsed_reviewed_at = datetime.strptime(
            reviewed_at,
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise TaskPackageError(
            "independent review reviewed_at is not a real UTC date-time"
        ) from exc
    if parsed_reviewed_at > datetime.now(timezone.utc):
        raise TaskPackageError("independent review reviewed_at cannot be in the future")

    try:
        review_level = ReviewLevel(review["review_level"])
    except (TypeError, ValueError) as exc:
        raise TaskPackageError("independent review has an unknown review_level") from exc

    raw_reviewers = review["reviewers"]
    if (
        not isinstance(raw_reviewers, list)
        or not raw_reviewers
        or len(raw_reviewers) > 16
    ):
        raise TaskPackageError(
            "independent review requires between one and sixteen reviewers"
        )

    reviewers: list[ReviewerEvidence] = []
    for index, raw in enumerate(raw_reviewers):
        context = f"independent review reviewers[{index}]"
        if not isinstance(raw, dict) or set(raw) != {
            "reviewer_id",
            "reviewer_kind",
            "independent",
            "conflict_disclosure",
        }:
            raise TaskPackageError(f"{context} has unexpected fields")
        reviewer_id = raw["reviewer_id"]
        reviewer_kind = raw["reviewer_kind"]
        independent = raw["independent"]
        disclosure = raw["conflict_disclosure"]
        if (
            not isinstance(reviewer_id, str)
            or _REVIEWER_ID.fullmatch(reviewer_id) is None
        ):
            raise TaskPackageError(
                f"{context}.reviewer_id must be a stable typed identity"
            )
        if reviewer_kind not in {"human", "machine"}:
            raise TaskPackageError(
                f"{context}.reviewer_kind must be 'human' or 'machine'"
            )
        if not reviewer_id.startswith(f"{reviewer_kind}."):
            raise TaskPackageError(
                f"{context}.reviewer_id kind does not match reviewer_kind"
            )
        if not isinstance(independent, bool):
            raise TaskPackageError(f"{context}.independent must be boolean")
        if not isinstance(disclosure, dict) or set(disclosure) != {
            "status",
            "details",
        }:
            raise TaskPackageError(
                f"{context}.conflict_disclosure has unexpected fields"
            )
        conflict_status = disclosure["status"]
        conflict_details = disclosure["details"]
        if conflict_status not in {"none", "declared"}:
            raise TaskPackageError(
                f"{context}.conflict_disclosure.status is invalid"
            )
        if (
            not isinstance(conflict_details, str)
            or len(conflict_details) > 2048
        ):
            raise TaskPackageError(
                f"{context}.conflict_disclosure.details must be text"
            )
        if conflict_status == "none" and conflict_details != "":
            raise TaskPackageError(
                f"{context} with no conflict must use empty details"
            )
        if conflict_status == "declared" and not conflict_details.strip():
            raise TaskPackageError(
                f"{context} must describe its declared conflict"
            )
        reviewers.append(
            ReviewerEvidence(
                reviewer_id=reviewer_id,
                reviewer_kind=reviewer_kind,
                independent=independent,
                conflict_status=conflict_status,
                conflict_details=conflict_details,
            )
        )

    reviewer_ids = [reviewer.reviewer_id for reviewer in reviewers]
    if len(set(reviewer_ids)) != len(reviewer_ids):
        raise TaskPackageError("independent review reviewer identities must be distinct")

    if review_level is ReviewLevel.MACHINE_FIXTURE:
        if len(reviewers) != 1 or any(
            reviewer.reviewer_kind != "machine" for reviewer in reviewers
        ):
            raise TaskPackageError(
                "machine-fixture review requires exactly one machine reviewer"
            )
    elif review_level is ReviewLevel.MACHINE_DUAL_REVIEW:
        if len(reviewers) < 2 or any(
            reviewer.reviewer_kind != "machine" for reviewer in reviewers
        ):
            raise TaskPackageError(
                "machine-dual-review requires at least two machine reviewers"
            )
    elif any(
        reviewer.reviewer_kind != "human" or not reviewer.independent
        for reviewer in reviewers
    ):
        raise TaskPackageError(
            "human-independent review requires independent human reviewers"
        )

    structurally_complete_human_review = (
        review_level is ReviewLevel.HUMAN_INDEPENDENT
        and len(reviewers) >= 2
        and all(
            reviewer.reviewer_kind == "human"
            and reviewer.independent
            and reviewer.conflict_status == "none"
            for reviewer in reviewers
        )
    )
    claimed_official = review["official_review_eligible"]
    if not isinstance(claimed_official, bool):
        raise TaskPackageError(
            "independent review official_review_eligible must be boolean"
        )
    if claimed_official is not structurally_complete_human_review:
        raise TaskPackageError(
            "independent review official_review_eligible is inconsistent "
            "with reviewer evidence"
        )

    expected_status = (
        ReviewSuiteStatus.INTERNAL_MACHINE_REVIEWED
        if review_level
        in {ReviewLevel.MACHINE_FIXTURE, ReviewLevel.MACHINE_DUAL_REVIEW}
        else (
            ReviewSuiteStatus.HUMAN_INDEPENDENT_REVIEWED
            if structurally_complete_human_review
            else ReviewSuiteStatus.HUMAN_REVIEW_INCOMPLETE
        )
    )
    try:
        suite_status = ReviewSuiteStatus(review["suite_status"])
    except (TypeError, ValueError) as exc:
        raise TaskPackageError("independent review has an unknown suite_status") from exc
    if suite_status is not expected_status:
        raise TaskPackageError(
            "independent review suite_status is inconsistent with reviewer evidence"
        )

    # The package can only describe who claims to have reviewed it. Official
    # eligibility requires a separate trust boundary that verifies every
    # reviewer for the task-reviewer role and this exact task subject.
    official_review_eligible = False
    if structurally_complete_human_review and reviewer_trust is not None:
        verified = True
        for reviewer in reviewers:
            try:
                approval_verified = reviewer_trust.verifies(
                    reviewer_id=reviewer.reviewer_id,
                    role=ReviewerRole.TASK_REVIEWER,
                    task_id=str(subject["task_id"]),
                    task_version=str(subject["task_version"]),
                    manifest_core_digest=expected_subject_digest,
                    reviewed_at=reviewed_at,
                )
            except Exception:
                approval_verified = False
            if approval_verified is not True:
                verified = False
                break
        official_review_eligible = verified

    return IndependentReviewEvidence(
        review_level=review_level,
        suite_status=suite_status,
        reviewed_at=reviewed_at,
        reviewers=tuple(reviewers),
        official_review_eligible=official_review_eligible,
    )


def _validation_case(
    root: Path,
    descriptor: dict[str, Any],
    *,
    context: str,
    expected: str,
) -> tuple[str, Path]:
    document_path = _descriptor_file(root, descriptor, context=context)
    document = _load_json_object(document_path, context=context)
    if set(document) != {"schema", "candidate", "expected"}:
        raise TaskPackageError(f"{context} has unexpected fields")
    if document["schema"] != "atv.validation-case/v1":
        raise TaskPackageError(f"{context} has an unsupported schema")
    if descriptor["schema"] != document["schema"]:
        raise TaskPackageError(f"{context} descriptor schema does not match document")
    if document["expected"] != expected:
        raise TaskPackageError(f"{context} expected result must be {expected!r}")
    try:
        relative = str(document["candidate"])
        safe_relative_path(relative, field=f"{context}.candidate")
        candidate = confined_path(root, relative, field=f"{context}.candidate")
    except (TypeError, UnsafePathError) as exc:
        raise TaskPackageError(str(exc)) from exc
    if not candidate.is_dir():
        raise TaskPackageError(f"{context} candidate must be a directory")
    return relative, candidate


@dataclass(frozen=True, slots=True)
class TaskPackage:
    """A safe immutable view of one task package on disk."""

    root: Path
    _manifest_json: str
    digest: str
    review_evidence: IndependentReviewEvidence

    @classmethod
    def load(
        cls,
        root: Path | str,
        *,
        reviewer_trust: ReviewerTrustVerifier | None = None,
    ) -> "TaskPackage":
        path = Path(os.path.abspath(os.fspath(root)))
        try:
            assert_safe_tree(path)
        except UnsafePathError as exc:
            raise TaskPackageError(str(exc)) from exc

        manifest_path = path / "task.json"
        if not manifest_path.is_file():
            raise TaskPackageError("task package is missing task.json")
        try:
            manifest_data = read_stable_confined_regular_file(
                path,
                "task.json",
                max_bytes=4 * 1024 * 1024,
            )
            manifest = json.loads(manifest_data.decode("utf-8"))
        except (UnsafePathError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskPackageError(f"task.json is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise TaskPackageError("task.json must contain an object")

        try:
            from atv_bench.protocol.schemas import (
                SchemaKind,
                default_schema_store,
            )

            default_schema_store().validate(manifest, SchemaKind.TASK)
        except Exception as exc:
            raise TaskPackageError(
                f"task.json does not conform to atv.task/v1: {exc}"
            ) from exc

        workspace = confined_path(path, "public/workspace", field="public_workspace")
        grader = confined_path(path, "trusted/grader.json", field="trusted.grader")
        result_schema = confined_path(
            path,
            "trusted/grade-result.schema.json",
            field="trusted.result_schema",
        )
        if not workspace.is_dir():
            raise TaskPackageError("public/workspace must be a directory")
        if not grader.is_file() or not result_schema.is_file():
            raise TaskPackageError(
                "trusted grader and grade-result schema files are required"
            )

        prompt = confined_path(path, manifest["prompt"]["path"], field="prompt.path")
        if not prompt.is_file():
            raise TaskPackageError("prompt.path must reference a regular file")
        try:
            prompt_data = read_stable_confined_regular_file(
                path,
                manifest["prompt"]["path"],
                max_bytes=4 * 1024 * 1024,
            )
            prompt_data.decode("utf-8")
        except (UnsafePathError, UnicodeDecodeError) as exc:
            raise TaskPackageError("prompt must be UTF-8 text") from exc
        _digest_matches_file(
            prompt,
            manifest["prompt"]["digest"],
            context="prompt.digest",
        )
        if (
            manifest["source"]["tree_digest"]["value"]
            != tree_digest(workspace)
        ):
            raise TaskPackageError(
                "source.tree_digest does not match public/workspace"
            )
        _digest_matches_file(
            grader,
            manifest["grader"]["hidden_inputs_digest"],
            context="grader.hidden_inputs_digest",
        )
        _digest_matches_file(
            result_schema,
            manifest["grader"]["result_schema_digest"],
            context="grader.result_schema_digest",
        )

        evidence = manifest["validation_evidence"]
        cases: list[tuple[str, Path]] = []
        for context, descriptor, expected in (
            ("validation_evidence.oracle", evidence["oracle"], "pass"),
            ("validation_evidence.noop", evidence["noop"], "fail"),
            *(
                (
                    f"validation_evidence.alternative_solutions[{index}]",
                    descriptor,
                    "pass",
                )
                for index, descriptor in enumerate(
                    evidence["alternative_solutions"]
                )
            ),
            *(
                (
                    f"validation_evidence.exploit_cases[{index}]",
                    descriptor,
                    "fail",
                )
                for index, descriptor in enumerate(evidence["exploit_cases"])
            ),
            *(
                (
                    f"validation_evidence.mutation_cases[{index}]",
                    descriptor,
                    "fail",
                )
                for index, descriptor in enumerate(evidence["mutation_cases"])
            ),
        ):
            cases.append(
                _validation_case(
                    path,
                    descriptor,
                    context=context,
                    expected=expected,
                )
            )

        case_paths = [relative for relative, _ in cases]
        if case_paths[1] != "public/workspace":
            raise TaskPackageError(
                "no-op validation case must reference public/workspace"
            )
        if any(
            not relative.startswith("trusted/")
            for index, relative in enumerate(case_paths)
            if index != 1
        ):
            raise TaskPackageError(
                "oracle, alternative, exploit, and mutation cases "
                "must remain under trusted/"
            )
        if len(set(case_paths)) != len(case_paths):
            raise TaskPackageError("validation cases must use distinct candidates")

        review_descriptor = evidence["independent_review"]
        review_path = _descriptor_file(
            path,
            review_descriptor,
            context="validation_evidence.independent_review",
        )
        review = _load_json_object(
            review_path,
            context="validation_evidence.independent_review",
        )
        review_evidence = _parse_review_evidence(
            review,
            descriptor_schema=review_descriptor["schema"],
            manifest=manifest,
            reviewer_trust=reviewer_trust,
        )

        frozen_manifest = canonical_json_bytes(manifest).decode("utf-8")
        return cls(
            root=path,
            _manifest_json=frozen_manifest,
            digest=tree_digest(path),
            review_evidence=review_evidence,
        )

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    @property
    def category(self) -> str:
        return str(self.manifest["category"])

    @property
    def review_level(self) -> ReviewLevel:
        return self.review_evidence.review_level

    @property
    def suite_status(self) -> ReviewSuiteStatus:
        return self.review_evidence.suite_status

    @property
    def official_review_eligible(self) -> bool:
        return self.review_evidence.official_review_eligible

    @property
    def task_ref(self) -> TaskRef:
        return TaskRef(id=self.id, version=self.version, digest=self.digest)

    @property
    def prompt_path(self) -> Path:
        return confined_path(self.root, self.manifest["prompt"]["path"])

    @property
    def public_workspace(self) -> Path:
        return confined_path(self.root, "public/workspace")

    @property
    def grader_path(self) -> Path:
        return confined_path(self.root, "trusted/grader.json")

    def candidate_path(self, relative: str) -> Path:
        path = confined_path(self.root, relative, field="candidate")
        if not path.is_dir():
            raise TaskPackageError(f"candidate is not a directory: {relative}")
        return path

    def validation_cases(self) -> tuple[tuple["TaskGate", str, Path, bool], ...]:
        evidence = self.manifest["validation_evidence"]

        def candidate(
            descriptor: dict[str, Any],
            *,
            context: str,
            expected: str,
        ) -> Path:
            _, path = _validation_case(
                self.root,
                descriptor,
                context=context,
                expected=expected,
            )
            return path

        cases: list[tuple[TaskGate, str, Path, bool]] = [
            (
                TaskGate.ORACLE,
                "oracle",
                candidate(
                    evidence["oracle"],
                    context="validation_evidence.oracle",
                    expected="pass",
                ),
                True,
            ),
            (
                TaskGate.NO_OP,
                "no_op",
                candidate(
                    evidence["noop"],
                    context="validation_evidence.noop",
                    expected="fail",
                ),
                False,
            ),
        ]
        for field, gate, expected in (
            ("alternative_solutions", TaskGate.ALTERNATIVE, True),
            ("exploit_cases", TaskGate.EXPLOIT, False),
            ("mutation_cases", TaskGate.MUTATION, False),
        ):
            for index, descriptor in enumerate(evidence[field]):
                cases.append(
                    (
                        gate,
                        f"{field}[{index}]",
                        candidate(
                            descriptor,
                            context=f"validation_evidence.{field}[{index}]",
                            expected="pass" if expected else "fail",
                        ),
                        expected,
                    )
                )
        return tuple(cases)


def load_task_suite(
    roots: Sequence[Path | str],
    *,
    reviewer_trust: ReviewerTrustVerifier | None = None,
) -> tuple[TaskPackage, ...]:
    packages = tuple(
        TaskPackage.load(root, reviewer_trust=reviewer_trust) for root in roots
    )
    if not packages:
        raise TaskPackageError("task suite must contain at least one package")
    seen_ids: set[str] = set()
    for package in packages:
        if package.id in seen_ids:
            raise TaskPackageError(f"duplicate task id in suite: {package.id}")
        seen_ids.add(package.id)
    return packages


class TaskGate(str, Enum):
    ORACLE = "oracle"
    NO_OP = "no_op"
    ALTERNATIVE = "alternative"
    EXPLOIT = "exploit"
    MUTATION = "mutation"
    DETERMINISM = "determinism"


@dataclass(frozen=True, slots=True)
class TaskGateResult:
    gate: TaskGate
    case: str
    expected_grade_pass: bool
    observed_grade_pass: bool | None
    observed_score: float | None
    deterministic: bool
    passed: bool
    evidence_digest: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "case": self.case,
            "expected_grade_pass": self.expected_grade_pass,
            "observed_grade_pass": self.observed_grade_pass,
            "observed_score": self.observed_score,
            "deterministic": self.deterministic,
            "passed": self.passed,
            "evidence_digest": self.evidence_digest,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TaskValidationReport:
    task_id: str
    task_version: str
    package_digest: str
    review_level: ReviewLevel
    suite_status: ReviewSuiteStatus
    official_review_eligible: bool
    reviewer_count: int
    human_reviewer_count: int
    gates: tuple[TaskGateResult, ...]

    @property
    def eligible(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates)

    @property
    def machine_eligible(self) -> bool:
        return self.eligible

    @property
    def official_eligible(self) -> bool:
        return self.machine_eligible and self.official_review_eligible

    @property
    def grader_replay_count(self) -> int:
        return sum(
            1 for gate in self.gates if gate.gate is not TaskGate.DETERMINISM
        )

    @property
    def grader_nondeterministic_count(self) -> int:
        return sum(
            1
            for gate in self.gates
            if gate.gate is not TaskGate.DETERMINISM and not gate.deterministic
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.task-validation/v1",
            "task_id": self.task_id,
            "task_version": self.task_version,
            "package_digest": self.package_digest,
            "eligible": self.eligible,
            "machine_eligible": self.machine_eligible,
            "review_level": self.review_level.value,
            "suite_status": self.suite_status.value,
            "official_review_eligible": self.official_review_eligible,
            "official_eligible": self.official_eligible,
            "reviewer_count": self.reviewer_count,
            "human_reviewer_count": self.human_reviewer_count,
            "grader_replay_count": self.grader_replay_count,
            "grader_nondeterministic_count": self.grader_nondeterministic_count,
            "gates": [gate.to_dict() for gate in self.gates],
        }


class TaskPackageValidator:
    """Run all machine-checkable acceptance gates without trusting task claims."""

    def validate(
        self,
        package: TaskPackage,
        grader: TaskGrader,
    ) -> TaskValidationReport:
        results: list[TaskGateResult] = []
        deterministic_cases: list[bool] = []
        receipt = ControllerAssertedLifecycleReceipt.completed(
            controller_id="task-package-validator"
        )

        for gate, case, candidate, expected_pass in package.validation_cases():
            try:
                first = grader.grade(
                    package,
                    candidate,
                    lifecycle_receipt=receipt,
                )
                second = grader.grade(
                    package,
                    candidate,
                    lifecycle_receipt=receipt,
                )
                deterministic = (
                    first.result_digest == second.result_digest
                    and first.passed == second.passed
                    and float(first.score) == float(second.score)
                )
                expectation_met = first.passed is expected_pass
                if expected_pass:
                    expectation_met = expectation_met and float(first.score) == 1.0
                gate_passed = deterministic and expectation_met
                message = (
                    "expected result observed"
                    if gate_passed
                    else "grade expectation or repeatability failed"
                )
                results.append(
                    TaskGateResult(
                        gate=gate,
                        case=case,
                        expected_grade_pass=expected_pass,
                        observed_grade_pass=first.passed,
                        observed_score=float(first.score),
                        deterministic=deterministic,
                        passed=gate_passed,
                        evidence_digest=first.result_digest,
                        message=message,
                    )
                )
                deterministic_cases.append(deterministic)
            except Exception as exc:  # Validation reports all gates instead of hiding one.
                results.append(
                    TaskGateResult(
                        gate=gate,
                        case=case,
                        expected_grade_pass=expected_pass,
                        observed_grade_pass=None,
                        observed_score=None,
                        deterministic=False,
                        passed=False,
                        evidence_digest=None,
                        message=f"grader error: {type(exc).__name__}: {exc}",
                    )
                )
                deterministic_cases.append(False)

        all_deterministic = bool(deterministic_cases) and all(deterministic_cases)
        results.append(
            TaskGateResult(
                gate=TaskGate.DETERMINISM,
                case="all-validation-cases",
                expected_grade_pass=True,
                observed_grade_pass=all_deterministic,
                observed_score=1.0 if all_deterministic else 0.0,
                deterministic=all_deterministic,
                passed=all_deterministic,
                evidence_digest=None,
                message=(
                    "all repeated grades were byte-stable"
                    if all_deterministic
                    else "one or more repeated grades differed"
                ),
            )
        )
        return TaskValidationReport(
            task_id=package.id,
            task_version=package.version,
            package_digest=package.digest,
            review_level=package.review_level,
            suite_status=package.suite_status,
            official_review_eligible=package.official_review_eligible,
            reviewer_count=len(package.review_evidence.reviewers),
            human_reviewer_count=package.review_evidence.human_reviewer_count,
            gates=tuple(results),
        )
