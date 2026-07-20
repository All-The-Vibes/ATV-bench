from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from atv_bench.protocol import (
    ProtocolAuthorityError,
    ProtocolDecodeError,
    ProtocolSession,
    ProtocolStateError,
    SessionState,
    canonical_digest,
    canonical_json_bytes,
    canonical_jsonl,
    validate_conformance,
    verify_merged_transcript,
)


def _harness_canonical_events(documents):
    return [event for event in documents["events"] if event["source"] == "harness"]


def _complete_session(documents):
    session = ProtocolSession(documents["harness"], documents["request"])
    canonical_harness = _harness_canonical_events(documents)
    for raw, canonical in zip(documents["harness_events"], canonical_harness, strict=True):
        session.receive_harness_event(
            deepcopy(raw),
            recorded_at=canonical["recorded_at"],
        )
        if raw["type"] == "hello":
            session.record_controller_accept(
                recorded_at=documents["accepted"]["recorded_at"]
            )
    return session


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(type="accepted"),
        lambda event: event.update(type="cancel"),
        lambda event: event.update(type="controller_error"),
        lambda event: event.update(source="controller"),
        lambda event: event.update(recorded_at="2026-07-19T12:00:00Z"),
        lambda event: event.update(sequence=0),
        lambda event: event.update(request_digest={"algorithm": "sha256", "value": "0" * 64}),
        lambda event: event.update(
            type="error",
            failure={
                "code": "fake-infrastructure",
                "scope": "infrastructure",
                "retryable": True,
                "infrastructure": True,
            },
        ),
    ],
)
def test_raw_harness_channel_rejects_controller_authority(protocol_documents, mutate):
    session = ProtocolSession(
        protocol_documents["harness"], protocol_documents["request"]
    )
    event = deepcopy(protocol_documents["harness_events"][0])
    mutate(event)
    with pytest.raises(ProtocolAuthorityError):
        session.receive_harness_event(
            event,
            recorded_at="2026-07-19T12:00:00Z",
        )
    assert session.state is SessionState.EXPECT_HELLO
    assert session.events == ()


def test_controller_stamps_and_injects_acceptance_out_of_band(protocol_documents):
    session = ProtocolSession(
        protocol_documents["harness"], protocol_documents["request"]
    )
    raw_hello = deepcopy(protocol_documents["harness_events"][0])
    canonical_hello = session.receive_harness_line(
        canonical_json_bytes(raw_hello) + b"\n",
        recorded_at="2026-07-19T12:00:00Z",
    )
    assert canonical_hello["source"] == "harness"
    assert canonical_hello["recorded_at"] == "2026-07-19T12:00:00Z"
    assert "recorded_at" not in raw_hello

    with pytest.raises(ProtocolStateError, match="must record accepted"):
        session.receive_harness_event(
            deepcopy(protocol_documents["harness_events"][1]),
            recorded_at="2026-07-19T12:00:01Z",
        )

    accepted = session.record_controller_accept(
        recorded_at="2026-07-19T12:00:01Z"
    )
    assert accepted["source"] == "controller"
    assert accepted["sequence"] == 1
    assert accepted["request_digest"] == canonical_digest(
        protocol_documents["request"]
    )
    assert session.state is SessionState.ACTIVE


def test_controller_observed_times_are_monotonic_and_transactional(protocol_documents):
    session = ProtocolSession(
        protocol_documents["harness"], protocol_documents["request"]
    )
    session.receive_harness_event(
        deepcopy(protocol_documents["harness_events"][0]),
        recorded_at="2026-07-19T12:00:02Z",
    )
    with pytest.raises(ProtocolStateError, match="monotonic"):
        session.record_controller_accept(recorded_at="2026-07-19T12:00:01Z")
    assert len(session.events) == 1
    assert session.state is SessionState.WAIT_ACCEPT

    session.record_controller_accept(recorded_at="2026-07-19T12:00:02Z")
    with pytest.raises(ProtocolStateError, match="monotonic"):
        session.receive_harness_event(
            deepcopy(protocol_documents["harness_events"][1]),
            recorded_at="2026-07-19T12:00:01Z",
        )
    assert len(session.events) == 2

    session.receive_harness_event(
        deepcopy(protocol_documents["harness_events"][1]),
        recorded_at="2026-07-19T12:00:03Z",
    )
    assert [event["recorded_at"] for event in session.events] == [
        "2026-07-19T12:00:02Z",
        "2026-07-19T12:00:02Z",
        "2026-07-19T12:00:03Z",
    ]


