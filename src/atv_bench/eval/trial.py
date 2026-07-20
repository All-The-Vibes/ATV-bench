"""Immutable definitions for one independent harness trial."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ._canonical import require_sha256, sha256_json


def _require_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    if value != value.strip():
        raise ValueError(f"{field} must not have surrounding whitespace")
    return value


@dataclass(frozen=True, slots=True)
class Budget:
    """Exact limits used for a controlled comparison cell."""

    wall_time_seconds: int
    max_model_tokens: int
    max_model_calls: int
    max_cost_microusd: int

    def __post_init__(self) -> None:
        for field in (
            "wall_time_seconds",
            "max_model_tokens",
            "max_model_calls",
            "max_cost_microusd",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "wall_time_seconds": self.wall_time_seconds,
            "max_model_tokens": self.max_model_tokens,
            "max_model_calls": self.max_model_calls,
            "max_cost_microusd": self.max_cost_microusd,
        }

    @property
    def digest(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class TaskRef:
    id: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _require_text(self.id, field="task id")
        _require_text(self.version, field="task version")
        require_sha256(self.digest, field="task digest")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class HarnessRef:
    id: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _require_text(self.id, field="harness id")
        _require_text(self.version, field="harness version")
        require_sha256(self.digest, field="harness digest")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class ModelPolicyRef:
    id: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _require_text(self.id, field="model policy id")
        _require_text(self.version, field="model policy version")
        require_sha256(self.digest, field="model policy digest")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "digest": self.digest}

    @property
    def analysis_id(self) -> str:
        return f"{self.id}@{self.version}#sha256:{self.digest}"


@dataclass(frozen=True, slots=True)
class BudgetProfile:
    id: str
    budget: Budget

    def __post_init__(self) -> None:
        _require_text(self.id, field="budget profile id")
        if not isinstance(self.budget, Budget):
            raise TypeError("budget must be a Budget")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "budget": self.budget.to_dict()}

    @property
    def analysis_id(self) -> str:
        return f"{self.id}#sha256:{self.budget.digest}"


@dataclass(frozen=True, slots=True)
class TrialSpec:
    """A preregistered independent trial, not a game, test, or model call."""

    benchmark_release: str
    protocol_version: str
    schedule_id: str
    task: TaskRef
    harness: HarnessRef
    model_policy: ModelPolicyRef
    budget_profile: BudgetProfile
    repetition: int
    schedule_seed: int

    def __post_init__(self) -> None:
        _require_text(self.benchmark_release, field="benchmark release")
        _require_text(self.protocol_version, field="protocol version")
        require_sha256(self.schedule_id, field="schedule id")
        if not isinstance(self.repetition, int) or isinstance(self.repetition, bool):
            raise ValueError("repetition must be an integer")
        if self.repetition < 0:
            raise ValueError("repetition must be non-negative")
        if not isinstance(self.schedule_seed, int) or isinstance(self.schedule_seed, bool):
            raise ValueError("schedule_seed must be an integer")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.trial-spec/v1",
            "benchmark_release": self.benchmark_release,
            "protocol_version": self.protocol_version,
            "schedule_id": self.schedule_id,
            "task": self.task.to_dict(),
            "harness": self.harness.to_dict(),
            "model_policy": self.model_policy.to_dict(),
            "budget_profile": self.budget_profile.to_dict(),
            "repetition": self.repetition,
            "schedule_seed": self.schedule_seed,
        }

    @property
    def trial_id(self) -> str:
        return sha256_json(self.identity_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "trial_id": self.trial_id}


@dataclass(frozen=True, slots=True)
class TrialAttempt:
    """One fresh process/workspace attempt for a planned trial."""

    spec: TrialSpec
    attempt_number: int
    fresh_nonce: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_number, int) or isinstance(self.attempt_number, bool):
            raise ValueError("attempt_number must be an integer")
        if self.attempt_number <= 0:
            raise ValueError("attempt_number must be positive")
        require_sha256(self.fresh_nonce, field="fresh nonce")

    @property
    def attempt_id(self) -> str:
        return sha256_json(
            {
                "schema": "atv.trial-attempt/v1",
                "trial_id": self.spec.trial_id,
                "attempt_number": self.attempt_number,
                "fresh_nonce": self.fresh_nonce,
            }
        )

    @property
    def workspace_id(self) -> str:
        return sha256_json(
            {
                "schema": "atv.fresh-workspace/v1",
                "attempt_id": self.attempt_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.trial-attempt/v1",
            "trial_id": self.spec.trial_id,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "fresh_nonce": self.fresh_nonce,
            "workspace_id": self.workspace_id,
        }


class InfrastructureStatus(str, Enum):
    OK = "ok"
    SETUP_FAILED = "setup_failed"
    RUNNER_FAILED = "runner_failed"
    MODEL_GATEWAY_FAILED = "model_gateway_failed"
    GRADER_FAILED = "grader_failed"
    ARTIFACT_CORRUPT = "artifact_corrupt"
    CANCELLED = "cancelled"


class HarnessStatus(str, Enum):
    NOT_RUN = "not_run"
    COMPLETED = "completed"
    NO_EDIT = "no_edit"
    INVALID_ARTIFACT = "invalid_artifact"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_UNREACHABLE = "model_unreachable"
    AUTH_FAILED = "auth_failed"
    POLICY_DENIED = "policy_denied"
    PROTOCOL_ERROR = "protocol_error"
    CRASHED = "crashed"


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    """Controller-observed outcome with infrastructure separated from harness behavior."""

    trial_id: str
    attempt_id: str
    infrastructure_status: InfrastructureStatus
    harness_status: HarnessStatus
    score: float | None
    reason_code: str = ""

    def __post_init__(self) -> None:
        require_sha256(self.trial_id, field="trial id")
        require_sha256(self.attempt_id, field="attempt id")
        if not isinstance(self.infrastructure_status, InfrastructureStatus):
            raise TypeError("infrastructure_status must be InfrastructureStatus")
        if not isinstance(self.harness_status, HarnessStatus):
            raise TypeError("harness_status must be HarnessStatus")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise TypeError("score must be numeric or None")
            if not 0.0 <= float(self.score) <= 1.0:
                raise ValueError("score must be between 0 and 1")
            object.__setattr__(self, "score", float(self.score))

        if self.infrastructure_status is not InfrastructureStatus.OK:
            if self.score is not None:
                raise ValueError("infrastructure failures must not carry a rankable score")
            if not self.reason_code:
                raise ValueError("infrastructure failures require a reason_code")
            return

        if self.harness_status is HarnessStatus.NOT_RUN:
            raise ValueError("infrastructure OK cannot leave the harness not_run")
        if self.harness_status is HarnessStatus.COMPLETED:
            if self.score is None:
                raise ValueError("completed harness outcomes require a trusted grade score")
        elif self.score != 0.0:
            raise ValueError("rankable harness failures must carry score 0.0")
        if self.harness_status is not HarnessStatus.COMPLETED and not self.reason_code:
            raise ValueError("harness failures require a reason_code")

    @property
    def rankable(self) -> bool:
        return self.infrastructure_status is InfrastructureStatus.OK

    @property
    def retryable_infrastructure_failure(self) -> bool:
        return self.infrastructure_status is not InfrastructureStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.trial-outcome/v1",
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "infrastructure_status": self.infrastructure_status.value,
            "harness_status": self.harness_status.value,
            "score": self.score,
            "reason_code": self.reason_code,
            "rankable": self.rankable,
        }
