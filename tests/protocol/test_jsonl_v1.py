from __future__ import annotations

import io
import json
from copy import deepcopy

import pytest

from atv_bench.protocol import (
    HarnessStatus,
    IntegrityError,
    JsonlProtocolParser,
    ProtocolDecodeError,
    ProtocolLimitError,
    ProtocolLimits,
    ProtocolSession,
    ProtocolStateError,
    SchemaValidationError,
    canonical_digest,
    canonical_jsonl,
    validate_conformance,
    verify_artifact_event,
)


def _renumber(events):
    for index, event in enumerate(events):
        event["sequence"] = index
    return events


def _session_from_documents(documents):
    session = ProtocolSession(documents["harness"], documents["request"])
    harness_sequence = 0
    accepted_recorded_at = next(
        event["recorded_at"]
        for event in documents["events"]
        if event["type"] == "accepted"
    )
    for canonical_event in documents["events"]:
        if canonical_event["source"] != "harness":
            continue
        raw_event = deepcopy(canonical_event)
        raw_event["schema"] = "atv.harness-event/v1"
        raw_event["harness_sequence"] = harness_sequence
        raw_event.pop("source")
        raw_event.pop("sequence")
        recorded_at = raw_event.pop("recorded_at")
        session.receive_harness_event(raw_event, recorded_at=recorded_at)
        harness_sequence += 1
        if canonical_event["type"] == "hello":
            session.record_controller_accept(recorded_at=accepted_recorded_at)
    return session


def test_valid_transcript_and_conformance_report(protocol_documents):
    documents = protocol_documents
    transcript, report = validate_conformance(
        _session_from_documents(documents),
        documents["harness"],
        documents["request"],
    )
    assert transcript.status is HarnessStatus.COMPLETED
    assert report.status is HarnessStatus.COMPLETED
    assert report.event_count == 6
    assert len(report.canonical_transcript_sha256) == 64


def test_crlf_and_complete_final_line_without_lf_are_accepted(protocol_documents):
    data = canonical_jsonl(protocol_documents["events"]).replace(b"\n", b"\r\n")
    parsed = JsonlProtocolParser().parse_bytes(data.rstrip(b"\r\n"))
    assert parsed.status is HarnessStatus.COMPLETED


@pytest.mark.parametrize(
    ("mutator", "error_type"),
    [
        (lambda data: b"\xef\xbb\xbf" + data, ProtocolDecodeError),
        (lambda data: data.replace(b"\n", b"\n\n", 1), ProtocolDecodeError),
        (lambda data: data.replace(b"\n", b"\n   \n", 1), ProtocolDecodeError),
        (lambda data: b"human log line\n" + data, ProtocolDecodeError),
        (lambda data: data[:-5], ProtocolDecodeError),
        (lambda data: data + b"\xff\n", ProtocolDecodeError),
        (lambda data: data.replace(b"\n", b"\x00\n", 1), ProtocolDecodeError),
    ],
)
def test_non_protocol_or_invalid_utf8_output_fails(
    protocol_documents, mutator, error_type
):
    data = mutator(canonical_jsonl(protocol_documents["events"]))
    with pytest.raises(error_type):
        JsonlProtocolParser().parse_bytes(data)


def test_duplicate_json_key_and_float_are_rejected(protocol_documents):
    events = protocol_documents["events"]
    first = canonical_jsonl([events[0]]).rstrip(b"\n")
    duplicate = first[:-1] + b',"type":"hello"}\n'
    with pytest.raises(ProtocolDecodeError, match="duplicate"):
        JsonlProtocolParser().parse_bytes(duplicate + canonical_jsonl(events[1:]))

    floated = first[:-1] + b',"extra":1.5}\n'
    with pytest.raises(ProtocolDecodeError, match="floating-point"):
        JsonlProtocolParser().parse_bytes(floated + canonical_jsonl(events[1:]))


def test_event_ordering_is_strict(protocol_documents):
    events = deepcopy(protocol_documents["events"])
    events[0] = deepcopy(events[2])
    _renumber(events)
    with pytest.raises(ProtocolStateError, match="first event"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))

    events = deepcopy(protocol_documents["events"])
    events[1] = deepcopy(events[2])
    _renumber(events)
    with pytest.raises(ProtocolStateError, match="second event"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))

    with pytest.raises(ProtocolStateError, match="EOF"):
        JsonlProtocolParser().parse_bytes(
            canonical_jsonl(deepcopy(protocol_documents["events"][:-1]))
        )


def test_duplicate_result_and_post_terminal_event_fail(protocol_documents):
    base = deepcopy(protocol_documents["events"])
    duplicate = deepcopy(base[-1])
    duplicate["sequence"] = len(base)
    with pytest.raises(ProtocolStateError, match="after terminal"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(base + [duplicate]))

    status = deepcopy(base[2])
    status["sequence"] = len(base)
    with pytest.raises(ProtocolStateError, match="after terminal"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(base + [status]))


