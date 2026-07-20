"""Local harness adapter contract and hardened process runtime.

This module is deliberately a *local, unverified* execution layer.  It provides
safe defaults for development runs:

* a default-deny inherited environment with an explicit allowlist;
* shell-free process launch and bounded stdout/stderr/log capture;
* full process-tree cleanup on timeout, cancellation, and normal completion;
* committed/staged/unstaged/untracked repository capture; and
* typed controller observations that keep harness-reported model/usage data
  explicitly unverified.

Official benchmark claims still require an isolated runner, model gateway, and
signed attestations.  Nothing emitted by this local adapter becomes verified
merely because a harness printed it.
"""
from __future__ import annotations

import ctypes
import dataclasses
import enum
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from atv_bench.adapters.snapshot import (
    DiffLimitExceeded,
    SnapshotRejected,
    capture_diff,
    seed_base,
)

MAX_STDOUT_BYTES = 512 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_LOG_BYTES = 128 * 1024
TERMINATION_GRACE_SECONDS = 1.0

# These are operational variables rather than credentials.  Everything else is
# denied unless the caller explicitly names it in AdapterRequest.env_allowlist.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
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
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AdapterStatus(str, enum.Enum):
    OK = "ok"
    NO_EDIT = "no_edit"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_DENIED = "policy_denied"


class EvidenceSource(str, enum.Enum):
    UNAVAILABLE = "unavailable"
    HARNESS_REPORTED = "harness_reported"
    CONTROLLER_OBSERVED = "controller_observed"
    GATEWAY_ATTESTED = "gateway_attested"


class TerminationReason(str, enum.Enum):
    EXITED = "exited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class CleanupStatus(str, enum.Enum):
    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class ProcessTreeCleanupResult:
    status: CleanupStatus = CleanupStatus.NOT_ATTEMPTED
    error: str | None = None

    @property
    def attempted(self) -> bool:
        return self.status is not CleanupStatus.NOT_ATTEMPTED

    @property
    def succeeded(self) -> bool | None:
        if self.status is CleanupStatus.NOT_ATTEMPTED:
            return None
        return self.status is CleanupStatus.SUCCEEDED


@dataclasses.dataclass(frozen=True)
class Budget:
    max_turns: int = 10
    max_seconds: int = 300
    max_tokens: int = 200_000

    def __post_init__(self) -> None:
        if self.max_turns < 0 or self.max_seconds < 0 or self.max_tokens < 0:
            raise ValueError("budget values must be non-negative")

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AdapterRequest:
    repo_path: str
    goal: str
    model: str = "auto"
    budget: Budget = dataclasses.field(default_factory=Budget)
    bot_file: str = "bot.py"
    env_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "env_allowlist", tuple(self.env_allowlist))
        for name in self.env_allowlist:
            _validate_env_name(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "goal": self.goal,
            "model": self.model,
            "budget": self.budget.to_dict(),
            "bot_file": self.bot_file,
            "env_allowlist": list(self.env_allowlist),
        }


@dataclasses.dataclass(frozen=True)
class Usage:
    tokens: int = 0
    seconds: float = 0.0
    turns: int = 0
    source: EvidenceSource = EvidenceSource.HARNESS_REPORTED
    verified: bool = False

    def __post_init__(self) -> None:
        if self.tokens < 0 or self.seconds < 0 or self.turns < 0:
            raise ValueError("usage values must be non-negative")
        if self.verified and self.source is not EvidenceSource.GATEWAY_ATTESTED:
            raise ValueError("only gateway-attested usage may be marked verified")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "seconds": self.seconds,
            "turns": self.turns,
            "source": self.source.value,
            "verified": self.verified,
        }


