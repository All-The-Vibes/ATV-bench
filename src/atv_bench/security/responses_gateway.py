"""Strict OpenAI Responses HTTP front-end for brokered benchmark model access.

The existing :mod:`atv_bench.security.gateway` deliberately exposes a small,
text-only provider contract.  Copilot BYOK traffic needs the richer Responses
shape (message history, function tools, function-call output, and SSE events).
This module therefore reuses the public credential-broker, route, usage, and
attestation primitives while defining an explicit rich-provider interface.

Security boundaries:

* Harnesses authenticate with an opaque trial handle, never a provider secret.
* The provider credential crosses only ``CredentialBroker.invoke_provider``.
* Requests and provider events are size-bounded and fail closed on unknown API
  control fields.
* Route identity and budget lineage are signed into receipts.
* One shared ``ResponsesBudgetLedger`` accounts for every request handled by
  all ``ResponsesGateway`` instances to which it is supplied.
* Logs contain digests and counters, not prompts, tool arguments, handles, or
  provider credentials.

The implemented wire subset covers function tools, function-call history and
outputs, text/reasoning deltas, non-streaming responses, and SSE terminal
events.  It deliberately rejects background jobs, native hosted tools, unknown
event types, and chunked request bodies until those paths have dedicated
conformance coverage.

The module is transport-ready but provider-neutral.  A deployment must supply a
``ResponsesBackend`` adapter which performs the actual provider HTTP call,
honours the cancellation event, and does not hide transparent retries.  That
explicit seam is intentional: the text-only ``ModelProvider`` contract cannot
faithfully carry Responses tool events and must not be presented as if it
could.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.server
import json
import math
import queue
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, NoReturn, Protocol
from urllib.parse import urlsplit

from atv_bench.security.attestation import (
    AttestationSigner,
    canonical_json_bytes,
)
from atv_bench.security.broker import (
    Authorization,
    BrokerError,
    BrokerErrorCode,
    BudgetIdentity,
    CredentialBroker,
    TrialBudget,
    UnderreportPolicy,
)
from atv_bench.security.gateway import (
    GatewayStatus,
    ProviderUsage,
    RouteDefinition,
    UsageSummary,
    conservative_token_count,
)


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]*$")
_TERMINAL_STREAM_EVENTS = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)
_SUPPORTED_STREAM_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.completed",
        "response.incomplete",
        "response.failed",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.refusal.delta",
        "response.refusal.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "error",
    }
)
_STREAM_EVENT_FIELDS = frozenset(
    {
        "type",
        "sequence_number",
        "response",
        "item",
        "part",
        "output_index",
        "content_index",
        "item_id",
        "delta",
        "text",
        "arguments",
        "name",
        "call_id",
        "logprobs",
        "error",
        "code",
        "message",
        "param",
        "obfuscation",
        "refusal",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "background",
        "conversation",
        "include",
        "input",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "model",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
    }
)
_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "object",
        "created_at",
        "completed_at",
        "status",
        "background",
        "error",
        "incomplete_details",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "model",
        "output",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "store",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "truncation",
        "usage",
        "user",
        "metadata",
    }
)


class ResponsesGatewayStatus(str, Enum):
    SUCCESS = "success"
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN_HANDLE = "unknown_handle"
    EXPIRED_HANDLE = "expired_handle"
    REVOKED_HANDLE = "revoked_handle"
    REPLAYED_HANDLE = "replayed_handle"
    POLICY_DENIED = "policy_denied"
    ROUTE_MISMATCH = "route_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_FAILURE = "provider_failure"
    USAGE_UNDERREPORTED = "usage_underreported"
    CANCELLED = "cancelled"


class ResponsesProtocolError(Exception):
    """Safe request/transport failure suitable for an OpenAI-style error body."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        param: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.safe_message = message
        self.param = param


class ResponsesBackendError(Exception):
    """Typed provider-adapter failure with no backend exception text exposure."""

    def __init__(
        self,
        *,
        retryable: bool = False,
        request_id: str | None = None,
    ):
        super().__init__("responses backend call failed")
        self.retryable = bool(retryable)
        self.request_id = request_id


class ResponsesCancelled(Exception):
    """Internal cooperative-cancellation sentinel."""


class _StreamOutputLimitExceeded(Exception):
    """Provider emitted more visible output than the reserved request limit."""


@dataclass(frozen=True)
class ResponsesGatewayConfig:
    max_request_bytes: int = 1_048_576
    max_response_bytes: int = 8_388_608
    max_stream_event_bytes: int = 1_048_576
    max_header_bytes: int = 16_384
    max_string_bytes: int = 262_144
    max_collection_items: int = 1_024
    max_json_depth: int = 24
    max_json_nodes: int = 50_000
    max_output_tokens_per_request: int = 262_144
    max_log_records: int = 1_024
    max_stream_queue_events: int = 64
    queue_poll_seconds: float = 0.05

    def __post_init__(self) -> None:
        integer_fields = (
            "max_request_bytes",
            "max_response_bytes",
            "max_stream_event_bytes",
            "max_header_bytes",
            "max_string_bytes",
            "max_collection_items",
            "max_json_depth",
            "max_json_nodes",
            "max_output_tokens_per_request",
            "max_log_records",
            "max_stream_queue_events",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.queue_poll_seconds, bool)
            or not isinstance(self.queue_poll_seconds, (int, float))
            or not math.isfinite(self.queue_poll_seconds)
            or self.queue_poll_seconds <= 0
        ):
            raise ValueError("queue_poll_seconds must be a positive finite number")


@dataclass(frozen=True)
class ResponsesBackendRequest:
    gateway_request_id: str
    provider_request_id: str
    provider_model: str
    payload: Mapping[str, Any] = field(repr=False)
    cancel_event: threading.Event = field(repr=False, compare=False)


@dataclass(frozen=True)
class ResponsesBackendResponse:
    provider_id: str
    model: str
    request_id: str
    response: Mapping[str, Any] = field(repr=False)
    usage: ProviderUsage

    def __post_init__(self) -> None:
        for name in ("provider_id", "model", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if not isinstance(self.response, Mapping):
            raise TypeError("response must be a mapping")


@dataclass(frozen=True)
class ResponsesStreamChunk:
    """One provider SSE event and, on the terminal event, its authoritative result."""

    event: Mapping[str, Any] = field(repr=False)
    final_response: ResponsesBackendResponse | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.event, Mapping):
            raise TypeError("stream event must be a mapping")