def test_controller_cancel_and_error_are_injected_not_harness_forged(protocol_documents):
    session = ProtocolSession(
        protocol_documents["harness"], protocol_documents["request"]
    )
    session.receive_harness_event(
        deepcopy(protocol_documents["harness_events"][0]),
        recorded_at="2026-07-19T12:00:00Z",
    )
    session.record_controller_accept(recorded_at="2026-07-19T12:00:01Z")
    error = session.record_controller_error(
        recorded_at="2026-07-19T12:00:02Z",
        failure={
            "code": "gateway-retry",
            "scope": "runner",
            "retryable": True,
            "infrastructure": True,
        },
    )
    cancel = session.record_controller_cancel(
        recorded_at="2026-07-19T12:00:03Z",
        reason_code="deadline",
        grace_period_ms=5000,
    )
    assert error["source"] == "controller"
    assert cancel["source"] == "controller"
    assert session.state is SessionState.CANCELLING

    forbidden = deepcopy(protocol_documents["harness_events"][1])
    forbidden["type"] = "model_call"
    with pytest.raises(ProtocolStateError, match="while cancelling"):
        session.receive_harness_event(
            forbidden,
            recorded_at="2026-07-19T12:00:04Z",
        )

    result = deepcopy(protocol_documents["harness_events"][-1])
    result["harness_sequence"] = 1
    result["harness_result"]["status"] = "cancelled"
    result["harness_result"]["exit"]["code"] = None
    result["harness_result"]["exit"]["cancelled"] = True
    result["harness_result"]["output_tree_digest"] = None
    result["harness_result"]["artifacts"] = []
    result["harness_result"]["failure"] = {
        "code": "cancelled",
        "scope": "harness",
        "retryable": False,
    }
    session.receive_harness_event(
        result,
        recorded_at="2026-07-19T12:00:04Z",
    )
    transcript = session.finish()
    assert transcript.authority_verified is True
    assert [event["type"] for event in transcript.events] == [
        "hello",
        "accepted",
        "controller_error",
        "cancel",
        "result",
    ]


def test_post_result_partial_line_and_early_eof_fail(protocol_documents):
    session = _complete_session(protocol_documents)
    transcript = session.finish()
    assert transcript.authority_verified is True

    post_result = deepcopy(protocol_documents["harness_events"][1])
    post_result["harness_sequence"] = 5
    with pytest.raises(ProtocolStateError, match="after terminal"):
        session.receive_harness_event(
            post_result,
            recorded_at="2026-07-19T12:00:11Z",
        )

    fresh = ProtocolSession(
        protocol_documents["harness"], protocol_documents["request"]
    )
    with pytest.raises(ProtocolDecodeError, match="malformed JSON"):
        fresh.receive_harness_line(
            b'{"schema":"atv.harness-event/v1"',
            recorded_at="2026-07-19T12:00:00Z",
        )
    with pytest.raises(ProtocolDecodeError, match="one physical line"):
        fresh.receive_harness_line(
            b"{}\n{}\n",
            recorded_at="2026-07-19T12:00:00Z",
        )
    fresh.receive_harness_event(
        deepcopy(protocol_documents["harness_events"][0]),
        recorded_at="2026-07-19T12:00:00Z",
    )
    with pytest.raises(ProtocolStateError, match="EOF"):
        fresh.eof()


def test_conformance_rejects_bytes_and_integrity_only_transcripts(protocol_documents):
    merged_bytes = canonical_jsonl(protocol_documents["events"])
    merged = verify_merged_transcript(merged_bytes, protocol_documents["request"])
    assert merged.authority_verified is False
    with pytest.raises(ProtocolAuthorityError, match="raw JSONL bytes"):
        validate_conformance(
            merged_bytes,
            protocol_documents["harness"],
            protocol_documents["request"],
        )
    with pytest.raises(ProtocolAuthorityError, match="integrity evidence only"):
        validate_conformance(
            merged,
            protocol_documents["harness"],
            protocol_documents["request"],
        )
    forged_flag = replace(merged, authority_verified=True)
    with pytest.raises(ProtocolAuthorityError, match="integrity evidence only"):
        validate_conformance(
            forged_flag,
            protocol_documents["harness"],
            protocol_documents["request"],
        )

    session = _complete_session(protocol_documents)
    transcript, report = validate_conformance(
        session,
        protocol_documents["harness"],
        protocol_documents["request"],
    )
    assert transcript.authority_verified is True
    assert report.event_count == len(protocol_documents["events"])


def test_session_binds_request_snapshot_even_if_caller_mutates_input(protocol_documents):
    request = deepcopy(protocol_documents["request"])
    session = ProtocolSession(protocol_documents["harness"], request)
    expected = canonical_digest(request)
    request["prompt"]["text"] = "caller mutation after session creation"
    session.receive_harness_event(
        deepcopy(protocol_documents["harness_events"][0]),
        recorded_at="2026-07-19T12:00:00Z",
    )
    accepted = session.record_controller_accept(
        recorded_at="2026-07-19T12:00:01Z"
    )
    assert accepted["request_digest"] == expected
    assert accepted["request_digest"] == session.request_digest