@dataclasses.dataclass(frozen=True)
class RuntimeObservation:
    """Facts observed by the controller rather than claimed by the harness."""

    process_id: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    termination_reason: TerminationReason = TerminationReason.EXITED
    timed_out: bool = False
    cancelled: bool = False
    duration_seconds: float = 0.0
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    environment_keys: tuple[str, ...] = ()
    process_tree_cleanup_attempted: bool = False
    process_tree_cleanup_status: CleanupStatus = CleanupStatus.NOT_ATTEMPTED
    process_tree_cleanup_error: str | None = None

    @property
    def process_tree_cleanup_succeeded(self) -> bool | None:
        if self.process_tree_cleanup_status is CleanupStatus.NOT_ATTEMPTED:
            return None
        return self.process_tree_cleanup_status is CleanupStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "termination_reason": self.termination_reason.value,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "duration_seconds": self.duration_seconds,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "environment_keys": list(self.environment_keys),
            "process_tree_cleanup_attempted": self.process_tree_cleanup_attempted,
            "process_tree_cleanup_succeeded": self.process_tree_cleanup_succeeded,
            "process_tree_cleanup_status": self.process_tree_cleanup_status.value,
            "process_tree_cleanup_error": self.process_tree_cleanup_error,
            "source": EvidenceSource.CONTROLLER_OBSERVED.value,
        }


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    runtime: RuntimeObservation


@dataclasses.dataclass(frozen=True)
class AdapterResult:
    status: AdapterStatus
    diff: str
    log: str
    usage: Usage
    model: str = "unknown"
    model_source: EvidenceSource = EvidenceSource.HARNESS_REPORTED
    model_verified: bool = False
    runtime: RuntimeObservation = dataclasses.field(default_factory=RuntimeObservation)

    def __post_init__(self) -> None:
        if self.model_verified and self.model_source is not EvidenceSource.GATEWAY_ATTESTED:
            raise ValueError("only a gateway-attested model may be marked verified")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "diff": self.diff,
            "log": self.log,
            "usage": self.usage.to_dict(),
            "model": self.model,
            "model_source": self.model_source.value,
            "model_verified": self.model_verified,
            "runtime": self.runtime.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _validate_env_name(name: str) -> None:
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        raise ValueError(f"invalid environment variable name: {name!r}")