class ResponsesBackend(Protocol):
    """Provider adapter required by :class:`ResponsesGateway`.

    Implementations receive broker-held credentials only inside the trusted
    callback.  ``stream`` must yield exactly one terminal chunk whose
    ``final_response`` is populated.  Both methods must observe
    ``request.cancel_event``; ``cancel`` is the active interruption hook used
    when an HTTP client disconnects.  One method invocation represents exactly
    one provider model call; adapters must not perform unreported retries.
    """

    def create(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> ResponsesBackendResponse: ...

    def stream(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> Iterable[ResponsesStreamChunk]: ...

    def cancel(self, provider_request_id: str) -> None: ...


@dataclass(frozen=True)
class ResponsesGatewayLogRecord:
    request_id: str
    trial_id: str
    attempt_id: str
    request_digest: str
    requested_model: str
    route_id: str
    resolved_provider_model: str
    provider_request_id: str
    status: ResponsesGatewayStatus
    streaming: bool
    request_bytes: int
    streamed_events: int
    usage: UsageSummary
    started_at_ms: int
    completed_at_ms: int
    attestation: Mapping[str, Any] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "request_digest": self.request_digest,
            "requested_model": self.requested_model,
            "route_id": self.route_id,
            "resolved_provider_model": self.resolved_provider_model,
            "provider_request_id": self.provider_request_id,
            "status": self.status.value,
            "streaming": self.streaming,
            "request_bytes": self.request_bytes,
            "streamed_events": self.streamed_events,
            "usage": self.usage.to_dict(),
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "attestation": copy.deepcopy(dict(self.attestation)),
        }


@dataclass(frozen=True)
class ResponsesHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes | "ResponsesStreamBody"

    @property
    def streaming(self) -> bool:
        return isinstance(self.body, ResponsesStreamBody)


@dataclass(frozen=True)
class _Reservation:
    identity: BudgetIdentity
    planned: UsageSummary


@dataclass
class _Counters:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: int = 0

    def add(self, usage: UsageSummary) -> None:
        self.calls += usage.model_calls
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens
        self.cost_microusd += usage.cost_microusd

    def subtract(self, usage: UsageSummary) -> None:
        self.calls -= usage.model_calls
        self.input_tokens -= usage.input_tokens
        self.output_tokens -= usage.output_tokens
        self.total_tokens -= usage.total_tokens
        self.cost_microusd -= usage.cost_microusd

    def usage(self) -> UsageSummary:
        return UsageSummary(
            model_calls=self.calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            cost_microusd=self.cost_microusd,
        )


@dataclass
class _Ledger:
    committed: _Counters = field(default_factory=_Counters)
    reserved: _Counters = field(default_factory=_Counters)


class ResponsesBudgetLedger:
    """Thread-safe central budget ledger for rich Responses traffic."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ledgers: dict[BudgetIdentity, _Ledger] = {}

    def reserve(
        self,
        identity: BudgetIdentity,
        budget: TrialBudget,
        planned: UsageSummary,
    ) -> _Reservation:
        with self._lock:
            ledger = self._ledgers.setdefault(identity, _Ledger())
            combined = (
                ledger.committed.usage().plus(ledger.reserved.usage()).plus(planned)
            )
            _raise_if_budget_exceeded(combined, budget)
            ledger.reserved.add(planned)
            return _Reservation(identity=identity, planned=planned)

    def finalize(
        self,
        reservation: _Reservation,
        budget: TrialBudget,
        actual: UsageSummary,
    ) -> GatewayStatus | None:
        with self._lock:
            ledger = self._ledgers[reservation.identity]
            ledger.reserved.subtract(reservation.planned)
            ledger.committed.add(actual)
            try:
                _raise_if_budget_exceeded(ledger.committed.usage(), budget)
            except _BudgetExceeded as exc:
                return exc.status
            return None

    def cancel(self, reservation: _Reservation) -> None:
        with self._lock:
            ledger = self._ledgers[reservation.identity]
            ledger.reserved.subtract(reservation.planned)

    def state(
        self,
        identity: BudgetIdentity,
    ) -> tuple[UsageSummary, UsageSummary]:
        with self._lock:
            ledger = self._ledgers.get(identity)
            if ledger is None:
                return UsageSummary(), UsageSummary()
            return ledger.committed.usage(), ledger.reserved.usage()

    def cumulative(self, identity: BudgetIdentity) -> UsageSummary:
        return self.state(identity)[0]


class _BudgetExceeded(Exception):
    def __init__(self, status: GatewayStatus):
        super().__init__(status.value)
        self.status = status


def _raise_if_budget_exceeded(usage: UsageSummary, budget: TrialBudget) -> None:
    if usage.model_calls > budget.max_model_calls:
        raise _BudgetExceeded(GatewayStatus.CALL_BUDGET_EXCEEDED)
    if usage.input_tokens > budget.max_input_tokens:
        raise _BudgetExceeded(GatewayStatus.INPUT_TOKEN_BUDGET_EXCEEDED)
    if usage.output_tokens > budget.max_output_tokens:
        raise _BudgetExceeded(GatewayStatus.OUTPUT_TOKEN_BUDGET_EXCEEDED)
    if usage.total_tokens > budget.max_total_tokens:
        raise _BudgetExceeded(GatewayStatus.TOTAL_TOKEN_BUDGET_EXCEEDED)
    if usage.cost_microusd > budget.max_cost_microusd:
        raise _BudgetExceeded(GatewayStatus.COST_BUDGET_EXCEEDED)


@dataclass
class _PreparedRequest:
    request_id: str
    provider_request_id: str
    handle: str = field(repr=False)
    authorization: Authorization
    route: RouteDefinition
    public_payload: dict[str, Any] = field(repr=False)
    provider_payload: dict[str, Any] = field(repr=False)
    request_digest: str
    request_bytes: int
    input_tokens: int
    planned: UsageSummary
    reservation: _Reservation
    streaming: bool
    started_at_ms: int
    model_receipt: Mapping[str, Any]


@dataclass(frozen=True)
class _StreamTerminal:
    error: Exception | None = None


class ResponsesStreamBody:
    """Bounded SSE iterator with cooperative close/disconnect cancellation."""

    def __init__(
        self,
        *,
        events: "queue.Queue[bytes | _StreamTerminal]",
        cancel_event: threading.Event,
        cancel_backend: Callable[[], None],
        poll_seconds: float,
    ):
        self._events = events
        self._cancel_event = cancel_event
        self._cancel_backend = cancel_backend
        self._poll_seconds = poll_seconds
        self._closed = False
        self._finished = False
        self._lock = threading.Lock()

    def __iter__(self) -> Iterator[bytes]:
        try:
            while True:
                try:
                    item = self._events.get(timeout=self._poll_seconds)
                except queue.Empty:
                    if self._finished:
                        return
                    continue
                if isinstance(item, _StreamTerminal):
                    with self._lock:
                        self._finished = True
                    return
                yield item
        finally:
            if not self._finished:
                self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._finished:
                return
            self._cancel_event.set()
        self._cancel_backend()


class _JsonLimits:
    def __init__(self, config: ResponsesGatewayConfig):
        self.config = config
        self.nodes = 0

    def check(self, value: Any, *, path: str = "$", depth: int = 0) -> None:
        self.nodes += 1
        if self.nodes > self.config.max_json_nodes:
            _invalid("request JSON contains too many values", path)
        if depth > self.config.max_json_depth:
            _invalid("request JSON nesting is too deep", path)
        if value is None or type(value) in {bool, int}:
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                _invalid("non-finite numbers are not allowed", path)
            return
        if isinstance(value, str):
            if len(value.encode("utf-8")) > self.config.max_string_bytes:
                _invalid("string exceeds the configured byte limit", path)
            return
        if isinstance(value, Mapping):
            if len(value) > self.config.max_collection_items:
                _invalid("object contains too many fields", path)
            for key, item in value.items():
                if not isinstance(key, str):
                    _invalid("object keys must be strings", path)
                self.check(item, path=f"{path}.{key}", depth=depth + 1)
            return
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            if len(value) > self.config.max_collection_items:
                _invalid("array contains too many items", path)
            for index, item in enumerate(value):
                self.check(item, path=f"{path}[{index}]", depth=depth + 1)
            return
        _invalid(f"unsupported JSON value {type(value).__name__}", path)


def _invalid(message: str, param: str | None = None) -> NoReturn:
    raise ResponsesProtocolError(
        status_code=400,
        code="invalid_request",
        message=message,
        param=param,
    )


def _strict_json_loads(body: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                _invalid(f"duplicate object field {key!r}")
            output[key] = value
        return output

    try:
        text = body.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: _invalid(
                f"non-finite number {value!r} is not allowed"
            ),
        )
    except UnicodeDecodeError as exc:
        _invalid("request body must be UTF-8 JSON")
        raise AssertionError from exc
    except json.JSONDecodeError as exc:
        _invalid("request body is not valid JSON")
        raise AssertionError from exc


def _wire_json_bytes(value: Any) -> bytes:
    """Deterministic API JSON which preserves validated finite float values."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _exact_keys(
    value: Mapping[str, Any],
    allowed: frozenset[str] | set[str],
    *,
    path: str,
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _invalid(f"unknown field {unknown[0]!r}", f"{path}.{unknown[0]}")


def _require_text(
    value: Any,
    *,
    path: str,
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        _invalid(
            "field must be non-empty text" if nonempty else "field must be text", path
        )
    return value


def _require_bool(value: Any, *, path: str) -> bool:
    if type(value) is not bool:
        _invalid("field must be a boolean", path)
    return value


def _require_int(
    value: Any,
    *,
    path: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("field must be an integer", path)
    if value < minimum or (maximum is not None and value > maximum):
        _invalid("integer field is outside the supported range", path)
    return value


def _validate_content_part(part: Any, *, path: str) -> None:
    if not isinstance(part, Mapping):
        _invalid("content part must be an object", path)
    part_type = _require_text(part.get("type"), path=f"{path}.type")
    fields_by_type = {
        "input_text": {"type", "text"},
        "output_text": {"type", "text", "annotations", "logprobs"},
        "refusal": {"type", "refusal"},
        "input_image": {"type", "detail", "file_id", "image_url"},
        "input_file": {"type", "file_data", "file_id", "file_url", "filename"},
    }
    allowed = fields_by_type.get(part_type)
    if allowed is None:
        _invalid(f"unsupported content part type {part_type!r}", f"{path}.type")
    _exact_keys(part, allowed, path=path)
    if part_type in {"input_text", "output_text"}:
        _require_text(part.get("text"), path=f"{path}.text", nonempty=False)
    elif part_type == "refusal":
        _require_text(part.get("refusal"), path=f"{path}.refusal", nonempty=False)
    elif part_type == "input_image":
        sources = [name for name in ("file_id", "image_url") if part.get(name)]
        if len(sources) != 1:
            _invalid("input_image requires exactly one source", path)
    elif part_type == "input_file":
        sources = [
            name for name in ("file_data", "file_id", "file_url") if part.get(name)
        ]
        if len(sources) != 1:
            _invalid("input_file requires exactly one source", path)


def _validate_content(value: Any, *, path: str) -> None:
    if isinstance(value, str):
        return
    if not isinstance(value, list) or not value:
        _invalid("content must be text or a non-empty array", path)
    for index, part in enumerate(value):
        _validate_content_part(part, path=f"{path}[{index}]")


def _validate_input_item(item: Any, *, path: str) -> None:
    if not isinstance(item, Mapping):
        _invalid("input item must be an object", path)
    item_type = item.get("type", "message" if "role" in item else None)
    item_type = _require_text(item_type, path=f"{path}.type")
    if item_type == "message":
        _exact_keys(item, {"id", "type", "role", "content", "status"}, path=path)
        role = _require_text(item.get("role"), path=f"{path}.role")
        if role not in {"user", "assistant", "system", "developer"}:
            _invalid("unsupported message role", f"{path}.role")
        _validate_content(item.get("content"), path=f"{path}.content")
        return
    if item_type == "function_call":
        _exact_keys(
            item,
            {"id", "type", "status", "arguments", "call_id", "name"},
            path=path,
        )
        for field_name in ("arguments", "call_id", "name"):
            _require_text(
                item.get(field_name),
                path=f"{path}.{field_name}",
                nonempty=field_name != "arguments",
            )
        return
    if item_type == "function_call_output":
        _exact_keys(item, {"id", "type", "call_id", "output", "status"}, path=path)
        _require_text(item.get("call_id"), path=f"{path}.call_id")
        output = item.get("output")
        if isinstance(output, str):
            return
        if not isinstance(output, list):
            _invalid(
                "function_call_output output must be text or an array", f"{path}.output"
            )
        for index, part in enumerate(output):
            _validate_content_part(part, path=f"{path}.output[{index}]")
        return
    if item_type == "item_reference":
        _exact_keys(item, {"type", "id"}, path=path)
        _require_text(item.get("id"), path=f"{path}.id")
        return
    _invalid(f"unsupported input item type {item_type!r}", f"{path}.type")


def _validate_tools(value: Any, *, path: str) -> None:
    if not isinstance(value, list):
        _invalid("tools must be an array", path)
    names: set[str] = set()
    for index, tool in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(tool, Mapping):
            _invalid("tool must be an object", item_path)
        _exact_keys(
            tool,
            {"type", "name", "description", "parameters", "strict"},
            path=item_path,
        )
        if tool.get("type") != "function":
            _invalid("only function tools are supported", f"{item_path}.type")
        name = _require_text(tool.get("name"), path=f"{item_path}.name")
        if name in names:
            _invalid("tool names must be unique", f"{item_path}.name")
        names.add(name)
        if "description" in tool:
            _require_text(
                tool["description"],
                path=f"{item_path}.description",
                nonempty=False,
            )
        if "parameters" in tool and not isinstance(tool["parameters"], Mapping):
            _invalid(
                "function parameters must be a JSON Schema object",
                f"{item_path}.parameters",
            )
        if "strict" in tool:
            _require_bool(tool["strict"], path=f"{item_path}.strict")


def _validate_tool_choice(value: Any, *, path: str) -> None:
    if isinstance(value, str):
        if value not in {"none", "auto", "required"}:
            _invalid("unsupported tool_choice", path)
        return
    if not isinstance(value, Mapping):
        _invalid("tool_choice must be text or an object", path)
    _exact_keys(value, {"type", "name"}, path=path)
    if value.get("type") != "function":
        _invalid("only function tool_choice objects are supported", f"{path}.type")
    _require_text(value.get("name"), path=f"{path}.name")


def _validate_response_request(
    payload: Any,
    *,
    config: ResponsesGatewayConfig,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _invalid("request body must be a JSON object")
    _JsonLimits(config).check(payload)
    _exact_keys(payload, _REQUEST_FIELDS, path="$")
    model = _require_text(payload.get("model"), path="$.model")
    if not _MODEL_RE.fullmatch(model):
        _invalid("model contains unsupported characters", "$.model")
    if "input" not in payload:
        _invalid("input is required", "$.input")
    request_input = payload["input"]
    if not isinstance(request_input, str):
        if not isinstance(request_input, list) or not request_input:
            _invalid("input must be text or a non-empty array", "$.input")
        for index, item in enumerate(request_input):
            _validate_input_item(item, path=f"$.input[{index}]")
    if "instructions" in payload and payload["instructions"] is not None:
        _require_text(
            payload["instructions"],
            path="$.instructions",
            nonempty=False,
        )
    max_output = payload.get("max_output_tokens", 4096)
    _require_int(
        max_output,
        path="$.max_output_tokens",
        minimum=1,
        maximum=config.max_output_tokens_per_request,
    )
    if "max_tool_calls" in payload:
        _require_int(payload["max_tool_calls"], path="$.max_tool_calls", minimum=1)
    for name in ("background", "parallel_tool_calls", "store", "stream"):
        if name in payload:
            _require_bool(payload[name], path=f"$.{name}")
    if payload.get("background") is True:
        _invalid("background Responses are not supported", "$.background")
    if payload.get("store") is True:
        _invalid(
            "benchmark Responses must not be stored by the provider",
            "$.store",
        )
    for name in (
        "conversation",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
    ):
        if payload.get(name) is not None:
            _invalid(
                "provider-side response, conversation, prompt, and cache state "
                "is not allowed in benchmark requests",
                f"$.{name}",
            )
    if "temperature" in payload:
        value = payload["temperature"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _invalid("temperature must be numeric", "$.temperature")
        if not math.isfinite(value) or not 0 <= value <= 2:
            _invalid("temperature must be between 0 and 2", "$.temperature")
    if "top_p" in payload:
        value = payload["top_p"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _invalid("top_p must be numeric", "$.top_p")
        if not math.isfinite(value) or not 0 <= value <= 1:
            _invalid("top_p must be between 0 and 1", "$.top_p")
    if "top_logprobs" in payload:
        _require_int(
            payload["top_logprobs"],
            path="$.top_logprobs",
            minimum=0,
            maximum=20,
        )
    if "tools" in payload:
        _validate_tools(payload["tools"], path="$.tools")
    if "tool_choice" in payload:
        _validate_tool_choice(payload["tool_choice"], path="$.tool_choice")
    if "metadata" in payload:
        metadata = payload["metadata"]
        if not isinstance(metadata, Mapping):
            _invalid("metadata must be an object", "$.metadata")
        for key, value in metadata.items():
            _require_text(key, path="$.metadata key")
            _require_text(value, path=f"$.metadata.{key}", nonempty=False)
    if "include" in payload:
        include = payload["include"]
        if not isinstance(include, list):
            _invalid("include must be an array", "$.include")
        for index, value in enumerate(include):
            _require_text(value, path=f"$.include[{index}]")
    if "stream_options" in payload:
        options = payload["stream_options"]
        if not isinstance(options, Mapping):
            _invalid("stream_options must be an object", "$.stream_options")
        _exact_keys(options, {"include_obfuscation"}, path="$.stream_options")
        if "include_obfuscation" in options:
            _require_bool(
                options["include_obfuscation"],
                path="$.stream_options.include_obfuscation",
            )
    if "reasoning" in payload:
        reasoning = payload["reasoning"]
        if not isinstance(reasoning, Mapping):
            _invalid("reasoning must be an object", "$.reasoning")
        _exact_keys(reasoning, {"effort", "summary"}, path="$.reasoning")
        if "effort" in reasoning and reasoning["effort"] not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            _invalid("unsupported reasoning effort", "$.reasoning.effort")
        if "summary" in reasoning and reasoning["summary"] not in {
            "auto",
            "concise",
            "detailed",
        }:
            _invalid("unsupported reasoning summary", "$.reasoning.summary")
    if "text" in payload:
        text = payload["text"]
        if not isinstance(text, Mapping):
            _invalid("text must be an object", "$.text")
        _exact_keys(text, {"format", "verbosity"}, path="$.text")
        if "verbosity" in text and text["verbosity"] not in {
            "low",
            "medium",
            "high",
        }:
            _invalid("unsupported text verbosity", "$.text.verbosity")
        if "format" in text:
            fmt = text["format"]
            if not isinstance(fmt, Mapping):
                _invalid("text.format must be an object", "$.text.format")
            fmt_type = fmt.get("type")
            if fmt_type == "text":
                _exact_keys(fmt, {"type"}, path="$.text.format")
            elif fmt_type == "json_schema":
                _exact_keys(
                    fmt,
                    {"type", "name", "description", "schema", "strict"},
                    path="$.text.format",
                )
                _require_text(fmt.get("name"), path="$.text.format.name")
                if not isinstance(fmt.get("schema"), Mapping):
                    _invalid(
                        "json_schema format requires a schema object",
                        "$.text.format.schema",
                    )
            else:
                _invalid("unsupported text format", "$.text.format.type")
    for name in (
        "conversation",
        "previous_response_id",
        "prompt_cache_key",
        "safety_identifier",
        "service_tier",
        "truncation",
        "user",
    ):
        if name in payload and payload[name] is not None:
            _require_text(payload[name], path=f"$.{name}")
    if "prompt" in payload and payload["prompt"] is not None:
        prompt = payload["prompt"]
        if not isinstance(prompt, Mapping):
            _invalid("prompt must be an object", "$.prompt")
        _exact_keys(prompt, {"id", "version", "variables"}, path="$.prompt")
        _require_text(prompt.get("id"), path="$.prompt.id")
        if "version" in prompt:
            _require_text(prompt["version"], path="$.prompt.version")
        if "variables" in prompt and not isinstance(prompt["variables"], Mapping):
            _invalid("prompt variables must be an object", "$.prompt.variables")
    normalized = copy.deepcopy(dict(payload))
    normalized.setdefault("max_output_tokens", max_output)
    normalized.setdefault("stream", False)
    normalized.setdefault("store", False)
    for name in (
        "conversation",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
    ):
        if normalized.get(name) is None:
            normalized.pop(name, None)
    return normalized


def _validate_function_caller(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ResponsesBackendError()
    caller_type = value.get("type")
    if caller_type == "direct":
        if set(value) != {"type"}:
            raise ResponsesBackendError()
        return
    if caller_type == "program":
        if set(value) != {"type", "caller_id"}:
            raise ResponsesBackendError()
        if not isinstance(value.get("caller_id"), str) or not value["caller_id"]:
            raise ResponsesBackendError()
        return
    raise ResponsesBackendError()


def _validate_output_item(item: Any, *, path: str) -> None:
    if not isinstance(item, Mapping):
        raise ResponsesBackendError()
    item_type = item.get("type")
    if not isinstance(item_type, str):
        raise ResponsesBackendError()
    fields = {
        "message": {"id", "type", "status", "role", "content"},
        "function_call": {
            "id",
            "type",
            "status",
            "arguments",
            "call_id",
            "name",
            "caller",
        },
        "reasoning": {
            "id",
            "type",
            "status",
            "summary",
            "content",
            "encrypted_content",
        },
        "refusal": {"id", "type", "status", "refusal"},
    }.get(item_type)
    if fields is None or set(item) - fields:
        raise ResponsesBackendError()
    if item_type == "message":
        if item.get("role") != "assistant":
            raise ResponsesBackendError()
        content = item.get("content")
        if not isinstance(content, list):
            raise ResponsesBackendError()
        for index, part in enumerate(content):
            try:
                _validate_content_part(part, path=f"{path}.content[{index}]")
            except ResponsesProtocolError as exc:
                raise ResponsesBackendError() from exc
    elif item_type == "function_call":
        for name in ("arguments", "call_id", "name"):
            if not isinstance(item.get(name), str):
                raise ResponsesBackendError()
        if "caller" in item:
            _validate_function_caller(item["caller"])


def _validate_backend_response(
    result: ResponsesBackendResponse,
    *,
    route: RouteDefinition,
    config: ResponsesGatewayConfig,
) -> dict[str, Any]:
    if result.provider_id != route.provider_id or result.model != route.provider_model:
        raise ResponsesBackendError(request_id=result.request_id)
    response = copy.deepcopy(dict(result.response))
    _JsonLimits(config).check(response)
    if set(response) - _RESPONSE_FIELDS:
        raise ResponsesBackendError(request_id=result.request_id)
    required = {"id", "object", "created_at", "status", "model", "output"}
    if not required.issubset(response):
        raise ResponsesBackendError(request_id=result.request_id)
    if response["object"] != "response":
        raise ResponsesBackendError(request_id=result.request_id)
    if response["model"] != route.provider_model:
        raise ResponsesBackendError(request_id=result.request_id)
    if response["status"] not in {
        "completed",
        "incomplete",
        "failed",
        "cancelled",
    }:
        raise ResponsesBackendError(request_id=result.request_id)
    if not isinstance(response["id"], str) or not response["id"]:
        raise ResponsesBackendError(request_id=result.request_id)
    if isinstance(response["created_at"], bool) or not isinstance(
        response["created_at"],
        (int, float),
    ):
        raise ResponsesBackendError(request_id=result.request_id)
    if isinstance(response["created_at"], float) and not math.isfinite(
        response["created_at"]
    ):
        raise ResponsesBackendError(request_id=result.request_id)
    completed_at = response.get("completed_at")
    if completed_at is not None:
        if isinstance(completed_at, bool) or not isinstance(
            completed_at,
            (int, float),
        ):
            raise ResponsesBackendError(request_id=result.request_id)
        if isinstance(completed_at, float) and not math.isfinite(completed_at):
            raise ResponsesBackendError(request_id=result.request_id)
    prompt_cache_key = response.get("prompt_cache_key")
    if prompt_cache_key is not None and not isinstance(prompt_cache_key, str):
        raise ResponsesBackendError(request_id=result.request_id)
    prompt_cache_options = response.get("prompt_cache_options")
    if prompt_cache_options is not None:
        if (
            not isinstance(prompt_cache_options, Mapping)
            or set(prompt_cache_options) != {"mode", "ttl"}
            or prompt_cache_options.get("mode") not in {"implicit", "explicit"}
            or prompt_cache_options.get("ttl") != "30m"
        ):
            raise ResponsesBackendError(request_id=result.request_id)
    if response.get("prompt_cache_retention") not in {
        None,
        "in_memory",
        "24h",
    }:
        raise ResponsesBackendError(request_id=result.request_id)
    output = response["output"]
    if not isinstance(output, list):
        raise ResponsesBackendError(request_id=result.request_id)
    for index, item in enumerate(output):
        try:
            _validate_output_item(item, path=f"$.output[{index}]")
        except ResponsesProtocolError as exc:
            raise ResponsesBackendError(request_id=result.request_id) from exc
    return response


def _observed_output_text(response: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            for name in ("name", "arguments"):
                value = item.get(name)
                if isinstance(value, str):
                    pieces.append(value)
        elif item_type == "message":
            for part in item.get("content", []):
                if not isinstance(part, Mapping):
                    continue
                for name in ("text", "refusal"):
                    value = part.get(name)
                    if isinstance(value, str):
                        pieces.append(value)
        elif item_type == "reasoning":
            for key in ("summary", "content"):
                value = item.get(key)
                if isinstance(value, str):
                    pieces.append(value)
                elif isinstance(value, list):
                    pieces.extend(
                        str(part.get("text", ""))
                        for part in value
                        if isinstance(part, Mapping)
                    )
    return "\n".join(pieces)


def _contains_secret(
    value: Any,
    secret: str | bytes,
    seen: set[int] | None = None,
) -> bool:
    seen = seen or set()
    object_id = id(value)
    if object_id in seen:
        return False
    seen.add(object_id)
    secret_bytes = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    secret_text = (
        secret.decode("utf-8", errors="ignore") if isinstance(secret, bytes) else secret
    )
    if isinstance(value, str):
        return bool(secret_text) and secret_text in value
    if isinstance(value, (bytes, bytearray)):
        return bool(secret_bytes) and secret_bytes in bytes(value)
    if is_dataclass(value):
        return any(
            _contains_secret(getattr(value, field.name), secret, seen)
            for field in value.__dataclass_fields__.values()
        )
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secret, seen) or _contains_secret(item, secret, seen)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return any(_contains_secret(item, secret, seen) for item in value)
    return False


def _error_body(
    *,
    message: str,
    code: str,
    param: str | None = None,
    error_type: str = "invalid_request_error",
) -> bytes:
    return canonical_json_bytes(
        {
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        }
    )


def _gateway_status_for_broker(code: BrokerErrorCode) -> ResponsesGatewayStatus:
    return {
        BrokerErrorCode.UNKNOWN_HANDLE: ResponsesGatewayStatus.UNKNOWN_HANDLE,
        BrokerErrorCode.EXPIRED_HANDLE: ResponsesGatewayStatus.EXPIRED_HANDLE,
        BrokerErrorCode.REVOKED_HANDLE: ResponsesGatewayStatus.REVOKED_HANDLE,
        BrokerErrorCode.REPLAYED_HANDLE: ResponsesGatewayStatus.REPLAYED_HANDLE,
    }.get(code, ResponsesGatewayStatus.POLICY_DENIED)


def _http_for_gateway_status(status: ResponsesGatewayStatus) -> int:
    if status is ResponsesGatewayStatus.SUCCESS:
        return 200
    if status in {
        ResponsesGatewayStatus.UNAUTHORIZED,
        ResponsesGatewayStatus.UNKNOWN_HANDLE,
        ResponsesGatewayStatus.EXPIRED_HANDLE,
        ResponsesGatewayStatus.REVOKED_HANDLE,
        ResponsesGatewayStatus.REPLAYED_HANDLE,
    }:
        return 401
    if status in {
        ResponsesGatewayStatus.POLICY_DENIED,
        ResponsesGatewayStatus.ROUTE_MISMATCH,
    }:
        return 403
    if status is ResponsesGatewayStatus.BUDGET_EXCEEDED:
        return 429
    if status is ResponsesGatewayStatus.CANCELLED:
        return 499
    return 502


def _safe_status_message(status: ResponsesGatewayStatus) -> str:
    return {
        ResponsesGatewayStatus.INVALID_REQUEST: "invalid Responses request",
        ResponsesGatewayStatus.UNAUTHORIZED: "missing or invalid bearer capability",
        ResponsesGatewayStatus.UNKNOWN_HANDLE: "opaque trial handle is unknown",
        ResponsesGatewayStatus.EXPIRED_HANDLE: "opaque trial handle has expired",
        ResponsesGatewayStatus.REVOKED_HANDLE: "opaque trial handle has been revoked",
        ResponsesGatewayStatus.REPLAYED_HANDLE: "completed trial handle cannot be replayed",
        ResponsesGatewayStatus.POLICY_DENIED: "request is denied by trial policy",
        ResponsesGatewayStatus.ROUTE_MISMATCH: "resolved provider route did not match policy",
        ResponsesGatewayStatus.BUDGET_EXCEEDED: "trial model budget exceeded",
        ResponsesGatewayStatus.PROVIDER_FAILURE: "model provider request failed",
        ResponsesGatewayStatus.USAGE_UNDERREPORTED: "provider usage was underreported",
        ResponsesGatewayStatus.CANCELLED: "request was cancelled",
        ResponsesGatewayStatus.SUCCESS: "success",
    }[status]


def _receipt_header(receipt: Mapping[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(canonical_json_bytes(receipt))
        .rstrip(b"=")
        .decode("ascii")
    )


def _sse_event(event: Mapping[str, Any]) -> bytes:
    return b"data: " + _wire_json_bytes(event) + b"\n\n"


class ResponsesGateway:
    """Strict HTTP-compatible Responses router over ``CredentialBroker``."""

    def __init__(
        self,
        *,
        broker: CredentialBroker,
        routes: Iterable[RouteDefinition],
        backends: Mapping[str, ResponsesBackend],
        signer: AttestationSigner,
        trial_id_resolver: Callable[[str], str],
        budget_ledger: ResponsesBudgetLedger | None = None,
        config: ResponsesGatewayConfig = ResponsesGatewayConfig(),
        clock: Callable[[], float] = time.time,
        request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        provider_request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        attestation_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        token_counter: Callable[[str], int] = conservative_token_count,
    ):
        if not callable(trial_id_resolver):
            raise TypeError("trial_id_resolver must be callable")
        self._broker = broker
        self._backends = dict(backends)
        self._signer = signer
        self._trial_id_resolver = trial_id_resolver
        self._ledger = budget_ledger or ResponsesBudgetLedger()
        self._config = config
        self._clock = clock
        self._request_id_factory = request_id_factory
        self._provider_request_id_factory = provider_request_id_factory
        self._attestation_id_factory = attestation_id_factory
        self._token_counter = token_counter
        self._routes: dict[str, RouteDefinition] = {}
        for route in routes:
            if route.public_model in self._routes:
                raise ValueError(f"duplicate public model route {route.public_model!r}")
            if route.provider_id not in self._backends:
                raise ValueError(f"route provider {route.provider_id!r} has no backend")
            self._routes[route.public_model] = route
        self._log_lock = threading.RLock()
        self._logs: deque[ResponsesGatewayLogRecord] = deque(
            maxlen=config.max_log_records
        )

    @property
    def max_request_bytes(self) -> int:
        return self._config.max_request_bytes

    def logs(self) -> tuple[ResponsesGatewayLogRecord, ...]:
        with self._log_lock:
            return tuple(self._logs)

    def receipt_for_request(self, request_id: str) -> Mapping[str, Any] | None:
        with self._log_lock:
            for record in reversed(self._logs):
                if record.request_id == request_id:
                    return copy.deepcopy(dict(record.attestation))
        return None

    def cumulative_usage(
        self,
        handle: str,
    ) -> UsageSummary:
        trial_id = self._resolve_trial(handle)
        authorization = self._broker.authorize(handle, trial_id=trial_id)
        return self._ledger.cumulative(authorization.budget_identity)

    def handle_http(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        cancel_event: threading.Event | None = None,
    ) -> ResponsesHttpResponse:
        try:
            normalized_headers = self._validate_http_envelope(
                method=method,
                path=path,
                headers=headers,
                body=body,
            )
            handle = self._bearer_handle(normalized_headers)
            payload = _validate_response_request(
                _strict_json_loads(body),
                config=self._config,
            )
            prepared = self._prepare(
                handle=handle,
                payload=payload,
                request_bytes=len(body),
            )
        except ResponsesProtocolError as exc:
            error_body = _error_body(
                message=exc.safe_message,
                code=exc.code,
                param=exc.param,
            )
            return ResponsesHttpResponse(
                status_code=exc.status_code,
                headers=self._json_headers()
                + (("Content-Length", str(len(error_body))),),
                body=error_body,
            )
        except BrokerError as exc:
            status = _gateway_status_for_broker(exc.code)
            return self._status_error(status)

        effective_cancel = cancel_event or threading.Event()
        if prepared.streaming:
            return self._stream_response(prepared, effective_cancel)
        return self._non_stream_response(prepared, effective_cancel)

    def _validate_http_envelope(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> dict[str, str]:
        if method.upper() != "POST":
            raise ResponsesProtocolError(
                status_code=405,
                code="method_not_allowed",
                message="only POST is supported",
            )
        parsed = urlsplit(path)
        if parsed.path != "/v1/responses" or parsed.query or parsed.fragment:
            raise ResponsesProtocolError(
                status_code=404,
                code="not_found",
                message="route not found",
            )
        if not isinstance(body, bytes):
            raise TypeError("HTTP body must be bytes")
        if len(body) > self._config.max_request_bytes:
            raise ResponsesProtocolError(
                status_code=413,
                code="request_too_large",
                message="request body exceeds the configured byte limit",
            )
        normalized: dict[str, str] = {}
        header_bytes = 0
        for raw_name, raw_value in headers.items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                raise ResponsesProtocolError(
                    status_code=400,
                    code="invalid_headers",
                    message="HTTP headers must be text",
                )
            name = raw_name.strip().lower()
            value = raw_value.strip()
            if not name or name in normalized:
                raise ResponsesProtocolError(
                    status_code=400,
                    code="invalid_headers",
                    message="duplicate or empty HTTP header",
                )
            if not _HEADER_VALUE_RE.fullmatch(value):
                raise ResponsesProtocolError(
                    status_code=400,
                    code="invalid_headers",
                    message="HTTP header contains unsupported characters",
                )
            header_bytes += len(name.encode()) + len(value.encode()) + 4
            normalized[name] = value
        if header_bytes > self._config.max_header_bytes:
            raise ResponsesProtocolError(
                status_code=431,
                code="headers_too_large",
                message="HTTP headers exceed the configured byte limit",
            )
        content_type = normalized.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ResponsesProtocolError(
                status_code=415,
                code="unsupported_media_type",
                message="Content-Type must be application/json",
            )
        if normalized.get("content-encoding", "identity").lower() != "identity":
            raise ResponsesProtocolError(
                status_code=415,
                code="unsupported_content_encoding",
                message="compressed request bodies are not supported",
            )
        return normalized

    @staticmethod
    def _bearer_handle(headers: Mapping[str, str]) -> str:
        authorization = headers.get("authorization")
        if authorization is None:
            raise ResponsesProtocolError(
                status_code=401,
                code="invalid_api_key",
                message="missing bearer capability",
            )
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            raise ResponsesProtocolError(
                status_code=401,
                code="invalid_api_key",
                message="invalid bearer capability",
            )
        return parts[1]

    def _resolve_trial(self, handle: str) -> str:
        try:
            trial_id = self._trial_id_resolver(handle)
        except Exception:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_HANDLE,
                "opaque trial handle is unknown",
            ) from None
        if not isinstance(trial_id, str) or not trial_id:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_HANDLE,
                "opaque trial handle is unknown",
            )
        return trial_id

    def _prepare(
        self,
        *,
        handle: str,
        payload: dict[str, Any],
        request_bytes: int,
    ) -> _PreparedRequest:
        started_at_ms = int(self._clock() * 1000)
        request_id = self._request_id_factory()
        provider_request_id = self._provider_request_id_factory()
        trial_id = self._resolve_trial(handle)
        authorization = self._broker.authorize(handle, trial_id=trial_id)
        requested_model = payload["model"]
        route = self._routes.get(requested_model)
        if route is None:
            raise BrokerError(
                BrokerErrorCode.POLICY_DENIED,
                "requested model is not allowlisted",
            )
        authorization = self._broker.authorize(
            handle,
            trial_id=trial_id,
            route_id=route.route_id,
        )
        provider_payload = copy.deepcopy(payload)
        provider_payload["model"] = route.provider_model
        safe_digest_payload = copy.deepcopy(payload)
        safe_digest_payload["input_sha256"] = hashlib.sha256(
            _wire_json_bytes(payload["input"])
        ).hexdigest()
        safe_digest_payload.pop("input", None)
        if "instructions" in safe_digest_payload:
            instructions = safe_digest_payload.pop("instructions")
            safe_digest_payload["instructions_sha256"] = hashlib.sha256(
                str(instructions).encode("utf-8")
            ).hexdigest()
        if "tools" in safe_digest_payload:
            tools = safe_digest_payload.pop("tools")
            safe_digest_payload["tools_sha256"] = hashlib.sha256(
                _wire_json_bytes(tools)
            ).hexdigest()
        request_digest = hashlib.sha256(
            _wire_json_bytes(safe_digest_payload)
        ).hexdigest()
        input_tokens = self._count_tokens(
            _wire_json_bytes(provider_payload).decode("utf-8")
        )
        max_output = provider_payload["max_output_tokens"]
        planned = UsageSummary(
            model_calls=1,
            input_tokens=input_tokens,
            output_tokens=max_output,
            total_tokens=input_tokens + max_output,
            cost_microusd=route.minimum_cost(input_tokens, max_output),
        )
        model_receipt = self._sign_model_receipt(
            request_id=request_id,
            request_digest=request_digest,
            authorization=authorization,
            route=route,
            requested_model=requested_model,
            started_at_ms=started_at_ms,
        )
        try:
            reservation = self._ledger.reserve(
                authorization.budget_identity,
                authorization.policy.budget,
                planned,
            )
        except _BudgetExceeded as exc:
            raise ResponsesProtocolError(
                status_code=429,
                code=exc.status.value,
                message="trial model budget exceeded",
            ) from None
        return _PreparedRequest(
            request_id=request_id,
            provider_request_id=provider_request_id,
            handle=handle,
            authorization=authorization,
            route=route,
            public_payload=payload,
            provider_payload=provider_payload,
            request_digest=request_digest,
            request_bytes=request_bytes,
            input_tokens=input_tokens,
            planned=planned,
            reservation=reservation,
            streaming=bool(payload["stream"]),
            started_at_ms=started_at_ms,
            model_receipt=model_receipt,
        )

    def _count_tokens(self, text: str) -> int:
        value = self._token_counter(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token_counter must return a non-negative integer")
        return value

    def _backend_request(
        self,
        prepared: _PreparedRequest,
        cancel_event: threading.Event,
    ) -> ResponsesBackendRequest:
        return ResponsesBackendRequest(
            gateway_request_id=prepared.request_id,
            provider_request_id=prepared.provider_request_id,
            provider_model=prepared.route.provider_model,
            payload=copy.deepcopy(prepared.provider_payload),
            cancel_event=cancel_event,
        )

    def _non_stream_response(
        self,
        prepared: _PreparedRequest,
        cancel_event: threading.Event,
    ) -> ResponsesHttpResponse:
        backend = self._backends[prepared.route.provider_id]
        backend_request = self._backend_request(prepared, cancel_event)
        finalized = False
        provider_started = False

        def invoke(
            credential: str | bytes,
            request: ResponsesBackendRequest,
        ) -> ResponsesBackendResponse:
            nonlocal provider_started
            if cancel_event.is_set():
                raise ResponsesCancelled()
            provider_started = True
            return backend.create(credential, request)

        try:
            if cancel_event.is_set():
                raise ResponsesCancelled()
            result = self._broker.invoke_provider(
                prepared.handle,
                trial_id=prepared.authorization.trial_id,
                route_id=prepared.route.route_id,
                provider_id=prepared.route.provider_id,
                invoker=invoke,
                request=backend_request,
            )
            if cancel_event.is_set():
                raise ResponsesCancelled()
            if not isinstance(result, ResponsesBackendResponse):
                raise ResponsesBackendError()
            response = _validate_backend_response(
                result,
                route=prepared.route,
                config=self._config,
            )
            actual, underreported, output_limit = self._actual_usage(
                prepared,
                result,
                response,
            )
            overage = self._ledger.finalize(
                prepared.reservation,
                prepared.authorization.policy.budget,
                actual,
            )
            finalized = True
            if overage is not None:
                return self._terminal_error(
                    prepared,
                    ResponsesGatewayStatus.BUDGET_EXCEEDED,
                    actual,
                    provider_request_id=result.request_id,
                )
            if output_limit:
                return self._terminal_error(
                    prepared,
                    ResponsesGatewayStatus.PROVIDER_FAILURE,
                    actual,
                    provider_request_id=result.request_id,
                )
            if (
                underreported
                and prepared.authorization.policy.underreport_policy
                is UnderreportPolicy.REJECT
            ):
                return self._terminal_error(
                    prepared,
                    ResponsesGatewayStatus.USAGE_UNDERREPORTED,
                    actual,
                    provider_request_id=result.request_id,
                )
            if response["status"] in {"failed", "cancelled"}:
                return self._terminal_error(
                    prepared,
                    ResponsesGatewayStatus.PROVIDER_FAILURE,
                    actual,
                    provider_request_id=result.request_id,
                )
            public_response = self._public_response(
                response,
                prepared.route.public_model,
                actual,
            )
            body = _wire_json_bytes(public_response)
            if len(body) > self._config.max_response_bytes:
                return self._terminal_error(
                    prepared,
                    ResponsesGatewayStatus.PROVIDER_FAILURE,
                    actual,
                    provider_request_id=result.request_id,
                )
            record = self._finish_log(
                prepared,
                status=ResponsesGatewayStatus.SUCCESS,
                usage=actual,
                provider_request_id=result.request_id,
                streamed_events=0,
            )
            return ResponsesHttpResponse(
                status_code=200,
                headers=self._success_headers(
                    prepared,
                    record.attestation,
                    streaming=False,
                    content_length=len(body),
                ),
                body=body,
            )
        except ResponsesCancelled:
            usage = prepared.planned if provider_started else UsageSummary()
            if provider_started:
                self._cancel_backend(backend, prepared.provider_request_id)
            if not finalized:
                if provider_started:
                    self._ledger.finalize(
                        prepared.reservation,
                        prepared.authorization.policy.budget,
                        usage,
                    )
                else:
                    self._ledger.cancel(prepared.reservation)
            return self._terminal_error(
                prepared,
                ResponsesGatewayStatus.CANCELLED,
                usage,
                provider_request_id=prepared.provider_request_id,
            )
        except BrokerError as exc:
            if not finalized:
                if exc.provider_started:
                    self._ledger.finalize(
                        prepared.reservation,
                        prepared.authorization.policy.budget,
                        prepared.planned,
                    )
                else:
                    self._ledger.cancel(prepared.reservation)
            status = (
                ResponsesGatewayStatus.PROVIDER_FAILURE
                if exc.provider_started
                else _gateway_status_for_broker(exc.code)
            )
            return self._terminal_error(
                prepared,
                status,
                prepared.planned if exc.provider_started else UsageSummary(),
                provider_request_id=prepared.provider_request_id,
            )
        except Exception:
            if not finalized:
                self._ledger.finalize(
                    prepared.reservation,
                    prepared.authorization.policy.budget,
                    prepared.planned,
                )
            return self._terminal_error(
                prepared,
                ResponsesGatewayStatus.PROVIDER_FAILURE,
                prepared.planned,
                provider_request_id=prepared.provider_request_id,
            )

    def _stream_response(
        self,
        prepared: _PreparedRequest,
        cancel_event: threading.Event,
    ) -> ResponsesHttpResponse:
        backend = self._backends[prepared.route.provider_id]
        provider_started = threading.Event()
        events: queue.Queue[bytes | _StreamTerminal] = queue.Queue(
            maxsize=self._config.max_stream_queue_events
        )
        body = ResponsesStreamBody(
            events=events,
            cancel_event=cancel_event,
            cancel_backend=lambda: (
                self._cancel_backend(
                    backend,
                    prepared.provider_request_id,
                )
                if provider_started.is_set()
                else None
            ),
            poll_seconds=self._config.queue_poll_seconds,
        )
        thread = threading.Thread(
            target=self._run_stream_worker,
            args=(
                prepared,
                backend,
                cancel_event,
                provider_started,
                events,
            ),
            name=f"atv-responses-{prepared.request_id[:12]}",
            daemon=True,
        )
        thread.start()
        return ResponsesHttpResponse(
            status_code=200,
            headers=self._success_headers(
                prepared,
                terminal_receipt=None,
                streaming=True,
                content_length=None,
            ),
            body=body,
        )

    def _run_stream_worker(
        self,
        prepared: _PreparedRequest,
        backend: ResponsesBackend,
        cancel_event: threading.Event,
        provider_started: threading.Event,
        events: "queue.Queue[bytes | _StreamTerminal]",
    ) -> None:
        backend_request = self._backend_request(prepared, cancel_event)
        finalized = False
        streamed_events = 0
        result_request_id = prepared.provider_request_id
        observed_stream_output = ""

        def put(
            item: bytes | _StreamTerminal,
            *,
            allow_cancelled: bool = False,
        ) -> None:
            while True:
                if (
                    cancel_event.is_set()
                    and isinstance(item, bytes)
                    and not allow_cancelled
                ):
                    raise ResponsesCancelled()
                try:
                    events.put(item, timeout=self._config.queue_poll_seconds)
                    return
                except queue.Full:
                    if cancel_event.is_set():
                        raise ResponsesCancelled()

        def invoke(
            credential: str | bytes,
            request: ResponsesBackendRequest,
        ) -> ResponsesBackendResponse:
            nonlocal finalized, observed_stream_output
            nonlocal streamed_events, result_request_id
            if cancel_event.is_set():
                raise ResponsesCancelled()
            provider_started.set()
            final_result: ResponsesBackendResponse | None = None
            terminal_seen = False
            for raw_chunk in backend.stream(credential, request):
                if cancel_event.is_set():
                    raise ResponsesCancelled()
                if not isinstance(raw_chunk, ResponsesStreamChunk):
                    raise ResponsesBackendError()
                if _contains_secret(raw_chunk, credential):
                    raise BrokerError(
                        BrokerErrorCode.CREDENTIAL_LEAK,
                        "provider stream contained broker-held credential material",
                        provider_started=True,
                    )
                event = self._validated_stream_event(raw_chunk.event, prepared.route)
                event_type = event["type"]
                if event["sequence_number"] != streamed_events:
                    raise ResponsesBackendError()
                if terminal_seen:
                    raise ResponsesBackendError()
                if event_type in _TERMINAL_STREAM_EVENTS:
                    terminal_seen = True
                    if raw_chunk.final_response is None:
                        raise ResponsesBackendError()
                    final_result = raw_chunk.final_response
                    result_request_id = final_result.request_id
                    response = _validate_backend_response(
                        final_result,
                        route=prepared.route,
                        config=self._config,
                    )
                    actual, underreported, output_limit = self._actual_usage(
                        prepared,
                        final_result,
                        response,
                    )
                    overage = self._ledger.finalize(
                        prepared.reservation,
                        prepared.authorization.policy.budget,
                        actual,
                    )
                    finalized = True
                    terminal_status = ResponsesGatewayStatus.SUCCESS
                    if overage is not None:
                        terminal_status = ResponsesGatewayStatus.BUDGET_EXCEEDED
                    elif output_limit or response["status"] in {"failed", "cancelled"}:
                        terminal_status = ResponsesGatewayStatus.PROVIDER_FAILURE
                    elif (
                        underreported
                        and prepared.authorization.policy.underreport_policy
                        is UnderreportPolicy.REJECT
                    ):
                        terminal_status = ResponsesGatewayStatus.USAGE_UNDERREPORTED
                    if terminal_status is not ResponsesGatewayStatus.SUCCESS:
                        self._finish_log(
                            prepared,
                            status=terminal_status,
                            usage=actual,
                            provider_request_id=final_result.request_id,
                            streamed_events=streamed_events,
                        )
                        put(
                            _sse_event(
                                self._stream_error_event(
                                    terminal_status,
                                    prepared.request_id,
                                    streamed_events,
                                )
                            )
                        )
                        streamed_events += 1
                        return final_result
                    public_response = self._public_response(
                        response,
                        prepared.route.public_model,
                        actual,
                    )
                    event["response"] = public_response
                    encoded = _sse_event(event)
                    if len(encoded) > self._config.max_stream_event_bytes:
                        raise ResponsesBackendError(request_id=final_result.request_id)
                    record = self._finish_log(
                        prepared,
                        status=ResponsesGatewayStatus.SUCCESS,
                        usage=actual,
                        provider_request_id=final_result.request_id,
                        streamed_events=streamed_events + 1,
                    )
                    # The full usage receipt is retained in the bounded trusted log.
                    # Do not add non-OpenAI fields to provider events: Copilot and
                    # other strict clients must receive a wire-compatible stream.
                    del record
                    encoded = _sse_event(event)
                    if len(encoded) > self._config.max_stream_event_bytes:
                        raise ResponsesBackendError(request_id=final_result.request_id)
                    put(encoded)
                    streamed_events += 1
                    return final_result
                if raw_chunk.final_response is not None:
                    raise ResponsesBackendError()
                if event_type in {
                    "response.output_text.delta",
                    "response.refusal.delta",
                    "response.function_call_arguments.delta",
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise ResponsesBackendError()
                    candidate = observed_stream_output + delta
                    if (
                        self._count_tokens(candidate)
                        > prepared.public_payload["max_output_tokens"]
                    ):
                        raise _StreamOutputLimitExceeded()
                    observed_stream_output = candidate
                encoded = _sse_event(event)
                if len(encoded) > self._config.max_stream_event_bytes:
                    raise ResponsesBackendError()
                put(encoded)
                streamed_events += 1
            if cancel_event.is_set():
                raise ResponsesCancelled()
            if not terminal_seen or final_result is None:
                raise ResponsesBackendError()
            return final_result

        try:
            self._broker.invoke_provider(
                prepared.handle,
                trial_id=prepared.authorization.trial_id,
                route_id=prepared.route.route_id,
                provider_id=prepared.route.provider_id,
                invoker=invoke,
                request=backend_request,
            )
            put(_StreamTerminal())
        except _StreamOutputLimitExceeded:
            cancel_event.set()
            self._cancel_backend(backend, prepared.provider_request_id)
            if not finalized:
                self._ledger.finalize(
                    prepared.reservation,
                    prepared.authorization.policy.budget,
                    prepared.planned,
                )
                finalized = True
                self._finish_log(
                    prepared,
                    status=ResponsesGatewayStatus.PROVIDER_FAILURE,
                    usage=prepared.planned,
                    provider_request_id=result_request_id,
                    streamed_events=streamed_events,
                )
            try:
                put(
                    _sse_event(
                        self._stream_error_event(
                            ResponsesGatewayStatus.PROVIDER_FAILURE,
                            prepared.request_id,
                            streamed_events,
                        )
                    ),
                    allow_cancelled=True,
                )
                put(_StreamTerminal(), allow_cancelled=True)
            except ResponsesCancelled:
                pass
        except ResponsesCancelled:
            usage = prepared.planned if provider_started.is_set() else UsageSummary()
            if provider_started.is_set():
                self._cancel_backend(backend, prepared.provider_request_id)
            if not finalized:
                if provider_started.is_set():
                    self._ledger.finalize(
                        prepared.reservation,
                        prepared.authorization.policy.budget,
                        usage,
                    )
                else:
                    self._ledger.cancel(prepared.reservation)
                finalized = True
                self._finish_log(
                    prepared,
                    status=ResponsesGatewayStatus.CANCELLED,
                    usage=usage,
                    provider_request_id=result_request_id,
                    streamed_events=streamed_events,
                )
            try:
                put(_StreamTerminal())
            except ResponsesCancelled:
                pass
        except BrokerError as exc:
            if not finalized:
                if exc.provider_started:
                    usage = prepared.planned
                    self._ledger.finalize(
                        prepared.reservation,
                        prepared.authorization.policy.budget,
                        usage,
                    )
                else:
                    usage = UsageSummary()
                    self._ledger.cancel(prepared.reservation)
                status = (
                    ResponsesGatewayStatus.PROVIDER_FAILURE
                    if exc.provider_started
                    else _gateway_status_for_broker(exc.code)
                )
                self._finish_log(
                    prepared,
                    status=status,
                    usage=usage,
                    provider_request_id=result_request_id,
                    streamed_events=streamed_events,
                )
                try:
                    put(
                        _sse_event(
                            self._stream_error_event(
                                status,
                                prepared.request_id,
                                streamed_events,
                            )
                        )
                    )
                except ResponsesCancelled:
                    pass
            try:
                put(_StreamTerminal())
            except ResponsesCancelled:
                pass
        except Exception:
            if not finalized:
                self._ledger.finalize(
                    prepared.reservation,
                    prepared.authorization.policy.budget,
                    prepared.planned,
                )
                self._finish_log(
                    prepared,
                    status=ResponsesGatewayStatus.PROVIDER_FAILURE,
                    usage=prepared.planned,
                    provider_request_id=result_request_id,
                    streamed_events=streamed_events,
                )
                try:
                    put(
                        _sse_event(
                            self._stream_error_event(
                                ResponsesGatewayStatus.PROVIDER_FAILURE,
                                prepared.request_id,
                                streamed_events,
                            )
                        )
                    )
                except ResponsesCancelled:
                    pass
            try:
                put(_StreamTerminal())
            except ResponsesCancelled:
                pass

    def _validated_stream_event(
        self,
        raw_event: Mapping[str, Any],
        route: RouteDefinition,
    ) -> dict[str, Any]:
        event = copy.deepcopy(dict(raw_event))
        _JsonLimits(self._config).check(event)
        event_type = event.get("type")
        if event_type not in _SUPPORTED_STREAM_EVENTS:
            raise ResponsesBackendError()
        if set(event) - _STREAM_EVENT_FIELDS:
            raise ResponsesBackendError()
        sequence_number = event.get("sequence_number")
        if (
            isinstance(sequence_number, bool)
            or not isinstance(sequence_number, int)
            or sequence_number < 0
        ):
            raise ResponsesBackendError()
        if event_type == "error":
            # Provider error objects can contain internal diagnostics.  Convert
            # them to the gateway's fixed safe error event in the worker.
            raise ResponsesBackendError()
        if "response" in event:
            response = event["response"]
            if not isinstance(response, Mapping):
                raise ResponsesBackendError()
            response = copy.deepcopy(dict(response))
            if set(response) - _RESPONSE_FIELDS:
                raise ResponsesBackendError()
            if response.get("model") not in {None, route.provider_model}:
                raise ResponsesBackendError()
            if "object" in response and response["object"] != "response":
                raise ResponsesBackendError()
            if "output" in response:
                output = response["output"]
                if not isinstance(output, list):
                    raise ResponsesBackendError()
                for index, item in enumerate(output):
                    _validate_output_item(
                        item,
                        path=f"$.response.output[{index}]",
                    )
            response["model"] = route.public_model
            event["response"] = response
        elif event_type.startswith("response.") and event_type in {
            "response.created",
            "response.in_progress",
            "response.completed",
            "response.incomplete",
            "response.failed",
        }:
            raise ResponsesBackendError()
        if event_type in {
            "response.output_item.added",
            "response.output_item.done",
        }:
            _validate_output_item(event.get("item"), path="$.item")
        if event_type in {
            "response.content_part.added",
            "response.content_part.done",
        }:
            try:
                _validate_content_part(event.get("part"), path="$.part")
            except ResponsesProtocolError as exc:
                raise ResponsesBackendError() from exc
        text_field = {
            "response.output_text.delta": "delta",
            "response.output_text.done": "text",
            "response.refusal.delta": "delta",
            "response.refusal.done": "refusal",
            "response.function_call_arguments.delta": "delta",
            "response.function_call_arguments.done": "arguments",
            "response.reasoning_summary_text.delta": "delta",
            "response.reasoning_summary_text.done": "text",
            "response.reasoning_text.delta": "delta",
            "response.reasoning_text.done": "text",
        }.get(event_type)
        if text_field is not None and not isinstance(event.get(text_field), str):
            raise ResponsesBackendError()
        return event

    def _actual_usage(
        self,
        prepared: _PreparedRequest,
        result: ResponsesBackendResponse,
        response: Mapping[str, Any],
    ) -> tuple[UsageSummary, bool, bool]:
        observed_output = self._count_tokens(_observed_output_text(response))
        reported = result.usage
        minimum_cost = prepared.route.minimum_cost(
            prepared.input_tokens,
            observed_output,
        )
        underreported = (
            reported.input_tokens < prepared.input_tokens
            or reported.output_tokens < observed_output
            or reported.cost_microusd < minimum_cost
        )
        actual_input = max(prepared.input_tokens, reported.input_tokens)
        actual_output = max(observed_output, reported.output_tokens)
        actual = UsageSummary(
            model_calls=1,
            input_tokens=actual_input,
            output_tokens=actual_output,
            total_tokens=actual_input + actual_output,
            cost_microusd=max(
                reported.cost_microusd,
                prepared.route.minimum_cost(actual_input, actual_output),
            ),
        )
        output_limit = actual_output > prepared.public_payload["max_output_tokens"]
        return actual, underreported, output_limit

    @staticmethod
    def _public_response(
        response: Mapping[str, Any],
        public_model: str,
        usage: UsageSummary,
    ) -> dict[str, Any]:
        public = copy.deepcopy(dict(response))
        public["model"] = public_model
        public["usage"] = {
            "input_tokens": usage.input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": usage.output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": usage.total_tokens,
        }
        return public

    def _sign_model_receipt(
        self,
        *,
        request_id: str,
        request_digest: str,
        authorization: Authorization,
        route: RouteDefinition,
        requested_model: str,
        started_at_ms: int,
    ) -> Mapping[str, Any]:
        return self._signer.sign(
            {
                "schema": "atv.responses-model-resolution/v1",
                "attestation_id": self._attestation_id_factory(),
                "gateway_request_id": request_id,
                "request_digest": request_digest,
                "trial_id": authorization.trial_id,
                "attempt_id": authorization.attempt_id,
                "requested_model": requested_model,
                "resolved_route": route.public_dict(),
                "trial_policy_digest": authorization.policy_digest,
                "budget_identity": authorization.budget_identity.to_dict(),
                "handle_issuance": authorization.issuance.to_dict(),
                "started_at_ms": started_at_ms,
                "trust_assumptions": self._signer.trust_assumptions.to_dict(),
            }
        ).to_dict()

    def _finish_log(
        self,
        prepared: _PreparedRequest,
        *,
        status: ResponsesGatewayStatus,
        usage: UsageSummary,
        provider_request_id: str,
        streamed_events: int,
    ) -> ResponsesGatewayLogRecord:
        completed_at_ms = int(self._clock() * 1000)
        cumulative, reserved = self._ledger.state(
            prepared.authorization.budget_identity
        )
        attestation = self._signer.sign(
            {
                "schema": "atv.responses-route-usage/v1",
                "attestation_id": self._attestation_id_factory(),
                "gateway_request_id": prepared.request_id,
                "provider_request_id": provider_request_id,
                "request_digest": prepared.request_digest,
                "trial_id": prepared.authorization.trial_id,
                "attempt_id": prepared.authorization.attempt_id,
                "status": status.value,
                "streaming": prepared.streaming,
                "requested_model": prepared.route.public_model,
                "resolved_route": prepared.route.public_dict(),
                "usage": usage.to_dict(),
                "cumulative_usage": cumulative.to_dict(),
                "in_flight_reserved_usage": reserved.to_dict(),
                "budget": asdict(prepared.authorization.policy.budget),
                "underreport_policy": (
                    prepared.authorization.policy.underreport_policy.value
                ),
                "trial_policy_digest": prepared.authorization.policy_digest,
                "budget_identity": (prepared.authorization.budget_identity.to_dict()),
                "handle_issuance": prepared.authorization.issuance.to_dict(),
                "started_at_ms": prepared.started_at_ms,
                "completed_at_ms": completed_at_ms,
                "trust_assumptions": self._signer.trust_assumptions.to_dict(),
            }
        ).to_dict()
        record = ResponsesGatewayLogRecord(
            request_id=prepared.request_id,
            trial_id=prepared.authorization.trial_id,
            attempt_id=prepared.authorization.attempt_id,
            request_digest=prepared.request_digest,
            requested_model=prepared.route.public_model,
            route_id=prepared.route.route_id,
            resolved_provider_model=prepared.route.provider_model,
            provider_request_id=provider_request_id,
            status=status,
            streaming=prepared.streaming,
            request_bytes=prepared.request_bytes,
            streamed_events=streamed_events,
            usage=usage,
            started_at_ms=prepared.started_at_ms,
            completed_at_ms=completed_at_ms,
            attestation=attestation,
        )
        with self._log_lock:
            self._logs.append(record)
        return record

    def _terminal_error(
        self,
        prepared: _PreparedRequest,
        status: ResponsesGatewayStatus,
        usage: UsageSummary,
        *,
        provider_request_id: str,
    ) -> ResponsesHttpResponse:
        record = self._finish_log(
            prepared,
            status=status,
            usage=usage,
            provider_request_id=provider_request_id,
            streamed_events=0,
        )
        body = _error_body(
            message=_safe_status_message(status),
            code=status.value,
            error_type="api_error",
        )
        return ResponsesHttpResponse(
            status_code=_http_for_gateway_status(status),
            headers=self._success_headers(
                prepared,
                record.attestation,
                streaming=False,
                content_length=len(body),
            ),
            body=body,
        )

    @staticmethod
    def _stream_error_event(
        status: ResponsesGatewayStatus,
        request_id: str,
        sequence_number: int,
    ) -> dict[str, Any]:
        return {
            "type": "error",
            "sequence_number": sequence_number,
            "code": status.value,
            "message": _safe_status_message(status),
            "param": None,
            "response": {"id": request_id},
        }

    def _success_headers(
        self,
        prepared: _PreparedRequest,
        terminal_receipt: Mapping[str, Any] | None,
        *,
        streaming: bool,
        content_length: int | None,
    ) -> tuple[tuple[str, str], ...]:
        headers = [
            (
                "Content-Type",
                "text/event-stream; charset=utf-8"
                if streaming
                else "application/json; charset=utf-8",
            ),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-ATV-Gateway-Request-ID", prepared.request_id),
            ("X-ATV-Requested-Model", prepared.route.public_model),
            ("X-ATV-Resolved-Route", prepared.route.route_id),
            ("X-ATV-Resolved-Model", prepared.route.provider_model),
            ("X-ATV-Model-Receipt", _receipt_header(prepared.model_receipt)),
        ]
        if terminal_receipt is not None:
            headers.append(("X-ATV-Usage-Receipt", _receipt_header(terminal_receipt)))
        if content_length is not None:
            headers.append(("Content-Length", str(content_length)))
        return tuple(headers)

    @staticmethod
    def _json_headers() -> tuple[tuple[str, str], ...]:
        return (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        )

    def _status_error(
        self,
        status: ResponsesGatewayStatus,
    ) -> ResponsesHttpResponse:
        body = _error_body(
            message=_safe_status_message(status),
            code=status.value,
            error_type="authentication_error",
        )
        return ResponsesHttpResponse(
            status_code=_http_for_gateway_status(status),
            headers=self._json_headers() + (("Content-Length", str(len(body))),),
            body=body,
        )

    @staticmethod
    def _cancel_backend(
        backend: ResponsesBackend,
        provider_request_id: str,
    ) -> None:
        try:
            backend.cancel(provider_request_id)
        except Exception:
            pass


class ResponsesHttpServer:
    """Small stdlib HTTP/1.1 host for ``ResponsesGateway``.

    It intentionally binds to loopback by default.  Production TLS, workload
    identity, rate limiting, and multi-process coordination belong in the
    deployment front proxy/operator rather than this protocol module.
    """

    def __init__(
        self,
        gateway: ResponsesGateway,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.gateway = gateway
        self.host = host
        self.requested_port = port
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        gateway = self.gateway

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                raw_lengths = self.headers.get_all("Content-Length", failobj=[])
                if len(raw_lengths) != 1:
                    self._write_simple_error(
                        411,
                        "length_required",
                        "exactly one Content-Length header is required",
                    )
                    return
                if self.headers.get("Transfer-Encoding") is not None:
                    self._write_simple_error(
                        415,
                        "unsupported_transfer_encoding",
                        "chunked request bodies are not supported",
                    )
                    return
                try:
                    length = int(raw_lengths[0])
                except (TypeError, ValueError):
                    self._write_simple_error(
                        400,
                        "invalid_content_length",
                        "Content-Length must be an integer",
                    )
                    return
                if length < 0 or length > gateway.max_request_bytes:
                    self._write_simple_error(
                        413,
                        "request_too_large",
                        "request body exceeds the configured byte limit",
                    )
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self._write_simple_error(
                        400,
                        "incomplete_request",
                        "request body ended before Content-Length bytes arrived",
                    )
                    return
                headers: dict[str, str] = {}
                for name in self.headers:
                    values = self.headers.get_all(name, failobj=[])
                    if len(values) != 1:
                        self._write_simple_error(
                            400,
                            "invalid_headers",
                            "duplicate HTTP headers are not allowed",
                        )
                        return
                    headers[name] = values[0]
                response = gateway.handle_http(
                    method="POST",
                    path=self.path,
                    headers=headers,
                    body=body,
                )
                self.send_response(response.status_code)
                for name, value in response.headers:
                    self.send_header(name, value)
                if response.streaming:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if isinstance(response.body, bytes):
                    self.wfile.write(response.body)
                    self.wfile.flush()
                    return
                try:
                    for chunk in response.body:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    response.body.close()
                finally:
                    response.body.close()

            def _write_simple_error(
                self,
                status_code: int,
                code: str,
                message: str,
            ) -> None:
                body = _error_body(message=message, code=code)
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

        return Handler

    def start(self) -> None:
        if self._httpd is not None:
            raise RuntimeError("ResponsesHttpServer is already started")
        self._httpd = http.server.ThreadingHTTPServer(
            (self.host, self.requested_port),
            self._handler(),
        )
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="atv-responses-http",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("ResponsesHttpServer is not started")
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
