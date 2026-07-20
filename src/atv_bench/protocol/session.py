"""Trusted two-party protocol session for process and OCI runners.

Raw harness messages never carry controller authority. The session validates a
``atv.harness-event/v1`` envelope, stamps controller-observed fields, negotiates after
``hello``, and injects controller events into the canonical merged transcript.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .canonical import canonical_digest, canonical_json_bytes, canonical_jsonl
from .capabilities import build_accepted_event, negotiate_capabilities
from .errors import (
    ProtocolAuthorityError,
    ProtocolLimitError,
    ProtocolStateError,
)
from .jsonl import (
    MergedTranscriptVerifier,
    ProtocolLimits,
    decode_json_object_line,
)
from .schemas import SchemaKind, SchemaStore, default_schema_store
from .types import EventType, NegotiatedProtocol, ProtocolTranscript

HARNESS_EVENT_SCHEMA = "atv.harness-event/v1"
_SESSION_AUTHORITY_TOKEN = object()
_HARNESS_EVENT_TYPES = {
    EventType.HELLO.value,
    EventType.STATUS.value,
    EventType.MODEL_CALL.value,
    EventType.TOOL_CALL.value,
    EventType.CHECKPOINT.value,
    EventType.ARTIFACT.value,
    EventType.USAGE.value,
    EventType.ERROR.value,
    EventType.RESULT.value,
}
_CONTROLLER_EVENT_TYPES = {
    EventType.ACCEPTED.value,
    EventType.CANCEL.value,
    EventType.CONTROLLER_ERROR.value,
}
_CONTROLLER_ONLY_FIELDS = {
    "source",
    "recorded_at",
    "sequence",
    "selected_protocol_version",
    "effective_budget_limits",
    "effective_protocol_limits",
    "request_digest",
    "policy_digest",
    "reason_code",
    "grace_period_ms",
}
_CONTROLLER_FAILURE_SCOPES = {"grader", "runner", "infrastructure"}
_CANCELLING_HARNESS_EVENTS = {
    EventType.STATUS.value,
    EventType.ARTIFACT.value,
    EventType.USAGE.value,
    EventType.ERROR.value,
    EventType.RESULT.value,
}
_USAGE_FIELDS = (
    "wall_time_ms",
    "cpu_time_ms",
    "model_input_tokens",
    "model_output_tokens",
    "model_total_tokens",
    "model_calls",
    "cost_microusd",
    "tool_calls",
    "memory_bytes",
    "storage_bytes",
    "pids",
    "stdout_bytes",
    "stderr_bytes",
    "artifact_bytes",
)


class SessionState(str, Enum):
    EXPECT_HELLO = "expect_hello"
    WAIT_ACCEPT = "wait_accept"
    ACTIVE = "active"
    CANCELLING = "cancelling"
    TERMINATED = "terminated"
    FINISHED = "finished"


def _parse_recorded_at(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProtocolStateError("controller recorded_at must be a UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProtocolStateError(f"invalid controller recorded_at timestamp: {value!r}") from exc


def _usage_decreased(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> str | None:
    for field in _USAGE_FIELDS:
        before = previous[field]
        after = current[field]
        if before is not None and after is None:
            return field
        if before is not None and after is not None and after < before:
            return field
    return None


class ProtocolSession:
    """Deterministic authority-preserving session for one harness attempt."""

    def __init__(
        self,
        harness_manifest: Mapping[str, Any],
        trial_request: Mapping[str, Any],
        *,
        store: SchemaStore | None = None,
        hard_limits: ProtocolLimits | None = None,
    ) -> None:
        self.store = store or default_schema_store()
        self.store.validate(harness_manifest, SchemaKind.HARNESS)
        self.store.validate(trial_request, SchemaKind.TRIAL_REQUEST)
        self.harness_manifest = deepcopy(dict(harness_manifest))
        self.trial_request = deepcopy(dict(trial_request))
        requested_limits = ProtocolLimits(**self.trial_request["protocol_limits"])
        if hard_limits is not None:
            for field in (
                "max_line_bytes",
                "max_total_bytes",
                "max_events",
                "max_depth",
                "max_nodes",
                "max_object_properties",
            ):
                if getattr(requested_limits, field) > getattr(hard_limits, field):
                    raise ProtocolLimitError(
                        f"requested {field} exceeds runner hard ceiling"
                    )
        self.limits = requested_limits
        self.request_digest = canonical_digest(self.trial_request)
        self.state = SessionState.EXPECT_HELLO
        self._events: list[dict[str, Any]] = []
        self._hello: dict[str, Any] | None = None
        self._accepted: dict[str, Any] | None = None
        self._result_event: dict[str, Any] | None = None
        self._negotiation: NegotiatedProtocol | None = None
        self._next_harness_sequence = 0
        self._raw_harness_bytes = 0
        self._canonical_bytes = 0
        self._last_recorded_at: datetime | None = None
        self._last_usage: Mapping[str, Any] | None = None
        self._finished: ProtocolTranscript | None = None

    @property
    def events(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(deepcopy(self._events))

    @property
    def negotiation(self) -> NegotiatedProtocol | None:
        return self._negotiation

    def _check_time(self, recorded_at: str) -> datetime:
        observed = _parse_recorded_at(recorded_at)
        if self._last_recorded_at is not None and observed < self._last_recorded_at:
            raise ProtocolStateError(
                "controller recorded_at timestamps must be monotonic"
            )
        return observed

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        if len(self._events) >= self.limits.max_events:
            raise ProtocolLimitError(
                f"event count exceeds {self.limits.max_events}",
                event_index=len(self._events),
            )
        canonical_size = len(canonical_json_bytes(event)) + 1
        if self._canonical_bytes + canonical_size > self.limits.max_total_bytes:
            raise ProtocolLimitError(
                f"canonical transcript exceeds {self.limits.max_total_bytes} bytes"
            )
        self.store.validate(event, SchemaKind.EVENT)
        self._events.append(deepcopy(event))
        self._canonical_bytes += canonical_size
        return deepcopy(event)

    def receive_harness_line(
        self,
        raw_line: bytes | bytearray | memoryview,
        *,
        recorded_at: str,
    ) -> dict[str, Any]:
        raw = bytes(raw_line)
        if self._raw_harness_bytes + len(raw) > self.limits.max_total_bytes:
            raise ProtocolLimitError(
                f"raw harness channel exceeds {self.limits.max_total_bytes} bytes"
            )
        event = decode_json_object_line(
            raw,
            limits=self.limits,
            event_index=self._next_harness_sequence,
        )
        canonical = self.receive_harness_event(
            event,
            recorded_at=recorded_at,
            raw_size_bytes=len(raw),
        )
        return canonical

    def receive_harness_event(
        self,
        event: Mapping[str, Any],
        *,
        recorded_at: str,
        raw_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        if self.state in {SessionState.TERMINATED, SessionState.FINISHED}:
            raise ProtocolStateError("harness event appears after terminal result")
        raw = deepcopy(dict(event))
        if raw.get("schema") != HARNESS_EVENT_SCHEMA:
            raise ProtocolAuthorityError(
                f"raw harness event schema must be {HARNESS_EVENT_SCHEMA!r}"
            )
        event_type = raw.get("type")
        if event_type in _CONTROLLER_EVENT_TYPES:
            raise ProtocolAuthorityError(
                f"harness cannot emit controller-authorized event {event_type!r}"
            )
        if event_type not in _HARNESS_EVENT_TYPES:
            raise ProtocolAuthorityError(
                f"harness event type is not authorized: {event_type!r}"
            )
        forged_fields = sorted(_CONTROLLER_ONLY_FIELDS & raw.keys())
        if forged_fields:
            raise ProtocolAuthorityError(
                "harness supplied controller-only fields: "
                + ", ".join(forged_fields)
            )
        failure = raw.get("failure")
        if isinstance(failure, Mapping) and (
            failure.get("infrastructure") is True
            or failure.get("scope") in _CONTROLLER_FAILURE_SCOPES
        ):
            raise ProtocolAuthorityError(
                "harness cannot classify a failure as controller infrastructure"
            )
        harness_sequence = raw.pop("harness_sequence", None)
        if harness_sequence != self._next_harness_sequence:
            raise ProtocolStateError(
                "harness_sequence must be contiguous from zero; "
                f"expected {self._next_harness_sequence}, received {harness_sequence!r}"
            )
        if raw.get("trial_id") != self.trial_request["trial_id"]:
            raise ProtocolStateError("raw harness trial_id does not match the request")
        if raw.get("attempt_id") != self.trial_request["attempt_id"]:
            raise ProtocolStateError("raw harness attempt_id does not match the request")

        if self.state is SessionState.EXPECT_HELLO:
            if event_type != EventType.HELLO.value:
                raise ProtocolStateError("first raw harness event must be hello")
        elif self.state is SessionState.WAIT_ACCEPT:
            raise ProtocolStateError(
                "controller must record accepted before running harness events"
            )
        elif self.state is SessionState.CANCELLING:
            if event_type not in _CANCELLING_HARNESS_EVENTS:
                raise ProtocolStateError(
                    f"harness event {event_type!r} is not allowed while cancelling"
                )
        elif self.state is not SessionState.ACTIVE:
            raise ProtocolStateError(f"invalid session state: {self.state.value}")
        elif event_type == EventType.HELLO.value:
            raise ProtocolStateError("duplicate hello event")

        observed_time = self._check_time(recorded_at)
        canonical = {
            **raw,
            "schema": "atv.event/v1",
            "source": "harness",
            "sequence": len(self._events),
            "recorded_at": recorded_at,
        }
        if raw_size_bytes is None:
            raw_size_bytes = len(canonical_json_bytes(event)) + 1
        if raw_size_bytes < 0:
            raise ValueError("raw_size_bytes cannot be negative")
        if self._raw_harness_bytes + raw_size_bytes > self.limits.max_total_bytes:
            raise ProtocolLimitError(
                f"raw harness channel exceeds {self.limits.max_total_bytes} bytes"
            )

        next_usage: Mapping[str, Any] | None = None
        if event_type == EventType.USAGE.value:
            current_usage = canonical["cumulative_reported"]
            if self._last_usage is not None:
                decreased = _usage_decreased(self._last_usage, current_usage)
                if decreased:
                    raise ProtocolStateError(
                        f"cumulative usage decreases field {decreased!r}"
                    )
            next_usage = current_usage
        elif event_type == EventType.RESULT.value and self._last_usage is not None:
            decreased = _usage_decreased(
                self._last_usage,
                canonical["harness_result"]["reported_usage"],
            )
            if decreased:
                raise ProtocolStateError(
                    f"terminal usage decreases cumulative field {decreased!r}"
                )

        appended = self._append(canonical)
        self._last_recorded_at = observed_time
        if next_usage is not None:
            self._last_usage = next_usage
        self._raw_harness_bytes += raw_size_bytes
        self._next_harness_sequence += 1
        if event_type == EventType.HELLO.value:
            self._hello = deepcopy(canonical)
            self.state = SessionState.WAIT_ACCEPT
        elif event_type == EventType.RESULT.value:
            self._result_event = deepcopy(canonical)
            self.state = SessionState.TERMINATED
        return appended

    def record_controller_accept(
        self,
        *,
        recorded_at: str,
        emitted_at: str | None = None,
    ) -> dict[str, Any]:
        if self.state is not SessionState.WAIT_ACCEPT or self._hello is None:
            raise ProtocolStateError("controller can accept only after exactly one hello")
        negotiation = negotiate_capabilities(
            self.harness_manifest,
            self.trial_request,
            self._hello,
            store=self.store,
        )
        observed_time = self._check_time(recorded_at)
        accepted = build_accepted_event(
            self.trial_request,
            negotiation,
            emitted_at=emitted_at or recorded_at,
            recorded_at=recorded_at,
            sequence=len(self._events),
        )
        if accepted["request_digest"] != self.request_digest:
            raise ProtocolAuthorityError(
                "controller accepted event is not bound to the exact trial request"
            )
        appended = self._append(accepted)
        self._last_recorded_at = observed_time
        self._accepted = deepcopy(accepted)
        self._negotiation = negotiation
        self.state = SessionState.ACTIVE
        return appended

    def record_controller_cancel(
        self,
        *,
        recorded_at: str,
        reason_code: str,
        grace_period_ms: int,
        emitted_at: str | None = None,
    ) -> dict[str, Any]:
        if self.state is not SessionState.ACTIVE:
            raise ProtocolStateError("controller can cancel only an active session")
        observed_time = self._check_time(recorded_at)
        event = {
            "schema": "atv.event/v1",
            "type": EventType.CANCEL.value,
            "source": "controller",
            "protocol_version": 1,
            "trial_id": self.trial_request["trial_id"],
            "attempt_id": self.trial_request["attempt_id"],
            "sequence": len(self._events),
            "emitted_at": emitted_at or recorded_at,
            "recorded_at": recorded_at,
            "reason_code": reason_code,
            "grace_period_ms": grace_period_ms,
        }
        appended = self._append(event)
        self._last_recorded_at = observed_time
        self.state = SessionState.CANCELLING
        return appended

    def record_controller_error(
        self,
        *,
        recorded_at: str,
        failure: Mapping[str, Any],
        emitted_at: str | None = None,
    ) -> dict[str, Any]:
        if self.state not in {SessionState.ACTIVE, SessionState.CANCELLING}:
            raise ProtocolStateError(
                "controller error is not valid in the current session state"
            )
        observed_time = self._check_time(recorded_at)
        event = {
            "schema": "atv.event/v1",
            "type": EventType.CONTROLLER_ERROR.value,
            "source": "controller",
            "protocol_version": 1,
            "trial_id": self.trial_request["trial_id"],
            "attempt_id": self.trial_request["attempt_id"],
            "sequence": len(self._events),
            "emitted_at": emitted_at or recorded_at,
            "recorded_at": recorded_at,
            "failure": deepcopy(dict(failure)),
        }
        appended = self._append(event)
        self._last_recorded_at = observed_time
        return appended

    def finish(self) -> ProtocolTranscript:
        """Record harness EOF and return an authority-verified canonical transcript."""
        if self._finished is not None:
            return self._finished
        if self.state is not SessionState.TERMINATED:
            raise ProtocolStateError(
                "harness EOF before exactly one terminal result event"
            )
        assert self._hello is not None
        assert self._accepted is not None
        assert self._result_event is not None
        merged = MergedTranscriptVerifier(
            store=self.store,
            limits=self.limits,
            expected_trial_id=self.trial_request["trial_id"],
            expected_attempt_id=self.trial_request["attempt_id"],
            expected_request_digest=self.request_digest,
        ).parse_bytes(canonical_jsonl(self._events))
        if tuple(merged.events) != tuple(self._events):
            raise ProtocolStateError("canonical merged transcript changed during verification")
        self._finished = ProtocolTranscript(
            events=tuple(deepcopy(self._events)),
            hello=deepcopy(self._hello),
            accepted=deepcopy(self._accepted),
            result_event=deepcopy(self._result_event),
            authority_verified=True,
            _authority_token=_SESSION_AUTHORITY_TOKEN,
        )
        self.state = SessionState.FINISHED
        return self._finished

    def eof(self) -> ProtocolTranscript:
        return self.finish()


def has_session_authority(transcript: ProtocolTranscript) -> bool:
    return transcript._authority_token is _SESSION_AUTHORITY_TOKEN