def _bounded_capture_limit(value: int, maximum: int, *, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return min(value, maximum)


def build_child_environment(
    allowlist: Sequence[str] = (),
    *,
    source: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment from an explicit inherited-key allowlist.

    ``overrides`` are controller-authored values (for example ATV request
    metadata), not inherited ambient variables.
    """

    source = os.environ if source is None else source
    requested = list(DEFAULT_ENV_ALLOWLIST)
    requested.extend(allowlist)
    for name in requested:
        _validate_env_name(name)

    # Windows environment names are case-insensitive.  Keep the source spelling
    # but match requested keys case-insensitively on every platform.
    wanted = {name.upper() for name in requested}
    result: dict[str, str] = {}
    for key, value in source.items():
        if key.upper() not in wanted:
            continue
        if "\x00" in key or "\x00" in value:
            raise ValueError(f"NUL is not allowed in environment variable {key!r}")
        result[key] = value

    for key, value in (overrides or {}).items():
        _validate_env_name(key)
        value = str(value)
        if "\x00" in value:
            raise ValueError(f"NUL is not allowed in environment variable {key!r}")
        result[key] = value
    return result


class _TailBuffer:
    """A bounded byte ring that keeps the diagnostically useful tail."""

    def __init__(self, limit: int) -> None:
        if limit < 0:
            raise ValueError("capture limits must be non-negative")
        self.limit = limit
        self.data = bytearray()
        self.total = 0
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.limit == 0:
            if chunk:
                self.truncated = True
            return
        if len(chunk) >= self.limit:
            self.data[:] = chunk[-self.limit :]
            self.truncated = self.total > self.limit
            return
        overflow = len(self.data) + len(chunk) - self.limit
        if overflow > 0:
            del self.data[:overflow]
            self.truncated = True
        self.data.extend(chunk)
        if self.total > self.limit:
            self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


def _drain_pipe(stream, sink: _TailBuffer) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            sink.feed(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _write_stdin(stream, data: bytes) -> None:
    try:
        stream.write(data)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


class _WindowsJob:
    """Best-effort Job Object used to terminate a Windows process tree."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.handle = None
        if os.name != "nt":
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.TerminateJobObject.restype = ctypes.c_int
            kernel32.QueryInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_void_p,
            ]
            kernel32.QueryInformationJobObject.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.CreateJobObjectW(None, None)
            if handle and kernel32.AssignProcessToJobObject(
                handle, ctypes.c_void_p(int(proc._handle))  # type: ignore[attr-defined]
            ):
                self.handle = handle
                self._kernel32 = kernel32
            elif handle:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            self.handle = None

    def terminate(self) -> bool:
        if self.handle is None:
            return False
        return bool(self._kernel32.TerminateJobObject(self.handle, 1))

    def active_processes(self) -> int | None:
        if self.handle is None:
            return None

        class BasicAccounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", ctypes.c_uint),
                ("TotalProcesses", ctypes.c_uint),
                ("ActiveProcesses", ctypes.c_uint),
                ("TotalTerminatedProcesses", ctypes.c_uint),
            ]

        info = BasicAccounting()
        ok = self._kernel32.QueryInformationJobObject(
            self.handle,
            1,  # JobObjectBasicAccountingInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        return int(info.ActiveProcesses) if ok else None

    def terminate_and_confirm(self, timeout: float = 1.0) -> bool:
        if not self.terminate():
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            active = self.active_processes()
            if active == 0:
                return True
            if active is None or time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _resume_windows_process(proc: subprocess.Popen[bytes]) -> None:
    """Resume a CREATE_SUSPENDED process after it is inside its Job Object."""

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(
        ctypes.c_void_p(int(proc._handle))  # type: ignore[attr-defined]
    )
    if status != 0:
        raise OSError(f"NtResumeProcess failed with status 0x{status & 0xFFFFFFFF:08x}")


def _taskkill_tree(pid: int) -> bool:
    executable = shutil.which("taskkill")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env=build_child_environment(),
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _cleanup_success() -> ProcessTreeCleanupResult:
    return ProcessTreeCleanupResult(CleanupStatus.SUCCEEDED)


def _cleanup_failure(error: str) -> ProcessTreeCleanupResult:
    return ProcessTreeCleanupResult(CleanupStatus.FAILED, error)


def _posix_process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _wait_for_posix_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _posix_process_group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _terminate_process_tree(
    proc: subprocess.Popen[bytes],
    *,
    job: _WindowsJob | None,
    grace_seconds: float,
) -> ProcessTreeCleanupResult:
    """Terminate the process and every descendant in its isolated group/job."""

    if os.name == "nt":
        if job and job.handle is not None:
            if job.terminate_and_confirm(timeout=max(1.0, grace_seconds)):
                try:
                    proc.wait(timeout=max(1.0, grace_seconds))
                except subprocess.TimeoutExpired:
                    return _cleanup_failure(
                        "Windows Job Object emptied but parent did not report exit"
                    )
                return _cleanup_success()
        if _taskkill_tree(proc.pid):
            try:
                proc.wait(timeout=max(1.0, grace_seconds))
            except subprocess.TimeoutExpired:
                return _cleanup_failure("taskkill returned success but parent remained alive")
            return _cleanup_success()
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=max(1.0, grace_seconds))
            except subprocess.TimeoutExpired:
                pass
        return _cleanup_failure(
            "full Windows process-tree termination could not be confirmed"
        )

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return _cleanup_success()
    except OSError:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return _cleanup_failure("could not signal the POSIX process group")
    try:
        proc.wait(timeout=max(0.0, grace_seconds))
    except subprocess.TimeoutExpired:
        pass
    if _posix_process_group_exists(proc.pid):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return _cleanup_failure("could not force-kill the POSIX process group")
    try:
        proc.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    if not _wait_for_posix_group_exit(proc.pid, max(1.0, grace_seconds)):
        return _cleanup_failure("POSIX process group remained alive after termination")
    return _cleanup_success()


