"""Local, evidence-producing ATV League match execution.

GitHub Actions is deliberately limited to ordinary tests and static Pages deployment.
This module is the explicit operator-side scoring path: it stages the exact submitted
bot bytes, builds the packaged trusted arena into a temporary Docker image, executes the
bot under a locked-down policy, binds the adjudicated result to a trusted ``MatchSpec``,
and writes a content-addressed evidence bundle.

The implementation is intentionally independent from the harness benchmark protocol.
League scoring ranks a frozen bot artifact; it does not execute or compare harnesses.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from atv_bench import __version__
from atv_bench.elo import ANCHOR_IDENTITY


ARENA_BASE_IMAGE = (
    "python:3.12-slim-bookworm"
    "@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
)
ARENA_OPPONENT = ANCHOR_IDENTITY
MAX_BOT_BYTES = 256 * 1024
BUILD_TIMEOUT_SECONDS = 600.0
RUN_TIMEOUT_SECONDS = 60.0
CONTROL_TIMEOUT_SECONDS = 30.0
BUILD_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
RUN_OUTPUT_LIMIT_BYTES = 256 * 1024
CONTROL_OUTPUT_LIMIT_BYTES = 256 * 1024
TMPFS_BYTES = 16 * 1024 * 1024

_IDENTITY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_MATCH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ENGINE_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "CONTAINER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
    "XDG_RUNTIME_DIR",
}


class LeagueExecutorError(RuntimeError):
    """A local League match could not be executed or verified."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded result of one shell-free container-engine command."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    stdout_total_bytes: int
    stderr_total_bytes: int
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str
    timed_out: bool = False
    output_limit_exceeded: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.output_limit_exceeded
        )


class CommandEngine(Protocol):
    """Injectable command engine used by hermetic tests and the Docker CLI path."""

    executable: str

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class ArenaContext:
    """Digest inventory for a generated installed-package Docker context."""

    root: Path
    arena_source_sha256: str
    context_sha256: str
    files: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class LeagueScoreReceipt:
    """Operator-facing receipt for one verified local League execution."""

    bundle_dir: Path
    bundle_sha256: str
    bot_sha256: str
    result: Mapping[str, Any]
    ingested: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_dir": str(self.bundle_dir),
            "bundle_sha256": self.bundle_sha256,
            "bot_sha256": self.bot_sha256,
            "result": dict(self.result),
            "ingested": self.ingested,
        }


class _OutputBudget:
    """Thread-safe aggregate stdout+stderr storage and byte counter."""

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("output limit must be positive")
        self.limit = limit
        self.total = 0
        self.stream_totals = {"stdout": 0, "stderr": 0}
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.hashers = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
        self.exceeded = threading.Event()
        self._lock = threading.Lock()

    def feed(self, stream_name: str, chunk: bytes) -> None:
        with self._lock:
            prior = self.total
            self.total += len(chunk)
            self.stream_totals[stream_name] += len(chunk)
            self.hashers[stream_name].update(chunk)
            remaining = max(0, self.limit - prior)
            if remaining:
                self.buffers[stream_name].extend(chunk[:remaining])
            if self.total > self.limit:
                self.exceeded.set()


def _drain_pipe(stream, budget: _OutputBudget, stream_name: str) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            budget.feed(stream_name, chunk)
    finally:
        stream.close()


