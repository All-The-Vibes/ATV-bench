"""Fail-closed adapter from private eval objects to the public protocol v1."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from atv_bench.security.signing import (
    AttestationRole,
    OfficialBindings,
    OfficialTrustPolicy,
    SignedDsseEnvelope,
    TrustPolicyError,
)

from atv_bench.protocol import (
    SchemaKind,
    TrialStatus,
    canonical_digest,
    canonical_json_bytes as protocol_json_bytes,
    default_schema_store,
    sha256_bytes,
    strict_json_loads,
    verify_bundle_manifest,
)

from ._canonical import (
    RegularFileSnapshot,
    canonical_json_bytes as relaxed_json_bytes,
    safe_relative_path,
)
from .grader import (
    GradeResult,
    RunnerLifecycleReceipt,
    VerifiedRunnerLifecycleReceipt,
)
from .stats import AnalysisMode, Decision, PairedAnalysis
from .trial import (
    HarnessStatus,
    InfrastructureStatus,
    TrialAttempt,
    TrialOutcome,
    TrialSpec,
)


class ProtocolExportError(ValueError):
    """Private eval evidence cannot be exported as canonical protocol evidence."""


class _PolicyBoundBundle(dict):
    """In-process bundle carrying the explicitly supplied public trust policy.

    JSON serialization intentionally drops this metadata, so independent/public
    verification of plain JSON still requires an explicit policy argument.
    """

    def __init__(
        self,
        value: Mapping[str, Any],
        policy: OfficialTrustPolicy,
    ):
        super().__init__(value)
        self.official_trust_policy = policy

    def __deepcopy__(self, memo):
        copied = _PolicyBoundBundle(
            deepcopy(dict(self), memo),
            self.official_trust_policy,
        )
        memo[id(self)] = copied
        return copied


LOCAL_TRUST_TIER = "local-self-attested"
OFFICIAL_TRUST_TIER = "official-attested"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_OFFICIAL_ROLES = frozenset(
    {"admission", "harness-build", "execution", "model", "evaluation"}
)


INFRASTRUCTURE_STATUS_MAP: Mapping[
    InfrastructureStatus, TrialStatus | None
] = MappingProxyType(
    {
        InfrastructureStatus.OK: None,
        InfrastructureStatus.SETUP_FAILED: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.RUNNER_FAILED: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.MODEL_GATEWAY_FAILED: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.GRADER_FAILED: TrialStatus.GRADER_FAILED,
        InfrastructureStatus.ARTIFACT_CORRUPT: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.CANCELLED: TrialStatus.CANCELLED,
    }
)

HARNESS_STATUS_MAP: Mapping[HarnessStatus, TrialStatus | None] = MappingProxyType(
    {
        HarnessStatus.NOT_RUN: None,
        HarnessStatus.COMPLETED: None,
        HarnessStatus.NO_EDIT: TrialStatus.NO_EDIT,
        HarnessStatus.INVALID_ARTIFACT: TrialStatus.INVALID_ARTIFACT,
        HarnessStatus.TIMED_OUT: TrialStatus.TASK_TIMEOUT,
        HarnessStatus.BUDGET_EXHAUSTED: TrialStatus.BUDGET_EXHAUSTED,
        HarnessStatus.MODEL_UNREACHABLE: TrialStatus.MODEL_UNREACHABLE,
        HarnessStatus.AUTH_FAILED: TrialStatus.AUTH_FAILED,
        HarnessStatus.POLICY_DENIED: TrialStatus.POLICY_DENIED,
        HarnessStatus.PROTOCOL_ERROR: TrialStatus.PROTOCOL_ERROR,
        HarnessStatus.CRASHED: TrialStatus.HARNESS_CRASH,
    }
)


def protocol_trial_status(
    outcome: TrialOutcome,
    grade: GradeResult | None,
) -> TrialStatus:
    """Map every valid eval outcome to exactly one authoritative TrialStatus."""

    infrastructure = INFRASTRUCTURE_STATUS_MAP[outcome.infrastructure_status]
    if outcome.infrastructure_status is not InfrastructureStatus.OK:
        if infrastructure is None:
            raise ProtocolExportError("non-OK infrastructure status is unmapped")
        if grade is not None:
            raise ProtocolExportError(
                "infrastructure failures cannot export a grader result"
            )
        return infrastructure

    mapped = HARNESS_STATUS_MAP[outcome.harness_status]
    if outcome.harness_status is HarnessStatus.NOT_RUN:
        raise ProtocolExportError("rankable eval outcome says the harness never ran")
    if outcome.harness_status is not HarnessStatus.COMPLETED:
        if mapped is None:
            raise ProtocolExportError("harness status is unmapped")
        if grade is not None:
            raise ProtocolExportError(
                "non-completed harness status cannot export a grader result"
            )
        return mapped

    if grade is None:
        raise ProtocolExportError("completed harness outcome requires GradeResult")
    if outcome.score != grade.score:
        raise ProtocolExportError("TrialOutcome score does not match GradeResult")
    if grade.passed:
        return TrialStatus.SUCCESS
    if grade.score > 0.0:
        return TrialStatus.PARTIAL
    return TrialStatus.TASK_FAILED


@dataclass(frozen=True, slots=True)
class EvidenceDocument:
    schema: str
    path: str
    media_type: str
    data: bytes

    def __post_init__(self) -> None:
        safe_relative_path(self.path, field="evidence document path")
        if not self.schema or not self.media_type:
            raise ProtocolExportError("document schema and media_type are required")
        if not isinstance(self.data, bytes):
            raise TypeError("document data must be bytes")

    @classmethod
    def from_protocol_json(
        cls,
        *,
        schema: str,
        path: str,
        value: Mapping[str, Any],
    ) -> "EvidenceDocument":
        return cls(
            schema=schema,
            path=path,
            media_type="application/json",
            data=protocol_json_bytes(dict(value)),
        )

    @classmethod
    def from_relaxed_json(
        cls,
        *,
        schema: str,
        path: str,
        value: Mapping[str, Any],
    ) -> "EvidenceDocument":
        return cls(
            schema=schema,
            path=path,
            media_type="application/json",
            data=relaxed_json_bytes(dict(value)),
        )

    @property
    def descriptor(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "path": self.path,
            "media_type": self.media_type,
            "size_bytes": len(self.data),
            "digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(self.data),
            },
        }


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    path: str
    media_type: str
    data: bytes
    role: str

    def __post_init__(self) -> None:
        safe_relative_path(self.path, field="evidence artifact path")
        if not self.media_type or not self.role:
            raise ProtocolExportError("artifact media_type and role are required")
        if not isinstance(self.data, bytes):
            raise TypeError("artifact data must be bytes")

    @property
    def descriptor(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "size_bytes": len(self.data),
            "digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(self.data),
            },
            "role": self.role,
        }


def output_tree_evidence(
    snapshots: Sequence[RegularFileSnapshot],
    *,
    path: str = "artifacts/output-tree.json",
) -> EvidenceArtifact:
    """Encode the exact eval tree manifest whose digest GradeResult records."""

    manifest = {
        "files": [
            {
                "path": snapshot.path,
                "size": snapshot.size,
                "sha256": snapshot.sha256,
            }
            for snapshot in sorted(snapshots, key=lambda item: item.path)
        ]
    }
    return EvidenceArtifact(
        path=path,
        media_type="application/json",
        data=protocol_json_bytes(manifest),
        role="output-tree",
    )


@dataclass(frozen=True, slots=True)
class ModelEvidence:
    requested: str
    gateway_resolved: str | None
    provider_reported: str | None
    provider: str | None
    request_ids: tuple[str, ...]
    receipt: EvidenceDocument

    def protocol_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "gateway_resolved": self.gateway_resolved,
            "provider_reported": self.provider_reported,
            "provider": self.provider,
            "request_ids": list(self.request_ids),
            "receipt": self.receipt.descriptor,
        }


@dataclass(frozen=True, slots=True)
class GraderEvidence:
    identity: Mapping[str, Any]
    image_digest: str


@dataclass(frozen=True, slots=True)
class AttestationEvidence:
    role: str
    document: EvidenceDocument

    def protocol_dict(self) -> dict[str, Any]:
        return {"role": self.role, "document": self.document.descriptor}


@dataclass(frozen=True, slots=True)
class RunnerEvidence:
    run_id: str
    track: str
    task_set: Mapping[str, Any]
    identity: Mapping[str, Any]
    platform: Mapping[str, Any]
    runtime_digest: str
    started_at: str
    ended_at: str
    duration_ms: int
    exit: Mapping[str, Any]
    reported_usage: Mapping[str, Any]
    observed_usage: Mapping[str, Any]
    authoritative_usage: Mapping[str, Any]
    lifecycle_receipt: RunnerLifecycleReceipt
    lifecycle_document: EvidenceDocument
    prior_attempt_ids: tuple[str, ...] = ()
    retry_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolExportEvidence:
    trust_tier: str
    created_at: str
    harness_manifest: Mapping[str, Any]
    task_manifest: Mapping[str, Any]
    trial_request: Mapping[str, Any]
    event_stream: EvidenceDocument
    harness_result: Mapping[str, Any]
    runner: RunnerEvidence
    models: tuple[ModelEvidence, ...]
    grader: GraderEvidence | None
    attestations: tuple[AttestationEvidence, ...]
    output_tree: EvidenceArtifact | None
    artifacts: tuple[EvidenceArtifact, ...] = ()
    logs: tuple[EvidenceDocument, ...] = ()
    model_free: bool = False


@dataclass(frozen=True, slots=True)
class ProtocolExport:
    bundle: Mapping[str, Any]
    documents: Mapping[str, bytes]
    official_trust_policy: OfficialTrustPolicy | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def verify(
        self,
        official_trust_policy: OfficialTrustPolicy | None = None,
    ) -> Mapping[str, Any]:
        return verify_public_protocol_export(
            self.bundle,
            self.documents,
            official_trust_policy=(
                official_trust_policy
                or self.official_trust_policy
                or getattr(self.bundle, "official_trust_policy", None)
            ),
        )

    @property
    def trial_result(self) -> Mapping[str, Any]:
        return self.verify()


def _digest(value: str) -> dict[str, str]:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProtocolExportError("expected lowercase sha256 digest")
    return {"algorithm": "sha256", "value": value}


def _versioned_ref(id_: str, version: str, digest: str) -> dict[str, Any]:
    return {
        "id": id_,
        "version": version,
        "manifest_digest": _digest(digest),
    }


def _model_policy_identity(model_policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": model_policy["id"],
        "version": model_policy["version"],
        "policy_digest": deepcopy(model_policy["policy_digest"]),
    }


def model_policy_analysis_id(model_policy: Mapping[str, Any]) -> str:
    """Return the full immutable model-policy identity used by analyses."""

    try:
        policy_id = model_policy["id"]
        version = model_policy["version"]
        digest = model_policy["policy_digest"]["value"]
        _digest(digest)
    except (KeyError, TypeError, ProtocolExportError) as exc:
        raise ProtocolExportError("model policy identity is incomplete") from exc
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolExportError("model policy id is invalid")
    if not isinstance(version, str) or not version:
        raise ProtocolExportError("model policy version is invalid")
    return f"{policy_id}@{version}#sha256:{digest}"


def _budget_identity(
    profile_id: str,
    budget_limits: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "limits_digest": canonical_digest(budget_limits),
    }


def budget_analysis_id(
    profile_id: str,
    budget_limits: Mapping[str, Any],
) -> str:
    """Return the full immutable budget identity used by analyses."""

    if not isinstance(profile_id, str) or not profile_id:
        raise ProtocolExportError("budget profile id is invalid")
    digest = canonical_digest(budget_limits)["value"]
    return f"{profile_id}#sha256:{digest}"


def _analysis_identity(
    *,
    model_policy: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "model_policy": _model_policy_identity(model_policy),
        "budget": deepcopy(dict(budget)),
    }


def official_result_subject_digest(
    *,
    request: Mapping[str, Any],
    harness_result: Mapping[str, Any],
) -> str:
    """Digest the exact pre-grading result subject shared by official signatures.

    The subject deliberately excludes signatures and the final bundle wrapper to
    avoid a circular digest. It includes the complete canonical request and harness
    result, so run/trial identity or execution-result rewrites require new official
    signatures. The evaluation signature separately binds the canonical grade core.
    """

    if request.get("schema") != "atv.trial-request/v1":
        raise ProtocolExportError("official result subject request schema is invalid")
    if harness_result.get("schema") != "atv.harness-result/v1":
        raise ProtocolExportError(
            "official result subject harness-result schema is invalid"
        )
    try:
        subject = {
            "schema": "atv.official-result-subject/v1",
            "benchmark_release": request["benchmark_release"],
            "run_id": request["run_id"],
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "trial_request_digest": canonical_digest(request),
            "harness_result_digest": canonical_digest(harness_result),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolExportError(
            "official result subject identity is incomplete"
        ) from exc
    return canonical_digest(subject)["value"]


def _failure_code(value: str, fallback: str) -> str:
    candidate = value.strip().lower().replace("_", "-")
    candidate = re.sub(r"[^a-z0-9._-]+", "-", candidate).strip("-._")
    if not candidate or not _IDENTIFIER.fullmatch(candidate):
        return fallback
    return candidate


def _failure(
    status: TrialStatus,
    outcome: TrialOutcome,
) -> dict[str, Any] | None:
    if status in {TrialStatus.SUCCESS, TrialStatus.TASK_FAILED, TrialStatus.PARTIAL}:
        return None
    defaults: dict[TrialStatus, tuple[str, str, bool, bool]] = {
        TrialStatus.NO_EDIT: ("no-edit", "harness", False, False),
        TrialStatus.INVALID_ARTIFACT: (
            "invalid-artifact",
            "artifact",
            False,
            False,
        ),
        TrialStatus.TASK_TIMEOUT: ("task-timeout", "harness", False, False),
        TrialStatus.MODEL_UNREACHABLE: (
            "model-unreachable",
            "model",
            False,
            False,
        ),
        TrialStatus.AUTH_FAILED: ("auth-failed", "model", False, False),
        TrialStatus.POLICY_DENIED: ("policy-denied", "policy", False, False),
        TrialStatus.BUDGET_EXHAUSTED: (
            "budget-exhausted",
            "harness",
            False,
            False,
        ),
        TrialStatus.HARNESS_CRASH: ("harness-crash", "harness", False, False),
        TrialStatus.PROTOCOL_ERROR: (
            "protocol-error",
            "protocol",
            False,
            False,
        ),
        TrialStatus.GRADER_FAILED: ("grader-failed", "grader", True, True),
        TrialStatus.INFRASTRUCTURE_ERROR: (
            "infrastructure-error",
            "infrastructure",
            True,
            True,
        ),
        TrialStatus.CANCELLED: ("cancelled", "runner", False, True),
    }
    code, scope, retryable, infrastructure = defaults[status]
    return {
        "code": _failure_code(outcome.reason_code, code),
        "scope": scope,
        "retryable": retryable,
        "infrastructure": infrastructure,
    }


def _ensure_descriptor_matches(
    descriptor: Mapping[str, Any],
    data: bytes,
) -> None:
    if descriptor["size_bytes"] != len(data):
        raise ProtocolExportError(f"descriptor size mismatch: {descriptor['path']}")
    if descriptor["digest"] != {
        "algorithm": "sha256",
        "value": sha256_bytes(data),
    }:
        raise ProtocolExportError(f"descriptor digest mismatch: {descriptor['path']}")


def _add_document(
    documents: dict[str, bytes],
    descriptor: Mapping[str, Any],
    data: bytes,
) -> None:
    path = str(descriptor["path"])
    _ensure_descriptor_matches(descriptor, data)
    if path in documents:
        raise ProtocolExportError(f"evidence path collision: {path}")
    documents[path] = data


def _validate_identity_bindings(
    spec: TrialSpec,
    attempt: TrialAttempt,
    evidence: ProtocolExportEvidence,
) -> None:
    store = default_schema_store()
    harness = dict(evidence.harness_manifest)
    task = dict(evidence.task_manifest)
    request = dict(evidence.trial_request)
    store.validate(harness, SchemaKind.HARNESS)
    store.validate(task, SchemaKind.TASK)
    store.validate(request, SchemaKind.TRIAL_REQUEST)

    harness_digest = canonical_digest(harness)
    task_digest = canonical_digest(task)
    if spec.harness.digest != harness_digest["value"]:
        raise ProtocolExportError("TrialSpec harness digest does not match manifest")
    if spec.task.digest != task_digest["value"]:
        raise ProtocolExportError("TrialSpec task digest does not match manifest")
    if request["harness"] != _versioned_ref(
        spec.harness.id, spec.harness.version, spec.harness.digest
    ):
        raise ProtocolExportError("trial request harness does not match TrialSpec")
    if request["task"] != _versioned_ref(
        spec.task.id, spec.task.version, spec.task.digest
    ):
        raise ProtocolExportError("trial request task does not match TrialSpec")
    if (
        request["model_policy"]["id"] != spec.model_policy.id
        or request["model_policy"]["version"] != spec.model_policy.version
        or request["model_policy"]["policy_digest"] != _digest(spec.model_policy.digest)
    ):
        raise ProtocolExportError("trial request model policy does not match TrialSpec")
    if request["benchmark_release"] != spec.benchmark_release:
        raise ProtocolExportError("benchmark release does not match TrialSpec")
    if request["schedule_id"] != spec.schedule_id:
        raise ProtocolExportError("schedule id does not match TrialSpec")
    if request["trial_id"] != spec.trial_id:
        raise ProtocolExportError("trial id does not match TrialSpec")
    if request["attempt_id"] != attempt.attempt_id:
        raise ProtocolExportError("attempt id does not match TrialAttempt")
    if request["order_assignment"]["repetition"] != spec.repetition:
        raise ProtocolExportError("request repetition does not match TrialSpec")
    if spec.protocol_version != "atv.trial/v1" or request["protocol_version"] != 1:
        raise ProtocolExportError("unsupported eval/protocol version mapping")
    if request["run_id"] != evidence.runner.run_id:
        raise ProtocolExportError("trial request run_id does not match runner evidence")
    if request["track"] != evidence.runner.track:
        raise ProtocolExportError("trial request track does not match runner evidence")
    if request["task_set"] != dict(evidence.runner.task_set):
        raise ProtocolExportError(
            "trial request task_set does not match runner evidence"
        )
    budget = spec.budget_profile.budget
    expected_budget_fields = {
        "wall_time_ms": budget.wall_time_seconds * 1_000,
        "model_total_tokens": budget.max_model_tokens,
        "model_calls": budget.max_model_calls,
        "cost_microusd": budget.max_cost_microusd,
    }
    if any(
        request["budget_limits"].get(name) != value
        for name, value in expected_budget_fields.items()
    ):
        raise ProtocolExportError(
            "trial request budget limits do not match TrialSpec budget profile"
        )


def _validate_analysis_identity(
    spec: TrialSpec,
    analysis: PairedAnalysis,
    request: Mapping[str, Any],
) -> None:
    expected_model_policy_id = model_policy_analysis_id(request["model_policy"])
    expected_budget_profile_id = budget_analysis_id(
        spec.budget_profile.id,
        request["budget_limits"],
    )
    if analysis.model_policy_id != expected_model_policy_id:
        raise ProtocolExportError(
            "analysis model policy identity does not match TrialSpec"
        )
    if analysis.budget_profile_id != expected_budget_profile_id:
        raise ProtocolExportError(
            "analysis budget profile identity does not match TrialSpec"
        )


def _validate_runner_exit(
    status: TrialStatus,
    exit_evidence: Mapping[str, Any],
) -> None:
    if status in {TrialStatus.SUCCESS, TrialStatus.TASK_FAILED, TrialStatus.PARTIAL}:
        if (
            exit_evidence.get("code") != 0
            or exit_evidence.get("signal") is not None
            or exit_evidence.get("timed_out")
            or exit_evidence.get("cancelled")
        ):
            raise ProtocolExportError("completed evaluation requires a clean exit")
    if status is TrialStatus.TASK_TIMEOUT and not exit_evidence.get("timed_out"):
        raise ProtocolExportError("task_timeout requires timed_out execution evidence")
    if status is TrialStatus.CANCELLED and not exit_evidence.get("cancelled"):
        raise ProtocolExportError("cancelled status requires cancelled exit evidence")


_HARNESS_PROTOCOL_STATUS: Mapping[HarnessStatus, str | None] = MappingProxyType(
    {
        HarnessStatus.NOT_RUN: None,
        HarnessStatus.COMPLETED: "completed",
        HarnessStatus.NO_EDIT: "no_edit",
        HarnessStatus.INVALID_ARTIFACT: "invalid_artifact",
        HarnessStatus.TIMED_OUT: "task_timeout",
        HarnessStatus.BUDGET_EXHAUSTED: "budget_exhausted",
        HarnessStatus.MODEL_UNREACHABLE: "model_unreachable",
        HarnessStatus.AUTH_FAILED: "auth_failed",
        HarnessStatus.POLICY_DENIED: "policy_denied",
        HarnessStatus.PROTOCOL_ERROR: None,
        HarnessStatus.CRASHED: "harness_crash",
    }
)


def _validate_harness_result(
    *,
    outcome: TrialOutcome,
    grade: GradeResult | None,
    evidence: ProtocolExportEvidence,
) -> None:
    result = evidence.harness_result
    if result.get("schema") != "atv.harness-result/v1":
        raise ProtocolExportError("harness result schema is missing or invalid")
    expected = _HARNESS_PROTOCOL_STATUS[outcome.harness_status]
    if (
        outcome.infrastructure_status is InfrastructureStatus.OK
        and expected is not None
        and result.get("status") != expected
    ):
        raise ProtocolExportError("harness result status contradicts TrialOutcome")
    if result.get("exit") != dict(evidence.runner.exit):
        raise ProtocolExportError("harness result exit contradicts runner evidence")
    if result.get("reported_usage") != dict(evidence.runner.reported_usage):
        raise ProtocolExportError("harness result usage contradicts runner evidence")
    expected_artifacts = [artifact.descriptor for artifact in evidence.artifacts]
    if result.get("artifacts") != expected_artifacts:
        raise ProtocolExportError("harness result artifacts contradict supplied evidence")
    expected_tree = _digest(grade.output_tree_digest) if grade is not None else None
    if result.get("output_tree_digest") != expected_tree:
        raise ProtocolExportError("harness result output-tree digest is mismatched")


def _official_bindings(
    *,
    spec: TrialSpec,
    attempt: TrialAttempt,
    grade: GradeResult,
    evidence: ProtocolExportEvidence,
) -> OfficialBindings:
    if evidence.grader is None:
        raise ProtocolExportError("official bindings require grader evidence")
    return OfficialBindings(
        benchmark_release=spec.benchmark_release,
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        task_digest=spec.task.digest,
        harness_digest=spec.harness.digest,
        model_digest=spec.model_policy.digest,
        budget_digest=canonical_digest(
            evidence.trial_request["budget_limits"]
        )["value"],
        runner_digest=evidence.runner.runtime_digest,
        grader_digest=grade.grader_digest,
        grader_image_digest=evidence.grader.image_digest,
        output_digest=grade.output_tree_digest,
        result_digest=official_result_subject_digest(
            request=evidence.trial_request,
            harness_result=evidence.harness_result,
        ),
    )


def _attestation_envelope(attestation: AttestationEvidence) -> SignedDsseEnvelope:
    try:
        value = strict_json_loads(attestation.document.data.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise TypeError("envelope is not an object")
        return SignedDsseEnvelope.from_dict(value)
    except (UnicodeDecodeError, ValueError, TypeError, TrustPolicyError) as exc:
        raise ProtocolExportError(
            f"official {attestation.role} attestation is not a signed DSSE envelope"
        ) from exc


def _validate_trust(
    *,
    spec: TrialSpec,
    attempt: TrialAttempt,
    grade: GradeResult | None,
    analysis: PairedAnalysis,
    evidence: ProtocolExportEvidence,
    official_trust_policy: OfficialTrustPolicy | None,
) -> bool:
    receipt = evidence.runner.lifecycle_receipt
    receipt.validate_for_grading()
    if evidence.trust_tier == LOCAL_TRUST_TIER:
        if evidence.runner.lifecycle_document.descriptor["digest"] != _digest(
            receipt.receipt_digest
        ):
            raise ProtocolExportError(
                "runner lifecycle document does not match lifecycle receipt"
            )
    else:
        try:
            lifecycle_payload = strict_json_loads(
                evidence.runner.lifecycle_document.data.decode("utf-8")
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolExportError("official lifecycle document is malformed") from exc
        if (
            not isinstance(lifecycle_payload, Mapping)
            or lifecycle_payload.get("execution_envelope_digest")
            != receipt.receipt_digest
        ):
            raise ProtocolExportError(
                "official lifecycle document does not bind execution envelope"
            )
    roles = [attestation.role for attestation in evidence.attestations]
    if len(set(roles)) != len(roles):
        raise ProtocolExportError("attestation roles must be unique")

    if evidence.trust_tier == LOCAL_TRUST_TIER:
        if evidence.attestations:
            raise ProtocolExportError(
                "local exports cannot include official attestation roles"
            )
        if receipt.official_verified:
            raise ProtocolExportError(
                "official runner receipt cannot be relabelled as local evidence"
            )
        if grade is not None and grade.official_verified:
            raise ProtocolExportError(
                "official grade cannot be relabelled as local evidence"
            )
        return False

    if evidence.trust_tier != OFFICIAL_TRUST_TIER:
        raise ProtocolExportError("unsupported export trust tier")
    if official_trust_policy is None:
        raise ProtocolExportError(
            "official export requires explicit OfficialTrustPolicy"
        )
    if not isinstance(receipt, VerifiedRunnerLifecycleReceipt):
        raise ProtocolExportError(
            "official export requires VerifiedRunnerLifecycleReceipt"
        )
    if (
        not receipt.execution_complete
        or not receipt.official_verified
        or not receipt.credentials_destroyed
        or not receipt.hidden_inputs_mounted_after_exit
    ):
        raise ProtocolExportError("trusted runner lifecycle receipt is incomplete")
    if grade is not None:
        if not grade.official_verified:
            raise ProtocolExportError("official export requires official grader result")
        if grade.lifecycle_receipt_digest != receipt.receipt_digest:
            raise ProtocolExportError(
                "grader result does not bind the trusted runner receipt"
            )
        if grade.evaluation_envelope is None:
            raise ProtocolExportError(
                "official grader result lacks verified evaluation envelope"
            )
    if not analysis.publication_eligible:
        raise ProtocolExportError(
            "official export requires passing publication quality gates"
        )
    if analysis.publication_decision is Decision.INCONCLUSIVE:
        raise ProtocolExportError(
            "official rankable export requires a conclusive publication decision"
        )
    if (
        analysis.analysis_mode is not AnalysisMode.OFFICIAL
        or analysis.quality_gate_failures
        or analysis.task_count < 50
        or any(len(effect.repetitions) < 5 for effect in analysis.effects)
    ):
        raise ProtocolExportError(
            "analysis does not independently satisfy official publication gates"
        )
    if spec.harness.id not in {analysis.harness_a, analysis.harness_b}:
        raise ProtocolExportError("analysis does not include this harness")
    if spec.task.id not in {effect.task_id for effect in analysis.effects}:
        raise ProtocolExportError("analysis does not include this task")

    required_roles = {"admission", "harness-build", "execution", "evaluation"}
    if not evidence.model_free:
        required_roles.add("model")
    missing = required_roles - set(roles)
    if missing:
        raise ProtocolExportError(
            "official export is missing attestations: " + ", ".join(sorted(missing))
        )
    assert grade is not None
    bindings = _official_bindings(
        spec=spec,
        attempt=attempt,
        grade=grade,
        evidence=evidence,
    )
    by_role = {item.role: item for item in evidence.attestations}
    role_map = {role.value: role for role in AttestationRole}
    verified_envelopes: dict[str, SignedDsseEnvelope] = {}
    for role_name in required_roles:
        role = role_map[role_name]
        attestation = by_role[role_name]
        envelope = _attestation_envelope(attestation)
        claims: dict[str, Any] = {}
        if role is AttestationRole.EXECUTION:
            claims = {
                "execution_complete": True,
                "credentials_destroyed": True,
                "hidden_inputs_mounted_after_exit": True,
            }
        elif role is AttestationRole.EVALUATION:
            claims = {
                "lifecycle_receipt_digest": receipt.receipt_digest,
                "grade_core_digest": grade.grade_core_digest,
            }
        try:
            official_trust_policy.verify(
                envelope,
                role=role,
                bindings=bindings,
                required_claims=claims,
            )
        except TrustPolicyError as exc:
            raise ProtocolExportError(
                f"official {role_name} attestation did not verify: {exc}"
            ) from exc
        verified_envelopes[role_name] = envelope
    if (
        verified_envelopes["execution"].digest != receipt.receipt_digest
        or verified_envelopes["execution"].to_dict()
        != receipt.execution_envelope.to_dict()
    ):
        raise ProtocolExportError(
            "execution attestation does not match verified lifecycle receipt"
        )
    if (
        verified_envelopes["evaluation"].digest
        != grade.evaluation_envelope_digest
        or verified_envelopes["evaluation"].to_dict()
        != grade.evaluation_envelope.to_dict()
    ):
        raise ProtocolExportError(
            "evaluation attestation does not match official GradeResult"
        )
    return True


def _validate_models(
    evidence: ProtocolExportEvidence,
    *,
    spec: TrialSpec,
    attempt: TrialAttempt,
) -> None:
    allowed = set(evidence.trial_request["model_policy"]["allowed_models"])
    authoritative = evidence.runner.authoritative_usage
    if evidence.model_free:
        if evidence.models:
            raise ProtocolExportError("model-free export cannot contain model evidence")
        for field in (
            "model_input_tokens",
            "model_output_tokens",
            "model_total_tokens",
            "model_calls",
            "cost_microusd",
        ):
            if authoritative.get(field) not in {0, None}:
                raise ProtocolExportError(
                    "model-free export has nonzero authoritative model usage"
                )
        return
    if not evidence.models:
        raise ProtocolExportError("model-backed export requires gateway model evidence")
    for model in evidence.models:
        if model.requested not in allowed:
            raise ProtocolExportError("model evidence requested a disallowed model")
        if evidence.trust_tier == OFFICIAL_TRUST_TIER:
            if (
                model.gateway_resolved is None
                or model.provider_reported is None
                or model.provider is None
                or not model.request_ids
            ):
                raise ProtocolExportError(
                    "official model evidence is missing gateway/provider fields"
                )
        try:
            receipt = strict_json_loads(model.receipt.data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolExportError("model receipt is not valid canonical JSON") from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "atv.model-receipt/v1"
            or receipt.get("trial_id") != spec.trial_id
            or receipt.get("attempt_id") != attempt.attempt_id
            or receipt.get("requested_model") != model.requested
            or receipt.get("resolved_model") != model.gateway_resolved
            or receipt.get("provider_reported") != model.provider_reported
            or receipt.get("provider") != model.provider
            or receipt.get("request_ids") != list(model.request_ids)
        ):
            raise ProtocolExportError("model receipt does not bind supplied model evidence")


def _validate_artifact_contract(evidence: ProtocolExportEvidence) -> None:
    contract = evidence.trial_request["output"]
    descriptors = [artifact.descriptor for artifact in evidence.artifacts]
    paths = [descriptor["path"] for descriptor in descriptors]
    if len(paths) != len(set(paths)):
        raise ProtocolExportError("artifact paths must be unique")
    if len(paths) > contract["max_files"]:
        raise ProtocolExportError("artifact count exceeds trial output contract")
    if sum(descriptor["size_bytes"] for descriptor in descriptors) > contract[
        "max_total_bytes"
    ]:
        raise ProtocolExportError("artifact bytes exceed trial output contract")
    allowed_media_types = set(contract["allowed_media_types"])
    if any(
        descriptor["media_type"] not in allowed_media_types
        for descriptor in descriptors
    ):
        raise ProtocolExportError("artifact media type exceeds trial output contract")
    if not contract["allow_any_relative_path"]:
        allowed_paths = set(contract["allowed_paths"])
        if any(path not in allowed_paths for path in paths):
            raise ProtocolExportError("artifact path exceeds trial output contract")


def _evaluation(
    *,
    status: TrialStatus,
    grade: GradeResult | None,
    grader: GraderEvidence | None,
    grader_document: EvidenceDocument | None,
    task_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if status in {TrialStatus.SUCCESS, TrialStatus.TASK_FAILED, TrialStatus.PARTIAL}:
        if grade is None or grader is None or grader_document is None:
            raise ProtocolExportError(
                "graded trial status requires grade and grader evidence"
            )
        possible = int(task_manifest["grader"]["score_scale"]["possible"])
        earned = int(round(grade.score * possible))
        task_outcome = {
            TrialStatus.SUCCESS: "pass",
            TrialStatus.TASK_FAILED: "fail",
            TrialStatus.PARTIAL: "partial",
        }[status]
        return {
            "state": "completed",
            "task_outcome": task_outcome,
            "task_success": status is TrialStatus.SUCCESS,
            "score": {
                "earned": earned,
                "possible": possible,
                "unit": "points",
            },
            "metrics": [],
            "grader": {
                "identity": dict(grader.identity),
                "image_digest": _digest(grader.image_digest),
            },
            "raw_result_digest": deepcopy(grader_document.descriptor["digest"]),
        }
    if status is TrialStatus.GRADER_FAILED:
        return {
            "state": "grader_failed",
            "task_outcome": "not_graded",
            "task_success": None,
            "score": None,
            "metrics": [],
            "grader": None,
            "raw_result_digest": None,
        }
    return {
        "state": "not_run",
        "task_outcome": "not_graded",
        "task_success": None,
        "score": None,
        "metrics": [],
        "grader": None,
        "raw_result_digest": None,
    }


def export_protocol_bundle(
    *,
    spec: TrialSpec,
    attempt: TrialAttempt,
    outcome: TrialOutcome,
    grade: GradeResult | None,
    analysis: PairedAnalysis,
    evidence: ProtocolExportEvidence,
    official_trust_policy: OfficialTrustPolicy | None = None,
) -> ProtocolExport:
    """Export canonical trial-result and bundle documents or fail closed."""

    if attempt.spec != spec:
        raise ProtocolExportError("TrialAttempt does not bind TrialSpec")
    if outcome.trial_id != spec.trial_id or outcome.attempt_id != attempt.attempt_id:
        raise ProtocolExportError("TrialOutcome does not bind TrialSpec/TrialAttempt")
    _validate_identity_bindings(spec, attempt, evidence)
    _validate_analysis_identity(spec, analysis, evidence.trial_request)
    status = protocol_trial_status(outcome, grade)
    _validate_runner_exit(status, evidence.runner.exit)
    _validate_models(evidence, spec=spec, attempt=attempt)
    _validate_artifact_contract(evidence)
    _validate_harness_result(
        outcome=outcome,
        grade=grade,
        evidence=evidence,
    )
    rankable = _validate_trust(
        spec=spec,
        attempt=attempt,
        grade=grade,
        analysis=analysis,
        evidence=evidence,
        official_trust_policy=official_trust_policy,
    )
    if evidence.trust_tier == OFFICIAL_TRUST_TIER and (
        grade is None or evidence.grader is None
    ):
        raise ProtocolExportError(
            "official export requires grader identity and grader result"
        )

    if status in {TrialStatus.SUCCESS, TrialStatus.TASK_FAILED, TrialStatus.PARTIAL}:
        if evidence.output_tree is None or grade is None:
            raise ProtocolExportError("graded status requires output-tree evidence")
        if evidence.output_tree.descriptor["digest"] != _digest(
            grade.output_tree_digest
        ):
            raise ProtocolExportError("output-tree evidence does not match GradeResult")
        if evidence.grader is None:
            raise ProtocolExportError("graded status requires grader identity")
    elif evidence.output_tree is not None and outcome.infrastructure_status is not InfrastructureStatus.OK:
        raise ProtocolExportError(
            "infrastructure failure cannot publish an authoritative output tree"
        )

    request_document = EvidenceDocument.from_protocol_json(
        schema="atv.trial-request/v1",
        path="trial/request.json",
        value=evidence.trial_request,
    )
    harness_manifest_document = EvidenceDocument.from_protocol_json(
        schema="atv.harness/v1",
        path="manifests/harness.json",
        value=evidence.harness_manifest,
    )
    task_manifest_document = EvidenceDocument.from_protocol_json(
        schema="atv.task/v1",
        path="manifests/task.json",
        value=evidence.task_manifest,
    )
    harness_result_document = EvidenceDocument.from_protocol_json(
        schema="atv.harness-result/v1",
        path="trial/harness-result.json",
        value=evidence.harness_result,
    )
    grader_document = (
        EvidenceDocument.from_relaxed_json(
            schema="atv.grade-result/v1",
            path="grader/result.json",
            value=grade.to_dict(),
        )
        if grade is not None
        else None
    )
    budget_identity = _budget_identity(
        spec.budget_profile.id,
        evidence.trial_request["budget_limits"],
    )
    analysis_identity = _analysis_identity(
        model_policy=evidence.trial_request["model_policy"],
        budget=budget_identity,
    )
    analysis_document = EvidenceDocument.from_relaxed_json(
        schema="atv.paired-analysis/v1",
        path="analysis/paired.json",
        value={
            **analysis.to_dict(),
            "identity": analysis_identity,
        },
    )
    attestation_descriptors = [
        attestation.protocol_dict() for attestation in evidence.attestations
    ]
    model_protocol = [model.protocol_dict() for model in evidence.models]
    artifact_descriptors = [artifact.descriptor for artifact in evidence.artifacts]

    trial_result = {
        "schema": "atv.trial-result/v1",
        "protocol_version": 1,
        "benchmark_release": spec.benchmark_release,
        "track": evidence.runner.track,
        "run_id": evidence.runner.run_id,
        "trial_id": spec.trial_id,
        "attempt_id": attempt.attempt_id,
        "task_set": dict(evidence.runner.task_set),
        "task": _versioned_ref(spec.task.id, spec.task.version, spec.task.digest),
        "harness": _versioned_ref(
            spec.harness.id, spec.harness.version, spec.harness.digest
        ),
        "model_policy": deepcopy(evidence.trial_request["model_policy"]),
        "budget": deepcopy(budget_identity),
        "trust_tier": evidence.trust_tier,
        "rankable": rankable and outcome.rankable,
        "status": status.value,
        "failure": _failure(status, outcome),
        "protocol": {
            "request": request_document.descriptor,
            "event_stream": evidence.event_stream.descriptor,
            "harness_result": harness_result_document.descriptor,
        },
        "execution": {
            "runner": dict(evidence.runner.identity),
            "platform": dict(evidence.runner.platform),
            "runtime_digest": _digest(evidence.runner.runtime_digest),
            "started_at": evidence.runner.started_at,
            "ended_at": evidence.runner.ended_at,
            "duration_ms": evidence.runner.duration_ms,
            "exit": dict(evidence.runner.exit),
        },
        "output_tree_digest": (
            _digest(grade.output_tree_digest)
            if grade is not None
            and status
            in {TrialStatus.SUCCESS, TrialStatus.TASK_FAILED, TrialStatus.PARTIAL}
            else None
        ),
        "artifacts": artifact_descriptors,
        "usage": {
            "reported": dict(evidence.runner.reported_usage),
            "observed": dict(evidence.runner.observed_usage),
            "authoritative": dict(evidence.runner.authoritative_usage),
        },
        "models": model_protocol,
        "analysis": {
            "document": analysis_document.descriptor,
            **deepcopy(analysis_identity),
        },
        "evaluation": _evaluation(
            status=status,
            grade=grade,
            grader=evidence.grader,
            grader_document=grader_document,
            task_manifest=evidence.task_manifest,
        ),
        "retry": {
            "attempt_index": attempt.attempt_number - 1,
            "prior_attempt_ids": list(evidence.runner.prior_attempt_ids),
            "reason": evidence.runner.retry_reason,
        },
        "attestations": attestation_descriptors,
    }
    store = default_schema_store()
    store.validate(trial_result, SchemaKind.TRIAL_RESULT)
    trial_result_document = EvidenceDocument.from_protocol_json(
        schema="atv.trial-result/v1",
        path="trial/result.json",
        value=trial_result,
    )

    contents = {
        "harness_manifest": harness_manifest_document.descriptor,
        "task_manifest": task_manifest_document.descriptor,
        "trial_request": request_document.descriptor,
        "event_stream": evidence.event_stream.descriptor,
        "harness_result": harness_result_document.descriptor,
        "trial_result": trial_result_document.descriptor,
        "output_tree": (
            evidence.output_tree.descriptor if evidence.output_tree is not None else None
        ),
        "grader_result": (
            grader_document.descriptor if grader_document is not None else None
        ),
        "artifacts": artifact_descriptors,
        "logs": [
            analysis_document.descriptor,
            evidence.runner.lifecycle_document.descriptor,
            *[log.descriptor for log in evidence.logs],
        ],
        "model_receipts": [model.receipt.descriptor for model in evidence.models],
        "attestations": attestation_descriptors,
    }
    contents_digest = canonical_digest(contents)
    bundle = {
        "schema": "atv.bundle/v1",
        "bundle_id": "bundle-" + contents_digest["value"][:32],
        "created_at": evidence.created_at,
        "trust_tier": evidence.trust_tier,
        "canonicalization": "atv.canonical-json/v1",
        "hash_algorithm": "sha256",
        "run_id": evidence.runner.run_id,
        "trial_id": spec.trial_id,
        "attempt_id": attempt.attempt_id,
        "contents": contents,
        "contents_digest": contents_digest,
        "runner": dict(evidence.runner.identity),
        "platform": dict(evidence.runner.platform),
    }
    store.validate(bundle, SchemaKind.BUNDLE)
    verify_bundle_manifest(bundle, store=store)

    documents: dict[str, bytes] = {}
    for document in (
        harness_manifest_document,
        task_manifest_document,
        request_document,
        evidence.event_stream,
        harness_result_document,
        trial_result_document,
        analysis_document,
        evidence.runner.lifecycle_document,
        *evidence.logs,
        *(model.receipt for model in evidence.models),
        *(attestation.document for attestation in evidence.attestations),
    ):
        _add_document(documents, document.descriptor, document.data)
    if grader_document is not None:
        _add_document(documents, grader_document.descriptor, grader_document.data)
    if evidence.output_tree is not None:
        _add_document(
            documents,
            evidence.output_tree.descriptor,
            evidence.output_tree.data,
        )
    for artifact in evidence.artifacts:
        _add_document(documents, artifact.descriptor, artifact.data)

    bound_bundle = (
        _PolicyBoundBundle(bundle, official_trust_policy)
        if evidence.trust_tier == OFFICIAL_TRUST_TIER
        and official_trust_policy is not None
        else bundle
    )
    exported = ProtocolExport(
        bundle=bound_bundle,
        documents=MappingProxyType(documents),
        official_trust_policy=official_trust_policy,
    )
    exported.verify()
    return exported


def _verify_document_descriptor(
    documents: Mapping[str, bytes],
    descriptor: Mapping[str, Any],
) -> bytes:
    path = str(descriptor["path"])
    try:
        data = documents[path]
    except KeyError as exc:
        raise ProtocolExportError(f"bundle document is missing: {path}") from exc
    _ensure_descriptor_matches(descriptor, data)
    return data


def _load_document_object(
    documents: Mapping[str, bytes],
    descriptor: Mapping[str, Any],
    *,
    label: str,
    relaxed: bool = False,
) -> Mapping[str, Any]:
    try:
        data = _verify_document_descriptor(documents, descriptor)
        value = (
            json.loads(data.decode("utf-8"))
            if relaxed
            else strict_json_loads(data.decode("utf-8"))
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolExportError(f"{label} is malformed") from exc
    if not isinstance(value, Mapping):
        raise ProtocolExportError(f"{label} must be a JSON object")
    return value


_GRADE_CORE_FIELDS = (
    "schema",
    "passed",
    "score",
    "pass_score",
    "assertions",
    "grader_digest",
    "output_tree_digest",
    "lifecycle_receipt_digest",
    "trust_tier",
    "official_verified",
)
_GRADE_PAYLOAD_FIELDS = (
    *_GRADE_CORE_FIELDS,
    "grade_core_digest",
    "evaluation_envelope_digest",
    "evaluation_key_id",
)


def _verify_grader_result_integrity(
    grader_result: Mapping[str, Any],
    *,
    official: bool,
) -> str | None:
    expected_fields = {*_GRADE_PAYLOAD_FIELDS, "result_digest"}
    if set(grader_result) != expected_fields:
        raise ProtocolExportError(
            "grader result fields do not match atv.grade-result/v1"
        )
    if grader_result.get("schema") != "atv.grade-result/v1":
        raise ProtocolExportError("grader result schema is invalid")
    passed = grader_result.get("passed")
    score = grader_result.get("score")
    pass_score = grader_result.get("pass_score")
    if type(passed) is not bool:
        raise ProtocolExportError("grader result passed must be boolean")
    for name, value in (("score", score), ("pass_score", pass_score)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ProtocolExportError(
                f"grader result {name} must be finite and between zero and one"
            )
    if passed is not (float(score) >= float(pass_score)):
        raise ProtocolExportError(
            "grader result passed contradicts score and pass_score"
        )
    if not isinstance(grader_result.get("assertions"), list):
        raise ProtocolExportError("grader result assertions must be an array")
    for field_name in (
        "grader_digest",
        "output_tree_digest",
        "lifecycle_receipt_digest",
        "result_digest",
    ):
        try:
            _digest(grader_result[field_name])
        except (KeyError, ProtocolExportError) as exc:
            raise ProtocolExportError(
                f"grader result {field_name} is invalid"
            ) from exc

    expected_trust_tier = OFFICIAL_TRUST_TIER if official else LOCAL_TRUST_TIER
    if (
        grader_result.get("trust_tier") != expected_trust_tier
        or grader_result.get("official_verified") is not official
    ):
        raise ProtocolExportError(
            "grader result trust tier does not match bundle trust"
        )

    core_payload = {
        field_name: deepcopy(grader_result[field_name])
        for field_name in _GRADE_CORE_FIELDS
    }
    computed_core_digest = sha256_bytes(relaxed_json_bytes(core_payload))
    if official:
        if grader_result.get("grade_core_digest") != computed_core_digest:
            raise ProtocolExportError(
                "grader result grade_core_digest does not match canonical grade core"
            )
        try:
            _digest(grader_result["evaluation_envelope_digest"])
        except (KeyError, ProtocolExportError) as exc:
            raise ProtocolExportError(
                "official grader result evaluation envelope digest is invalid"
            ) from exc
        if not isinstance(grader_result.get("evaluation_key_id"), str) or not (
            grader_result["evaluation_key_id"]
        ):
            raise ProtocolExportError(
                "official grader result evaluation key id is invalid"
            )
    elif any(
        grader_result.get(field_name) is not None
        for field_name in (
            "grade_core_digest",
            "evaluation_envelope_digest",
            "evaluation_key_id",
        )
    ):
        raise ProtocolExportError(
            "local grader result cannot carry official evaluation fields"
        )

    payload = {
        field_name: deepcopy(grader_result[field_name])
        for field_name in _GRADE_PAYLOAD_FIELDS
    }
    if sha256_bytes(relaxed_json_bytes(payload)) != grader_result["result_digest"]:
        raise ProtocolExportError(
            "grader result result_digest does not match canonical payload"
        )
    return computed_core_digest if official else None


def _task_grader_image_digest(task_manifest: Mapping[str, Any]) -> str:
    try:
        image = task_manifest["grader"]["image"]
        marker = "@sha256:"
        if not isinstance(image, str) or marker not in image:
            raise ValueError("missing sha256 image pin")
        digest = image.rsplit(marker, 1)[1]
        _digest(digest)
        return digest
    except (KeyError, TypeError, ValueError, ProtocolExportError) as exc:
        raise ProtocolExportError(
            "task manifest grader image digest is invalid"
        ) from exc


def _trial_id_from_request(
    request: Mapping[str, Any],
    *,
    budget_profile_id: str,
) -> str:
    try:
        budget_limits = request["budget_limits"]
        wall_time_ms = budget_limits["wall_time_ms"]
        if (
            isinstance(wall_time_ms, bool)
            or not isinstance(wall_time_ms, int)
            or wall_time_ms % 1_000
        ):
            raise ValueError("wall_time_ms is not an exact whole second")
        identity = {
            "schema": "atv.trial-spec/v1",
            "benchmark_release": request["benchmark_release"],
            "protocol_version": "atv.trial/v1",
            "schedule_id": request["schedule_id"],
            "task": {
                "id": request["task"]["id"],
                "version": request["task"]["version"],
                "digest": request["task"]["manifest_digest"]["value"],
            },
            "harness": {
                "id": request["harness"]["id"],
                "version": request["harness"]["version"],
                "digest": request["harness"]["manifest_digest"]["value"],
            },
            "model_policy": {
                "id": request["model_policy"]["id"],
                "version": request["model_policy"]["version"],
                "digest": request["model_policy"]["policy_digest"]["value"],
            },
            "budget_profile": {
                "id": budget_profile_id,
                "budget": {
                    "wall_time_seconds": wall_time_ms // 1_000,
                    "max_model_tokens": budget_limits["model_total_tokens"],
                    "max_model_calls": budget_limits["model_calls"],
                    "max_cost_microusd": budget_limits["cost_microusd"],
                },
            },
            "repetition": request["order_assignment"]["repetition"],
            "schedule_seed": request["seed"],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolExportError(
            "trial request cannot reconstruct the TrialSpec identity"
        ) from exc
    return sha256_bytes(relaxed_json_bytes(identity))


def _signed_official_bindings(
    envelopes: Mapping[str, SignedDsseEnvelope],
) -> OfficialBindings:
    signed_bindings: OfficialBindings | None = None
    for role_name, envelope in envelopes.items():
        try:
            statement = envelope.statement()
            predicate = statement["predicate"]
            candidate = OfficialBindings(**dict(predicate["bindings"]))
        except (KeyError, TypeError, ValueError, TrustPolicyError) as exc:
            raise ProtocolExportError(
                f"public {role_name} attestation bindings are malformed"
            ) from exc
        if signed_bindings is None:
            signed_bindings = candidate
        elif candidate != signed_bindings:
            raise ProtocolExportError(
                "official attestations do not share one binding identity"
            )
    if signed_bindings is None:
        raise ProtocolExportError("official attestation set is empty")
    return signed_bindings


def _public_official_bindings(
    *,
    request: Mapping[str, Any],
    harness_manifest: Mapping[str, Any],
    task_manifest: Mapping[str, Any],
    harness_result: Mapping[str, Any],
    trial_result: Mapping[str, Any],
    grader_result: Mapping[str, Any],
    signed_bindings: OfficialBindings,
) -> OfficialBindings:
    try:
        evaluation = trial_result["evaluation"]
        expected = OfficialBindings(
            benchmark_release=request["benchmark_release"],
            trial_id=request["trial_id"],
            attempt_id=request["attempt_id"],
            task_digest=canonical_digest(task_manifest)["value"],
            harness_digest=canonical_digest(harness_manifest)["value"],
            model_digest=request["model_policy"]["policy_digest"]["value"],
            budget_digest=canonical_digest(request["budget_limits"])["value"],
            runner_digest=signed_bindings.runner_digest,
            grader_digest=grader_result["grader_digest"],
            grader_image_digest=_task_grader_image_digest(task_manifest),
            output_digest=grader_result["output_tree_digest"],
            result_digest=official_result_subject_digest(
                request=request,
                harness_result=harness_result,
            ),
        )
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ProtocolExportError(
            "official public binding fields are incomplete"
        ) from exc
    if expected != signed_bindings:
        raise ProtocolExportError(
            "signed official bindings do not match canonical source documents"
        )
    if (
        trial_result["execution"]["runtime_digest"]
        != _digest(expected.runner_digest)
        or evaluation["grader"]["image_digest"]
        != _digest(expected.grader_image_digest)
    ):
        raise ProtocolExportError(
            "trial result runner/grader identity differs from signed bindings"
        )
    return expected


def _verify_graded_trial_fields(
    *,
    trial_result: Mapping[str, Any],
    grader_result: Mapping[str, Any],
    grader_descriptor: Mapping[str, Any],
    task_manifest: Mapping[str, Any],
    harness_result: Mapping[str, Any],
    contents: Mapping[str, Any],
    official: bool,
) -> None:
    score = float(grader_result["score"])
    passed = bool(grader_result["passed"])
    expected_status = (
        TrialStatus.SUCCESS
        if passed
        else TrialStatus.PARTIAL
        if score > 0.0
        else TrialStatus.TASK_FAILED
    )
    expected_outcome = {
        TrialStatus.SUCCESS: "pass",
        TrialStatus.PARTIAL: "partial",
        TrialStatus.TASK_FAILED: "fail",
    }[expected_status]
    evaluation = trial_result["evaluation"]
    try:
        possible = int(task_manifest["grader"]["score_scale"]["possible"])
        expected_score = {
            "earned": int(round(score * possible)),
            "possible": possible,
            "unit": "points",
        }
        expected_output_digest = _digest(grader_result["output_tree_digest"])
        grader = evaluation["grader"]
    except (KeyError, TypeError, ValueError, ProtocolExportError) as exc:
        raise ProtocolExportError(
            "graded source documents are incomplete"
        ) from exc

    if (
        trial_result["status"] != expected_status.value
        or trial_result["failure"] is not None
        or evaluation["state"] != "completed"
        or evaluation["task_outcome"] != expected_outcome
        or evaluation["task_success"] is not passed
        or evaluation["score"] != expected_score
        or evaluation["metrics"] != []
        or evaluation["raw_result_digest"] != grader_descriptor["digest"]
        or trial_result["output_tree_digest"] != expected_output_digest
    ):
        raise ProtocolExportError(
            "trial status/evaluation differs from the authoritative grader result"
        )
    if (
        not isinstance(grader, Mapping)
        or grader["image_digest"]
        != _digest(_task_grader_image_digest(task_manifest))
    ):
        raise ProtocolExportError(
            "trial grader identity differs from the authoritative task manifest"
        )
    if contents["output_tree"] is None or (
        contents["output_tree"]["digest"] != expected_output_digest
    ):
        raise ProtocolExportError(
            "grader output digest differs from bundled output-tree evidence"
        )
    if (
        harness_result.get("schema") != "atv.harness-result/v1"
        or harness_result.get("status") != "completed"
        or harness_result.get("failure") is not None
        or harness_result.get("exit") != trial_result["execution"]["exit"]
        or harness_result.get("output_tree_digest") != expected_output_digest
        or harness_result.get("artifacts") != contents["artifacts"]
        or harness_result.get("reported_usage")
        != trial_result["usage"]["reported"]
    ):
        raise ProtocolExportError(
            "trial result differs from the authoritative harness result"
        )
    if official and trial_result["rankable"] is not True:
        raise ProtocolExportError(
            "official completed grader result must remain rankable"
        )


def verify_public_protocol_export(
    bundle: Mapping[str, Any],
    documents: Mapping[str, bytes],
    *,
    official_trust_policy: OfficialTrustPolicy | None = None,
) -> Mapping[str, Any]:
    """Verify a reproduced public export using the protocol bundle as truth."""

    official_trust_policy = (
        official_trust_policy
        or getattr(bundle, "official_trust_policy", None)
    )
    store = default_schema_store()
    verify_bundle_manifest(bundle, store=store)
    contents = bundle["contents"]
    document_descriptors = [
        contents["harness_manifest"],
        contents["task_manifest"],
        contents["trial_request"],
        contents["event_stream"],
        contents["harness_result"],
        contents["trial_result"],
        *contents["logs"],
        *contents["model_receipts"],
        *(item["document"] for item in contents["attestations"]),
    ]
    if contents["grader_result"] is not None:
        document_descriptors.append(contents["grader_result"])
    for descriptor in document_descriptors:
        _verify_document_descriptor(documents, descriptor)
    artifact_descriptors = [*contents["artifacts"]]
    if contents["output_tree"] is not None:
        artifact_descriptors.append(contents["output_tree"])
    for descriptor in artifact_descriptors:
        _verify_document_descriptor(documents, descriptor)

    expected_paths = {
        str(descriptor["path"])
        for descriptor in [*document_descriptors, *artifact_descriptors]
    }
    if set(documents) != expected_paths:
        missing = sorted(expected_paths - set(documents))
        extra = sorted(set(documents) - expected_paths)
        raise ProtocolExportError(
            f"bundle document set mismatch; missing={missing}, extra={extra}"
        )

    trial_result = _load_document_object(
        documents,
        contents["trial_result"],
        label="trial result document",
    )
    harness_manifest = _load_document_object(
        documents,
        contents["harness_manifest"],
        label="harness manifest",
    )
    task_manifest = _load_document_object(
        documents,
        contents["task_manifest"],
        label="task manifest",
    )
    request = _load_document_object(
        documents,
        contents["trial_request"],
        label="trial request",
    )
    harness_result = _load_document_object(
        documents,
        contents["harness_result"],
        label="harness result",
    )
    store.validate(trial_result, SchemaKind.TRIAL_RESULT)
    store.validate(harness_manifest, SchemaKind.HARNESS)
    store.validate(task_manifest, SchemaKind.TASK)
    store.validate(request, SchemaKind.TRIAL_REQUEST)

    expected_task = _versioned_ref(
        task_manifest["id"],
        task_manifest["version"],
        canonical_digest(task_manifest)["value"],
    )
    expected_harness = _versioned_ref(
        harness_manifest["id"],
        harness_manifest["version"],
        canonical_digest(harness_manifest)["value"],
    )
    expected_protocol = {
        "request": contents["trial_request"],
        "event_stream": contents["event_stream"],
        "harness_result": contents["harness_result"],
    }
    if (
        trial_result["run_id"] != bundle["run_id"]
        or trial_result["trial_id"] != bundle["trial_id"]
        or trial_result["attempt_id"] != bundle["attempt_id"]
        or trial_result["trust_tier"] != bundle["trust_tier"]
        or request["run_id"] != bundle["run_id"]
        or request["trial_id"] != bundle["trial_id"]
        or request["attempt_id"] != bundle["attempt_id"]
    ):
        raise ProtocolExportError(
            "bundle, request, and trial result identities differ"
        )
    if (
        trial_result["execution"]["runner"] != bundle["runner"]
        or trial_result["execution"]["platform"] != bundle["platform"]
        or trial_result["artifacts"] != contents["artifacts"]
        or trial_result["attestations"] != contents["attestations"]
        or [model["receipt"] for model in trial_result["models"]]
        != contents["model_receipts"]
    ):
        raise ProtocolExportError(
            "bundle contents contradict the authoritative trial result"
        )
    if (
        request["protocol_version"] != 1
        or trial_result["protocol_version"] != 1
        or trial_result["benchmark_release"] != request["benchmark_release"]
        or trial_result["track"] != request["track"]
        or trial_result["task_set"] != request["task_set"]
        or request["task"] != expected_task
        or trial_result["task"] != expected_task
        or request["harness"] != expected_harness
        or trial_result["harness"] != expected_harness
        or trial_result["model_policy"] != request["model_policy"]
        or trial_result["protocol"] != expected_protocol
    ):
        raise ProtocolExportError(
            "trial result identity differs from canonical request/manifests"
        )
    if contents["output_tree"] is None:
        if trial_result["output_tree_digest"] is not None:
            raise ProtocolExportError("trial result references a missing output tree")
    elif (
        trial_result["output_tree_digest"] != contents["output_tree"]["digest"]
    ):
        raise ProtocolExportError("output-tree digest differs from trial result")
    for model in trial_result["models"]:
        receipt = strict_json_loads(
            _verify_document_descriptor(
                documents,
                model["receipt"],
            ).decode("utf-8")
        )
        if (
            receipt.get("trial_id") != bundle["trial_id"]
            or receipt.get("attempt_id") != bundle["attempt_id"]
            or receipt.get("requested_model") != model["requested"]
            or receipt.get("resolved_model") != model["gateway_resolved"]
            or receipt.get("provider_reported") != model["provider_reported"]
            or receipt.get("provider") != model["provider"]
            or receipt.get("request_ids") != model["request_ids"]
        ):
            raise ProtocolExportError("public model receipt is mismatched")

    analysis_descriptors = [
        descriptor
        for descriptor in contents["logs"]
        if descriptor["schema"] == "atv.paired-analysis/v1"
    ]
    lifecycle_descriptors = [
        descriptor
        for descriptor in contents["logs"]
        if descriptor["schema"] == "atv.runner-lifecycle/v1"
    ]
    if len(analysis_descriptors) != 1 or len(lifecycle_descriptors) != 1:
        raise ProtocolExportError(
            "bundle must contain exactly one analysis and lifecycle document"
        )
    analysis = _load_document_object(
        documents,
        analysis_descriptors[0],
        label="analysis evidence",
        relaxed=True,
    )
    lifecycle = _load_document_object(
        documents,
        lifecycle_descriptors[0],
        label="lifecycle evidence",
    )
    budget_binding = trial_result.get("budget")
    analysis_binding = trial_result.get("analysis")
    if not isinstance(budget_binding, Mapping) or not isinstance(
        analysis_binding, Mapping
    ):
        raise ProtocolExportError(
            "trial result is missing budget/analysis identity bindings"
        )
    budget_profile_id = budget_binding.get("profile_id")
    if not isinstance(budget_profile_id, str) or not budget_profile_id:
        raise ProtocolExportError("trial result budget profile id is invalid")
    expected_budget_identity = _budget_identity(
        budget_profile_id,
        request["budget_limits"],
    )
    expected_analysis_identity = _analysis_identity(
        model_policy=request["model_policy"],
        budget=expected_budget_identity,
    )
    expected_analysis_model_policy_id = model_policy_analysis_id(
        request["model_policy"]
    )
    expected_analysis_budget_id = budget_analysis_id(
        budget_profile_id,
        request["budget_limits"],
    )
    if dict(budget_binding) != expected_budget_identity:
        raise ProtocolExportError(
            "trial result budget digest differs from the canonical request"
        )
    if dict(analysis_binding) != {
        "document": analysis_descriptors[0],
        **expected_analysis_identity,
    }:
        raise ProtocolExportError(
            "trial result analysis descriptor/identity binding is mismatched"
        )
    if (
        analysis.get("identity") != expected_analysis_identity
        or analysis.get("model_policy_id")
        != expected_analysis_model_policy_id
        or analysis.get("budget_profile_id") != expected_analysis_budget_id
    ):
        raise ProtocolExportError(
            "analysis was produced for a different model-policy/budget identity"
        )
    if _trial_id_from_request(
        request,
        budget_profile_id=budget_profile_id,
    ) != bundle["trial_id"]:
        raise ProtocolExportError(
            "budget profile identity does not reproduce the signed trial id"
        )
    if trial_result["harness"]["id"] not in {
        analysis.get("harness_a"),
        analysis.get("harness_b"),
    }:
        raise ProtocolExportError("analysis does not include the trial harness")
    effects = analysis.get("effects")
    if not isinstance(effects, list) or trial_result["task"]["id"] not in {
        effect.get("task_id")
        for effect in effects
        if isinstance(effect, Mapping)
    }:
        raise ProtocolExportError("analysis does not include the trial task")

    grader_descriptor = contents["grader_result"]
    grader_result: Mapping[str, Any] | None = None
    computed_grade_core_digest: str | None = None
    official_bundle = bundle["trust_tier"] in {
        OFFICIAL_TRUST_TIER,
        "independently-reproduced",
    }
    if grader_descriptor is not None:
        grader_result = _load_document_object(
            documents,
            grader_descriptor,
            label="grader result",
            relaxed=True,
        )
        computed_grade_core_digest = _verify_grader_result_integrity(
            grader_result,
            official=official_bundle,
        )
        _verify_graded_trial_fields(
            trial_result=trial_result,
            grader_result=grader_result,
            grader_descriptor=grader_descriptor,
            task_manifest=task_manifest,
            harness_result=harness_result,
            contents=contents,
            official=official_bundle,
        )
    elif trial_result["evaluation"]["raw_result_digest"] is not None:
        raise ProtocolExportError("trial result references a missing grader result")

    if bundle["trust_tier"] in {
        LOCAL_TRUST_TIER,
        "community-reproducible",
    }:
        if trial_result["rankable"]:
            raise ProtocolExportError(
                "unofficial protocol result cannot be rankable"
            )
        if contents["attestations"]:
            raise ProtocolExportError(
                "unofficial protocol bundle cannot contain official attestations"
            )
        if lifecycle.get("official_verified") is not False:
            raise ProtocolExportError(
                "unofficial lifecycle evidence claims official trust"
            )
    elif official_bundle:
        if official_trust_policy is None:
            raise ProtocolExportError(
                "official public verification requires explicit OfficialTrustPolicy"
            )
        if grader_result is None:
            raise ProtocolExportError("official export is missing grader result")
        if trial_result["rankable"]:
            if (
                analysis.get("publication_eligible") is not True
                or analysis.get("publication_decision") == Decision.INCONCLUSIVE.value
                or analysis.get("quality_gate_failures") != []
                or analysis.get("task_count", 0) < 50
                or any(
                    len(effect.get("repetitions", [])) < 5
                    for effect in analysis.get("effects", [])
                )
            ):
                raise ProtocolExportError(
                    "official rankable result lacks publishable analysis evidence"
                )
        if (
            lifecycle.get("official_verified") is not True
            or lifecycle.get("trial_id") != bundle["trial_id"]
            or lifecycle.get("attempt_id") != bundle["attempt_id"]
        ):
            raise ProtocolExportError(
                "official lifecycle evidence is incomplete or mismatched"
            )
        role_descriptors = {
            item["role"]: item for item in contents["attestations"]
        }
        required_roles = {
            "admission",
            "harness-build",
            "execution",
            "evaluation",
        }
        if trial_result["models"]:
            required_roles.add("model")
        if (
            len(role_descriptors) != len(contents["attestations"])
            or set(role_descriptors) != required_roles
        ):
            raise ProtocolExportError(
                "official attestation role set is incomplete or contains extras"
            )
        envelopes: dict[str, SignedDsseEnvelope] = {}
        for attestation in contents["attestations"]:
            try:
                raw_envelope = strict_json_loads(
                    _verify_document_descriptor(
                        documents,
                        attestation["document"],
                    ).decode("utf-8")
                )
                envelope = SignedDsseEnvelope.from_dict(raw_envelope)
                AttestationRole(attestation["role"])
            except (
                UnicodeDecodeError,
                ValueError,
                KeyError,
                TypeError,
                TrustPolicyError,
            ) as exc:
                raise ProtocolExportError(
                    f"public {attestation['role']} attestation is malformed"
                ) from exc
            envelopes[attestation["role"]] = envelope
        signed_bindings = _signed_official_bindings(envelopes)
        bindings = _public_official_bindings(
            request=request,
            harness_manifest=harness_manifest,
            task_manifest=task_manifest,
            harness_result=harness_result,
            trial_result=trial_result,
            grader_result=grader_result,
            signed_bindings=signed_bindings,
        )
        verified_envelopes: dict[str, SignedDsseEnvelope] = {}
        for role_name, envelope in envelopes.items():
            try:
                role = AttestationRole(role_name)
                required_claims: dict[str, Any] = {}
                if role is AttestationRole.EXECUTION:
                    required_claims = {
                        "execution_complete": True,
                        "credentials_destroyed": True,
                        "hidden_inputs_mounted_after_exit": True,
                    }
                elif role is AttestationRole.EVALUATION:
                    required_claims = {
                        "lifecycle_receipt_digest": grader_result[
                            "lifecycle_receipt_digest"
                        ],
                        "grade_core_digest": computed_grade_core_digest,
                    }
                official_trust_policy.verify(
                    envelope,
                    role=role,
                    bindings=bindings,
                    required_claims=required_claims,
                )
            except (
                UnicodeDecodeError,
                ValueError,
                KeyError,
                TypeError,
                TrustPolicyError,
            ) as exc:
                raise ProtocolExportError(
                    f"public {role_name} attestation did not verify"
                ) from exc
            verified_envelopes[role_name] = envelope
        if (
            lifecycle.get("execution_envelope_digest")
            != verified_envelopes["execution"].digest
            or grader_result.get("lifecycle_receipt_digest")
            != verified_envelopes["execution"].digest
        ):
            raise ProtocolExportError(
                "lifecycle/grader does not bind verified execution envelope"
            )
        if (
            grader_result.get("evaluation_envelope_digest")
            != verified_envelopes["evaluation"].digest
        ):
            raise ProtocolExportError(
                "grader result does not bind verified evaluation envelope"
            )
    else:
        raise ProtocolExportError("unsupported protocol export trust tier")
    return trial_result
