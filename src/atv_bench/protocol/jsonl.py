"""Strict UTF-8 JSONL parser and protocol state machine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping

from .canonical import strict_json_loads, verify_digest
from .errors import (
    ProtocolDecodeError,
    ProtocolLimitError,
    ProtocolStateError,
    SchemaValidationError,
)
from .schemas import SchemaKind, SchemaStore, default_schema_store
from .types import EventType, ProtocolTranscript

_UTF8_BOM = b"\xef\xbb\xbf"
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
_NONTERMINAL_EVENTS = {
    EventType.STATUS.value,
    EventType.MODEL_CALL.value,
    EventType.TOOL_CALL.value,
    EventType.CHECKPOINT.value,
    EventType.ARTIFACT.value,
    EventType.USAGE.value,
    EventType.ERROR.value,
    EventType.CANCEL.value,
    EventType.CONTROLLER_ERROR.value,
}


@dataclass(frozen=True)
class ProtocolLimits:
    max_line_bytes: int = 262_144
    max_total_bytes: int = 33_554_432
    max_events: int = 20_000
    max_depth: int = 32
    max_nodes: int = 100_000
    max_object_properties: int = 256

    def __post_init__(self) -> None:
        for field_name in (
            "max_line_bytes",
            "max_total_bytes",
            "max_events",
            "max_depth",
            "max_nodes",
            "max_object_properties",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_line_bytes > self.max_total_bytes:
            raise ValueError("max_line_bytes cannot exceed max_total_bytes")


def _maximum_json_depth(text: str) -> int:
    depth = 0
    maximum = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            maximum = max(maximum, depth)
        elif character in "]}":
            depth -= 1
    return maximum


def _usage_not_decreasing(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> str | None:
    for field in _USAGE_FIELDS:
        prior_value = previous[field]
        current_value = current[field]
        if prior_value is not None and current_value is None:
            return field
        if (
            prior_value is not None
            and current_value is not None
            and current_value < prior_value
        ):
            return field
    return None


def _validate_node_limits(
    value: Any,
    *,
    max_nodes: int,
    max_object_properties: int,
) -> None:
    count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        count += 1
        if count > max_nodes:
            raise ProtocolLimitError(f"JSON node count exceeds {max_nodes}")
        if isinstance(current, dict):
            if len(current) > max_object_properties:
                raise ProtocolLimitError(
                    "JSON object property count exceeds "
                    f"{max_object_properties}"
                )
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def decode_json_object_line(
    raw_line: bytes | bytearray | memoryview,
    *,
    limits: ProtocolLimits,
    line_number: int = 1,
    event_index: int = 0,
) -> dict[str, Any]:
    """Decode exactly one bounded UTF-8 JSON object line without schema assumptions."""
    raw = bytes(raw_line)
    if raw.startswith(_UTF8_BOM):
        raise ProtocolDecodeError(
            "UTF-8 BOM is forbidden in protocol output",
            line_number=line_number,
            event_index=event_index,
        )
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw:
        raise ProtocolDecodeError(
            "receive_harness_line accepts exactly one physical line",
            line_number=line_number,
            event_index=event_index,
        )
    if not raw:
        raise ProtocolDecodeError(
            "blank JSONL lines are forbidden",
            line_number=line_number,
            event_index=event_index,
        )
    if len(raw) > limits.max_line_bytes:
        raise ProtocolLimitError(
            f"line exceeds {limits.max_line_bytes} bytes",
            line_number=line_number,
            event_index=event_index,
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolDecodeError(
            f"invalid UTF-8: {exc}",
            line_number=line_number,
            event_index=event_index,
        ) from exc
    if not text.strip():
        raise ProtocolDecodeError(
            "whitespace-only JSONL lines are forbidden",
            line_number=line_number,
            event_index=event_index,
        )
    if _maximum_json_depth(text) > limits.max_depth:
        raise ProtocolLimitError(
            f"JSON nesting exceeds depth {limits.max_depth}",
            line_number=line_number,
            event_index=event_index,
        )
    try:
        value = strict_json_loads(text)
    except ProtocolDecodeError as exc:
        raise ProtocolDecodeError(
            exc.message,
            line_number=line_number,
            event_index=event_index,
        ) from exc
    try:
        _validate_node_limits(
            value,
            max_nodes=limits.max_nodes,
            max_object_properties=limits.max_object_properties,
        )
    except ProtocolLimitError as exc:
        raise ProtocolLimitError(
            exc.message,
            line_number=line_number,
            event_index=event_index,
        ) from exc
    if not isinstance(value, dict):
        raise ProtocolDecodeError(
            "each JSONL line must contain one JSON object",
            line_number=line_number,
            event_index=event_index,
        )
    return value


class JsonlProtocolParser:
    """Integrity-only verifier for a merged normalized transcript.

    It validates schema and state but **cannot prove event authority**, because all bytes
    may have come from one untrusted source. Scored runs must use ``ProtocolSession`` to
    receive raw harness events and inject controller events out-of-band.
    """

    def __init__(
        self,
        *,
        store: SchemaStore | None = None,
        limits: ProtocolLimits | None = None,
        expected_trial_id: str | None = None,
        expected_attempt_id: str | None = None,
        expected_request_digest: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store or default_schema_store()
        self.limits = limits or ProtocolLimits()
        self.expected_trial_id = expected_trial_id
        self.expected_attempt_id = expected_attempt_id
        self.expected_request_digest = (
            dict(expected_request_digest) if expected_request_digest else None
        )

    def parse_stream(self, stream: BinaryIO) -> ProtocolTranscript:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = stream.readline(self.limits.max_line_bytes + 3)
            if not isinstance(line, bytes):
                raise ProtocolDecodeError("protocol stream must be opened in binary mode")
            if not line:
                break
            content = line[:-1] if line.endswith(b"\n") else line
            if content.endswith(b"\r"):
                content = content[:-1]
            if len(content) > self.limits.max_line_bytes:
                raise ProtocolLimitError(
                    f"line exceeds {self.limits.max_line_bytes} bytes"
                )
            total += len(line)
            if total > self.limits.max_total_bytes:
                raise ProtocolLimitError(
                    f"protocol output exceeds {self.limits.max_total_bytes} bytes"
                )
            chunks.append(line)
        return self.parse_bytes(b"".join(chunks))

    def parse_bytes(self, data: bytes | bytearray | memoryview) -> ProtocolTranscript:
        raw = bytes(data)
        if len(raw) > self.limits.max_total_bytes:
            raise ProtocolLimitError(
                f"protocol output exceeds {self.limits.max_total_bytes} bytes"
            )
        if raw.startswith(_UTF8_BOM):
            raise ProtocolDecodeError("UTF-8 BOM is forbidden in protocol output")
        if not raw:
            raise ProtocolStateError("empty protocol output; expected hello event")

        lines = raw.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        events: list[Mapping[str, Any]] = []
        state = "hello"
        hello: Mapping[str, Any] | None = None
        accepted: Mapping[str, Any] | None = None
        result_event: Mapping[str, Any] | None = None
        trial_id = self.expected_trial_id
        attempt_id = self.expected_attempt_id
        last_usage: Mapping[str, Any] | None = None

        for line_number, raw_line in enumerate(lines, start=1):
            if len(events) >= self.limits.max_events:
                raise ProtocolLimitError(
                    f"event count exceeds {self.limits.max_events}",
                    line_number=line_number,
                    event_index=len(events),
                )
            event = decode_json_object_line(
                raw_line,
                limits=self.limits,
                line_number=line_number,
                event_index=len(events),
            )
            try:
                self.store.validate(event, SchemaKind.EVENT)
            except SchemaValidationError as exc:
                raise SchemaValidationError(
                    exc.message,
                    line_number=line_number,
                    event_index=len(events),
                    path=exc.path,
                ) from exc

            event_index = len(events)
            if event["sequence"] != event_index:
                raise ProtocolStateError(
                    f"sequence must be contiguous from zero; expected {event_index}, "
                    f"received {event['sequence']}",
                    line_number=line_number,
                    event_index=event_index,
                )
            trial_id = trial_id or event["trial_id"]
            attempt_id = attempt_id or event["attempt_id"]
            if event["trial_id"] != trial_id:
                raise ProtocolStateError(
                    "trial_id changed within the event stream",
                    line_number=line_number,
                    event_index=event_index,
                )
            if event["attempt_id"] != attempt_id:
                raise ProtocolStateError(
                    "attempt_id changed within the event stream",
                    line_number=line_number,
                    event_index=event_index,
                )

            event_type = event["type"]
            if state == "hello":
                if event_type != EventType.HELLO.value:
                    raise ProtocolStateError(
                        "first event must be hello",
                        line_number=line_number,
                        event_index=event_index,
                    )
                hello = event
                state = "accepted"
            elif state == "accepted":
                if event_type != EventType.ACCEPTED.value:
                    raise ProtocolStateError(
                        "second event must be accepted",
                        line_number=line_number,
                        event_index=event_index,
                    )
                assert hello is not None
                if (
                    event["selected_protocol_version"]
                    not in hello["supported_protocol_versions"]
                ):
                    raise ProtocolStateError(
                        "accepted protocol version was not advertised by hello",
                        line_number=line_number,
                        event_index=event_index,
                    )
                if (
                    self.expected_request_digest is not None
                    and event["request_digest"] != self.expected_request_digest
                ):
                    raise ProtocolStateError(
                        "accepted request_digest does not match the expected request",
                        line_number=line_number,
                        event_index=event_index,
                    )
                accepted = event
                state = "running"
            elif state == "running":
                if event_type == EventType.RESULT.value:
                    nested = event["harness_result"]
                    if last_usage is not None:
                        decreased = _usage_not_decreasing(
                            last_usage, nested["reported_usage"]
                        )
                        if decreased:
                            raise ProtocolStateError(
                                f"terminal usage decreases cumulative field {decreased!r}",
                                line_number=line_number,
                                event_index=event_index,
                            )
                    result_event = event
                    state = "terminated"
                elif event_type not in _NONTERMINAL_EVENTS:
                    raise ProtocolStateError(
                        f"event {event_type!r} is not valid after accepted",
                        line_number=line_number,
                        event_index=event_index,
                    )
            else:
                raise ProtocolStateError(
                    "event appears after terminal result",
                    line_number=line_number,
                    event_index=event_index,
                )

            if event_type == EventType.USAGE.value:
                current_usage = event["cumulative_reported"]
                if last_usage is not None:
                    decreased = _usage_not_decreasing(last_usage, current_usage)
                    if decreased:
                        raise ProtocolStateError(
                            f"cumulative usage decreases field {decreased!r}",
                            line_number=line_number,
                            event_index=event_index,
                        )
                last_usage = current_usage
            events.append(event)

        if state != "terminated" or result_event is None:
            raise ProtocolStateError(
                "EOF before exactly one terminal result event",
                event_index=len(events),
            )
        assert hello is not None
        assert accepted is not None
        return ProtocolTranscript(
            events=tuple(events),
            hello=hello,
            accepted=accepted,
            result_event=result_event,
            authority_verified=False,
        )


def verify_artifact_event(event: Mapping[str, Any], content: bytes) -> None:
    if event.get("type") != EventType.ARTIFACT.value:
        raise ProtocolStateError("artifact verification requires an artifact event")
    artifact = event["artifact"]
    if artifact["size_bytes"] != len(content):
        raise ProtocolStateError(
            f"artifact size mismatch: declared {artifact['size_bytes']}, "
            f"observed {len(content)}"
        )
    verify_digest(content, artifact["digest"])


MergedTranscriptVerifier = JsonlProtocolParser