def _engine_environment() -> dict[str, str]:
    """Pass only variables needed by the local Docker client, never arbitrary secrets."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENGINE_ENV_ALLOWLIST and "\x00" not in value
    }


def _terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Hard-stop a Docker client and every local child it spawned."""
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_engine_environment(),
                timeout=10,
                check=False,
                shell=False,
            )
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if proc.poll() is None:
                proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class DockerCliEngine:
    """Bounded, argv-only Docker CLI adapter."""

    def __init__(self, executable: str) -> None:
        candidate = Path(executable)
        if candidate.is_file():
            resolved = str(candidate.resolve())
        else:
            found = shutil.which(executable)
            if found is None:
                raise LeagueExecutorError(f"Docker executable not found: {executable}")
            resolved = str(Path(found).resolve())
        self.executable = resolved
        self._environment = _engine_environment()

    @property
    def client_environment(self) -> Mapping[str, str]:
        """Return the fixed, secret-minimized environment used by this client."""
        return dict(self._environment)

    def bind_verified_local_endpoint(self, endpoint: str) -> None:
        """Pin later commands to the exact local endpoint that passed preflight."""
        _local_docker_transport(endpoint)
        self._environment.pop("DOCKER_CONTEXT", None)
        self._environment.pop("CONTAINER_HOST", None)
        self._environment["DOCKER_HOST"] = endpoint

    @classmethod
    def auto(cls) -> "DockerCliEngine":
        found = shutil.which("docker")
        if found is None:
            raise LeagueExecutorError(
                "Docker is required for `atv-bench league-score`; install Docker "
                "Desktop/Engine and ensure `docker` is on PATH."
            )
        return cls(found)

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> CommandResult:
        command = tuple(str(part) for part in argv)
        if not command or Path(command[0]).resolve() != Path(self.executable).resolve():
            raise ValueError("engine argv must begin with the resolved Docker executable")
        if any("\x00" in part for part in command):
            raise ValueError("engine argv cannot contain NUL")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")

        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            start_new_session = True

        budget = _OutputBudget(output_limit_bytes)
        started = time.monotonic()
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(self._environment),
            shell=False,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        assert proc.stdout is not None and proc.stderr is not None
        readers = [
            threading.Thread(
                target=_drain_pipe,
                args=(proc.stdout, budget, "stdout"),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_pipe,
                args=(proc.stderr, budget, "stderr"),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        deadline = started + timeout_seconds
        timed_out = False
        try:
            while proc.poll() is None:
                if budget.exceeded.is_set():
                    _terminate_process_tree(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_process_tree(proc)
                    break
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if proc.poll() is None:
                _terminate_process_tree(proc)
            raise
        finally:
            if proc.poll() is None:
                _terminate_process_tree(proc)
            for reader in readers:
                reader.join(timeout=5)

        return CommandResult(
            argv=command,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=bytes(budget.buffers["stdout"]),
            stderr=bytes(budget.buffers["stderr"]),
            stdout_total_bytes=budget.stream_totals["stdout"],
            stderr_total_bytes=budget.stream_totals["stderr"],
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            stdout_sha256=budget.hashers["stdout"].hexdigest(),
            stderr_sha256=budget.hashers["stderr"].hexdigest(),
            timed_out=timed_out,
            output_limit_exceeded=budget.exceeded.is_set(),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n"
    )


def _file_inventory(root: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise LeagueExecutorError(f"generated arena context contains a symlink: {path}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        inventory[relative] = {"sha256": _sha256(data), "size_bytes": len(data)}
    return inventory


def _inventory_digest(inventory: Mapping[str, Mapping[str, Any]]) -> str:
    return _sha256(
        json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    )


def materialize_arena_context(destination: Path) -> ArenaContext:
    """Generate a Docker context solely from packaged ``atv_bench.arena`` modules.

    This deliberately does not read the repository-level ``arena/`` directory, so the
    same command works from an installed wheel or source distribution.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    package_root = destination / "pkg" / "atv_bench"
    arena_root = package_root / "arena"
    arena_root.mkdir(parents=True)

    top_level_init = resources.files("atv_bench").joinpath("__init__.py")
    if not top_level_init.is_file():
        raise LeagueExecutorError("packaged atv_bench/__init__.py is missing")
    (package_root / "__init__.py").write_bytes(top_level_init.read_bytes())

    required = ("__init__.py", "__main__.py", "engine.py", "referee.py")
    packaged_arena = resources.files("atv_bench.arena")
    missing: list[str] = []
    for name in required:
        source = packaged_arena.joinpath(name)
        if not source.is_file():
            missing.append(name)
            continue
        (arena_root / name).write_bytes(source.read_bytes())
    if missing:
        raise LeagueExecutorError(
            f"installed package is missing trusted arena modules: {', '.join(missing)}"
        )

    arena_inventory = _file_inventory(destination / "pkg")
    arena_source_sha256 = _inventory_digest(arena_inventory)
    wrapper = """\
import os
from atv_bench.arena import __main__ as arena_main

game = os.environ.get("ATV_GAME", "")
if game != "lightcycles":
    raise SystemExit("unsupported trusted arena game")
try:
    seed = int(os.environ.get("ATV_SEED", "0"))
except ValueError as exc:
    raise SystemExit("ATV_SEED must be an integer") from exc
arena_main.SEED = seed
raise SystemExit(arena_main.main())
"""
    (destination / "league_entrypoint.py").write_text(wrapper, encoding="utf-8")

    if not re.search(r"@sha256:[0-9a-f]{64}$", ARENA_BASE_IMAGE):
        raise LeagueExecutorError("arena base image is not digest-pinned")
    dockerfile = f"""\
FROM {ARENA_BASE_IMAGE}
LABEL org.opencontainers.image.title="atv-bench-league-arena" \\
      org.opencontainers.image.version="{__version__}" \\
      org.opencontainers.image.atv.arena-source-sha256="{arena_source_sha256}"
COPY pkg/ /opt/arena/
COPY league_entrypoint.py /opt/league_entrypoint.py
ENV PYTHONPATH=/opt/arena \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python3", "/opt/league_entrypoint.py"]
CMD ["/work/main.py"]
"""
    (destination / "Dockerfile").write_text(dockerfile, encoding="utf-8")

    files = _file_inventory(destination)
    return ArenaContext(
        root=destination,
        arena_source_sha256=arena_source_sha256,
        context_sha256=_inventory_digest(files),
        files=files,
    )


def _read_validated_bot(path: Path) -> bytes:
    """Read one exact regular-file snapshot, without following a final symlink."""
    path = Path(path)
    if path.is_symlink():
        raise LeagueExecutorError("bot path must be a regular file, not a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LeagueExecutorError(f"cannot open bot file {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise LeagueExecutorError("bot path must be a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_BOT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BOT_BYTES:
                raise LeagueExecutorError(
                    f"bot is larger than the {MAX_BOT_BYTES}-byte League limit"
                )
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not data:
        raise LeagueExecutorError("bot file is empty")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LeagueExecutorError(f"bot must be UTF-8 text: {exc}") from exc
    return data


def _stage_bot(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        path.chmod(0o444)
    except OSError:
        pass
    staged = path.read_bytes()
    if staged != data:
        raise LeagueExecutorError("staged bot bytes do not match the validated source")
    return path


def _validate_inputs(
    *,
    submitter: str,
    match_id: str,
    game: str,
    seed: int,
) -> None:
    from atv_bench.games import assert_playable

    if not _IDENTITY_RE.fullmatch(submitter):
        raise LeagueExecutorError(
            "submitter must be a GitHub-login-shaped slug (1-39 alphanumerics/hyphens)"
        )
    if submitter == ARENA_OPPONENT:
        raise LeagueExecutorError("submitter must differ from the trusted arena opponent")
    if not _MATCH_ID_RE.fullmatch(match_id):
        raise LeagueExecutorError(
            "match id must be 1-128 safe characters beginning with an alphanumeric"
        )
    try:
        assert_playable(game)
    except ValueError as exc:
        raise LeagueExecutorError(str(exc)) from exc
    if game != "lightcycles":
        raise LeagueExecutorError("the packaged League executor currently supports lightcycles")
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed <= 2**31 - 1):
        raise LeagueExecutorError("seed must be an integer in the range 0..2147483647")


def _build_argv(engine: CommandEngine, *, tag: str, context: Path) -> list[str]:
    return [
        engine.executable,
        "build",
        "--network",
        "none",
        "--tag",
        tag,
        str(context),
    ]


def _run_argv(
    engine: CommandEngine,
    *,
    image_ref: str,
    container_name: str,
    staged_dir: Path,
    submitter: str,
    match_id: str,
    game: str,
    seed: int,
) -> list[str]:
    return [
        engine.executable,
        "run",
        "--rm",
        "--name",
        container_name,
        "--init",
        "--network",
        "none",
        "--ipc",
        "none",
        "--log-driver",
        "none",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1.0",
        "--pids-limit",
        "128",
        "--read-only",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--ulimit",
        "nofile=64:64",
        "--stop-timeout",
        "1",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={TMPFS_BYTES},mode=1777",
        "--workdir",
        "/work",
        "--env",
        f"ATV_SUBMITTER={submitter}",
        "--env",
        f"ATV_OPPONENT={ARENA_OPPONENT}",
        "--env",
        f"ATV_MATCH_ID={match_id}",
        "--env",
        f"ATV_GAME={game}",
        "--env",
        f"ATV_SEED={seed}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--mount",
        f"type=bind,source={staged_dir},target=/work,readonly",
        image_ref,
        "/work/main.py",
    ]


def _require_command_ok(result: CommandResult, stage: str) -> None:
    if result.timed_out:
        raise LeagueExecutorError(f"{stage} exceeded its wall-clock timeout")
    if result.output_limit_exceeded:
        raise LeagueExecutorError(f"{stage} exceeded its bounded output allowance")
    if result.exit_code != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        raise LeagueExecutorError(
            f"{stage} failed with exit code {result.exit_code}"
            + (f": {detail}" if detail else "")
        )


def _command_engine_environment(engine: CommandEngine) -> Mapping[str, str]:
    captured = getattr(engine, "client_environment", None)
    if isinstance(captured, Mapping):
        return captured
    return _engine_environment()


def _local_docker_transport(endpoint: str) -> str:
    """Accept only an explicitly local Docker socket transport."""
    value = endpoint.strip()
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise LeagueExecutorError("Docker endpoint must be one non-empty local socket URI")
    lowered = value.lower().replace("\\", "/")
    if lowered.startswith("unix://"):
        socket_path = lowered[len("unix://") :]
        if socket_path.startswith("/") and socket_path != "/":
            return "unix"
    if lowered.startswith("npipe://"):
        pipe_path = lowered[len("npipe://") :]
        if pipe_path.startswith("//./pipe/") and len(pipe_path) > len("//./pipe/"):
            return "npipe"
    raise LeagueExecutorError(
        "League execution requires a verified local unix:// or npipe:// Docker "
        f"endpoint; refusing {value!r}"
    )


def _single_line(result: CommandResult, stage: str) -> str:
    _require_command_ok(result, stage)
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise LeagueExecutorError(f"{stage} did not return UTF-8") from exc
    if not text or len(text.splitlines()) != 1:
        raise LeagueExecutorError(f"{stage} must return exactly one non-empty line")
    return text


def _required_daemon_text(info: Mapping[str, Any], key: str) -> str:
    value = info.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LeagueExecutorError(f"Docker daemon identity is missing {key}")
    return value.strip()


def _probe_local_docker_daemon(engine: CommandEngine) -> dict[str, Any]:
    """Bind Docker to a local socket and capture the daemon identity/security posture."""
    client_environment = _command_engine_environment(engine)
    docker_host = str(client_environment.get("DOCKER_HOST", "")).strip()
    docker_context = str(client_environment.get("DOCKER_CONTEXT", "")).strip()
    if docker_host and docker_context:
        raise LeagueExecutorError(
            "DOCKER_HOST and DOCKER_CONTEXT cannot both be set for League execution"
        )

    context_name: str | None = None
    context_inspect_sha256: str | None = None
    if docker_host:
        endpoint = docker_host
        endpoint_source = "DOCKER_HOST"
    else:
        context_show = engine.execute(
            [engine.executable, "context", "show"],
            timeout_seconds=CONTROL_TIMEOUT_SECONDS,
            output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
        )
        context_name = _single_line(context_show, "Docker context selection probe")
        if docker_context and context_name != docker_context:
            raise LeagueExecutorError(
                "Docker context selection does not match the configured DOCKER_CONTEXT"
            )
        context_inspect = engine.execute(
            [
                engine.executable,
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
                context_name,
            ],
            timeout_seconds=CONTROL_TIMEOUT_SECONDS,
            output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
        )
        raw_endpoint = _single_line(context_inspect, "Docker context endpoint probe")
        try:
            endpoint_value = json.loads(raw_endpoint)
        except json.JSONDecodeError as exc:
            raise LeagueExecutorError(
                "Docker context endpoint probe did not return canonical JSON"
            ) from exc
        if not isinstance(endpoint_value, str):
            raise LeagueExecutorError("Docker context endpoint must be a string")
        endpoint = endpoint_value.strip()
        endpoint_source = "docker-context"
        context_inspect_sha256 = context_inspect.stdout_sha256

    transport = _local_docker_transport(endpoint)
    binder = getattr(engine, "bind_verified_local_endpoint", None)
    if callable(binder):
        binder(endpoint)

    info_result = engine.execute(
        [engine.executable, "info", "--format", "{{json .}}"],
        timeout_seconds=CONTROL_TIMEOUT_SECONDS,
        output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
    )
    raw_info = _single_line(info_result, "Docker daemon identity/security probe")
    try:
        info = json.loads(raw_info)
    except json.JSONDecodeError as exc:
        raise LeagueExecutorError(
            "Docker daemon identity/security probe did not return canonical JSON"
        ) from exc
    if not isinstance(info, dict):
        raise LeagueExecutorError("Docker daemon identity/security probe must return an object")
    security_options = info.get("SecurityOptions")
    if not isinstance(security_options, list) or not all(
        isinstance(item, str) for item in security_options
    ):
        raise LeagueExecutorError("Docker daemon SecurityOptions must be a string array")

    daemon = {
        "id": _required_daemon_text(info, "ID"),
        "name": _required_daemon_text(info, "Name"),
        "server_version": _required_daemon_text(info, "ServerVersion"),
        "operating_system": _required_daemon_text(info, "OperatingSystem"),
        "os_type": _required_daemon_text(info, "OSType"),
        "architecture": _required_daemon_text(info, "Architecture"),
        "kernel_version": _required_daemon_text(info, "KernelVersion"),
        "storage_driver": _required_daemon_text(info, "Driver"),
        "cgroup_driver": _required_daemon_text(info, "CgroupDriver"),
        "security_options": sorted(security_options),
        "rootless": any(
            option == "name=rootless" or option.startswith("name=rootless,")
            for option in security_options
        ),
        "info_stdout_sha256": info_result.stdout_sha256,
    }
    return {
        "endpoint": {
            "source": endpoint_source,
            "context": context_name,
            "uri": endpoint,
            "transport": transport,
            "local_socket_verified": True,
            "context_inspect_stdout_sha256": context_inspect_sha256,
        },
        "daemon": daemon,
    }


def _parse_and_bind_result(
    raw_stdout: bytes,
    *,
    work_dir: Path,
    submitter: str,
    match_id: str,
    game: str,
    seed: int,
    bot_sha256: str,
) -> dict[str, Any]:
    from atv_bench.publish import MatchSpec, SpecMismatch, bind_ok_to_spec, validate_artifact

    try:
        text = raw_stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LeagueExecutorError(f"arena result is not UTF-8: {exc}") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise LeagueExecutorError(
            f"trusted arena must emit exactly one JSON result line; received {len(lines)}"
        )
    raw_path = work_dir / "arena-result.json"
    raw_path.write_text(lines[0] + "\n", encoding="utf-8")
    try:
        data = validate_artifact(str(raw_path))
    except ValueError as exc:
        raise LeagueExecutorError(f"arena result failed the League schema: {exc}") from exc
    if data.get("status") != "ok":
        raise LeagueExecutorError(
            f"trusted arena returned non-adjudicated status {data.get('status')!r}"
        )
    if data.get("game") != game:
        raise LeagueExecutorError(
            f"arena result game {data.get('game')!r} does not match issued game {game!r}"
        )
    if data.get("seed") != seed:
        raise LeagueExecutorError(
            f"arena result seed {data.get('seed')!r} does not match issued seed {seed!r}"
        )
    spec = MatchSpec(
        submitter=submitter,
        opponent=ARENA_OPPONENT,
        match_id=match_id,
        bot_sha256=bot_sha256,
    )
    try:
        bound = bind_ok_to_spec(data, spec)
    except SpecMismatch as exc:
        raise LeagueExecutorError(f"arena result did not bind to the issued MatchSpec: {exc}") from exc
    bound["game"] = game
    bound["seed"] = seed
    return bound


def _redact_argv(
    argv: Sequence[str],
    *,
    engine: CommandEngine,
    context: Path,
    staged_dir: Path,
    tag: str,
    container_name: str,
    image_id: str,
) -> list[str]:
    replacements = {
        engine.executable: "${DOCKER}",
        str(context): "${CONTEXT}",
        str(staged_dir): "${STAGED_BOT_DIR}",
        tag: "${IMAGE_TAG}",
        container_name: "${CONTAINER_NAME}",
        image_id: "${IMAGE_ID}",
    }
    redacted: list[str] = []
    for part in argv:
        value = replacements.get(str(part), str(part))
        for source, replacement in replacements.items():
            if source and source in value:
                value = value.replace(source, replacement)
        redacted.append(value)
    return redacted


def _cleanup_engine_resources(
    engine: CommandEngine,
    *,
    container_name: str,
    image_tag: str,
) -> dict[str, Any]:
    """Remove only unique run names, then prove those names are absent."""
    remove_container = engine.execute(
        [engine.executable, "rm", "--force", container_name],
        timeout_seconds=CONTROL_TIMEOUT_SECONDS,
        output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
    )
    inspect_container = engine.execute(
        [
            engine.executable,
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"name=^/{container_name}$",
        ],
        timeout_seconds=CONTROL_TIMEOUT_SECONDS,
        output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
    )
    remove_image = engine.execute(
        [engine.executable, "image", "rm", image_tag],
        timeout_seconds=CONTROL_TIMEOUT_SECONDS,
        output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
    )
    inspect_image = engine.execute(
        [
            engine.executable,
            "image",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"reference={image_tag}",
        ],
        timeout_seconds=CONTROL_TIMEOUT_SECONDS,
        output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
    )
    return {
        "container_remove_exit_code": remove_container.exit_code,
        "container_probe_exit_code": inspect_container.exit_code,
        "container_absent": inspect_container.ok and not inspect_container.stdout.strip(),
        "image_tag_remove_exit_code": remove_image.exit_code,
        "image_tag_probe_exit_code": inspect_image.exit_code,
        "image_tag_absent": inspect_image.ok and not inspect_image.stdout.strip(),
        "image_removal_scope": "unique-run-tag-only",
        "image_id_removal_attempted": False,
        "image_id_absence_claimed": False,
    }


def _cleanup_verification_failure(cleanup: Mapping[str, Any]) -> LeagueExecutorError | None:
    if cleanup.get("cleanup_error"):
        return LeagueExecutorError(
            "temporary Docker resource cleanup raised an error: "
            f"{cleanup['cleanup_error']}"
        )
    failures: list[str] = []
    if not cleanup.get("container_absent"):
        failures.append(
            "container absence was not verified "
            f"(probe exit {cleanup.get('container_probe_exit_code')!r})"
        )
    if not cleanup.get("image_tag_absent"):
        failures.append(
            "unique image-tag absence was not verified "
            f"(probe exit {cleanup.get('image_tag_probe_exit_code')!r})"
        )
    if failures:
        return LeagueExecutorError(
            "temporary Docker resource cleanup could not be verified: "
            + "; ".join(failures)
        )
    return None


def _engine_executable_sha256(engine: CommandEngine) -> str | None:
    try:
        path = Path(engine.executable)
        if path.is_file():
            return _sha256(path.read_bytes())
    except OSError:
        pass
    return None


@contextmanager
def _exclusive_store_lock(store_dir: Path, *, timeout_seconds: float = 30.0):
    """Cross-platform advisory lock for the store's read-dedup-append sequence."""
    if timeout_seconds <= 0:
        raise ValueError("store lock timeout must be positive")
    root = Path(store_dir)
    root.mkdir(parents=True, exist_ok=True)
    normalized = os.path.normcase(str(root.resolve()))
    lock_key = _sha256(os.fsencode(normalized))
    lock_root = Path(tempfile.gettempdir()) / "atv-bench-league-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{lock_key}.lock"
    if lock_path.is_symlink():
        raise LeagueExecutorError("local store lock path must not be a symlink")
    handle = lock_path.open("a+b")
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise LeagueExecutorError(
                        f"timed out waiting for the local League store lock at {lock_path}"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write_bundle(
    output_dir: Path,
    *,
    result: Mapping[str, Any],
    meta: Mapping[str, Any],
    reproduction: Mapping[str, Any],
    logs: Mapping[str, bytes],
    materials: Mapping[str, bytes],
) -> tuple[Path, str]:
    output_dir = Path(output_dir)
    if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
        raise LeagueExecutorError("output directory must be a real directory, not a link/file")
    object_root = output_dir / "sha256"
    object_root.mkdir(parents=True, exist_ok=True)
    if object_root.is_symlink() or not object_root.is_dir():
        raise LeagueExecutorError("output sha256 directory must be a real directory")

    artifacts: dict[str, bytes] = {
        "result.json": _json_bytes(result),
        "meta.json": _json_bytes(meta),
        "reproduction.json": _json_bytes(reproduction),
        **{name: bytes(data) for name, data in logs.items()},
        **{name: bytes(data) for name, data in materials.items()},
    }
    checksums = {
        "schema": "atv.league-score-checksums/v1",
        "algorithm": "sha256",
        "files": {
            name: {"sha256": _sha256(data), "size_bytes": len(data)}
            for name, data in sorted(artifacts.items())
        },
    }
    checksums_bytes = _json_bytes(checksums)
    bundle_sha256 = _sha256(checksums_bytes)
    artifacts["checksums.json"] = checksums_bytes
    final = object_root / bundle_sha256

    if final.exists():
        if not final.is_dir() or final.is_symlink():
            raise LeagueExecutorError(f"content-address collision at {final}")
        for name, data in artifacts.items():
            candidate = final / name
            if not candidate.is_file() or candidate.read_bytes() != data:
                raise LeagueExecutorError(f"content-address collision for {name} at {final}")
        return final, bundle_sha256

    temporary = object_root / f".tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        for name, data in artifacts.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        os.replace(temporary, final)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return final, bundle_sha256


def execute_league_score(
    *,
    submitter: str,
    bot_path: Path,
    match_id: str,
    game: str,
    seed: int,
    output_dir: Path,
    local_store: Path | None = None,
    engine: CommandEngine | None = None,
) -> LeagueScoreReceipt:
    """Execute and verify one frozen-bot League match outside GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        raise LeagueExecutorError(
            "`atv-bench league-score` refuses to run inside GitHub Actions; "
            "Actions is Pages/test-only by repository policy."
        )
    _validate_inputs(
        submitter=submitter,
        match_id=match_id,
        game=game,
        seed=seed,
    )
    bot_bytes = _read_validated_bot(Path(bot_path))
    bot_sha256 = _sha256(bot_bytes)
    resolved_engine = engine or DockerCliEngine.auto()
    engine_attestation = _probe_local_docker_daemon(resolved_engine)

    token = uuid.uuid4().hex
    image_tag = f"atv-bench/league-score:{token}"
    container_name = f"atv-league-{token[:20]}"
    started_at = _utc_now()
    build_result: CommandResult | None = None
    run_result: CommandResult | None = None
    version_result: CommandResult | None = None
    context: ArenaContext | None = None
    build_argv: list[str] = []
    run_argv: list[str] = []
    bound_result: dict[str, Any] | None = None
    image_id = ""
    cleanup: dict[str, Any] = {
        "container_absent": False,
        "image_tag_absent": False,
        "image_removal_scope": "unique-run-tag-only",
        "image_id_removal_attempted": False,
        "image_id_absence_claimed": False,
    }
    primary_error: BaseException | None = None

    with tempfile.TemporaryDirectory(prefix="atv-league-score-") as temporary_root:
        work_root = Path(temporary_root)
        context_dir = work_root / "context"
        staged_dir = work_root / "staged"
        staged_bot = _stage_bot(staged_dir / "main.py", bot_bytes)
        try:
            context = materialize_arena_context(context_dir)
            version_result = resolved_engine.execute(
                [resolved_engine.executable, "--version"],
                timeout_seconds=CONTROL_TIMEOUT_SECONDS,
                output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
            )
            _require_command_ok(version_result, "Docker version probe")

            build_argv = _build_argv(
                resolved_engine,
                tag=image_tag,
                context=context.root,
            )
            build_result = resolved_engine.execute(
                build_argv,
                timeout_seconds=BUILD_TIMEOUT_SECONDS,
                output_limit_bytes=BUILD_OUTPUT_LIMIT_BYTES,
            )
            _require_command_ok(build_result, "trusted arena image build")

            image_inspect = resolved_engine.execute(
                [
                    resolved_engine.executable,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    image_tag,
                ],
                timeout_seconds=CONTROL_TIMEOUT_SECONDS,
                output_limit_bytes=CONTROL_OUTPUT_LIMIT_BYTES,
            )
            _require_command_ok(image_inspect, "trusted arena image inspection")
            image_id = image_inspect.stdout.decode("utf-8", errors="strict").strip()
            if not _DIGEST_RE.fullmatch(image_id):
                raise LeagueExecutorError(
                    f"Docker returned an invalid immutable image id: {image_id!r}"
                )

            if staged_bot.read_bytes() != bot_bytes:
                raise LeagueExecutorError("staged bot bytes changed before Docker execution")
            run_argv = _run_argv(
                resolved_engine,
                image_ref=image_id,
                container_name=container_name,
                staged_dir=staged_dir,
                submitter=submitter,
                match_id=match_id,
                game=game,
                seed=seed,
            )
            run_result = resolved_engine.execute(
                run_argv,
                timeout_seconds=RUN_TIMEOUT_SECONDS,
                output_limit_bytes=RUN_OUTPUT_LIMIT_BYTES,
            )
            _require_command_ok(run_result, "sandboxed League match")
            if staged_bot.read_bytes() != bot_bytes:
                raise LeagueExecutorError("staged bot bytes changed during Docker execution")
            bound_result = _parse_and_bind_result(
                run_result.stdout,
                work_dir=work_root,
                submitter=submitter,
                match_id=match_id,
                game=game,
                seed=seed,
                bot_sha256=bot_sha256,
            )
        except BaseException as exc:
            primary_error = exc
        finally:
            try:
                cleanup = _cleanup_engine_resources(
                    resolved_engine,
                    container_name=container_name,
                    image_tag=image_tag,
                )
            except BaseException as exc:
                cleanup = {
                    "container_absent": False,
                    "image_tag_absent": False,
                    "image_removal_scope": "unique-run-tag-only",
                    "image_id_removal_attempted": False,
                    "image_id_absence_claimed": False,
                    "cleanup_error": f"{type(exc).__name__}: {exc}",
                }

        cleanup_failure = _cleanup_verification_failure(cleanup)
        if primary_error is not None and cleanup_failure is not None:
            raise LeagueExecutorError(
                f"{primary_error}; additionally, {cleanup_failure}"
            ) from primary_error
        if cleanup_failure is not None:
            raise cleanup_failure
        if primary_error is not None:
            raise primary_error
        assert context is not None
        assert version_result is not None
        assert build_result is not None
        assert run_result is not None
        assert bound_result is not None

        finished_at = _utc_now()
        engine_version = version_result.stdout.decode("utf-8", errors="replace").strip()
        resource_policy = {
            "network": "none",
            "user": "65534:65534",
            "read_only_root": True,
            "capabilities": "drop-all",
            "no_new_privileges": True,
            "cpus": 1.0,
            "memory_bytes": 512 * 1024 * 1024,
            "memory_swap_bytes": 512 * 1024 * 1024,
            "pids_limit": 128,
            "wall_timeout_seconds": RUN_TIMEOUT_SECONDS,
            "captured_output_limit_bytes": RUN_OUTPUT_LIMIT_BYTES,
            "tmpfs_bytes": TMPFS_BYTES,
            "nofile_limit": 64,
        }
        meta = {
            "schema": "atv.league-score-meta/v1",
            "status": "verified-local",
            "trust_tier": "local-self-attested",
            "rankable": False,
            "publication": "reviewed result data is required before Pages publication",
            "started_at": started_at,
            "finished_at": finished_at,
            "match_spec": {
                "submitter": submitter,
                "opponent": ARENA_OPPONENT,
                "match_id": match_id,
                "game": game,
                "seed": seed,
                "seed_semantics": "label-only; current Lightcycles gameplay is unseeded",
                "bot_sha256": bot_sha256,
            },
            "binding_verified": True,
            "adjudication": "packaged-trusted-arena",
            "bot": {
                "source_name": Path(bot_path).name,
                "sha256": bot_sha256,
                "size_bytes": len(bot_bytes),
                "staged_bytes_verified_before_and_after": True,
            },
            "arena": {
                "base_image": ARENA_BASE_IMAGE,
                "arena_source_sha256": context.arena_source_sha256,
                "context_sha256": context.context_sha256,
                "image_id": image_id,
            },
            "engine": {
                "name": Path(resolved_engine.executable).name,
                "version": engine_version,
                "executable_sha256": _engine_executable_sha256(resolved_engine),
                **engine_attestation,
            },
            "resource_policy": resource_policy,
            "commands": {
                "build": {
                    "exit_code": build_result.exit_code,
                    "duration_ms": build_result.duration_ms,
                    "stdout_total_bytes": build_result.stdout_total_bytes,
                    "stderr_total_bytes": build_result.stderr_total_bytes,
                    "stdout_sha256": build_result.stdout_sha256,
                    "stderr_sha256": build_result.stderr_sha256,
                },
                "run": {
                    "exit_code": run_result.exit_code,
                    "duration_ms": run_result.duration_ms,
                    "stdout_total_bytes": run_result.stdout_total_bytes,
                    "stderr_total_bytes": run_result.stderr_total_bytes,
                    "stdout_sha256": run_result.stdout_sha256,
                    "stderr_sha256": run_result.stderr_sha256,
                },
            },
            "cleanup": cleanup,
            "local_store": {
                "requested": local_store is not None,
                "mutation_order": "content-addressed evidence before local-store append",
                "ingestion_result": "reported by the CLI receipt, not this immutable bundle",
            },
        }
        reproduction = {
            "schema": "atv.league-score-reproduction/v1",
            "atv_bench_version": __version__,
            "python_version": platform.python_version(),
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
            },
            "prerequisites": ["Docker Engine/Desktop", "the submitted bot bytes"],
            "inputs": {
                "bot_sha256": bot_sha256,
                "submitter": submitter,
                "opponent": ARENA_OPPONENT,
                "match_id": match_id,
                "game": game,
                "seed": seed,
                "seed_semantics": "label-only; current Lightcycles gameplay is unseeded",
            },
            "arena": {
                "base_image": ARENA_BASE_IMAGE,
                "arena_source_sha256": context.arena_source_sha256,
                "context_sha256": context.context_sha256,
                "context_files": context.files,
                "image_id": image_id,
                "source": "importlib.resources:atv_bench.arena",
            },
            "resource_policy": resource_policy,
            "engine": engine_attestation,
            "argv": {
                "build": _redact_argv(
                    build_argv,
                    engine=resolved_engine,
                    context=context.root,
                    staged_dir=staged_dir,
                    tag=image_tag,
                    container_name=container_name,
                    image_id=image_id,
                ),
                "run": _redact_argv(
                    run_argv,
                    engine=resolved_engine,
                    context=context.root,
                    staged_dir=staged_dir,
                    tag=image_tag,
                    container_name=container_name,
                    image_id=image_id,
                ),
            },
            "notes": [
                "Materialize ${CONTEXT} from the installed atv_bench.arena package.",
                "Place bytes matching bot_sha256 at ${STAGED_BOT_DIR}/main.py.",
                "Use fresh unique ${IMAGE_TAG} and ${CONTAINER_NAME} values.",
                "The result is local evidence until reviewed result data is merged.",
            ],
            "local_store_ingest": {
                "requested": local_store is not None,
                "locking": "OS advisory lock keyed by the resolved local-store path",
                "mutation_order": "after the content-addressed evidence bundle is committed",
            },
        }
        logs = {
            "build.stdout.log": build_result.stdout,
            "build.stderr.log": build_result.stderr,
            "run.stdout.log": run_result.stdout,
            "run.stderr.log": run_result.stderr,
        }
        materials = {
            "materials/bot/main.py": bot_bytes,
            **{
                f"materials/arena-context/{relative}": (context.root / relative).read_bytes()
                for relative in sorted(context.files)
            },
        }
        bundle_dir, bundle_sha256 = _write_bundle(
            Path(output_dir),
            result=bound_result,
            meta=meta,
            reproduction=reproduction,
            logs=logs,
            materials=materials,
        )

        ingested = False
        if local_store is not None:
            from atv_bench.publish import MatchSpec, ingest_result

            ingest_path = work_root / "bound-result.json"
            ingest_path.write_bytes(_json_bytes(bound_result))
            spec = MatchSpec(
                submitter=submitter,
                opponent=ARENA_OPPONENT,
                match_id=match_id,
                bot_sha256=bot_sha256,
            )
            try:
                with _exclusive_store_lock(Path(local_store)):
                    ingested = ingest_result(
                        str(ingest_path),
                        store_dir=str(local_store),
                        spec=spec,
                    )
            except (OSError, ValueError, LeagueExecutorError) as exc:
                raise LeagueExecutorError(
                    f"local store ingestion failed after evidence was committed at "
                    f"{bundle_dir}: {exc}"
                ) from exc

    return LeagueScoreReceipt(
        bundle_dir=bundle_dir,
        bundle_sha256=bundle_sha256,
        bot_sha256=bot_sha256,
        result=bound_result,
        ingested=ingested,
    )


__all__ = [
    "ARENA_BASE_IMAGE",
    "ARENA_OPPONENT",
    "ArenaContext",
    "CommandEngine",
    "CommandResult",
    "DockerCliEngine",
    "LeagueExecutorError",
    "LeagueScoreReceipt",
    "execute_league_score",
    "materialize_arena_context",
]
