"""Typed protocol enums and immutable result records."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class EventType(str, Enum):
    HELLO = "hello"
    ACCEPTED = "accepted"
    STATUS = "status"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    CHECKPOINT = "checkpoint"
    ARTIFACT = "artifact"
    USAGE = "usage"
    ERROR = "error"
    CANCEL = "cancel"
    CONTROLLER_ERROR = "controller_error"
    RESULT = "result"


class HarnessStatus(str, Enum):
    COMPLETED = "completed"
    NO_EDIT = "no_edit"
    INVALID_ARTIFACT = "invalid_artifact"
    TASK_TIMEOUT = "task_timeout"
    MODEL_UNREACHABLE = "model_unreachable"
    AUTH_FAILED = "auth_failed"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HARNESS_CRASH = "harness_crash"
    CANCELLED = "cancelled"


class TrialStatus(str, Enum):
    SUCCESS = "success"
    TASK_FAILED = "task_failed"
    PARTIAL = "partial"
    NO_EDIT = "no_edit"
    INVALID_ARTIFACT = "invalid_artifact"
    TASK_TIMEOUT = "task_timeout"
    MODEL_UNREACHABLE = "model_unreachable"
    AUTH_FAILED = "auth_failed"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HARNESS_CRASH = "harness_crash"
    PROTOCOL_ERROR = "protocol_error"
    GRADER_FAILED = "grader_failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class NegotiatedProtocol:
    version: int
    capabilities: Mapping[str, Any]


@dataclass(frozen=True)
class ProtocolTranscript:
    events: tuple[Mapping[str, Any], ...]
    hello: Mapping[str, Any]
    accepted: Mapping[str, Any]
    result_event: Mapping[str, Any]
    authority_verified: bool = False
    _authority_token: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def result(self) -> Mapping[str, Any]:
        value = self.result_event["harness_result"]
        assert isinstance(value, dict)
        return value

    @property
    def harness_result(self) -> Mapping[str, Any]:
        return self.result

    @property
    def status(self) -> HarnessStatus:
        return HarnessStatus(self.result["status"])