def test_unknown_event_type_source_or_field_fails_schema(protocol_documents):
    events = deepcopy(protocol_documents["events"])
    events[2]["type"] = "freeform"
    with pytest.raises(SchemaValidationError):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))

    events = deepcopy(protocol_documents["events"])
    events[1]["source"] = "harness"
    with pytest.raises(SchemaValidationError):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))

    events = deepcopy(protocol_documents["events"])
    events[0]["secret"] = "not-allowed"
    with pytest.raises(SchemaValidationError):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))


def test_sequence_and_identity_must_remain_stable(protocol_documents):
    events = deepcopy(protocol_documents["events"])
    events[2]["sequence"] = 99
    with pytest.raises(ProtocolStateError, match="sequence"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))

    events = deepcopy(protocol_documents["events"])
    events[2]["trial_id"] = "different-trial"
    with pytest.raises(ProtocolStateError, match="trial_id changed"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))


def test_accepted_request_digest_is_bound(protocol_documents):
    events = deepcopy(protocol_documents["events"])
    events[1]["request_digest"]["value"] = "0" * 64
    parser = JsonlProtocolParser(
        expected_request_digest=canonical_digest(protocol_documents["request"])
    )
    with pytest.raises(ProtocolStateError, match="request_digest"):
        parser.parse_bytes(canonical_jsonl(events))


def test_cumulative_usage_cannot_decrease_or_become_unknown(protocol_documents):
    events = deepcopy(protocol_documents["events"])
    prior = deepcopy(events[3])
    prior["cumulative_reported"]["model_total_tokens"] += 1
    events.insert(3, prior)
    _renumber(events)
    with pytest.raises(ProtocolStateError, match="decreases"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))

    events = deepcopy(protocol_documents["events"])
    prior = deepcopy(events[3])
    events.insert(3, prior)
    events[4]["cumulative_reported"]["model_total_tokens"] = None
    events[-1]["harness_result"]["reported_usage"]["model_total_tokens"] = None
    _renumber(events)
    with pytest.raises(ProtocolStateError, match="decreases"):
        JsonlProtocolParser().parse_bytes(canonical_jsonl(events))


def test_protocol_limits_cover_total_line_event_depth_nodes_and_properties(
    protocol_documents,
):
    data = canonical_jsonl(protocol_documents["events"])
    longest = max(len(line) for line in data.splitlines())

    with pytest.raises(ProtocolLimitError, match="output exceeds"):
        JsonlProtocolParser(
            limits=ProtocolLimits(
                max_total_bytes=len(data) - 1,
                max_line_bytes=min(longest, len(data) - 1),
            )
        ).parse_bytes(data)

    with pytest.raises(ProtocolLimitError, match="line exceeds"):
        JsonlProtocolParser(
            limits=ProtocolLimits(
                max_total_bytes=len(data),
                max_line_bytes=longest - 1,
            )
        ).parse_bytes(data)

    with pytest.raises(ProtocolLimitError, match="event count"):
        JsonlProtocolParser(
            limits=ProtocolLimits(
                max_total_bytes=len(data),
                max_line_bytes=longest,
                max_events=2,
            )
        ).parse_bytes(data)

    nested = b'{"x":' + (b"[" * 8) + b"0" + (b"]" * 8) + b"}\n"
    with pytest.raises(ProtocolLimitError, match="nesting"):
        JsonlProtocolParser(
            limits=ProtocolLimits(
                max_total_bytes=1024,
                max_line_bytes=1024,
                max_depth=4,
            )
        ).parse_bytes(nested)

    nodes = b'{"x":[' + b",".join([b"0"] * 20) + b"]}\n"
    with pytest.raises(ProtocolLimitError, match="node count"):
        JsonlProtocolParser(
            limits=ProtocolLimits(
                max_total_bytes=1024,
                max_line_bytes=1024,
                max_nodes=10,
            )
        ).parse_bytes(nodes)

    properties = json.dumps({str(i): i for i in range(20)}).encode() + b"\n"
    with pytest.raises(ProtocolLimitError, match="property count"):
        JsonlProtocolParser(
            limits=ProtocolLimits(
                max_total_bytes=2048,
                max_line_bytes=2048,
                max_object_properties=10,
            )
        ).parse_bytes(properties)


def test_binary_stream_is_line_and_total_bounded(protocol_documents):
    data = canonical_jsonl(protocol_documents["events"])
    assert (
        JsonlProtocolParser().parse_stream(io.BytesIO(data)).status
        is HarnessStatus.COMPLETED
    )
    with pytest.raises(ProtocolDecodeError, match="binary mode"):
        JsonlProtocolParser().parse_stream(io.StringIO(data.decode("utf-8")))
    with pytest.raises(ProtocolLimitError, match="line exceeds"):
        JsonlProtocolParser(
            limits=ProtocolLimits(max_line_bytes=10, max_total_bytes=len(data))
        ).parse_stream(io.BytesIO(data))


def test_artifact_size_and_digest_verification(protocol_documents):
    artifact_event = protocol_documents["events"][4]
    content = protocol_documents["artifact_bytes"]
    verify_artifact_event(artifact_event, content)
    with pytest.raises(ProtocolStateError, match="size mismatch"):
        verify_artifact_event(artifact_event, content + b"x")
    bad = deepcopy(artifact_event)
    bad["artifact"]["digest"]["value"] = "0" * 64
    with pytest.raises(IntegrityError):
        verify_artifact_event(bad, content)


