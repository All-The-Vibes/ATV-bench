"""Attached OCI protocol roundtrip, authority, and adversarial transport tests."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from atv_bench.protocol import (
    ProtocolSession,
    canonical_json_bytes,
    canonical_jsonl,
    sha256_bytes,
    validate_conformance,
    verify_merged_transcript,
)
from atv_bench.protocol.session import has_session_authority
from atv_bench.sandbox.interactive import (
    CliInteractiveOciBackend,
    InteractiveCommandOutcome,
    InteractiveController,
    InteractiveOciTransport,
    InteractiveTransportLimits,
    InteractiveTransportStatus,
    build_interactive_run_argv,
)
from atv_bench.sandbox.oci import (
    CliOciEngine,
    ContainerPhase,
    ContainerSpec,
    DigestPinnedImage,
    EngineUnavailableError,
    MountSpec,
    OciNetworkPolicy,
    OciResourcePolicy,
)

pytest_plugins = ("tests.protocol.conftest",)


def _clock():
    counter = 0

    def timestamp() -> str:
        nonlocal counter
        value = f"2026-07-19T12:30:00.{counter:06d}Z"
        counter += 1
        return value

    return timestamp


def _spec(
    tmp_path: Path,
    *,
    name: str = "atv-interactive-test",
    wall_time_ms: int = 3_000,
) -> ContainerSpec:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    workspace.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    return ContainerSpec(
        phase=ContainerPhase.HARNESS,
        name=name,
        image=DigestPinnedImage.parse(
            "docker.io/library/python@sha256:" + "1" * 64
        ),
        command=("python", "-c", "pass"),
        mounts=(
            MountSpec(workspace, "/workspace", False),
            MountSpec(artifacts, "/artifacts", False),
        ),
        resources=OciResourcePolicy(
            wall_time_ms=wall_time_ms,
            memory_bytes=256 * 1024 * 1024,
            cpu_millis=1_000,
            pids_limit=64,
            storage_bytes=4 * 1024 * 1024,
            stdout_bytes=2 * 1024 * 1024,
            stderr_bytes=256 * 1024,
            artifact_bytes=2 * 1024 * 1024,
            tmpfs_bytes=2 * 1024 * 1024,
        ),
        network=OciNetworkPolicy.none(),
        working_directory="/workspace",
    )


def _raw_line(event: dict) -> bytes:
    return canonical_json_bytes(event) + b"\n"


def _stream(events) -> bytes:
    return b"".join(_raw_line(event) for event in events)


def _child_script(
    first_stdout: bytes,
    later_stdout: bytes = b"",
    *,
    read_accepted: bool = True,
    controller_type: str | None = None,
    stderr: bytes = b"",
    sleep_after: float = 0,
    chunk_size: int | None = None,
    exit_code: int = 0,
) -> str:
    first = base64.b64encode(first_stdout).decode("ascii")
    later = base64.b64encode(later_stdout).decode("ascii")
    error = base64.b64encode(stderr).decode("ascii")
    return f"""
import base64
import hashlib
import json
import sys
import time

def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def write_payload(payload):
    chunk_size = {chunk_size!r}
    if chunk_size:
        for offset in range(0, len(payload), chunk_size):
            sys.stdout.buffer.write(payload[offset:offset + chunk_size])
            sys.stdout.buffer.flush()
            time.sleep(0.001)
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()

request_line = sys.stdin.buffer.readline()
request = json.loads(request_line.decode('utf-8'))
write_payload(base64.b64decode({first!r}))
if {read_accepted!r}:
    accepted = json.loads(sys.stdin.buffer.readline().decode('utf-8'))
    expected = {{'algorithm': 'sha256', 'value': hashlib.sha256(canonical(request)).hexdigest()}}
    assert accepted['type'] == 'accepted'
    assert accepted['source'] == 'controller'
    assert accepted['request_digest'] == expected
if {controller_type!r} is not None:
    controller_event = json.loads(sys.stdin.buffer.readline().decode('utf-8'))
    assert controller_event['type'] == {controller_type!r}
sys.stderr.buffer.write(base64.b64decode({error!r}))
sys.stderr.buffer.flush()
write_payload(base64.b64decode({later!r}))
if {sleep_after!r}:
    time.sleep({sleep_after!r})