def _cleanup_descendants_after_exit(
    proc: subprocess.Popen[bytes],
    *,
    job: _WindowsJob | None,
) -> ProcessTreeCleanupResult:
    """Kill and confirm descendants that outlived a normally exiting parent."""

    if os.name == "nt":
        if not job or job.handle is None:
            return ProcessTreeCleanupResult()
        active = job.active_processes()
        if active == 0:
            return ProcessTreeCleanupResult()
        if active is None:
            return _cleanup_failure("could not inspect Windows Job Object state")
        if job.terminate_and_confirm():
            return _cleanup_success()
        return _cleanup_failure("orphaned Windows job processes could not be terminated")

    if not _posix_process_group_exists(proc.pid):
        return ProcessTreeCleanupResult()
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return _cleanup_success()
    except OSError:
        return _cleanup_failure("could not signal orphaned POSIX descendants")
    if not _wait_for_posix_group_exit(proc.pid, 0.1):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return _cleanup_failure("could not force-kill orphaned POSIX descendants")
    if not _wait_for_posix_group_exit(proc.pid, 1.0):
        return _cleanup_failure("orphaned POSIX descendants remained alive")
    return _cleanup_success()


def _resolve_executable(argv: Sequence[str], source_env: Mapping[str, str]) -> list[str]:
    if not argv:
        raise ValueError("process argv may not be empty")
    command = [str(part) for part in argv]
    if any("\x00" in part for part in command):
        raise ValueError("process argv may not contain NUL")
    executable = command[0]
    has_path = os.path.isabs(executable) or Path(executable).parent != Path(".")
    if not has_path:
        resolved = shutil.which(executable, path=source_env.get("PATH"))
        if resolved is None:
            raise FileNotFoundError(f"executable not found: {executable}")
        command[0] = resolved
    return command