def test_conformance_rejects_summary_output_contract_and_budget_drift(
    protocol_documents,
):
    documents = deepcopy(protocol_documents)
    documents["events"][-1]["harness_result"]["artifacts"] = []
    with pytest.raises(ProtocolStateError, match="artifacts"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )

    documents = deepcopy(protocol_documents)
    documents["events"][4]["artifact"]["path"] = "undeclared.py"
    documents["events"][-1]["harness_result"]["artifacts"][0]["path"] = "undeclared.py"
    with pytest.raises(ProtocolStateError, match="output contract"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )

    documents = deepcopy(protocol_documents)
    documents["events"][3]["cumulative_reported"]["model_total_tokens"] = 300_000
    documents["events"][-1]["harness_result"]["reported_usage"][
        "model_total_tokens"
    ] = 300_000
    with pytest.raises(ProtocolStateError, match="exceeds"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )


def test_unnegotiated_event_and_nonmonotonic_controller_time_fail(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["harness"]["capabilities"]["usage_events"] = False
    documents["request"]["harness"]["manifest_digest"] = canonical_digest(
        documents["harness"]
    )
    documents["hello"]["harness"] = deepcopy(documents["request"]["harness"])
    documents["hello"]["capabilities"]["usage_events"] = False
    documents["request"]["required_capabilities"]["usage_events"] = False
    documents["events"][0] = documents["hello"]
    with pytest.raises(ProtocolStateError, match="not negotiated"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )

    documents = deepcopy(protocol_documents)
    documents["events"][3]["recorded_at"] = "2026-07-19T12:00:01Z"
    with pytest.raises(ProtocolStateError, match="monotonic"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )


def test_prompt_digest_and_request_window_are_verified(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["request"]["prompt"]["text"] += " tampered"
    with pytest.raises(IntegrityError, match="prompt digest"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )

    documents = deepcopy(protocol_documents)
    documents["request"]["expires_at"] = documents["request"]["issued_at"]
    with pytest.raises(ProtocolStateError, match="expires_at"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )


def test_model_and_tool_events_are_policy_checked(protocol_documents):
    documents = deepcopy(protocol_documents)
    model_event = {
        "schema": "atv.event/v1",
        "type": "model_call",
        "source": "harness",
        "protocol_version": 1,
        "trial_id": documents["request"]["trial_id"],
        "attempt_id": documents["request"]["attempt_id"],
        "sequence": 0,
        "emitted_at": "2026-07-19T12:00:03Z",
        "recorded_at": "2026-07-19T12:00:03Z",
        "call_id": "model-call-1",
        "parent_call_id": None,
        "phase": "completed",
        "requested_model": "disallowed/model",
        "resolved_model": "disallowed/model",
        "provider": "provider",
        "provider_request_id": "provider-request-1",
        "usage_delta": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "calls": 1,
            "cost_microusd": 100,
        },
        "retry_index": 0,
        "elapsed_ms": 10,
        "finish_reason": "stop",
        "failure": None,
    }
    documents["events"].insert(3, model_event)
    _renumber(documents["events"])
    with pytest.raises(ProtocolStateError, match="disallowed model"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )

    documents = deepcopy(protocol_documents)
    tool_event = {
        "schema": "atv.event/v1",
        "type": "tool_call",
        "source": "harness",
        "protocol_version": 1,
        "trial_id": documents["request"]["trial_id"],
        "attempt_id": documents["request"]["attempt_id"],
        "sequence": 0,
        "emitted_at": "2026-07-19T12:00:03Z",
        "recorded_at": "2026-07-19T12:00:03Z",
        "call_id": "tool-call-1",
        "parent_call_id": None,
        "tool": "browser",
        "phase": "completed",
        "policy_decision": "allowed",
        "elapsed_ms": 10,
        "outcome": "success",
        "failure": None,
    }
    documents["events"].insert(3, tool_event)
    _renumber(documents["events"])
    with pytest.raises(ProtocolStateError, match="denied tool"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )


def test_token_usage_totals_are_consistent(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["events"][3]["cumulative_reported"]["model_total_tokens"] = 1
    documents["events"][-1]["harness_result"]["reported_usage"][
        "model_total_tokens"
    ] = 1
    with pytest.raises(ProtocolStateError, match="must equal"):
        validate_conformance(
            _session_from_documents(documents),
            documents["harness"],
            documents["request"],
        )


def test_canonical_transcript_hash_ignores_input_key_order(protocol_documents):
    left_documents = deepcopy(protocol_documents)
    right_documents = deepcopy(protocol_documents)
    right_documents["events"] = [
        {key: event[key] for key in reversed(event)}
        for event in right_documents["events"]
    ]
    _, left = validate_conformance(
        _session_from_documents(left_documents),
        left_documents["harness"],
        left_documents["request"],
    )
    _, right = validate_conformance(
        _session_from_documents(right_documents),
        right_documents["harness"],
        right_documents["request"],
    )
    assert left.canonical_transcript_sha256 == right.canonical_transcript_sha256