raise SystemExit({exit_code!r})
"""


class FakeInteractiveBackend:
    executable = sys.executable
    kind = "fake"

    def __init__(self, script: str, *, remove_exit_code: int = 0):
        self.script = script
        self.remove_exit_code = remove_exit_code
        self.process = None
        self.present = False
        self.start_calls = []
        self.kill_calls = []
        self.remove_calls = []
        self.shell_used = None

    def start_attached(self, spec):
        self.start_calls.append(spec)
        self.present = True
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            start_new_session = True
        self.shell_used = False
        self.process = subprocess.Popen(
            [sys.executable, "-c", self.script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        return self.process

    def kill_container(self, name, *, signal_name="KILL"):
        self.kill_calls.append((name, signal_name))
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        return InteractiveCommandOutcome(
            (self.executable, "kill", "--signal", signal_name, name),
            0,
        )

    def remove_container(self, name, *, force):
        self.remove_calls.append((name, force))
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.present = False
        argv = [self.executable, "rm"]
        if force:
            argv.append("-f")
        argv.append(name)
        return InteractiveCommandOutcome(tuple(argv), self.remove_exit_code)

    def container_exists(self, name):
        return self.present


def _run_fake(
    tmp_path,
    protocol_documents,
    script,
    *,
    request=None,
    controller=None,
    limits=None,
    wall_time_ms=15_000,
    cancel_event=None,
    remove_exit_code=0,
    before_cleanup=None,
):
    active_request = deepcopy(request or protocol_documents["request"])
    session = ProtocolSession(protocol_documents["harness"], active_request)
    backend = FakeInteractiveBackend(
        script,
        remove_exit_code=remove_exit_code,
    )
    spec = _spec(tmp_path, wall_time_ms=wall_time_ms)
    result = InteractiveOciTransport(
        backend,
        limits=limits
        or InteractiveTransportLimits(
            result_eof_timeout_ms=250,
            exited_pipe_timeout_ms=250,
            hard_kill_wait_ms=500,
        ),
        timestamp=_clock(),
    ).run(
        spec,
        session,
        controller=controller,
        cancel_event=cancel_event,
        before_cleanup=before_cleanup,
    )
    assert backend.remove_calls == [(spec.name, True)]
    assert backend.container_exists(spec.name) is False
    return result, backend, spec


def test_shell_free_attached_argv_has_stdin_but_no_tty_or_detach(tmp_path):
    argv = build_interactive_run_argv("docker", _spec(tmp_path))
    assert argv[:3] == ("docker", "run", "--interactive")
    assert argv.count("--interactive") == 1
    assert "--pull" in argv
    assert argv[argv.index("--pull") + 1] == "never"
    assert "--detach" not in argv
    assert "-t" not in argv
    assert "--tty" not in argv

    stdin_open = dataclasses.replace(_spec(tmp_path / "stdin"), stdin_open=True)
    argv = build_interactive_run_argv("docker", stdin_open)
    assert argv.count("--interactive") == 1


def test_fake_attached_roundtrip_is_authority_verified_and_secret_free(
    tmp_path,
    protocol_documents,
):
    request = deepcopy(protocol_documents["request"])
    canary = "ATV_INTERACTIVE_PROMPT_CANARY_DO_NOT_PUBLISH"
    request["prompt"]["text"] = canary
    request["prompt"]["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(canary.encode("utf-8")),
    }
    raw_events = deepcopy(protocol_documents["harness_events"])
    script = _child_script(
        _raw_line(raw_events[0]),
        _stream(raw_events[1:]),
    )
    result, backend, _ = _run_fake(
        tmp_path,
        protocol_documents,
        script,
        request=request,
    )

    assert result.status is InteractiveTransportStatus.COMPLETED
    assert result.transcript is not None
    assert result.transcript.authority_verified is True
    assert result.stdout
    assert result.stderr == b""
    assert has_session_authority(result.transcript) is True
    assert result.authority_verified is True
    assert result.evidence.accepted_written is True
    assert result.evidence.accepted_request_digest_matches is True
    assert result.evidence.cleanup.confirmed_absent is True
    assert result.evidence.controller_events == ("accepted",)
    assert backend.shell_used is False

    validate_conformance(
        result.transcript,
        protocol_documents["harness"],
        request,
    )
    merged = verify_merged_transcript(
        canonical_jsonl(result.transcript.events),
        request,
    )
    assert merged.authority_verified is False
    assert has_session_authority(merged) is False
    serialized_evidence = json.dumps(result.evidence.to_dict(), sort_keys=True)
    assert canary not in serialized_evidence
    assert request["prompt"]["text"] not in serialized_evidence
    assert "credentials" not in serialized_evidence


def test_pre_cleanup_observer_runs_before_exact_container_removal(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    observed = []
    backend_holder = {}

    def before_cleanup(spec):
        backend = backend_holder["backend"]
        observed.append((spec.name, backend.container_exists(spec.name)))

    active_request = deepcopy(protocol_documents["request"])
    session = ProtocolSession(protocol_documents["harness"], active_request)
    backend = FakeInteractiveBackend(
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
        )
    )
    backend_holder["backend"] = backend
    spec = _spec(tmp_path)
    result = InteractiveOciTransport(
        backend,
        limits=InteractiveTransportLimits(
            result_eof_timeout_ms=250,
            exited_pipe_timeout_ms=250,
            hard_kill_wait_ms=500,
        ),
        timestamp=_clock(),
    ).run(
        spec,
        session,
        before_cleanup=before_cleanup,
    )

    assert result.status is InteractiveTransportStatus.COMPLETED
    assert observed == [(spec.name, True)]
    assert backend.container_exists(spec.name) is False


def test_transcript_hash_is_canonical_across_framing_and_json_layout(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    canonical_result, _, _ = _run_fake(
        tmp_path / "canonical",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
        ),
    )

    def spaced_line(event):
        return (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=False,
                separators=(", ", ": "),
            ).encode("utf-8")
            + b"\r\n"
        )

    fragmented_result, _, _ = _run_fake(
        tmp_path / "fragmented",
        protocol_documents,
        _child_script(
            spaced_line(events[0]),
            b"".join(spaced_line(event) for event in events[1:]),
            chunk_size=7,
        ),
    )

    assert canonical_result.status is InteractiveTransportStatus.COMPLETED
    assert fragmented_result.status is InteractiveTransportStatus.COMPLETED
    assert canonical_result.transcript is not None
    assert fragmented_result.transcript is not None
    assert (
        canonical_result.transcript.events
        == fragmented_result.transcript.events
    )
    expected = hashlib.sha256(
        canonical_jsonl(canonical_result.transcript.events)
    ).hexdigest()
    assert canonical_result.evidence.transcript_sha256 == expected
    assert fragmented_result.evidence.transcript_sha256 == expected


def test_stdout_and_stderr_are_separate_and_stderr_is_not_disclosed(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    canary = b"ATV_STDERR_SECRET_CANARY"
    result, _, _ = _run_fake(
        tmp_path / "valid",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
            stderr=canary,
        ),
    )
    assert result.status is InteractiveTransportStatus.COMPLETED
    assert result.evidence.stderr_total_bytes == len(canary)
    assert result.evidence.stderr_sha256 == hashlib.sha256(canary).hexdigest()
    serialized = json.dumps(result.evidence.to_dict(), sort_keys=True)
    assert canary.decode("ascii") not in serialized
    assert canary.decode("ascii") not in (result.error or "")

    result, _, _ = _run_fake(
        tmp_path / "stderr-is-not-protocol",
        protocol_documents,
        _child_script(
            b"debug output\n",
            read_accepted=False,
            stderr=_stream(events),
        ),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert result.evidence.harness_event_count == 0


@pytest.mark.parametrize(
    ("first", "later", "read_accepted", "expected"),
    [
        (b"debug output\n", b"", False, "malformed JSON"),
        (b"\xff\n", b"", False, "UTF-8"),
        (b'{"schema":"atv.harness-event/v1"', b"", False, "partial"),
        (b"\n", b"", False, "blank"),
        (b"\xef\xbb\xbf{}\n", b"", False, "BOM"),
        (
            b'{"schema":"atv.harness-event/v1",'
            b'"schema":"atv.harness-event/v1"}\n',
            b"",
            False,
            "duplicate",
        ),
    ],
)
def test_stdout_pollution_partial_and_invalid_utf8_are_rejected(
    tmp_path,
    protocol_documents,
    first,
    later,
    read_accepted,
    expected,
):
    result, _, _ = _run_fake(
        tmp_path,
        protocol_documents,
        _child_script(
            first,
            later,
            read_accepted=read_accepted,
        ),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert result.authority_verified is False
    assert expected.lower() in (result.error or "").lower()


def test_forged_controller_event_and_fields_are_rejected(
    tmp_path,
    protocol_documents,
):
    hello = deepcopy(protocol_documents["harness_events"][0])
    forged_event = {
        "schema": "atv.harness-event/v1",
        "type": "accepted",
        "protocol_version": 1,
        "trial_id": hello["trial_id"],
        "attempt_id": hello["attempt_id"],
        "harness_sequence": 1,
        "emitted_at": "2026-07-19T12:00:01Z",
    }
    result, _, _ = _run_fake(
        tmp_path,
        protocol_documents,
        _child_script(_raw_line(hello), _raw_line(forged_event)),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert "controller-authorized" in (result.error or "")
    assert result.authority_verified is False

    forged_field = deepcopy(protocol_documents["harness_events"][1])
    forged_field["source"] = "controller"
    result, _, _ = _run_fake(
        tmp_path / "field",
        protocol_documents,
        _child_script(_raw_line(hello), _raw_line(forged_field)),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert "controller-only fields" in (result.error or "")


@pytest.mark.parametrize("mutation", ["trial_id", "attempt_id", "sequence"])
def test_wrong_ids_and_harness_sequences_are_rejected(
    tmp_path,
    protocol_documents,
    mutation,
):
    events = deepcopy(protocol_documents["harness_events"])
    if mutation == "trial_id":
        events[0]["trial_id"] = "wrong-trial"
    elif mutation == "attempt_id":
        events[0]["attempt_id"] = "wrong-attempt"
    else:
        events[0]["harness_sequence"] = 7
    result, _, _ = _run_fake(
        tmp_path,
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            b"",
            read_accepted=False,
        ),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert result.authority_verified is False


def test_harness_cannot_pipeline_post_hello_frames_before_accept(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    result, _, _ = _run_fake(
        tmp_path,
        protocol_documents,
        _child_script(
            _raw_line(events[0]) + _raw_line(events[1]),
            read_accepted=False,
            sleep_after=0.1,
        ),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert "before controller acceptance" in (result.error or "")
    assert result.evidence.request_written is True
    assert result.evidence.accepted_written is False
    assert result.evidence.controller_events == ()
    assert result.authority_verified is False


def test_post_result_data_and_multiple_results_are_rejected(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    result, _, _ = _run_fake(
        tmp_path,
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]) + b"stdout-after-result\n",
        ),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert "after terminal result" in (result.error or "")

    duplicate_result = deepcopy(events[-1])
    duplicate_result["harness_sequence"] += 1
    result, _, _ = _run_fake(
        tmp_path / "duplicate",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]) + _raw_line(duplicate_result),
        ),
    )
    assert result.status is InteractiveTransportStatus.PROTOCOL_ERROR
    assert result.authority_verified is False


def test_line_total_event_and_stderr_limits_fail_closed(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])

    request = deepcopy(protocol_documents["request"])
    request["protocol_limits"]["max_line_bytes"] = 64
    result, _, _ = _run_fake(
        tmp_path / "controller-line",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            b"",
            read_accepted=False,
        ),
        request=request,
    )
    assert result.status is InteractiveTransportStatus.LIMIT_ERROR
    assert result.evidence.request_written is False

    request = deepcopy(protocol_documents["request"])
    request["protocol_limits"]["max_line_bytes"] = 4_096
    result, _, _ = _run_fake(
        tmp_path / "harness-line",
        protocol_documents,
        _child_script(
            (b" " * 4_096) + _raw_line(events[0]),
            b"",
            read_accepted=False,
        ),
        request=request,
    )
    assert result.status is InteractiveTransportStatus.LIMIT_ERROR
    assert result.evidence.request_written is True
    assert result.evidence.harness_event_count == 0

    result, _, _ = _run_fake(
        tmp_path / "total",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            b"",
            read_accepted=False,
        ),
        limits=InteractiveTransportLimits(
            max_stdout_bytes=100,
            max_stderr_bytes=256,
            result_eof_timeout_ms=250,
            exited_pipe_timeout_ms=250,
            hard_kill_wait_ms=500,
        ),
    )
    assert result.status is InteractiveTransportStatus.LIMIT_ERROR

    request = deepcopy(protocol_documents["request"])
    request["protocol_limits"]["max_events"] = 3
    result, _, _ = _run_fake(
        tmp_path / "events",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
        ),
        request=request,
    )
    assert result.status is InteractiveTransportStatus.LIMIT_ERROR

    result, _, _ = _run_fake(
        tmp_path / "stderr",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
            stderr=b"x" * 1024,
        ),
        limits=InteractiveTransportLimits(
            max_stderr_bytes=32,
            result_eof_timeout_ms=250,
            exited_pipe_timeout_ms=250,
            hard_kill_wait_ms=500,
        ),
    )
    assert result.status is InteractiveTransportStatus.LIMIT_ERROR
    assert result.evidence.stderr_limit_exceeded is True
    assert result.authority_verified is False


def test_blocked_request_delivery_obeys_wall_timeout(
    tmp_path,
    protocol_documents,
):
    request = deepcopy(protocol_documents["request"])
    prompt = "x" * (128 * 1024)
    request["prompt"]["text"] = prompt
    request["prompt"]["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(prompt.encode("utf-8")),
    }
    assert (
        len(canonical_json_bytes(request))
        < request["protocol_limits"]["max_line_bytes"]
    )

    started = time.monotonic()
    result, backend, spec = _run_fake(
        tmp_path,
        protocol_documents,
        "import time; time.sleep(30)",
        request=request,
        wall_time_ms=250,
        limits=InteractiveTransportLimits(
            result_eof_timeout_ms=100,
            exited_pipe_timeout_ms=100,
            hard_kill_wait_ms=500,
        ),
    )
    assert time.monotonic() - started < 5
    assert result.status is InteractiveTransportStatus.TIMED_OUT
    assert result.authority_verified is False
    assert backend.kill_calls == [(spec.name, "KILL")]


def test_controller_cancel_and_error_are_sent_over_attached_stdin(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    controller = InteractiveController()
    controller.cancel("test-cancel", grace_period_ms=500)
    result, _, _ = _run_fake(
        tmp_path / "cancel",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
            controller_type="cancel",
        ),
        controller=controller,
    )
    assert result.status is InteractiveTransportStatus.CANCELLED
    assert result.authority_verified is True
    assert result.evidence.controller_events == ("accepted", "cancel")
    assert any(
        event["type"] == "cancel" and event["source"] == "controller"
        for event in result.transcript.events
    )

    controller = InteractiveController()
    controller.error(
        {
            "code": "test-controller-error",
            "scope": "runner",
            "retryable": True,
            "infrastructure": True,
        }
    )
    result, _, _ = _run_fake(
        tmp_path / "error",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
            controller_type="controller_error",
        ),
        controller=controller,
    )
    assert result.status is InteractiveTransportStatus.COMPLETED
    assert result.authority_verified is True
    assert result.evidence.controller_events == (
        "accepted",
        "controller_error",
    )


def test_external_cancellation_before_hello_is_cancelled_not_transport_error(
    tmp_path,
    protocol_documents,
):
    cancel_event = threading.Event()
    timer = threading.Timer(0.1, cancel_event.set)
    timer.start()
    try:
        result, backend, spec = _run_fake(
            tmp_path,
            protocol_documents,
            _child_script(
                b"",
                read_accepted=False,
                sleep_after=30,
            ),
            cancel_event=cancel_event,
        )
    finally:
        timer.cancel()
        timer.join()
    assert result.status is InteractiveTransportStatus.CANCELLED
    assert result.evidence.error_code == "cancelled"
    assert result.evidence.accepted_written is False
    assert result.authority_verified is False
    assert backend.kill_calls == [(spec.name, "KILL")]


def test_cancel_grace_then_hard_kill_and_exact_remove(
    tmp_path,
    protocol_documents,
):
    hello = protocol_documents["harness_events"][0]
    controller = InteractiveController()
    controller.cancel("test-hard-kill", grace_period_ms=20)
    result, backend, spec = _run_fake(
        tmp_path,
        protocol_documents,
        _child_script(
            _raw_line(hello),
            b"",
            controller_type="cancel",
            sleep_after=30,
        ),
        controller=controller,
        limits=InteractiveTransportLimits(
            result_eof_timeout_ms=200,
            exited_pipe_timeout_ms=200,
            hard_kill_wait_ms=500,
        ),
    )
    assert result.status is InteractiveTransportStatus.CANCELLED
    assert result.authority_verified is False
    assert backend.kill_calls == [(spec.name, "KILL")]
    assert result.evidence.cancel_grace_period_ms == 20
    assert result.evidence.hard_kill_exit_code == 0
    assert result.evidence.hard_kill_argv_sha256
    assert result.evidence.termination_actions == (
        "protocol_cancel",
        "hard_kill",
        "force_remove",
    )
    assert result.evidence.cleanup.confirmed_absent is True


def test_nonzero_exit_and_already_absent_cleanup_are_reported_precisely(
    tmp_path,
    protocol_documents,
):
    events = deepcopy(protocol_documents["harness_events"])
    result, _, _ = _run_fake(
        tmp_path / "nonzero",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
            exit_code=7,
        ),
    )
    assert result.status is InteractiveTransportStatus.NONZERO_EXIT
    assert result.evidence.process_exit_code == 7
    assert result.authority_verified is True

    result, _, _ = _run_fake(
        tmp_path / "already-absent",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
        ),
        remove_exit_code=1,
    )
    assert result.status is InteractiveTransportStatus.COMPLETED
    assert result.evidence.cleanup.remove_exit_code == 1
    assert result.evidence.cleanup.confirmed_absent is True
    assert result.authority_verified is True


def test_wall_timeout_hard_kills_and_pipe_leak_is_rejected(
    tmp_path,
    protocol_documents,
):
    hello = protocol_documents["harness_events"][0]
    result, backend, spec = _run_fake(
        tmp_path / "timeout",
        protocol_documents,
        _child_script(
            _raw_line(hello),
            b"",
            sleep_after=30,
        ),
        wall_time_ms=500,
    )
    assert result.status is InteractiveTransportStatus.TIMED_OUT
    assert result.authority_verified is False
    assert backend.kill_calls == [(spec.name, "KILL")]

    events = protocol_documents["harness_events"]
    result, backend, spec = _run_fake(
        tmp_path / "leak",
        protocol_documents,
        _child_script(
            _raw_line(events[0]),
            _stream(events[1:]),
            sleep_after=30,
        ),
        limits=InteractiveTransportLimits(
            result_eof_timeout_ms=50,
            exited_pipe_timeout_ms=50,
            hard_kill_wait_ms=500,
        ),
    )
    assert result.status is InteractiveTransportStatus.PIPE_LEAK
    assert result.evidence.pipe_leak_detected is True
    assert result.authority_verified is False
    assert backend.kill_calls == [(spec.name, "KILL")]


def _cached_engine_image_or_skip():
    try:
        engine = CliOciEngine.auto()
    except EngineUnavailableError as exc:
        pytest.skip(f"interactive OCI integration unavailable: {exc}")
    reachable, detail = engine.daemon_status()
    if not reachable:
        pytest.skip(
            f"interactive OCI integration unavailable: daemon unreachable: {detail}"
        )
    image = DigestPinnedImage.parse(
        "docker.io/library/python@sha256:"
        "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    try:
        inspection = engine.inspect_image(image)
    except Exception as exc:
        pytest.skip(
            "interactive OCI integration unavailable: digest-pinned Python image "
            f"is not cached: {exc}"
        )
    if not inspection.verified:
        pytest.skip(
            "interactive OCI integration unavailable: cached Python image did "
            "not verify the requested digest"
        )
    return engine, image


@pytest.mark.integration
def test_real_cached_oci_interactive_roundtrip_edits_workspace(
    tmp_path,
    protocol_documents,
):
    engine, image = _cached_engine_image_or_skip()
    workspace = tmp_path / "live-workspace"
    artifacts = tmp_path / "live-artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "main.py").write_text("print('before')\n", encoding="utf-8")
    try:
        workspace.chmod(0o777)
        artifacts.chmod(0o777)
        (workspace / "main.py").chmod(0o666)
    except OSError:
        pass

    harness = deepcopy(protocol_documents["harness"])
    request = deepcopy(protocol_documents["request"])
    artifact = deepcopy(protocol_documents["artifact"])
    artifact_bytes = protocol_documents["artifact_bytes"]
    usage = deepcopy(protocol_documents["harness_result"]["reported_usage"])
    result_payload = deepcopy(protocol_documents["harness_result"])
    suffix = secrets.token_hex(4)
    container_name = f"atv-interactive-live-{suffix}"
    script = f"""