def run_process(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout_seconds: float,
    env_allowlist: Sequence[str] = (),
    env_overrides: Mapping[str, str] | None = None,
    env_source: Mapping[str, str] | None = None,
    stdin_data: bytes | None = None,
    cancel_event: threading.Event | None = None,
    max_stdout_bytes: int = MAX_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_STDERR_BYTES,
    termination_grace_seconds: float = TERMINATION_GRACE_SECONDS,
) -> ProcessResult:
    """Run one command with bounded streams and process-tree lifecycle control."""

    if timeout_seconds < 0 or termination_grace_seconds < 0:
        raise ValueError("timeouts must be non-negative")
    max_stdout_bytes = _bounded_capture_limit(
        max_stdout_bytes, MAX_STDOUT_BYTES, name="max_stdout_bytes"
    )
    max_stderr_bytes = _bounded_capture_limit(
        max_stderr_bytes, MAX_STDERR_BYTES, name="max_stderr_bytes"
    )
    source_env = os.environ if env_source is None else env_source
    command = _resolve_executable(argv, source_env)
    child_env = build_child_environment(
        env_allowlist, source=source_env, overrides=env_overrides
    )

    stdout_sink = _TailBuffer(max_stdout_bytes)
    stderr_sink = _TailBuffer(max_stderr_bytes)
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
    else:
        start_new_session = True

    start = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=os.fspath(cwd),
        env=child_env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    job = _WindowsJob(proc) if os.name == "nt" else None
    if os.name == "nt":
        try:
            _resume_windows_process(proc)
        except OSError:
            if job is not None:
                job.terminate()
                job.close()
            else:
                proc.kill()
            proc.wait(timeout=1.0)
            raise
    assert proc.stdout is not None and proc.stderr is not None
    readers = [
        threading.Thread(target=_drain_pipe, args=(proc.stdout, stdout_sink), daemon=True),
        threading.Thread(target=_drain_pipe, args=(proc.stderr, stderr_sink), daemon=True),
    ]
    for reader in readers:
        reader.start()
    writer: threading.Thread | None = None
    if stdin_data is not None:
        assert proc.stdin is not None
        writer = threading.Thread(
            target=_write_stdin, args=(proc.stdin, stdin_data), daemon=True
        )
        writer.start()

    timed_out = False
    cancelled = False
    cleanup = ProcessTreeCleanupResult()
    deadline = start + timeout_seconds
    try:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                cleanup = _terminate_process_tree(
                    proc, job=job, grace_seconds=termination_grace_seconds
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                cleanup = _terminate_process_tree(
                    proc, job=job, grace_seconds=termination_grace_seconds
                )
                break
            try:
                proc.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue

        if not timed_out and not cancelled:
            cleanup = _cleanup_descendants_after_exit(proc, job=job)
    finally:
        if writer is not None:
            writer.join(timeout=1.0)
        for reader in readers:
            reader.join(timeout=5.0)
        # A descendant retaining a pipe handle must not keep capture threads
        # alive indefinitely. Tree cleanup above should normally close them.
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for reader in readers:
            reader.join(timeout=0.2)
        if any(reader.is_alive() for reader in readers):
            stdout_sink.truncated = True
            stderr_sink.truncated = True
            cleanup = _cleanup_failure(
                "process output pipes remained open after process-tree cleanup"
            )
        if job is not None:
            job.close()

    returncode = proc.poll()
    termination = (
        TerminationReason.CANCELLED
        if cancelled
        else TerminationReason.TIMEOUT
        if timed_out
        else TerminationReason.EXITED
    )
    observed_signal = (
        -returncode
        if os.name != "nt" and isinstance(returncode, int) and returncode < 0
        else None
    )
    runtime = RuntimeObservation(
        process_id=proc.pid,
        exit_code=returncode,
        signal=observed_signal,
        termination_reason=termination,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_seconds=max(0.0, time.monotonic() - start),
        stdout_bytes=stdout_sink.total,
        stderr_bytes=stderr_sink.total,
        stdout_truncated=stdout_sink.truncated,
        stderr_truncated=stderr_sink.truncated,
        environment_keys=tuple(sorted(child_env)),
        process_tree_cleanup_attempted=cleanup.attempted,
        process_tree_cleanup_status=cleanup.status,
        process_tree_cleanup_error=cleanup.error,
    )
    return ProcessResult(stdout_sink.text(), stderr_sink.text(), runtime)


def git_base(repo_path: str) -> str | None:
    """Return and pin HEAD before a harness starts editing."""

    try:
        return seed_base(Path(repo_path))
    except (OSError, SnapshotRejected):
        return None


def capture_repo_diff(
    repo_path: str,
    base: str | None,
    *,
    max_bytes: int | None = None,
) -> str:
    """Capture every safe repository change since ``base``."""

    if base is None:
        base = git_base(repo_path)
    if base is None:
        return ""
    kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
    return capture_diff(Path(repo_path), base, **kwargs)


def git_diff(repo_path: str) -> str:
    """Compatibility wrapper: capture working-tree and untracked changes vs HEAD."""

    return capture_repo_diff(repo_path, git_base(repo_path))


def parse_copilot_model(jsonl: str) -> str:
    """Parse a model label reported by Copilot output.

    This is useful metadata, but it remains ``harness_reported`` and unverified.
    """

    message_model: str | None = None
    checkpoint_model: str | None = None
    for line in jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event.get("type") == "assistant.message":
            model = data.get("model")
            if isinstance(model, str) and model and model != "auto":
                message_model = model
        elif event.get("type") == "session.usage_checkpoint":
            state = data.get("modelCacheState")
            if isinstance(state, list) and state and isinstance(state[0], dict):
                model = state[0].get("modelId")
                if isinstance(model, str) and model and model != "auto":
                    checkpoint_model = model
    return message_model or checkpoint_model or "unknown"


def _last_json_object(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _usage_from_response(response: dict[str, Any], *, seconds: float) -> Usage:
    raw = response.get("usage")
    if not isinstance(raw, dict):
        return Usage(
            seconds=seconds,
            source=EvidenceSource.UNAVAILABLE,
            verified=False,
        )
    try:
        tokens = max(0, int(raw.get("tokens", 0)))
    except (TypeError, ValueError):
        tokens = 0
    try:
        turns = max(0, int(raw.get("turns", 0)))
    except (TypeError, ValueError):
        turns = 0
    return Usage(
        tokens=tokens,
        seconds=seconds,
        turns=turns,
        source=EvidenceSource.HARNESS_REPORTED,
        verified=False,
    )


def _bounded_log(*parts: str, limit: int = MAX_LOG_BYTES) -> str:
    limit = _bounded_capture_limit(limit, MAX_LOG_BYTES, name="log_limit")
    encoded = "\n".join(part for part in parts if part).encode("utf-8", errors="replace")
    if limit <= 0:
        return ""
    return encoded[-limit:].decode("utf-8", errors="replace")


def _capture_after_run(repo: str, base: str | None) -> tuple[str, str | None]:
    try:
        return capture_repo_diff(repo, base), None
    except (SnapshotRejected, DiffLimitExceeded, OSError) as exc:
        return "", f"repository capture rejected: {exc}"


def _runtime_terminal_status(runtime: RuntimeObservation) -> AdapterStatus | None:
    if runtime.process_tree_cleanup_status is CleanupStatus.FAILED:
        return AdapterStatus.CLEANUP_FAILED
    if runtime.cancelled:
        return AdapterStatus.CANCELLED
    if runtime.timed_out:
        return AdapterStatus.TIMEOUT
    if runtime.exit_code != 0:
        return AdapterStatus.ERROR
    return None


class HarnessAdapter:
    """Base adapter for a local, self-attested harness invocation."""

    name: str = "base"

    def run(
        self,
        req: AdapterRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AdapterResult:
        raise NotImplementedError

    @staticmethod
    def available() -> bool:
        return False


class ClaudeCodeAdapter(HarnessAdapter):
    """Drive Claude Code headlessly with explicitly allowed environment keys."""

    name = "claude-code"

    @staticmethod
    def available() -> bool:
        return shutil.which("claude") is not None

    def run(
        self,
        req: AdapterRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AdapterResult:
        base = git_base(req.repo_path)
        if base is None:
            return AdapterResult(
                AdapterStatus.ERROR,
                "",
                "repository has no readable HEAD",
                Usage(source=EvidenceSource.UNAVAILABLE),
            )
        command = [
            "claude",
            "-p",
            req.goal,
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "json",
        ]
        if req.model and req.model != "auto":
            command += ["--model", req.model]
        try:
            process = run_process(
                command,
                cwd=req.repo_path,
                timeout_seconds=req.budget.max_seconds,
                env_allowlist=req.env_allowlist,
                cancel_event=cancel_event,
            )
        except (OSError, ValueError) as exc:
            return AdapterResult(
                AdapterStatus.ERROR,
                "",
                f"could not execute claude: {exc}",
                Usage(source=EvidenceSource.UNAVAILABLE),
            )

        diff, capture_error = _capture_after_run(req.repo_path, base)
        model = "unknown"
        tokens = 0
        try:
            payload = json.loads(process.stdout)
            model_usage = payload.get("modelUsage") or {}
            if model_usage:
                model = next(iter(model_usage))
                stats = model_usage[model]
                tokens = int(stats.get("inputTokens", 0)) + int(
                    stats.get("outputTokens", 0)
                )
        except (json.JSONDecodeError, TypeError, ValueError, StopIteration):
            pass
        usage = Usage(
            tokens=max(0, tokens),
            seconds=process.runtime.duration_seconds,
            turns=1 if process.runtime.exit_code == 0 else 0,
            source=EvidenceSource.HARNESS_REPORTED,
            verified=False,
        )
        runtime_status = _runtime_terminal_status(process.runtime)
        if runtime_status is not None:
            status = runtime_status
        elif capture_error:
            status = AdapterStatus.ERROR
        else:
            status = AdapterStatus.OK if diff.strip() else AdapterStatus.NO_EDIT
        return AdapterResult(
            status=status,
            diff=diff,
            log=_bounded_log(process.stdout, process.stderr, capture_error or ""),
            usage=usage,
            model=model,
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=False,
            runtime=process.runtime,
        )


class CopilotCliAdapter(HarnessAdapter):
    """Drive GitHub Copilot CLI without inheriting the ambient environment."""

    name = "copilot-cli"

    @staticmethod
    def available() -> bool:
        return shutil.which("copilot") is not None

    def run(
        self,
        req: AdapterRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AdapterResult:
        base = git_base(req.repo_path)
        if base is None:
            return AdapterResult(
                AdapterStatus.ERROR,
                "",
                "repository has no readable HEAD",
                Usage(source=EvidenceSource.UNAVAILABLE),
            )
        command = [
            "copilot",
            "-p",
            req.goal,
            "--allow-all-tools",
            "--no-ask-user",
            "--output-format",
            "json",
        ]
        if req.model and req.model != "auto":
            command += ["--model", req.model]
        try:
            process = run_process(
                command,
                cwd=req.repo_path,
                timeout_seconds=req.budget.max_seconds,
                env_allowlist=req.env_allowlist,
                cancel_event=cancel_event,
            )
        except (OSError, ValueError) as exc:
            return AdapterResult(
                AdapterStatus.ERROR,
                "",
                f"could not execute copilot: {exc}",
                Usage(source=EvidenceSource.UNAVAILABLE),
            )

        diff, capture_error = _capture_after_run(req.repo_path, base)
        combined = "\n".join(part for part in (process.stdout, process.stderr) if part)
        model = parse_copilot_model(process.stdout)
        usage = Usage(
            seconds=process.runtime.duration_seconds,
            turns=1 if process.runtime.exit_code == 0 else 0,
            source=EvidenceSource.HARNESS_REPORTED,
            verified=False,
        )
        runtime_status = _runtime_terminal_status(process.runtime)
        if runtime_status is not None:
            status = runtime_status
        elif capture_error:
            status = AdapterStatus.ERROR
        elif "Access denied by policy" in combined:
            status = AdapterStatus.POLICY_DENIED
        else:
            status = AdapterStatus.OK if diff.strip() else AdapterStatus.NO_EDIT
        return AdapterResult(
            status=status,
            diff=diff,
            log=_bounded_log(combined, capture_error or ""),
            usage=usage,
            model=model,
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=False,
            runtime=process.runtime,
        )


class CommandHarnessAdapter(HarnessAdapter):
    """Run an arbitrary command through the hardened local adapter contract."""

    name = "command"

    def __init__(
        self,
        command: Sequence[str],
        *,
        pass_request_on_stdin: bool = False,
        extra_env: Mapping[str, str] | None = None,
        env_allowlist: Sequence[str] = (),
        log_limit: int = MAX_LOG_BYTES,
        max_stdout_bytes: int = MAX_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
    ) -> None:
        if not command:
            raise ValueError("command adapter requires at least one argv element")
        self.command = tuple(str(part) for part in command)
        self.pass_request_on_stdin = pass_request_on_stdin
        self.extra_env = dict(extra_env or {})
        self.env_allowlist = tuple(env_allowlist)
        for name in self.env_allowlist:
            _validate_env_name(name)
        self.log_limit = _bounded_capture_limit(
            log_limit, MAX_LOG_BYTES, name="log_limit"
        )
        self.max_stdout_bytes = _bounded_capture_limit(
            max_stdout_bytes, MAX_STDOUT_BYTES, name="max_stdout_bytes"
        )
        self.max_stderr_bytes = _bounded_capture_limit(
            max_stderr_bytes, MAX_STDERR_BYTES, name="max_stderr_bytes"
        )

    def available(self) -> bool:
        executable = self.command[0]
        if os.path.isabs(executable) or Path(executable).parent != Path("."):
            return Path(executable).is_file()
        return shutil.which(executable) is not None

    def run(
        self,
        req: AdapterRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AdapterResult:
        repo = Path(req.repo_path)
        if not repo.is_dir():
            return AdapterResult(
                AdapterStatus.ERROR,
                "",
                f"repository directory not found: {repo}",
                Usage(source=EvidenceSource.UNAVAILABLE),
            )
        base = git_base(req.repo_path)
        if base is None:
            return AdapterResult(
                AdapterStatus.ERROR,
                "",
                f"repository has no readable HEAD: {repo}",
                Usage(source=EvidenceSource.UNAVAILABLE),
            )

        request_json = json.dumps(req.to_dict(), sort_keys=True)
        request_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="atv-bench-request-",
            delete=False,
        )
        process: ProcessResult | None = None
        try:
            request_file.write(request_json)
            request_file.close()
            replacements = {
                "{goal}": req.goal,
                "{repo}": str(repo.resolve()),
                "{bot_file}": req.bot_file,
                "{model}": req.model,
                "{request_path}": request_file.name,
            }
            command: list[str] = []
            for part in self.command:
                expanded = part
                for token, value in replacements.items():
                    expanded = expanded.replace(token, value)
                command.append(expanded)
            metadata_env = {
                **self.extra_env,
                "ATV_BENCH_REQUEST_JSON": request_json,
                "ATV_BENCH_REQUEST_PATH": request_file.name,
                "ATV_BENCH_GOAL": req.goal,
                "ATV_BENCH_REPO": str(repo.resolve()),
                "ATV_BENCH_BOT_FILE": req.bot_file,
                "ATV_BENCH_MODEL": req.model,
            }
            process = run_process(
                command,
                cwd=repo,
                timeout_seconds=req.budget.max_seconds,
                env_allowlist=(*self.env_allowlist, *req.env_allowlist),
                env_overrides=metadata_env,
                stdin_data=(request_json + "\n").encode("utf-8")
                if self.pass_request_on_stdin
                else None,
                cancel_event=cancel_event,
                max_stdout_bytes=self.max_stdout_bytes,
                max_stderr_bytes=self.max_stderr_bytes,
            )
        except (OSError, ValueError) as exc:
            diff, capture_error = _capture_after_run(req.repo_path, base)
            return AdapterResult(
                AdapterStatus.ERROR,
                diff,
                _bounded_log(f"could not execute harness command: {exc}", capture_error or ""),
                Usage(source=EvidenceSource.UNAVAILABLE),
            )
        finally:
            try:
                Path(request_file.name).unlink(missing_ok=True)
            except OSError:
                pass

        assert process is not None
        diff, capture_error = _capture_after_run(req.repo_path, base)
        response = _last_json_object(process.stdout)
        model = response.get("model")
        if not isinstance(model, str) or not model.strip() or model == "auto":
            model = "unknown"
        usage = _usage_from_response(
            response, seconds=process.runtime.duration_seconds
        )

        runtime_status = _runtime_terminal_status(process.runtime)
        if runtime_status is not None:
            status = runtime_status
        elif capture_error:
            status = AdapterStatus.ERROR
        else:
            raw_status = response.get("status")
            try:
                reported = (
                    AdapterStatus(raw_status) if isinstance(raw_status, str) else None
                )
            except ValueError:
                reported = None
            if reported in {
                AdapterStatus.BUDGET_EXHAUSTED,
                AdapterStatus.POLICY_DENIED,
                AdapterStatus.CANCELLED,
                AdapterStatus.TIMEOUT,
                AdapterStatus.ERROR,
            }:
                status = reported
            else:
                status = AdapterStatus.OK if diff.strip() else AdapterStatus.NO_EDIT

        log = _bounded_log(
            process.stdout,
            process.stderr,
            capture_error or "",
            limit=self.log_limit,
        )
        return AdapterResult(
            status=status,
            diff=diff,
            log=log,
            usage=usage,
            model=model,
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=False,
            runtime=process.runtime,
        )


ADAPTERS: dict[str, type[HarnessAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CopilotCliAdapter.name: CopilotCliAdapter,
}
