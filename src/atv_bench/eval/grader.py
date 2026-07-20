"""Trusted post-run grading that treats harness output only as data."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from atv_bench.security.signing import (
    AttestationRole,
    Ed25519StatementSigner,
    OfficialBindings,
    OfficialTrustPolicy,
    SignedDsseEnvelope,
    TrustPolicyError,
    build_official_statement,
)

from ._canonical import (
    DEFAULT_MAX_FILE_BYTES,
    TreeLimits,
    UnsafePathError,
    canonical_json_bytes,
    read_stable_confined_regular_file,
    safe_relative_path,
    sha256_bytes,
    sha256_json,
    snapshot_regular_tree,
    tree_digest_from_snapshots,
)


class GraderError(RuntimeError):
    """The trusted grader could not produce a valid grade."""


class GradingStateError(GraderError):
    """The supplied lifecycle receipt does not authorize post-run grading."""


class GradingTrustTier(str, Enum):
    LOCAL_SELF_ATTESTED = "local-self-attested"
    OFFICIAL_ATTESTED = "official-attested"


class RunnerLifecycleReceipt(ABC):
    """Controller lifecycle evidence required before grading.

    This interface is not itself an isolation boundary. The future OCI runner
    must supply a :class:`TrustedRunnerLifecycleReceipt` whose verification
    proves process exit, credential destruction, and late hidden-input mount.
    """

    @property
    @abstractmethod
    def execution_complete(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def trust_tier(self) -> GradingTrustTier:
        raise NotImplementedError

    @property
    @abstractmethod
    def official_verified(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def receipt_digest(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def validate_for_grading(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ControllerAssertedLifecycleReceipt(RunnerLifecycleReceipt):
    """Local controller assertion.

    This is sufficient for smoke tests and task-package validation only. It can
    never become official or verified, regardless of caller-provided values.
    """

    controller_id: str
    _execution_complete: bool

    @classmethod
    def completed(
        cls,
        *,
        controller_id: str = "local-controller",
    ) -> "ControllerAssertedLifecycleReceipt":
        return cls(controller_id=controller_id, _execution_complete=True)

    @classmethod
    def incomplete(
        cls,
        *,
        controller_id: str = "local-controller",
    ) -> "ControllerAssertedLifecycleReceipt":
        return cls(controller_id=controller_id, _execution_complete=False)

    def __post_init__(self) -> None:
        if not isinstance(self.controller_id, str) or not self.controller_id.strip():
            raise ValueError("controller_id must be non-empty")

    @property
    def execution_complete(self) -> bool:
        return self._execution_complete

    @property
    def trust_tier(self) -> GradingTrustTier:
        return GradingTrustTier.LOCAL_SELF_ATTESTED

    @property
    def official_verified(self) -> bool:
        return False

    @property
    def receipt_digest(self) -> str:
        return sha256_json(
            {
                "schema": "atv.controller-lifecycle-assertion/v1",
                "controller_id": self.controller_id,
                "execution_complete": self.execution_complete,
                "trust_tier": self.trust_tier.value,
                "official_verified": False,
            }
        )

    def validate_for_grading(self) -> None:
        if not self.execution_complete:
            raise GradingStateError(
                "controller has not asserted that harness execution completed"
            )


class TrustedRunnerLifecycleReceipt(RunnerLifecycleReceipt, ABC):
    """Interface the isolated official runner must implement.

    No implementation is supplied in the local evaluation core. Official code
    must verify a signed runner receipt before returning this interface.
    """

    @property
    def trust_tier(self) -> GradingTrustTier:
        return GradingTrustTier.OFFICIAL_ATTESTED

    @property
    def official_verified(self) -> bool:
        return True

    @property
    @abstractmethod
    def credentials_destroyed(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def hidden_inputs_mounted_after_exit(self) -> bool:
        raise NotImplementedError


_VERIFIED_RECEIPT_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedRunnerLifecycleReceipt(TrustedRunnerLifecycleReceipt):
    """Execution lifecycle receipt produced only after Ed25519 policy verification.

    Callers cannot promote a boolean or subclass into this receipt. Exporters and
    graders still reverify ``execution_envelope`` independently.
    """

    execution_envelope: SignedDsseEnvelope
    bindings: OfficialBindings
    key_id: str
    issued_at: str
    _claims: dict[str, Any] = field(repr=False)

    def __init__(
        self,
        *,
        execution_envelope: SignedDsseEnvelope,
        bindings: OfficialBindings,
        key_id: str,
        issued_at: str,
        claims: Mapping[str, Any],
        _seal: object,
    ):
        if _seal is not _VERIFIED_RECEIPT_SEAL:
            raise TypeError(
                "VerifiedRunnerLifecycleReceipt is created by verification only"
            )
        object.__setattr__(self, "execution_envelope", execution_envelope)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "key_id", key_id)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "_claims", dict(claims))

    @classmethod
    def from_execution_envelope(
        cls,
        *,
        envelope: SignedDsseEnvelope | Mapping[str, Any],
        trust_policy: OfficialTrustPolicy,
        bindings: OfficialBindings,
    ) -> "VerifiedRunnerLifecycleReceipt":
        if not isinstance(trust_policy, OfficialTrustPolicy):
            raise TypeError("trust_policy must be OfficialTrustPolicy")
        required = {
            "execution_complete": True,
            "credentials_destroyed": True,
            "hidden_inputs_mounted_after_exit": True,
        }
        verified = trust_policy.verify(
            envelope,
            role=AttestationRole.EXECUTION,
            bindings=bindings,
            required_claims=required,
        )
        candidate = (
            envelope
            if isinstance(envelope, SignedDsseEnvelope)
            else SignedDsseEnvelope.from_dict(envelope)
        )
        predicate = verified.statement["predicate"]
        return cls(
            execution_envelope=candidate,
            bindings=bindings,
            key_id=verified.key_id,
            issued_at=verified.issued_at,
            claims=predicate["claims"],
            _seal=_VERIFIED_RECEIPT_SEAL,
        )

    @property
    def execution_complete(self) -> bool:
        return self._claims.get("execution_complete") is True

    @property
    def credentials_destroyed(self) -> bool:
        return self._claims.get("credentials_destroyed") is True

    @property
    def hidden_inputs_mounted_after_exit(self) -> bool:
        return self._claims.get("hidden_inputs_mounted_after_exit") is True

    @property
    def receipt_digest(self) -> str:
        return self.execution_envelope.digest

    def validate_for_grading(self) -> None:
        if not (
            self.execution_complete
            and self.credentials_destroyed
            and self.hidden_inputs_mounted_after_exit
        ):
            raise GradingStateError("verified runner lifecycle receipt is incomplete")

    def lifecycle_payload(self) -> dict[str, Any]:
        return {
            "schema": "atv.runner-lifecycle/v2",
            "trial_id": self.bindings.trial_id,
            "attempt_id": self.bindings.attempt_id,
            "execution_complete": self.execution_complete,
            "credentials_destroyed": self.credentials_destroyed,
            "hidden_inputs_mounted_after_exit": self.hidden_inputs_mounted_after_exit,
            "trust_tier": self.trust_tier.value,
            "official_verified": True,
            "execution_envelope_digest": self.receipt_digest,
            "execution_key_id": self.key_id,
        }


@dataclass(frozen=True, slots=True)
class OfficialGradingContext:
    signer: Ed25519StatementSigner
    trust_policy: OfficialTrustPolicy
    bindings: OfficialBindings
    issued_at: str


@dataclass(frozen=True, slots=True)
class GradeAssertionResult:
    id: str
    kind: str
    path: str
    passed: bool
    weight: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "passed": self.passed,
            "weight": self.weight,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class GradeResult:
    passed: bool
    score: float
    pass_score: float
    assertions: tuple[GradeAssertionResult, ...]
    grader_digest: str
    output_tree_digest: str
    lifecycle_receipt_digest: str
    trust_tier: GradingTrustTier
    official_verified: bool
    grade_core_digest: str | None
    evaluation_envelope_digest: str | None
    evaluation_key_id: str | None
    evaluation_envelope: SignedDsseEnvelope | None = field(
        repr=False,
        compare=False,
    )
    result_digest: str

    def __post_init__(self) -> None:
        if self.official_verified is not (
            self.trust_tier is GradingTrustTier.OFFICIAL_ATTESTED
        ):
            raise ValueError(
                "official_verified must agree with the lifecycle trust tier"
            )
        for field_name, value in (
            ("grader_digest", self.grader_digest),
            ("output_tree_digest", self.output_tree_digest),
            ("lifecycle_receipt_digest", self.lifecycle_receipt_digest),
            ("result_digest", self.result_digest),
        ):
            if (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{field_name} must be a sha256 digest")
        official_fields = (
            self.grade_core_digest,
            self.evaluation_envelope_digest,
            self.evaluation_key_id,
            self.evaluation_envelope,
        )
        if self.official_verified:
            if any(value is None for value in official_fields):
                raise ValueError(
                    "official GradeResult requires verified evaluation envelope binding"
                )
            _require_grade_digest(
                self.grade_core_digest,
                field="grade_core_digest",
            )
            _require_grade_digest(
                self.evaluation_envelope_digest,
                field="evaluation_envelope_digest",
            )
            if self.evaluation_envelope.digest != self.evaluation_envelope_digest:
                raise ValueError("evaluation envelope digest does not match envelope")
        elif any(value is not None for value in official_fields):
            raise ValueError("local GradeResult cannot carry official evaluation evidence")
        if sha256_json(self.payload_dict()) != self.result_digest:
            raise ValueError("result_digest does not match canonical GradeResult payload")

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.grade-result/v1",
            "passed": self.passed,
            "score": self.score,
            "pass_score": self.pass_score,
            "assertions": [item.to_dict() for item in self.assertions],
            "grader_digest": self.grader_digest,
            "output_tree_digest": self.output_tree_digest,
            "lifecycle_receipt_digest": self.lifecycle_receipt_digest,
            "trust_tier": self.trust_tier.value,
            "official_verified": self.official_verified,
            "grade_core_digest": self.grade_core_digest,
            "evaluation_envelope_digest": self.evaluation_envelope_digest,
            "evaluation_key_id": self.evaluation_key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "result_digest": self.result_digest}


class TrustedPostRunGrader(ABC):
    """Interface implemented only by benchmark-owned grader code."""

    @abstractmethod
    def grade(
        self,
        task: Any,
        output_tree: Path,
        *,
        lifecycle_receipt: RunnerLifecycleReceipt,
        official_context: OfficialGradingContext | None = None,
    ) -> GradeResult:
        raise NotImplementedError


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise GraderError("json pointer must be empty or start with '/'")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                index = int(token)
            except ValueError as exc:
                raise KeyError(token) from exc
            current = current[index]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(token)
    return current


def _validate_spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise GraderError("grader specification must be an object")
    required = {"schema", "pass_score", "assertions"}
    if set(spec) != required:
        raise GraderError(
            "grader specification fields must be exactly: "
            + ", ".join(sorted(required))
        )
    if spec["schema"] != "atv.grader.file-assertions/v1":
        raise GraderError("unsupported grader schema")
    pass_score = spec["pass_score"]
    if isinstance(pass_score, bool) or not isinstance(pass_score, (int, float)):
        raise GraderError("pass_score must be numeric")
    if not 0.0 < float(pass_score) <= 1.0:
        raise GraderError("pass_score must be in (0, 1]")
    assertions = spec["assertions"]
    if not isinstance(assertions, list) or not assertions:
        raise GraderError("assertions must be a non-empty list")

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(assertions):
        if not isinstance(raw, dict):
            raise GraderError(f"assertions[{index}] must be an object")
        common = {"id", "kind", "path", "weight"}
        kind = raw.get("kind")
        if kind in {"text_equals", "text_contains"}:
            expected_fields = common | {"expected"}
            if not isinstance(raw.get("expected"), str):
                raise GraderError(f"assertions[{index}].expected must be text")
        elif kind == "json_value":
            expected_fields = common | {"pointer", "expected"}
            if not isinstance(raw.get("pointer"), str):
                raise GraderError(f"assertions[{index}].pointer must be text")
        elif kind in {"file_exists", "file_absent"}:
            expected_fields = common
        elif kind == "sha256_equals":
            expected_fields = common | {"expected"}
            expected = raw.get("expected")
            if (
                not isinstance(expected, str)
                or len(expected) != 64
                or any(ch not in "0123456789abcdef" for ch in expected)
            ):
                raise GraderError(
                    f"assertions[{index}].expected must be a sha256 digest"
                )
        else:
            raise GraderError(f"assertions[{index}] has unsupported kind: {kind!r}")
        if set(raw) != expected_fields:
            raise GraderError(
                f"assertions[{index}] fields do not match kind {kind!r}"
            )

        assertion_id = raw["id"]
        if not isinstance(assertion_id, str) or not assertion_id.strip():
            raise GraderError(f"assertions[{index}].id must be non-empty")
        if assertion_id in seen_ids:
            raise GraderError(f"duplicate assertion id: {assertion_id}")
        seen_ids.add(assertion_id)
        try:
            safe_relative_path(raw["path"], field=f"assertions[{index}].path")
        except (TypeError, UnsafePathError) as exc:
            raise GraderError(str(exc)) from exc
        weight = raw["weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise GraderError(f"assertions[{index}].weight must be numeric")
        if float(weight) <= 0:
            raise GraderError(f"assertions[{index}].weight must be positive")
        normalized = dict(raw)
        normalized["weight"] = float(weight)
        validated.append(normalized)

    return {
        "schema": spec["schema"],
        "pass_score": float(pass_score),
        "assertions": validated,
    }


class FileAssertionsGrader(TrustedPostRunGrader):
    """Deterministic grader for small trusted file and JSON assertions."""

    def __init__(self, spec: dict[str, Any]) -> None:
        normalized = _validate_spec(spec)
        self._spec_json = canonical_json_bytes(normalized).decode("utf-8")
        self.grader_digest = sha256_json(normalized)

    @classmethod
    def from_task(cls, task: Any) -> "FileAssertionsGrader":
        try:
            grader_path = Path(task.grader_path)
            relative = grader_path.relative_to(Path(task.root)).as_posix()
            data = read_stable_confined_regular_file(
                Path(task.root),
                relative,
                max_bytes=1024 * 1024,
            )
            spec = json.loads(data.decode("utf-8"))
        except (OSError, ValueError, UnsafePathError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraderError(f"cannot load trusted grader specification: {exc}") from exc
        return cls(spec)

    @property
    def spec(self) -> dict[str, Any]:
        return json.loads(self._spec_json)

    def _evaluate_assertion(
        self,
        files: dict[str, bytes],
        assertion: dict[str, Any],
    ) -> GradeAssertionResult:
        relative = assertion["path"]
        kind = assertion["kind"]
        passed = False
        detail = ""
        data = files.get(relative)

        try:
            if kind == "file_absent":
                passed = data is None
                detail = "absent" if passed else "unexpected file exists"
            elif kind == "file_exists":
                passed = data is not None
                detail = "regular file exists" if passed else "file is missing"
            elif data is None:
                detail = "file is missing"
            elif kind == "text_equals":
                observed = data.decode("utf-8")
                passed = observed == assertion["expected"]
                detail = "text matched" if passed else "text differed"
            elif kind == "text_contains":
                observed = data.decode("utf-8")
                passed = assertion["expected"] in observed
                detail = "text contained expected value" if passed else "text was absent"
            elif kind == "sha256_equals":
                observed_digest = sha256_bytes(data)
                passed = observed_digest == assertion["expected"]
                detail = (
                    "digest matched"
                    if passed
                    else f"digest differed: {observed_digest}"
                )
            elif kind == "json_value":
                document = json.loads(data.decode("utf-8"))
                observed = _json_pointer(document, assertion["pointer"])
                passed = observed == assertion["expected"]
                detail = "JSON value matched" if passed else "JSON value differed"
            else:  # Spec validation makes this unreachable.
                raise GraderError(f"unsupported assertion kind: {kind}")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError) as exc:
            passed = False
            detail = f"data could not satisfy assertion: {type(exc).__name__}"

        return GradeAssertionResult(
            id=assertion["id"],
            kind=kind,
            path=relative,
            passed=passed,
            weight=float(assertion["weight"]),
            detail=detail,
        )

    def grade(
        self,
        task: Any,
        output_tree: Path,
        *,
        lifecycle_receipt: RunnerLifecycleReceipt,
        official_context: OfficialGradingContext | None = None,
    ) -> GradeResult:
        if not isinstance(lifecycle_receipt, RunnerLifecycleReceipt):
            raise GradingStateError(
                "grading requires a RunnerLifecycleReceipt, not a boolean"
            )
        lifecycle_receipt.validate_for_grading()
        verified_receipt = (
            lifecycle_receipt
            if isinstance(lifecycle_receipt, VerifiedRunnerLifecycleReceipt)
            else None
        )
        if (
            lifecycle_receipt.trust_tier is GradingTrustTier.OFFICIAL_ATTESTED
            and verified_receipt is None
        ):
            raise GradingStateError(
                "official grading requires VerifiedRunnerLifecycleReceipt"
            )
        if verified_receipt is None and official_context is not None:
            raise GradingStateError(
                "local grading cannot accept official signing context"
            )
        if verified_receipt is not None and official_context is None:
            raise GradingStateError(
                "official grading requires evaluation signer and trust policy"
            )
        output_contract = task.manifest["output"]
        limits = TreeLimits(
            max_files=int(output_contract["max_files"]),
            max_total_bytes=int(output_contract["max_total_bytes"]),
            max_file_bytes=min(
                int(output_contract["max_total_bytes"]),
                DEFAULT_MAX_FILE_BYTES,
            ),
        )
        try:
            snapshots = snapshot_regular_tree(Path(output_tree), limits=limits)
        except UnsafePathError as exc:
            raise GraderError(str(exc)) from exc
        files = {snapshot.path: snapshot.data for snapshot in snapshots}

        spec = self.spec
        assertion_results = tuple(
            self._evaluate_assertion(files, assertion)
            for assertion in spec["assertions"]
        )
        total_weight = sum(item.weight for item in assertion_results)
        earned_weight = sum(
            item.weight for item in assertion_results if item.passed
        )
        score = round(earned_weight / total_weight, 12)
        pass_score = float(spec["pass_score"])
        core_payload = {
            "schema": "atv.grade-result/v1",
            "passed": score >= pass_score,
            "score": score,
            "pass_score": pass_score,
            "assertions": [item.to_dict() for item in assertion_results],
            "grader_digest": self.grader_digest,
            "output_tree_digest": tree_digest_from_snapshots(snapshots),
            "lifecycle_receipt_digest": lifecycle_receipt.receipt_digest,
            "trust_tier": lifecycle_receipt.trust_tier.value,
            "official_verified": lifecycle_receipt.official_verified,
        }
        grade_core_digest = (
            sha256_json(core_payload) if verified_receipt is not None else None
        )
        evaluation_envelope = None
        evaluation_envelope_digest = None
        evaluation_key_id = None
        if verified_receipt is not None:
            assert official_context is not None
            bindings = official_context.bindings
            if (
                bindings.trial_id != verified_receipt.bindings.trial_id
                or bindings.attempt_id != verified_receipt.bindings.attempt_id
                or bindings.grader_digest != self.grader_digest
                or bindings.output_digest != core_payload["output_tree_digest"]
            ):
                raise GradingStateError(
                    "official grading bindings do not match lifecycle/grader/output"
                )
            try:
                official_context.trust_policy.verify(
                    verified_receipt.execution_envelope,
                    role=AttestationRole.EXECUTION,
                    bindings=bindings,
                    required_claims={
                        "execution_complete": True,
                        "credentials_destroyed": True,
                        "hidden_inputs_mounted_after_exit": True,
                    },
                )
                statement = build_official_statement(
                    role=AttestationRole.EVALUATION,
                    bindings=bindings,
                    issued_at=official_context.issued_at,
                    claims={
                        "lifecycle_receipt_digest": verified_receipt.receipt_digest,
                        "grade_core_digest": grade_core_digest,
                    },
                )
                evaluation_envelope = official_context.signer.sign_statement(statement)
                verified_evaluation = official_context.trust_policy.verify(
                    evaluation_envelope,
                    role=AttestationRole.EVALUATION,
                    bindings=bindings,
                    required_claims={
                        "lifecycle_receipt_digest": verified_receipt.receipt_digest,
                        "grade_core_digest": grade_core_digest,
                    },
                )
            except TrustPolicyError as exc:
                raise GradingStateError(
                    f"official evaluation envelope did not verify: {exc}"
                ) from exc
            evaluation_envelope_digest = evaluation_envelope.digest
            evaluation_key_id = verified_evaluation.key_id
        payload = {
            **core_payload,
            "grade_core_digest": grade_core_digest,
            "evaluation_envelope_digest": evaluation_envelope_digest,
            "evaluation_key_id": evaluation_key_id,
        }
        result_digest = sha256_json(payload)
        return GradeResult(
            passed=bool(payload["passed"]),
            score=score,
            pass_score=pass_score,
            assertions=assertion_results,
            grader_digest=self.grader_digest,
            output_tree_digest=payload["output_tree_digest"],
            lifecycle_receipt_digest=lifecycle_receipt.receipt_digest,
            trust_tier=lifecycle_receipt.trust_tier,
            official_verified=lifecycle_receipt.official_verified,
            grade_core_digest=grade_core_digest,
            evaluation_envelope_digest=evaluation_envelope_digest,
            evaluation_key_id=evaluation_key_id,
            evaluation_envelope=evaluation_envelope,
            result_digest=result_digest,
        )


def _require_grade_digest(value: str | None, *, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a sha256 digest")