import base64
import hashlib
import json
import pathlib
import sys

HARNESS = json.loads({json.dumps(harness)!r})
ARTIFACT = json.loads({json.dumps(artifact)!r})
USAGE = json.loads({json.dumps(usage)!r})
RESULT = json.loads({json.dumps(result_payload)!r})
CONTENT = base64.b64decode({base64.b64encode(artifact_bytes).decode('ascii')!r})

def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def emit(event):
    sys.stdout.buffer.write(canonical(event) + b'\\n')
    sys.stdout.buffer.flush()

request = json.loads(sys.stdin.buffer.readline().decode('utf-8'))
emit({{
    'schema': 'atv.harness-event/v1',
    'type': 'hello',
    'protocol_version': 1,
    'trial_id': request['trial_id'],
    'attempt_id': request['attempt_id'],
    'harness_sequence': 0,
    'emitted_at': '2026-07-19T12:00:00Z',
    'supported_protocol_versions': [1],
    'capabilities': HARNESS['capabilities'],
    'harness': request['harness'],
}})
accepted = json.loads(sys.stdin.buffer.readline().decode('utf-8'))
expected = {{'algorithm': 'sha256', 'value': hashlib.sha256(canonical(request)).hexdigest()}}
assert accepted['type'] == 'accepted'
assert accepted['source'] == 'controller'
assert accepted['request_digest'] == expected
pathlib.Path('/workspace/main.py').write_bytes(CONTENT)
emit({{
    'schema': 'atv.harness-event/v1',
    'type': 'artifact',
    'protocol_version': 1,
    'trial_id': request['trial_id'],
    'attempt_id': request['attempt_id'],
    'harness_sequence': 1,
    'emitted_at': '2026-07-19T12:00:01Z',
    'artifact': ARTIFACT,
}})
emit({{
    'schema': 'atv.harness-event/v1',
    'type': 'usage',
    'protocol_version': 1,
    'trial_id': request['trial_id'],
    'attempt_id': request['attempt_id'],
    'harness_sequence': 2,
    'emitted_at': '2026-07-19T12:00:02Z',
    'cumulative_reported': USAGE,
}})
emit({{
    'schema': 'atv.harness-event/v1',
    'type': 'result',
    'protocol_version': 1,
    'trial_id': request['trial_id'],
    'attempt_id': request['attempt_id'],
    'harness_sequence': 3,
    'emitted_at': '2026-07-19T12:00:03Z',
    'harness_result': RESULT,
}})
"""
    resources = OciResourcePolicy(
        wall_time_ms=15_000,
        memory_bytes=256 * 1024 * 1024,
        cpu_millis=1_000,
        pids_limit=64,
        storage_bytes=8 * 1024 * 1024,
        stdout_bytes=2 * 1024 * 1024,
        stderr_bytes=256 * 1024,
        artifact_bytes=2 * 1024 * 1024,
        tmpfs_bytes=2 * 1024 * 1024,
    )
    spec = ContainerSpec(
        phase=ContainerPhase.HARNESS,
        name=container_name,
        image=image,
        command=("python", "-c", script),
        mounts=(
            MountSpec(workspace, "/workspace", False),
            MountSpec(artifacts, "/artifacts", False),
        ),
        resources=resources,
        network=OciNetworkPolicy.none(),
        working_directory="/workspace",
    )
    session = ProtocolSession(harness, request)
    backend = CliInteractiveOciBackend(engine.executable)
    result = InteractiveOciTransport(
        backend,
        limits=InteractiveTransportLimits(
            result_eof_timeout_ms=2_000,
            exited_pipe_timeout_ms=2_000,
            hard_kill_wait_ms=2_000,
        ),
    ).run(spec, session)

    assert result.status is InteractiveTransportStatus.COMPLETED, result.error
    assert result.authority_verified is True
    assert result.transcript is not None
    assert has_session_authority(result.transcript) is True
    validate_conformance(result.transcript, harness, request)
    assert (workspace / "main.py").read_bytes() == artifact_bytes
    assert result.evidence.accepted_request_digest_matches is True
    assert result.evidence.cleanup.confirmed_absent is True
    assert engine.container_exists(container_name) is False
