"""Authority-preserving attached OCI transport for protocol-v1 harnesses.

This module is intentionally standalone.  It does not modify or silently upgrade the
existing batch OCI runner.  A later integration can hand its harness ``ContainerSpec``
and trusted ``ProtocolSession`` to :class:`InteractiveOciTransport`.
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from atv_bench.protocol import (
    ProtocolError,
    ProtocolSession,
    ProtocolTranscript,
    SessionState,
    canonical_json_bytes,
    canonical_jsonl,
)
from atv_bench.protocol.errors import ProtocolLimitError
from atv_bench.protocol.session import has_session_authority

from .oci import ContainerSpec, build_run_argv


INTERACTIVE_EVIDENCE_SCHEMA = "atv.oci-interactive-evidence/v1"
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REASON_CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_ENGINE_ENV_ALLOWLIST = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "CONTAINER_HOST",
    "XDG_RUNTIME_DIR",
}
_EOF = object()


class InteractiveOciError(RuntimeError):
    """Base class for attached interactive transport failures."""


class InteractiveProtocolError(InteractiveOciError):
    """Harness stdout did not satisfy the authority-preserving protocol."""


class InteractiveLimitError(InteractiveOciError):
    """A bounded transport limit was exceeded."""


class InteractivePipeLeakError(InteractiveOciError):
    """The client exited or produced a result without closing its pipes."""


class InteractiveCleanupError(InteractiveOciError):
    """The exact harness container could not be proven absent."""


class InteractiveTimeoutError(InteractiveOciError):
    """The attached transport exceeded its wall-time budget."""


class InteractiveCancelledError(InteractiveOciError):
    """The attached transport was cancelled before protocol termination."""


class InteractiveTransportStatus(str, enum.Enum):
    COMPLETED = "completed"
    NONZERO_EXIT = "nonzero_exit"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    PROTOCOL_ERROR = "protocol_error"
    LIMIT_ERROR = "limit_error"
    PIPE_LEAK = "pipe_leak"
    TRANSPORT_ERROR = "transport_error"
    CLEANUP_ERROR = "cleanup_error"


@dataclasses.dataclass(frozen=True, slots=True)
class InteractiveTransportLimits:
    """Host-side ceilings in addition to the negotiated protocol limits."""

    max_stdout_bytes: int | None = None
    max_stderr_bytes: int = 256 * 1024
    poll_interval_ms: int = 10
    result_eof_timeout_ms: int = 1_000
    exited_pipe_timeout_ms: int = 1_000
    hard_kill_wait_ms: int = 2_000
    max_controller_actions: int = 32

    def __post_init__(self) -> None:
        if self.max_stdout_bytes is not None and (
            not isinstance(self.max_stdout_bytes, int)
            or isinstance(self.max_stdout_bytes, bool)
            or self.max_stdout_bytes <= 0
        ):
            raise ValueError("max_stdout_bytes must be a positive integer or None")
        for field in (
            "max_stderr_bytes",
            "poll_interval_ms",
            "result_eof_timeout_ms",
            "exited_pipe_timeout_ms",
            "hard_kill_wait_ms",
            "max_controller_actions",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")


@dataclasses.dataclass(frozen=True, slots=True)
class InteractiveCommandOutcome:
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool = False

    @property
    def argv_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(list(self.argv))
        ).hexdigest()


class AttachedProcess(Protocol):
    stdin: BinaryIO | None
    stdout: BinaryIO | None
    stderr: BinaryIO | None
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class InteractiveOciBackend(Protocol):
    """Minimal backend needed by the interactive harness phase."""

    executable: str
    kind: str

    def start_attached(self, spec: ContainerSpec) -> AttachedProcess: ...

    def kill_container(
        self,
        name: str,
        *,
        signal_name: str = "KILL",
    ) -> InteractiveCommandOutcome: ...

    def remove_container(
        self,
        name: str,
        *,
        force: bool,
    ) -> InteractiveCommandOutcome: ...

    def container_exists(self, name: str) -> bool: ...


def _engine_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENGINE_ENV_ALLOWLIST and "\x00" not in value
    }


def build_interactive_run_argv(
    engine_executable: str,
    spec: ContainerSpec,
) -> tuple[str, ...]:
    """Build attached Docker/Podman argv without a shell or TTY."""

    if spec.detached:
        raise ValueError("interactive OCI transport requires an attached container")
    base = build_run_argv(engine_executable, spec)
    if "--detach" in base:
        raise ValueError("interactive OCI argv cannot contain --detach")
    image_index = base.index(spec.image.reference)
    if "--interactive" in base[2:image_index]:
        return base
    return (base[0], base[1], "--interactive", *base[2:])


def _creation_options() -> tuple[int, bool]:
    if os.name == "nt":
        return (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            False,
        )
    return 0, True


def _terminate_client_process(process: AttachedProcess) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=_engine_environment(),
                    timeout=10,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


class CliInteractiveOciBackend:
    """Shell-free Docker/Podman CLI backend for attached execution."""

    def __init__(self, executable: str) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise InteractiveOciError(
                f"OCI engine executable was not found: {executable}"
            )
        self.executable = resolved
        self.kind = (
            "podman" if "podman" in Path(resolved).name.lower() else "docker"
        )

    @classmethod
    def auto(cls) -> "CliInteractiveOciBackend":
        for candidate in ("docker", "podman"):
            if shutil.which(candidate):
                return cls(candidate)
        raise InteractiveOciError("neither docker nor podman is installed")

    def start_attached(self, spec: ContainerSpec) -> subprocess.Popen[bytes]:
        creationflags, start_new_session = _creation_options()
        return subprocess.Popen(
            build_interactive_run_argv(self.executable, spec),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_engine_environment(),
            shell=False,
            bufsize=0,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

    def _command(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
    ) -> InteractiveCommandOutcome:
        command = tuple(str(value) for value in argv)
        if (
            not command
            or command[0] != self.executable
            or any("\x00" in value for value in command)
        ):
            raise ValueError("engine command is not a safe argv vector")
        creationflags, start_new_session = _creation_options()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_engine_environment(),
            shell=False,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        try:
            exit_code = process.wait(timeout=timeout_seconds)
            return InteractiveCommandOutcome(command, exit_code)
        except subprocess.TimeoutExpired:
            _terminate_client_process(process)
            return InteractiveCommandOutcome(command, process.poll(), timed_out=True)

    def kill_container(
        self,
        name: str,
        *,
        signal_name: str = "KILL",
    ) -> InteractiveCommandOutcome:
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise ValueError("container name is unsafe")
        normalized_signal = signal_name.upper()
        if normalized_signal not in {"KILL", "TERM", "INT"}:
            raise ValueError("unsupported container signal")
        return self._command(
            [
                self.executable,
                "kill",
                "--signal",
                normalized_signal,
                name,
            ]
        )

    def remove_container(
        self,
        name: str,
        *,
        force: bool,
    ) -> InteractiveCommandOutcome:
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise ValueError("container name is unsafe")
        argv = [self.executable, "rm"]
        if force:
            argv.append("-f")
        argv.append(name)
        return self._command(argv)

    def container_exists(self, name: str) -> bool:
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise ValueError("container name is unsafe")
        outcome = self._command([self.executable, "inspect", name])
        if outcome.timed_out or outcome.exit_code is None:
            raise InteractiveCleanupError(
                "container absence inspection did not complete"
            )
        return outcome.exit_code == 0


@dataclasses.dataclass(frozen=True, slots=True)
class _ControllerAction:
    kind: str
    reason_code: str | None = None
    grace_period_ms: int | None = None
    failure: Mapping[str, Any] | None = None
    terminate: bool = False


class InteractiveController:
    """Thread-safe controller action queue for a running transport."""

    def __init__(self) -> None:
        self._actions: queue.SimpleQueue[_ControllerAction] = queue.SimpleQueue()

    def cancel(
        self,
        reason_code: str = "controller-cancelled",
        *,
        grace_period_ms: int | None = None,
    ) -> None:
        if (
            not isinstance(reason_code, str)
            or _REASON_CODE_RE.fullmatch(reason_code) is None
        ):
            raise ValueError("reason_code must be a protocol identifier")
        if grace_period_ms is not None and (
            not isinstance(grace_period_ms, int)
            or isinstance(grace_period_ms, bool)
            or grace_period_ms < 0
        ):
            raise ValueError("grace_period_ms must be a non-negative integer")
        self._actions.put(
            _ControllerAction(
                kind="cancel",
                reason_code=reason_code,
                grace_period_ms=grace_period_ms,
            )
        )

    def error(
        self,
        failure: Mapping[str, Any],
        *,
        terminate: bool = False,
        grace_period_ms: int | None = None,
    ) -> None:
        if not isinstance(failure, Mapping):
            raise TypeError("failure must be a mapping")
        if grace_period_ms is not None and (
            not isinstance(grace_period_ms, int)
            or isinstance(grace_period_ms, bool)
            or grace_period_ms < 0
        ):
            raise ValueError("grace_period_ms must be a non-negative integer")
        self._actions.put(
            _ControllerAction(
                kind="error",
                failure=deepcopy(dict(failure)),
                terminate=bool(terminate),
                reason_code="controller-error",
                grace_period_ms=grace_period_ms,
            )
        )

    def _drain(self) -> tuple[_ControllerAction, ...]:
        actions: list[_ControllerAction] = []
        while True:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                return tuple(actions)


class _MonotonicUtcClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: datetime | None = None

    def __call__(self) -> str:
        with self._lock:
            observed = datetime.now(timezone.utc)
            if self._last is not None and observed <= self._last:
                observed = self._last + timedelta(microseconds=1)
            self._last = observed
            return observed.isoformat(timespec="microseconds").replace(
                "+00:00",
                "Z",
            )


class _LineFramer:
    def __init__(
        self,
        *,
        max_line_bytes: int,
        max_total_bytes: int,
    ) -> None:
        self.max_line_bytes = max_line_bytes
        self.max_total_bytes = max_total_bytes
        self.buffer = bytearray()
        self.capture = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        self.total += len(chunk)
        if self.total > self.max_total_bytes:
            raise InteractiveLimitError(
                f"stdout exceeds {self.max_total_bytes} bytes"
            )
        self.capture.extend(chunk)
        self.buffer.extend(chunk)
        lines: list[bytes] = []
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self.buffer[: newline + 1])
            del self.buffer[: newline + 1]
            if len(line) > self.max_line_bytes + 2:
                raise InteractiveLimitError(
                    f"stdout line exceeds {self.max_line_bytes} bytes"
                )
            if line in {b"\n", b"\r\n"}:
                raise InteractiveProtocolError(
                    "stdout pollution: blank protocol line"
                )
            lines.append(line)
        if len(self.buffer) > self.max_line_bytes + 2:
            raise InteractiveLimitError(
                f"stdout line exceeds {self.max_line_bytes} bytes"
            )
        return tuple(lines)

    def finish(self) -> None:
        if self.buffer:
            raise InteractiveProtocolError(
                "partial protocol line remained at stdout EOF"
            )


class _DigestCounter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.hasher = hashlib.sha256()
        self.capture = bytearray()
        self.exceeded = threading.Event()
        self.lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self.lock:
            self.total += len(chunk)
            self.hasher.update(chunk)
            remaining = max(0, self.limit - len(self.capture))
            if remaining:
                self.capture.extend(chunk[:remaining])
            if self.total > self.limit:
                self.exceeded.set()

    def snapshot(self) -> tuple[int, str, bool]:
        with self.lock:
            return self.total, self.hasher.hexdigest(), self.exceeded.is_set()

    def capture_bytes(self) -> bytes:
        with self.lock:
            return bytes(self.capture)


def _queue_pipe_item(
    output: queue.Queue[Any],
    value: Any,
    stop: threading.Event,
) -> bool:
    while not stop.is_set():
        try:
            output.put(value, timeout=0.05)
            return True
        except queue.Full:
            continue
    return False


def _read_pipe(
    stream: BinaryIO,
    output: queue.Queue[Any],
    stop: threading.Event,
) -> None:
    try:
        while True:
            reader = getattr(stream, "read1", None)
            chunk = reader(64 * 1024) if callable(reader) else stream.read(64 * 1024)
            if not chunk:
                return
            if not _queue_pipe_item(output, bytes(chunk), stop):
                return
    except (OSError, ValueError) as exc:
        _queue_pipe_item(output, exc, stop)
    finally:
        _queue_pipe_item(output, _EOF, stop)
        try:
            stream.close()
        except OSError:
            pass


def _drain_stderr(stream: BinaryIO, counter: _DigestCounter) -> None:
    try:
        while True:
            reader = getattr(stream, "read1", None)
            chunk = reader(64 * 1024) if callable(reader) else stream.read(64 * 1024)
            if not chunk:
                return
            counter.feed(bytes(chunk))
    except (OSError, ValueError):
        return
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _close_stream(stream: BinaryIO | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _encoded_json_line(
    value: Mapping[str, Any],
    *,
    max_line_bytes: int,
) -> bytes:
    encoded = canonical_json_bytes(dict(value))
    if len(encoded) > max_line_bytes:
        raise InteractiveLimitError(
            f"controller line exceeds {max_line_bytes} bytes"
        )
    return encoded + b"\n"


def _write_all(stream: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise BrokenPipeError("attached container stdin made no progress")
        view = view[written:]
    stream.flush()


def _write_json_line(
    process: AttachedProcess,
    payload: bytes,
    *,
    deadline: float,
    poll_interval_ms: int,
    cancel_event: threading.Event | None,
    write_threads: list[threading.Thread],
    thread_name: str,
) -> None:
    if process.stdin is None or process.stdin.closed:
        raise InteractiveOciError("attached container stdin is unavailable")
    if cancel_event is not None and cancel_event.is_set():
        raise InteractiveCancelledError(
            "external cancellation occurred during controller delivery"
        )

    outcome: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

    def writer() -> None:
        try:
            assert process.stdin is not None
            _write_all(process.stdin, payload)
        except BaseException as exc:
            outcome.put(exc)
        else:
            outcome.put(None)

    thread = threading.Thread(
        target=writer,
        daemon=True,
        name=thread_name,
    )
    write_threads.append(thread)
    thread.start()

    pending = object()
    while True:
        try:
            result = outcome.get_nowait()
        except queue.Empty:
            result = pending
        if result is not pending:
            if result is not None:
                raise InteractiveOciError(
                    "attached container stdin closed before controller delivery"
                ) from result
            return
        if cancel_event is not None and cancel_event.is_set():
            raise InteractiveCancelledError(
                "external cancellation occurred during controller delivery"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InteractiveTimeoutError(
                "wall time expired during controller delivery"
            )
        try:
            result = outcome.get(
                timeout=min(poll_interval_ms / 1_000, remaining)
            )
        except queue.Empty:
            continue
        if result is not None:
            raise InteractiveOciError(
                "attached container stdin closed before controller delivery"
            ) from result
        return


@dataclasses.dataclass(frozen=True, slots=True)
class InteractiveCleanupEvidence:
    container_name: str
    remove_attempted: bool
    remove_exit_code: int | None
    remove_argv_sha256: str | None
    confirmed_absent: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class InteractiveOciEvidence:
    container_name: str
    engine_kind: str
    image_digest: str
    request_digest: Mapping[str, str]
    run_argv_sha256: str
    request_written: bool
    accepted_written: bool
    accepted_request_digest_matches: bool
    controller_events: tuple[str, ...]
    termination_actions: tuple[str, ...]
    cancel_grace_period_ms: int | None
    hard_kill_exit_code: int | None
    hard_kill_argv_sha256: str | None
    harness_event_count: int
    stdout_total_bytes: int
    stderr_total_bytes: int
    stderr_sha256: str
    stderr_limit_exceeded: bool
    process_exit_code: int | None
    eof_observed: bool
    pipe_leak_detected: bool
    cleanup: InteractiveCleanupEvidence
    transcript_sha256: str | None
    authority_verified: bool
    started_at: str
    ended_at: str
    duration_ms: int
    error_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INTERACTIVE_EVIDENCE_SCHEMA,
            "mode": "interactive-attached-roundtrip",
            "container_name": self.container_name,
            "engine_kind": self.engine_kind,
            "image_digest": self.image_digest,
            "request_digest": dict(self.request_digest),
            "run_argv_sha256": self.run_argv_sha256,
            "request_written": self.request_written,
            "accepted_written": self.accepted_written,
            "accepted_request_digest_matches": (
                self.accepted_request_digest_matches
            ),
            "controller_events": list(self.controller_events),
            "termination_actions": list(self.termination_actions),
            "cancel_grace_period_ms": self.cancel_grace_period_ms,
            "hard_kill_exit_code": self.hard_kill_exit_code,
            "hard_kill_argv_sha256": self.hard_kill_argv_sha256,
            "harness_event_count": self.harness_event_count,
            "stdout_total_bytes": self.stdout_total_bytes,
            "stderr_total_bytes": self.stderr_total_bytes,
            "stderr_sha256": self.stderr_sha256,
            "stderr_limit_exceeded": self.stderr_limit_exceeded,
            "process_exit_code": self.process_exit_code,
            "eof_observed": self.eof_observed,
            "pipe_leak_detected": self.pipe_leak_detected,
            "cleanup": self.cleanup.to_dict(),
            "transcript_sha256": self.transcript_sha256,
            "authority_verified": self.authority_verified,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class InteractiveOciResult:
    status: InteractiveTransportStatus
    transcript: ProtocolTranscript | None
    evidence: InteractiveOciEvidence
    stdout: bytes
    stderr: bytes
    error: str | None = None

    @property
    def authority_verified(self) -> bool:
        return self.evidence.authority_verified


def _error_code(error: BaseException | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, InteractiveCancelledError):
        return "cancelled"
    if isinstance(error, (InteractiveLimitError, ProtocolLimitError)):
        return "limit_error"
    if isinstance(error, InteractiveTimeoutError):
        return "timed_out"
    if isinstance(error, InteractivePipeLeakError):
        return "pipe_leak"
    if isinstance(error, (InteractiveProtocolError, ProtocolError)):
        return "protocol_error"
    if isinstance(error, InteractiveCleanupError):
        return "cleanup_error"
    return "transport_error"


def _status_for_error(error: BaseException) -> InteractiveTransportStatus:
    if isinstance(error, InteractiveCancelledError):
        return InteractiveTransportStatus.CANCELLED
    if isinstance(error, (InteractiveLimitError, ProtocolLimitError)):
        return InteractiveTransportStatus.LIMIT_ERROR
    if isinstance(error, InteractiveTimeoutError):
        return InteractiveTransportStatus.TIMED_OUT
    if isinstance(error, InteractivePipeLeakError):
        return InteractiveTransportStatus.PIPE_LEAK
    if isinstance(error, (InteractiveProtocolError, ProtocolError)):
        return InteractiveTransportStatus.PROTOCOL_ERROR
    if isinstance(error, InteractiveCleanupError):
        return InteractiveTransportStatus.CLEANUP_ERROR
    return InteractiveTransportStatus.TRANSPORT_ERROR


class InteractiveOciTransport:
    """Run one attached OCI harness with a real controller/harness roundtrip."""

    def __init__(
        self,
        backend: InteractiveOciBackend,
        *,
        limits: InteractiveTransportLimits | None = None,
        timestamp: Callable[[], str] | None = None,
    ) -> None:
        self.backend = backend
        self.limits = limits or InteractiveTransportLimits()
        self.timestamp = timestamp or _MonotonicUtcClock()

    def run(
        self,
        spec: ContainerSpec,
        session: ProtocolSession,
        *,
        controller: InteractiveController | None = None,
        cancel_event: threading.Event | None = None,
        before_cleanup: Callable[[ContainerSpec], None] | None = None,
    ) -> InteractiveOciResult:
        if spec.detached:
            raise ValueError("interactive OCI transport requires attached execution")
        if session.state is not SessionState.EXPECT_HELLO:
            raise ValueError("ProtocolSession must be fresh and expect hello")
        if _CONTAINER_NAME_RE.fullmatch(spec.name) is None:
            raise ValueError("container name is unsafe")

        active_controller = controller or InteractiveController()
        pending_actions: list[_ControllerAction] = []
        process: AttachedProcess | None = None
        stdout_thread: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        stdin_write_threads: list[threading.Thread] = []
        stdout_queue: queue.Queue[Any] = queue.Queue(maxsize=8)
        stdout_reader_stop = threading.Event()
        stderr_counter = _DigestCounter(
            min(self.limits.max_stderr_bytes, spec.resources.stderr_bytes)
        )
        max_stdout = min(
            session.limits.max_total_bytes,
            spec.resources.stdout_bytes,
            self.limits.max_stdout_bytes
            if self.limits.max_stdout_bytes is not None
            else session.limits.max_total_bytes,
        )
        framer = _LineFramer(
            max_line_bytes=session.limits.max_line_bytes,
            max_total_bytes=max_stdout,
        )

        started_mono = time.monotonic()
        wall_deadline = (
            started_mono + (spec.resources.wall_time_ms / 1_000)
        )
        started_at = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        run_argv = build_interactive_run_argv(
            self.backend.executable,
            spec,
        )
        run_argv_sha256 = hashlib.sha256(
            canonical_json_bytes(list(run_argv))
        ).hexdigest()
        request_written = False
        accepted_written = False
        accepted_request_digest_matches = False
        controller_total_bytes = 0
        controller_events: list[str] = []
        controller_action_count = 0
        termination_actions: list[str] = []
        harness_event_count = 0
        stdout_eof = False
        pipe_leak = False
        result_seen_at: float | None = None
        process_exited_at: float | None = None
        cancellation_started = False
        cancellation_deadline: float | None = None
        cancel_grace_period_ms: int | None = None
        hard_kill_sent = False
        hard_kill_outcome: InteractiveCommandOutcome | None = None
        timed_out = False
        externally_cancelled = False
        transcript: ProtocolTranscript | None = None
        error: BaseException | None = None
        before_cleanup_error: BaseException | None = None
        status = InteractiveTransportStatus.TRANSPORT_ERROR
        start_attempted = False
        cleanup = InteractiveCleanupEvidence(
            container_name=spec.name,
            remove_attempted=False,
            remove_exit_code=None,
            remove_argv_sha256=None,
            confirmed_absent=False,
        )

        def send_controller_event(
            event: Mapping[str, Any],
            *,
            interruptible: bool = False,
            delivery_deadline: float | None = None,
        ) -> None:
            nonlocal controller_total_bytes
            assert process is not None
            payload = _encoded_json_line(
                event,
                max_line_bytes=session.limits.max_line_bytes,
            )
            if (
                controller_total_bytes + len(payload)
                > session.limits.max_total_bytes
            ):
                raise InteractiveLimitError(
                    "controller channel exceeds "
                    f"{session.limits.max_total_bytes} bytes"
                )
            _write_json_line(
                process,
                payload,
                deadline=(
                    wall_deadline
                    if delivery_deadline is None
                    else delivery_deadline
                ),
                poll_interval_ms=self.limits.poll_interval_ms,
                cancel_event=cancel_event if interruptible else None,
                write_threads=stdin_write_threads,
                thread_name=(
                    f"{spec.name}-stdin-{len(stdin_write_threads)}"
                ),
            )
            controller_total_bytes += len(payload)

        def begin_cancel(
            *,
            reason_code: str,
            grace_period_ms: int | None,
        ) -> None:
            nonlocal cancellation_started, cancellation_deadline
            nonlocal cancel_grace_period_ms
            if cancellation_started:
                return
            if session.state is not SessionState.ACTIVE:
                raise InteractiveProtocolError(
                    "controller cancellation requires an active protocol session"
                )
            requested_grace = int(
                session.trial_request["cancellation"]["grace_period_ms"]
            )
            grace = requested_grace if grace_period_ms is None else grace_period_ms
            event = session.record_controller_cancel(
                recorded_at=self.timestamp(),
                reason_code=reason_code,
                grace_period_ms=grace,
            )
            send_controller_event(
                event,
                delivery_deadline=max(
                    wall_deadline,
                    time.monotonic()
                    + (self.limits.hard_kill_wait_ms / 1_000),
                ),
            )
            controller_events.append("cancel")
            termination_actions.append("protocol_cancel")
            cancellation_started = True
            cancel_grace_period_ms = grace
            cancellation_deadline = time.monotonic() + (grace / 1_000)

        def apply_controller_actions() -> None:
            nonlocal controller_action_count, externally_cancelled
            drained = active_controller._drain()
            controller_action_count += len(drained)
            pending_actions.extend(drained)
            if controller_action_count > self.limits.max_controller_actions:
                raise InteractiveLimitError("controller action count exceeds limit")
            if session.state not in {SessionState.ACTIVE, SessionState.CANCELLING}:
                return
            while pending_actions:
                action = pending_actions.pop(0)
                if action.kind == "error":
                    assert action.failure is not None
                    event = session.record_controller_error(
                        recorded_at=self.timestamp(),
                        failure=action.failure,
                    )
                    send_controller_event(event)
                    controller_events.append("controller_error")
                    if action.terminate:
                        externally_cancelled = True
                        begin_cancel(
                            reason_code=action.reason_code or "controller-error",
                            grace_period_ms=action.grace_period_ms,
                        )
                elif action.kind == "cancel":
                    externally_cancelled = True
                    begin_cancel(
                        reason_code=action.reason_code or "controller-cancelled",
                        grace_period_ms=action.grace_period_ms,
                    )
                else:
                    raise InteractiveOciError("unknown controller action")

        try:
            start_attempted = True
            process = self.backend.start_attached(spec)
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise InteractiveOciError(
                    "attached OCI client did not expose all three pipes"
                )
            stdout_thread = threading.Thread(
                target=_read_pipe,
                args=(process.stdout, stdout_queue, stdout_reader_stop),
                daemon=True,
                name=f"{spec.name}-stdout",
            )
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(process.stderr, stderr_counter),
                daemon=True,
                name=f"{spec.name}-stderr",
            )
            stderr_thread.start()

            send_controller_event(
                session.trial_request,
                interruptible=True,
            )
            request_written = True
            stdout_thread.start()

            while not stdout_eof:
                now = time.monotonic()
                apply_controller_actions()
                if (
                    cancel_event is not None
                    and cancel_event.is_set()
                    and not cancellation_started
                ):
                    externally_cancelled = True
                    if session.state is SessionState.ACTIVE:
                        begin_cancel(
                            reason_code="external-cancel",
                            grace_period_ms=None,
                        )
                    else:
                        raise InteractiveCancelledError(
                            "external cancellation occurred before protocol acceptance"
                        )
                if now >= wall_deadline and not timed_out:
                    timed_out = True
                    if session.state is SessionState.ACTIVE:
                        begin_cancel(
                            reason_code="wall-time-exceeded",
                            grace_period_ms=None,
                        )
                    elif session.state is not SessionState.TERMINATED:
                        raise InteractiveTimeoutError(
                            "wall time expired before protocol acceptance"
                        )
                if (
                    cancellation_deadline is not None
                    and now >= cancellation_deadline
                    and process.poll() is None
                    and not hard_kill_sent
                ):
                    hard_kill_outcome = self.backend.kill_container(
                        spec.name,
                        signal_name="KILL",
                    )
                    termination_actions.append("hard_kill")
                    hard_kill_sent = True
                    _terminate_client_process(process)

                if stderr_counter.exceeded.is_set():
                    raise InteractiveLimitError(
                        "stderr exceeds the configured byte limit"
                    )

                try:
                    item = stdout_queue.get(
                        timeout=self.limits.poll_interval_ms / 1_000
                    )
                except queue.Empty:
                    item = None

                if item is _EOF:
                    stdout_eof = True
                    framer.finish()
                elif isinstance(item, BaseException):
                    raise InteractiveOciError(
                        "failed while reading attached stdout"
                    ) from item
                elif isinstance(item, bytes):
                    lines = framer.feed(item)
                    for line_index, line in enumerate(lines):
                        if session.state is SessionState.TERMINATED:
                            raise InteractiveProtocolError(
                                "stdout data appeared after terminal result"
                            )
                        try:
                            session.receive_harness_line(
                                line,
                                recorded_at=self.timestamp(),
                            )
                        except ProtocolError:
                            raise
                        except (TypeError, ValueError) as exc:
                            raise InteractiveProtocolError(
                                "harness stdout was not a valid protocol event"
                            ) from exc
                        harness_event_count += 1
                        if session.state is SessionState.WAIT_ACCEPT:
                            if (
                                line_index != len(lines) - 1
                                or framer.buffer
                            ):
                                raise InteractiveProtocolError(
                                    "harness emitted stdout beyond hello before "
                                    "controller acceptance"
                                )
                            accepted = session.record_controller_accept(
                                recorded_at=self.timestamp()
                            )
                            accepted_request_digest_matches = (
                                accepted["request_digest"]
                                == session.request_digest
                            )
                            send_controller_event(
                                accepted,
                                interruptible=True,
                            )
                            accepted_written = True
                            controller_events.append("accepted")
                            apply_controller_actions()
                        if session.state is SessionState.TERMINATED:
                            result_seen_at = time.monotonic()
                            _close_stream(process.stdin)

                exit_code = process.poll()
                if exit_code is not None and not stdout_eof:
                    if process_exited_at is None:
                        process_exited_at = time.monotonic()
                    elif (
                        time.monotonic() - process_exited_at
                        > self.limits.exited_pipe_timeout_ms / 1_000
                    ):
                        pipe_leak = True
                        raise InteractivePipeLeakError(
                            "OCI client exited while stdout remained open"
                        )
                if (
                    result_seen_at is not None
                    and not stdout_eof
                    and time.monotonic() - result_seen_at
                    > self.limits.result_eof_timeout_ms / 1_000
                ):
                    pipe_leak = True
                    raise InteractivePipeLeakError(
                        "harness emitted result without promptly closing stdout"
                    )

            if stderr_counter.exceeded.is_set():
                raise InteractiveLimitError(
                    "stderr exceeds the configured byte limit"
                )
            if session.state is SessionState.TERMINATED:
                transcript = session.finish()
            elif cancellation_started:
                error = InteractiveProtocolError(
                    "cancelled harness reached EOF without a terminal result"
                )
            else:
                raise InteractiveProtocolError(
                    "harness EOF arrived before terminal result"
                )

            try:
                process.wait(
                    timeout=self.limits.hard_kill_wait_ms / 1_000
                )
            except subprocess.TimeoutExpired as exc:
                pipe_leak = True
                raise InteractivePipeLeakError(
                    "attached OCI client remained alive after stdout EOF"
                ) from exc

            if transcript is not None:
                if timed_out:
                    status = InteractiveTransportStatus.TIMED_OUT
                elif externally_cancelled:
                    status = InteractiveTransportStatus.CANCELLED
                elif process.poll() not in (0, None):
                    status = InteractiveTransportStatus.NONZERO_EXIT
                else:
                    status = InteractiveTransportStatus.COMPLETED
            elif timed_out:
                status = InteractiveTransportStatus.TIMED_OUT
            elif externally_cancelled:
                status = InteractiveTransportStatus.CANCELLED
        except Exception as exc:
            error = exc
            status = _status_for_error(exc)
            if process is not None:
                if (
                    accepted_written
                    and session.state
                    in {SessionState.ACTIVE, SessionState.CANCELLING}
                ):
                    try:
                        controller_error = session.record_controller_error(
                            recorded_at=self.timestamp(),
                            failure={
                                "code": "interactive-transport-error",
                                "scope": "runner",
                                "retryable": True,
                                "infrastructure": True,
                            },
                        )
                        send_controller_event(controller_error)
                        controller_events.append("controller_error")
                    except (ProtocolError, InteractiveOciError, OSError, ValueError):
                        pass
                if process.poll() is None and not hard_kill_sent:
                    try:
                        hard_kill_outcome = self.backend.kill_container(
                            spec.name,
                            signal_name="KILL",
                        )
                        termination_actions.append("hard_kill")
                        hard_kill_sent = True
                    except Exception:
                        pass
                _terminate_client_process(process)
        finally:
            stdout_reader_stop.set()
            if process is not None:
                _close_stream(process.stdin)
            remove_outcome: InteractiveCommandOutcome | None = None
            confirmed_absent = False
            if start_attempted:
                if before_cleanup is not None:
                    try:
                        before_cleanup(spec)
                    except Exception as exc:
                        before_cleanup_error = exc
                try:
                    remove_outcome = self.backend.remove_container(
                        spec.name,
                        force=True,
                    )
                    termination_actions.append("force_remove")
                except Exception:
                    pass
                try:
                    confirmed_absent = not self.backend.container_exists(
                        spec.name
                    )
                except Exception:
                    confirmed_absent = False
            cleanup = InteractiveCleanupEvidence(
                container_name=spec.name,
                remove_attempted=start_attempted,
                remove_exit_code=(
                    remove_outcome.exit_code if remove_outcome else None
                ),
                remove_argv_sha256=(
                    remove_outcome.argv_sha256 if remove_outcome else None
                ),
                confirmed_absent=confirmed_absent,
            )

            if process is not None and process.poll() is None:
                _terminate_client_process(process)
            if process is not None:
                _close_stream(process.stdout)
                _close_stream(process.stderr)
            for thread in (
                *stdin_write_threads,
                stdout_thread,
                stderr_thread,
            ):
                if thread is not None and thread.ident is not None:
                    thread.join(
                        timeout=self.limits.exited_pipe_timeout_ms / 1_000
                    )
                    if thread.is_alive():
                        pipe_leak = True
            if stderr_counter.exceeded.is_set() and error is None:
                error = InteractiveLimitError(
                    "stderr exceeds the configured byte limit"
                )
                status = InteractiveTransportStatus.LIMIT_ERROR
            if pipe_leak and error is None:
                error = InteractivePipeLeakError(
                    "attached pipe reader did not terminate"
                )
                status = InteractiveTransportStatus.PIPE_LEAK
            if before_cleanup_error is not None and error is None:
                error = InteractiveOciError(
                    "pre-cleanup container inspection failed: "
                    f"{type(before_cleanup_error).__name__}"
                )
                status = InteractiveTransportStatus.TRANSPORT_ERROR
            if not cleanup.confirmed_absent:
                cleanup_error = InteractiveCleanupError(
                    "exact harness container removal was not verified"
                )
                if error is None:
                    error = cleanup_error
                status = InteractiveTransportStatus.CLEANUP_ERROR

        stderr_total, stderr_sha256, stderr_exceeded = stderr_counter.snapshot()
        stdout_capture = bytes(framer.capture)
        stderr_capture = stderr_counter.capture_bytes()
        ended_at = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        transcript_bytes = (
            canonical_jsonl(transcript.events) if transcript is not None else None
        )
        authority_verified = bool(
            transcript is not None
            and transcript.authority_verified
            and has_session_authority(transcript)
            and request_written
            and accepted_written
            and accepted_request_digest_matches
            and stdout_eof
            and not pipe_leak
            and not stderr_exceeded
            and cleanup.confirmed_absent
        )
        evidence = InteractiveOciEvidence(
            container_name=spec.name,
            engine_kind=self.backend.kind,
            image_digest=spec.image.digest,
            request_digest=dict(session.request_digest),
            run_argv_sha256=run_argv_sha256,
            request_written=request_written,
            accepted_written=accepted_written,
            accepted_request_digest_matches=accepted_request_digest_matches,
            controller_events=tuple(controller_events),
            termination_actions=tuple(termination_actions),
            cancel_grace_period_ms=cancel_grace_period_ms,
            hard_kill_exit_code=(
                hard_kill_outcome.exit_code if hard_kill_outcome else None
            ),
            hard_kill_argv_sha256=(
                hard_kill_outcome.argv_sha256 if hard_kill_outcome else None
            ),
            harness_event_count=harness_event_count,
            stdout_total_bytes=framer.total,
            stderr_total_bytes=stderr_total,
            stderr_sha256=stderr_sha256,
            stderr_limit_exceeded=stderr_exceeded,
            process_exit_code=process.poll() if process is not None else None,
            eof_observed=stdout_eof,
            pipe_leak_detected=pipe_leak,
            cleanup=cleanup,
            transcript_sha256=(
                hashlib.sha256(transcript_bytes).hexdigest()
                if transcript_bytes is not None
                else None
            ),
            authority_verified=authority_verified,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=max(0, int((time.monotonic() - started_mono) * 1_000)),
            error_code=_error_code(error),
        )
        return InteractiveOciResult(
            status=status,
            transcript=transcript,
            evidence=evidence,
            stdout=stdout_capture,
            stderr=stderr_capture,
            error=str(error) if error is not None else None,
        )
