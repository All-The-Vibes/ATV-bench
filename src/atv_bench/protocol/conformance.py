"""Black-box conformance checks over a complete normalized protocol transcript."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .canonical import canonical_digest, canonical_jsonl, sha256_bytes
from .capabilities import negotiate_capabilities
from .errors import (
    IntegrityError,
    ProtocolAuthorityError,
    ProtocolLimitError,
    ProtocolStateError,
)
from .jsonl import MergedTranscriptVerifier, ProtocolLimits
from .schemas import SchemaKind, SchemaStore, default_schema_store
from .types import HarnessStatus, ProtocolTranscript

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


@dataclass(frozen=True)
class ConformanceReport:
    protocol_version: int
    trial_id: str
    attempt_id: str
    event_count: int
    status: HarnessStatus
    canonical_transcript_sha256: str


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _limits_from_request(
    request: Mapping[str, Any],
    hard_limits: ProtocolLimits | None,
) -> ProtocolLimits:
    requested = ProtocolLimits(**request["protocol_limits"])
    if hard_limits is None:
        return requested
    for field in (
        "max_line_bytes",
        "max_total_bytes",
        "max_events",
        "max_depth",
        "max_nodes",
        "max_object_properties",
    ):
        if getattr(requested, field) > getattr(hard_limits, field):
            raise ProtocolLimitError(
                f"requested {field} exceeds runner hard ceiling"
            )
    return requested


def _ensure_usage_within_budget(
    usage: Mapping[str, Any], budget: Mapping[str, Any]
) -> None:
    for field in _USAGE_FIELDS:
        observed = usage[field]
        if observed is not None and observed > budget[field]:
            raise ProtocolStateError(
                f"reported usage exceeds effective budget for {field!r}"
            )


def _ensure_token_totals(
    usage: Mapping[str, Any],
    *,
    input_field: str,
    output_field: str,
    total_field: str,
) -> None:
    input_tokens = usage[input_field]
    output_tokens = usage[output_field]
    total_tokens = usage[total_field]
    if (
        input_tokens is not None
        and output_tokens is not None
        and total_tokens is not None
        and total_tokens != input_tokens + output_tokens
    ):
        raise ProtocolStateError(
            f"{total_field} must equal {input_field} + {output_field}"
        )


def validate_conformance(
    authority_source: ProtocolTranscript | Any,
    harness_manifest: Mapping[str, Any],
    trial_request: Mapping[str, Any],
    *,
    store: SchemaStore | None = None,
    limits: ProtocolLimits | None = None,
) -> tuple[ProtocolTranscript, ConformanceReport]:
    """Validate an authority-verified session transcript.

    Raw or merged stdout bytes are intentionally rejected. Use ``ProtocolSession`` to
    create trusted controller evidence. ``verify_merged_transcript`` is available for
    integrity-only diagnostics and never grants authority.
    """
    if isinstance(authority_source, (bytes, bytearray, memoryview)):
        raise ProtocolAuthorityError(
            "raw JSONL bytes cannot prove controller event authority; "
            "use ProtocolSession"
        )
    if isinstance(authority_source, ProtocolTranscript):
        transcript = authority_source
    elif hasattr(authority_source, "finish"):
        transcript = authority_source.finish()
        if not isinstance(transcript, ProtocolTranscript):
            raise ProtocolAuthorityError(
                "session finish() did not return a ProtocolTranscript"
            )
    else:
        raise ProtocolAuthorityError(
            "conformance requires ProtocolSession or ProtocolTranscript"
        )
    from .session import has_session_authority

    if not transcript.authority_verified or not has_session_authority(transcript):
        raise ProtocolAuthorityError(
            "merged transcript has integrity evidence only, not controller authority"
        )

    active_store = store or default_schema_store()
    active_store.validate(harness_manifest, SchemaKind.HARNESS)
    active_store.validate(trial_request, SchemaKind.TRIAL_REQUEST)
    issued_at = _parse_timestamp(trial_request["issued_at"])
    expires_at = _parse_timestamp(trial_request["expires_at"])
    if expires_at <= issued_at:
        raise ProtocolStateError("trial request expires_at must follow issued_at")
    prompt_bytes = trial_request["prompt"]["text"].encode("utf-8", errors="strict")
    expected_prompt_digest = {
        "algorithm": "sha256",
        "value": sha256_bytes(prompt_bytes),
    }
    if trial_request["prompt"]["digest"] != expected_prompt_digest:
        raise IntegrityError("trial request prompt digest does not match exact UTF-8 bytes")
    allowed_tools = set(trial_request["policy"]["tools"]["allowed"])
    denied_tools = set(trial_request["policy"]["tools"]["denied"])
    overlap = allowed_tools & denied_tools
    if overlap:
        raise ProtocolStateError(
            "tool policy both allows and denies: " + ", ".join(sorted(overlap))
        )
    output_contract = trial_request["output"]
    if not output_contract.get("allow_any_relative_path", False):
        missing_allowance = set(output_contract["required_paths"]) - set(
            output_contract["allowed_paths"]
        )
        if missing_allowance:
            raise ProtocolStateError(
                "required artifact paths are not allowed: "
                + ", ".join(sorted(missing_allowance))
            )

    request_digest = canonical_digest(trial_request)
    effective_limits = _limits_from_request(trial_request, limits)
    integrity_copy = MergedTranscriptVerifier(
        store=active_store,
        limits=effective_limits,
        expected_trial_id=trial_request["trial_id"],
        expected_attempt_id=trial_request["attempt_id"],
        expected_request_digest=request_digest,
    ).parse_bytes(canonical_jsonl(transcript.events))
    if tuple(integrity_copy.events) != tuple(transcript.events):
        raise ProtocolStateError(
            "authority transcript changed during merged integrity verification"
        )
    negotiation = negotiate_capabilities(
        harness_manifest,
        trial_request,
        transcript.hello,
        store=active_store,
    )

    accepted = transcript.accepted
    if accepted["selected_protocol_version"] != negotiation.version:
        raise ProtocolStateError("accepted protocol version differs from negotiation")
    if accepted["capabilities"] != dict(negotiation.capabilities):
        raise ProtocolStateError("accepted capabilities differ from negotiation")
    if accepted["effective_budget_limits"] != trial_request["budget_limits"]:
        raise ProtocolStateError("accepted effective budget differs from the request")
    if accepted["effective_protocol_limits"] != trial_request["protocol_limits"]:
        raise ProtocolStateError("accepted protocol limits differ from the request")
    if accepted["policy_digest"] != canonical_digest(trial_request["policy"]):
        raise ProtocolStateError("accepted policy digest differs from the request")

    result = transcript.result
    recorded_times = [_parse_timestamp(event["recorded_at"]) for event in transcript.events]
    if recorded_times != sorted(recorded_times):
        raise ProtocolStateError("controller recorded_at timestamps must be monotonic")

    event_artifacts = [
        event["artifact"]
        for event in transcript.events
        if event["type"] == "artifact"
    ]
    event_paths = [artifact["path"] for artifact in event_artifacts]
    if len(event_paths) != len(set(event_paths)):
        raise ProtocolStateError("artifact event paths must be unique")
    result_paths = [artifact["path"] for artifact in result["artifacts"]]
    if len(result_paths) != len(set(result_paths)):
        raise ProtocolStateError("terminal result artifact paths must be unique")
    if sorted(event_artifacts, key=lambda item: item["path"]) != sorted(
        result["artifacts"], key=lambda item: item["path"]
    ):
        raise ProtocolStateError(
            "terminal result artifacts do not exactly match artifact events"
        )
    allowed_paths = set(output_contract["allowed_paths"])
    allowed_media_types = set(output_contract["allowed_media_types"])
    for artifact in event_artifacts:
        if (
            not output_contract.get("allow_any_relative_path", False)
            and artifact["path"] not in allowed_paths
        ):
            raise ProtocolStateError(
                f"artifact path is outside the output contract: {artifact['path']!r}"
            )
        if artifact["media_type"] not in allowed_media_types:
            raise ProtocolStateError(
                "artifact media type is outside the output contract: "
                f"{artifact['media_type']!r}"
            )
    if len(event_artifacts) > output_contract["max_files"]:
        raise ProtocolStateError("artifact count exceeds the output contract")
    if sum(item["size_bytes"] for item in event_artifacts) > output_contract["max_total_bytes"]:
        raise ProtocolStateError("artifact bytes exceed the output contract")
    if result["status"] == "completed":
        missing = set(output_contract["required_paths"]) - set(event_paths)
        if missing:
            raise ProtocolStateError(
                "completed result is missing required artifacts: "
                + ", ".join(sorted(missing))
            )

    usage_events = [
        event["cumulative_reported"]
        for event in transcript.events
        if event["type"] == "usage"
    ]
    if usage_events and usage_events[-1] != result["reported_usage"]:
        raise ProtocolStateError(
            "terminal result usage must equal the final cumulative usage event"
        )
    _ensure_usage_within_budget(
        result["reported_usage"], accepted["effective_budget_limits"]
    )
    _ensure_token_totals(
        result["reported_usage"],
        input_field="model_input_tokens",
        output_field="model_output_tokens",
        total_field="model_total_tokens",
    )

    capabilities = accepted["capabilities"]
    event_requirements = {
        "model_call": "model_events",
        "tool_call": "tool_events",
        "usage": "usage_events",
        "checkpoint": "checkpoint_events",
    }
    for event in transcript.events:
        required_capability = event_requirements.get(event["type"])
        if required_capability and not capabilities[required_capability]:
            raise ProtocolStateError(
                f"event {event['type']!r} was not negotiated"
            )
        if event["type"] == "checkpoint":
            if not capabilities["resumable"] or not event["resumable"]:
                raise ProtocolStateError(
                    "checkpoint event requires negotiated resumability"
                )
        elif event["type"] == "model_call":
            allowed_models = set(trial_request["model_policy"]["allowed_models"])
            if event["requested_model"] not in allowed_models:
                raise ProtocolStateError(
                    f"model call requested disallowed model {event['requested_model']!r}"
                )
            resolved_model = event["resolved_model"]
            if resolved_model is not None and resolved_model not in allowed_models:
                raise ProtocolStateError(
                    f"model call resolved to disallowed model {resolved_model!r}"
                )
            _ensure_token_totals(
                event["usage_delta"],
                input_field="input_tokens",
                output_field="output_tokens",
                total_field="total_tokens",
            )
        elif event["type"] == "tool_call":
            tool = event["tool"]
            decision = event["policy_decision"]
            if tool in denied_tools and decision != "denied":
                raise ProtocolStateError(
                    f"denied tool {tool!r} was not reported as denied"
                )
            if decision == "allowed" and tool not in allowed_tools:
                raise ProtocolStateError(
                    f"undeclared tool {tool!r} was reported as allowed"
                )

    canonical_stream = canonical_jsonl(transcript.events)
    report = ConformanceReport(
        protocol_version=negotiation.version,
        trial_id=trial_request["trial_id"],
        attempt_id=trial_request["attempt_id"],
        event_count=len(transcript.events),
        status=HarnessStatus(result["status"]),
        canonical_transcript_sha256=sha256_bytes(canonical_stream),
    )
    return transcript, report


def verify_merged_transcript(
    data: bytes,
    trial_request: Mapping[str, Any],
    *,
    store: SchemaStore | None = None,
    limits: ProtocolLimits | None = None,
) -> ProtocolTranscript:
    """Verify merged transcript schema/state without making any authority claim."""
    active_store = store or default_schema_store()
    active_store.validate(trial_request, SchemaKind.TRIAL_REQUEST)
    effective_limits = _limits_from_request(trial_request, limits)
    return MergedTranscriptVerifier(
        store=active_store,
        limits=effective_limits,
        expected_trial_id=trial_request["trial_id"],
        expected_attempt_id=trial_request["attempt_id"],
        expected_request_digest=canonical_digest(trial_request),
    ).parse_bytes(data)


def verify_bundle_manifest(
    bundle: Mapping[str, Any], *, store: SchemaStore | None = None
) -> None:
    """Validate a bundle manifest and bind its contents object to contents_digest."""
    active_store = store or default_schema_store()
    active_store.validate(bundle, SchemaKind.BUNDLE)
    observed = canonical_digest(bundle["contents"])
    if observed != bundle["contents_digest"]:
        raise IntegrityError(
            "bundle contents_digest does not match canonical bundle contents"
        )
