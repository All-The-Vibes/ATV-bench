"""Wire ATV-bench harnesses into CodeClash's tournament (Lane B).

`register()` monkeypatch-REPLACES `codeclash.tournaments.pvp.get_agent` (the host-side
construction site verified by the gating spike — agents are built in
PvpTournament.__init__, run() executes host-side). A config whose `agent` key is an
ATV-bench harness (claude-code / copilot-cli) resolves to a HarnessPlayer bound to that
harness's adapter; any other key falls through to CodeClash's original get_agent, so
`dummy` / `mini` are never clobbered.

The HarnessPlayer is constructed lazily against the real CodeClash Player base so this
module imports without Docker; the Docker tree-container shim adapts DockerEnvironment
to the TreeContainerLike protocol players.py expects.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from atv_bench.adapters.contract import ADAPTERS, Budget, ProcessResult, run_process
from atv_bench.capture import (
    MAX_DEPTH,
    MAX_DIRECTORIES,
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PATH_BYTES,
    MAX_TOTAL_BYTES,
    CaptureRejected,
    read_confined_regular_file,
    scan_captured_tree,
)
from atv_bench.codeclash_env import (
    CODECLASH_LIGHTCYCLES_IMAGE,
    CODECLASH_LIGHTCYCLES_PIN,
    CODECLASH_PIN,
    CODECLASH_UBUNTU_2204_DIGEST,
    import_codeclash,
)
from atv_bench.players import (
    MAX_PLAYER_TREE_DIRECTORIES,
    MAX_PLAYER_TREE_ENTRIES,
    MAX_PLAYER_TREE_FILE_BYTES,
    MAX_PLAYER_TREE_FILES,
    MAX_PLAYER_TREE_TOTAL_BYTES,
    HarnessPlayerCore,
)

# Harness keys ATV-bench can BUILD a bot with (fingerprint-only harnesses excluded).
BUILDER_HARNESSES = tuple(ADAPTERS.keys())
MAX_CONTAINER_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_CODECLASH_ARCHIVE_BYTES = 96 * 1024 * 1024
MAX_CODECLASH_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CODECLASH_ARCHIVE_FILE_BYTES = 16 * 1024 * 1024
MAX_CODECLASH_ARCHIVE_FILES = 4_096
MAX_CODECLASH_ARCHIVE_ENTRIES = 8_192
MAX_CODECLASH_ARCHIVE_DIRECTORIES = 4_096
CONTAINER_COPY_TIMEOUT_SECONDS = 60
MAX_CONTAINER_STDERR_BYTES = 64 * 1024
IGNORED_CONTAINER_TREE_DIRS = (
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
)

_original_get_agent = None  # set on first register(), restored on unregister()
_original_copy_between: dict[object, object] = {}
_original_copy_to: dict[object, object] = {}
_original_atomic_write: dict[object, object] = {}
_original_build_image: dict[type, object] = {}
_original_pvp_end: dict[type, object] = {}
_original_environment_cleanup: dict[type, object] = {}
_original_logging_raise_exceptions: bool | None = None
_player_class_cache: dict[str, type] = {}


class _BoundedArchiveReader:
    """Expose at most ``limit`` bytes from an untrusted container stream."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.total
        if remaining < 0:
            raise CaptureRejected(
                f"container archive exceeds {self.limit} transferred bytes"
            )
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self.stream.read(requested)
        self.total += len(data)
        if self.total > self.limit:
            raise CaptureRejected(
                f"container archive exceeds {self.limit} transferred bytes"
            )
        return data


def _tar_parts(name: str) -> tuple[str, ...]:
    portable = name.replace("\\", "/")
    while portable.startswith("./"):
        portable = portable[2:]
    if portable in {"", "."}:
        return ()
    if portable.startswith(("/", "//")):
        raise CaptureRejected(f"absolute container archive path is not allowed: {name!r}")
    path = PurePosixPath(portable)
    parts = path.parts
    if not parts or ".." in parts or any(part in {"", "."} for part in parts):
        raise CaptureRejected(f"unsafe container archive path: {name!r}")
    if len(parts[0]) == 2 and parts[0][0].isalpha() and parts[0][1] == ":":
        raise CaptureRejected(f"drive-qualified container path is not allowed: {name!r}")
    if len(parts) > MAX_DEPTH:
        raise CaptureRejected(f"container archive exceeds depth limit ({MAX_DEPTH})")
    relative = "/".join(parts)
    if len(relative.encode("utf-8", errors="surrogatepass")) > MAX_PATH_BYTES:
        raise CaptureRejected(
            f"container archive path exceeds {MAX_PATH_BYTES} UTF-8 bytes"
        )
    if any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
        for part in parts
    ):
        raise CaptureRejected(f"container archive path contains control bytes: {name!r}")
    return tuple(parts)


def _materialize_bounded_tar(
    stream: BinaryIO,
    destination: Path,
    *,
    archive_limit: int = MAX_CONTAINER_ARCHIVE_BYTES,
    max_entries: int = MAX_ENTRIES,
    max_directories: int = MAX_DIRECTORIES,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> tuple[str, ...]:
    """Materialize a container tar stream only after enforcing hard bounds."""

    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    entries = 0
    directories = 0
    regular_files = 0
    total_bytes = 0
    seen: set[str] = set()
    captured: list[str] = []
    bounded = _BoundedArchiveReader(stream, archive_limit)
    try:
        archive = tarfile.open(fileobj=bounded, mode="r|*")
        with archive:
            for member in archive:
                parts = _tar_parts(member.name)
                if not parts:
                    continue
                entries += 1
                if entries > max_entries:
                    raise CaptureRejected(
                        f"container archive has too many entries (> {max_entries})"
                    )
                relative = "/".join(parts)
                portable_key = relative.casefold()
                if portable_key in seen:
                    raise CaptureRejected(
                        f"container archive contains a duplicate path: {relative}"
                    )
                seen.add(portable_key)
                target = root.joinpath(*parts)
                try:
                    target.resolve(strict=False).relative_to(root)
                except ValueError as exc:
                    raise CaptureRejected(
                        f"container archive path escapes destination: {relative}"
                    ) from exc

                if member.isdir():
                    directories += 1
                    if directories > max_directories:
                        raise CaptureRejected(
                            "container archive has too many directories "
                            f"(> {max_directories})"
                        )
                    target.mkdir(parents=True, exist_ok=False)
                    continue
                if member.issym() or member.islnk():
                    raise CaptureRejected(
                        f"link is not allowed in container archive: {relative}"
                    )
                if not member.isreg():
                    raise CaptureRejected(
                        f"special entry is not allowed in container archive: {relative}"
                    )
                regular_files += 1
                if regular_files > max_files:
                    raise CaptureRejected(
                        f"container archive has too many files (> {max_files})"
                    )
                if member.size > max_file_bytes:
                    raise CaptureRejected(
                        f"container archive file is too large: {relative} "
                        f"({member.size} bytes)"
                    )
                total_bytes += member.size
                if total_bytes > max_total_bytes:
                    raise CaptureRejected(
                        "container archive total file size exceeds "
                        f"{max_total_bytes} bytes"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise CaptureRejected(
                        f"container archive file could not be read: {relative}"
                    )
                data = source.read(member.size + 1)
                if len(data) != member.size:
                    raise CaptureRejected(
                        f"container archive file length changed: {relative}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open("xb") as handle:
                        handle.write(data)
                except OSError as exc:
                    raise CaptureRejected(
                        f"container archive path could not be materialized: {relative}"
                    ) from exc
                captured.append(relative)
    except (tarfile.TarError, OSError) as exc:
        raise CaptureRejected(f"invalid container archive: {exc}") from exc
    return tuple(captured)


def _container_cli(env) -> tuple[str, str]:
    container_id = getattr(env, "container_id", None)
    if not isinstance(container_id, str) or not container_id.strip():
        raise CaptureRejected("CodeClash container has no stable container id")
    config = getattr(env, "config", None)
    executable = getattr(config, "executable", "docker")
    if not isinstance(executable, str) or not executable.strip():
        raise CaptureRejected("CodeClash container runtime executable is unavailable")
    return executable, container_id


def _container_tar_command(
    executable: str,
    container_id: str,
    source: str,
) -> list[str]:
    excludes = [
        pattern
        for directory in IGNORED_CONTAINER_TREE_DIRS
        for pattern in (
            f"--exclude=./{directory}",
            f"--exclude=*/{directory}",
        )
    ]
    return [
        executable,
        "exec",
        container_id,
        "tar",
        *excludes,
        "-C",
        source,
        "-cf",
        "-",
        ".",
    ]


def _kill_cli_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=10,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()


def _bounded_copy_from_container(
    env,
    source: str,
    destination: Path,
    *,
    archive_limit: int = MAX_CONTAINER_ARCHIVE_BYTES,
    max_entries: int = MAX_ENTRIES,
    max_directories: int = MAX_DIRECTORIES,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> tuple[str, ...]:
    """Stream one directory through tar without an unbounded ``docker cp``."""

    executable, container_id = _container_cli(env)
    command = _container_tar_command(executable, container_id, source)
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    assert process.stdout is not None and process.stderr is not None
    stderr = bytearray()

    def drain_stderr() -> None:
        while True:
            chunk = process.stderr.read(16 * 1024)
            if not chunk:
                return
            stderr.extend(chunk)
            if len(stderr) > MAX_CONTAINER_STDERR_BYTES:
                del stderr[: len(stderr) - MAX_CONTAINER_STDERR_BYTES]

    parser_error: list[BaseException] = []
    captured: list[tuple[str, ...]] = []

    def parse_archive() -> None:
        try:
            captured.append(
                _materialize_bounded_tar(
                    process.stdout,
                    destination,
                    archive_limit=archive_limit,
                    max_entries=max_entries,
                    max_directories=max_directories,
                    max_files=max_files,
                    max_total_bytes=max_total_bytes,
                    max_file_bytes=max_file_bytes,
                )
            )
        except BaseException as exc:
            parser_error.append(exc)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    parser_thread = threading.Thread(target=parse_archive, daemon=True)
    stderr_thread.start()
    parser_thread.start()
    deadline = time.monotonic() + CONTAINER_COPY_TIMEOUT_SECONDS
    parser_thread.join(timeout=CONTAINER_COPY_TIMEOUT_SECONDS)
    if parser_thread.is_alive():
        _kill_cli_tree(process)
        parser_thread.join(timeout=5)
        raise CaptureRejected(
            f"container archive transfer exceeded {CONTAINER_COPY_TIMEOUT_SECONDS} seconds"
        )
    if parser_error:
        _kill_cli_tree(process)
    remaining = max(0.1, deadline - time.monotonic())
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _kill_cli_tree(process)
        process.wait(timeout=5)
        raise CaptureRejected(
            f"container archive transfer exceeded {CONTAINER_COPY_TIMEOUT_SECONDS} seconds"
        ) from exc
    stderr_thread.join(timeout=5)
    if parser_error:
        error = parser_error[0]
        if isinstance(error, CaptureRejected):
            raise error
        raise CaptureRejected(f"container archive parser failed: {error}") from error
    if process.returncode != 0:
        detail = bytes(stderr).decode("utf-8", errors="replace").strip()
        raise CaptureRejected(
            f"container archive command failed with {process.returncode}: {detail}"
        )
    if len(captured) != 1:
        raise CaptureRejected("container archive transfer produced no captured tree")
    return captured[0]


def _safe_container_directory(value: str | Path) -> str:
    portable = str(value).replace("\\", "/")
    path = PurePosixPath(portable)
    if (
        not path.is_absolute()
        or portable == "/"
        or ".." in path.parts
        or any(character in portable for character in ("\x00", "\r", "\n", ":"))
    ):
        raise CaptureRejected(f"unsafe container directory: {value!r}")
    return path.as_posix()


def _write_bytes_to_container(env, destination: str, data: bytes) -> None:
    executable, container_id = _container_cli(env)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="atv-container-file-",
        suffix=".bin",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        result = subprocess.run(
            [
                executable,
                "cp",
                str(temporary),
                f"{container_id}:{destination}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CONTAINER_COPY_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stdout + b"\n" + result.stderr)[
                -MAX_CONTAINER_STDERR_BYTES:
            ].decode("utf-8", errors="replace")
            raise CaptureRejected(
                f"container file copy failed for {destination}: {detail.strip()}"
            )
    except subprocess.TimeoutExpired as exc:
        raise CaptureRejected(
            f"container file copy exceeded {CONTAINER_COPY_TIMEOUT_SECONDS} seconds"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _bounded_host_path(path: Path) -> None:
    entries = 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        metadata = current.lstat()
        reparse = bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        if current.is_symlink() or reparse or not (
            current.is_file() or current.is_dir()
        ):
            raise CaptureRejected(f"unsafe host copy path: {current}")
        entries += 1
        if entries > 4096:
            raise CaptureRejected("host copy tree exceeds 4096 entries")
        if current.is_file():
            if getattr(metadata, "st_nlink", 1) > 1:
                raise CaptureRejected(f"hardlinked host copy file is not allowed: {current}")
            total += metadata.st_size
            if metadata.st_size > 4 * 1024 * 1024 or total > 32 * 1024 * 1024:
                raise CaptureRejected("host copy tree exceeds bounded byte limits")
            continue
        stack.extend(sorted(current.iterdir(), reverse=True))


def _bounded_copy_to_container(
    container,
    src_path: str | Path,
    dest_path: str | Path,
) -> None:
    source = Path(src_path).resolve(strict=True)
    _bounded_host_path(source)
    destination = _safe_container_directory(dest_path)
    parent = str(PurePosixPath(destination).parent)
    created = container.execute(
        {"command": f"mkdir -p -- {shlex.quote(parent)}"},
        cwd="/",
    )
    if created.get("returncode") != 0:
        raise CaptureRejected(
            f"destination parent creation failed: {created.get('output', '')}"
        )
    executable, container_id = _container_cli(container)
    try:
        result = subprocess.run(
            [
                executable,
                "cp",
                str(source),
                f"{container_id}:{destination}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CONTAINER_COPY_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureRejected(
            f"container copy exceeded {CONTAINER_COPY_TIMEOUT_SECONDS} seconds"
        ) from exc
    if result.returncode != 0:
        detail = (result.stdout + b"\n" + result.stderr)[
            -MAX_CONTAINER_STDERR_BYTES:
        ].decode("utf-8", errors="replace")
        raise CaptureRejected(
            f"container copy failed for {destination}: {detail.strip()}"
        )


def _atomic_write_text(path: Path, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _docker_command(argv: list[str], *, timeout_seconds: float) -> ProcessResult:
    result = run_process(
        argv,
        cwd=Path.cwd(),
        timeout_seconds=timeout_seconds,
        env_allowlist=(
            "CONTAINER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
        ),
        max_stdout_bytes=512 * 1024,
        max_stderr_bytes=512 * 1024,
    )
    if result.runtime.process_tree_cleanup_succeeded is False:
        raise RuntimeError(
            "Docker client process-tree cleanup could not be confirmed: "
            f"{result.runtime.process_tree_cleanup_error}"
        )
    return result


def _pinned_lightcycles_image_ready() -> bool:
    inspected = _docker_command(
        [
            "docker",
            "image",
            "inspect",
            CODECLASH_LIGHTCYCLES_IMAGE,
            "--format",
            "{{json .Config.Labels}}",
        ],
        timeout_seconds=60,
    )
    if inspected.runtime.exit_code != 0 or inspected.runtime.timed_out:
        return False
    try:
        labels = json.loads(inspected.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        return False
    expected = {
        "org.opencontainers.image.revision": CODECLASH_LIGHTCYCLES_PIN,
        "org.opencontainers.image.atv.codeclash-pin": CODECLASH_PIN,
        "org.opencontainers.image.atv.base-digest": CODECLASH_UBUNTU_2204_DIGEST,
    }
    if not isinstance(labels, dict) or any(
        labels.get(name) != value for name, value in expected.items()
    ):
        return False
    commit = _docker_command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            CODECLASH_LIGHTCYCLES_IMAGE,
            "git",
            "rev-parse",
            "HEAD",
        ],
        timeout_seconds=60,
    )
    return (
        commit.runtime.exit_code == 0
        and not commit.runtime.timed_out
        and commit.stdout.strip() == CODECLASH_LIGHTCYCLES_PIN
    )


def _build_pinned_lightcycles_image(self) -> None:
    """Build the CodeClash game image from immutable ATV-owned inputs."""

    with self._build_lock:
        if _pinned_lightcycles_image_ready():
            return
        dockerfile = (
            Path(__file__).resolve().parent
            / "assets"
            / "codeclash-lightcycles.Dockerfile"
        )
        if not dockerfile.is_file() or dockerfile.is_symlink():
            raise RuntimeError("pinned CodeClash LightCycles Dockerfile is unavailable")
        with tempfile.TemporaryDirectory(prefix="atv-codeclash-image-") as temporary:
            context = Path(temporary)
            (context / "Dockerfile").write_bytes(dockerfile.read_bytes())
            self.logger.info(
                "Building pinned CodeClash LightCycles image "
                f"{CODECLASH_LIGHTCYCLES_PIN[:12]}"
            )
            built = _docker_command(
                [
                    "docker",
                    "build",
                    "--pull=false",
                    "--tag",
                    CODECLASH_LIGHTCYCLES_IMAGE,
                    str(context),
                ],
                timeout_seconds=900,
            )
            if built.runtime.exit_code != 0 or built.runtime.timed_out:
                detail = built.stderr or built.stdout
                raise RuntimeError(
                    "failed to build pinned CodeClash LightCycles image: "
                    + detail[-4000:]
                )
        if not _pinned_lightcycles_image_ready():
            raise RuntimeError(
                "built CodeClash LightCycles image did not verify its pinned identity"
            )


def _cleanup_environment_exact(environment) -> None:
    container_id = getattr(environment, "container_id", None)
    if not isinstance(container_id, str) or not container_id:
        return
    executable, _ = _container_cli(environment)
    removed = _docker_command(
        [executable, "rm", "-f", container_id],
        timeout_seconds=60,
    )
    inspected = _docker_command(
        [executable, "inspect", container_id],
        timeout_seconds=30,
    )
    combined = (inspected.stdout + "\n" + inspected.stderr).casefold()
    absent = inspected.runtime.exit_code != 0 and any(
        marker in combined
        for marker in (
            "no such object",
            "no such container",
            "no container with name or id",
        )
    )
    if not absent:
        raise RuntimeError(
            "CodeClash container cleanup could not be confirmed: "
            f"remove_exit={removed.runtime.exit_code} "
            f"inspect_exit={inspected.runtime.exit_code}"
        )
    environment.container_id = None


def _cleanup_tournament_environments(tournament) -> None:
    environments = [
        getattr(agent, "environment", None)
        for agent in getattr(tournament, "agents", ())
    ]
    game = getattr(tournament, "game", None)
    environments.append(getattr(game, "environment", None))
    errors: list[str] = []
    seen: set[str] = set()
    for environment in environments:
        container_id = getattr(environment, "container_id", None)
        if not isinstance(container_id, str) or not container_id or container_id in seen:
            continue
        seen.add(container_id)
        try:
            _cleanup_environment_exact(environment)
        except Exception as exc:
            errors.append(f"{container_id}: {exc}")
    if errors:
        raise RuntimeError(
            "CodeClash environment cleanup failed: " + "; ".join(errors)
        )


def _cleanup_codeclash_environment_best_effort(environment) -> None:
    try:
        _cleanup_environment_exact(environment)
    except Exception:
        # Normal tournament completion uses the strict wrapper above. This method is
        # also reached from upstream __del__, where raising would only print noise.
        return


def _bounded_copy_between_containers(
    src_container,
    dest_container,
    src_path: str | Path,
    dest_path: str | Path,
) -> None:
    """Copy a bounded text tree without host-side recursive ``docker cp``."""

    source = _safe_container_directory(src_path)
    destination = _safe_container_directory(dest_path)
    tree = _DockerTreeContainer(src_container, source).read_tree()
    reset = dest_container.execute(
        {
            "command": (
                f"rm -rf -- {shlex.quote(destination)} && "
                f"mkdir -p -- {shlex.quote(destination)}"
            )
        },
        cwd="/",
    )
    if reset.get("returncode") != 0:
        raise CaptureRejected(
            f"destination container tree reset failed: {reset.get('output', '')}"
        )
    for relative, content in sorted(tree.items()):
        target = (PurePosixPath(destination) / relative).as_posix()
        parent = str(PurePosixPath(target).parent)
        created = dest_container.execute(
            {"command": f"mkdir -p -- {shlex.quote(parent)}"},
            cwd="/",
        )
        if created.get("returncode") != 0:
            raise CaptureRejected(
                f"destination directory creation failed: {created.get('output', '')}"
            )
        _write_bytes_to_container(
            dest_container,
            target,
            content.encode("utf-8"),
        )


def resolve_player_class(agent_key: str):
    """Return the HarnessPlayer class for a harness key, or None for a builtin key.

    None signals "fall through to CodeClash's own get_agent" (dummy/mini).
    """
    if agent_key not in BUILDER_HARNESSES:
        return None
    if agent_key not in _player_class_cache:
        _player_class_cache[agent_key] = _make_harness_player(agent_key)
    return _player_class_cache[agent_key]


def register() -> None:
    """Patch codeclash.tournaments.pvp.get_agent to resolve ATV-bench harnesses.

    Idempotent: a second call does not double-wrap.
    """
    global _original_get_agent, _original_logging_raise_exceptions
    cc = import_codeclash()
    if _original_get_agent is not None:
        return  # already registered
    _original_get_agent = cc.pvp.get_agent
    original = _original_get_agent

    def patched_get_agent(config, game_context, environment):
        player_cls = resolve_player_class(config.get("agent"))
        if player_cls is None:
            return original(config, game_context, environment)
        return player_cls(config, environment, game_context)

    cc.pvp.get_agent = patched_get_agent
    from codeclash.arenas import arena as arena_module
    from codeclash.arenas.lightcycles.lightcycles import LightCyclesArena
    from codeclash.tournaments.pvp import PvpTournament
    from codeclash.utils import atomic_write as atomic_write_module
    from codeclash.utils import environment as environment_module

    for module in (arena_module, cc.pvp, environment_module):
        if hasattr(module, "copy_between_containers"):
            _original_copy_between[module] = module.copy_between_containers
            module.copy_between_containers = _bounded_copy_between_containers
    for module in (cc.pvp, environment_module):
        if hasattr(module, "copy_to_container"):
            _original_copy_to[module] = module.copy_to_container
            module.copy_to_container = _bounded_copy_to_container
    for module in (cc.pvp, atomic_write_module):
        if hasattr(module, "atomic_write"):
            _original_atomic_write[module] = module.atomic_write
            module.atomic_write = _atomic_write_text
    _original_build_image[LightCyclesArena] = LightCyclesArena.build_image
    LightCyclesArena.build_image = _build_pinned_lightcycles_image
    environment_class = arena_module.ClashDockerEnvironment
    _original_environment_cleanup[environment_class] = environment_class.cleanup
    environment_class.cleanup = _cleanup_codeclash_environment_best_effort
    original_end = PvpTournament.end
    _original_pvp_end[PvpTournament] = original_end

    def end_with_exact_cleanup(tournament) -> None:
        try:
            original_end(tournament)
        finally:
            _cleanup_tournament_environments(tournament)

    PvpTournament.end = end_with_exact_cleanup
    if _original_logging_raise_exceptions is None:
        _original_logging_raise_exceptions = logging.raiseExceptions
    logging.raiseExceptions = False


def unregister() -> None:
    """Restore CodeClash's original get_agent."""
    global _original_get_agent, _original_logging_raise_exceptions
    if _original_get_agent is None:
        return
    cc = import_codeclash()
    cc.pvp.get_agent = _original_get_agent
    _original_get_agent = None
    for module, original in tuple(_original_copy_between.items()):
        module.copy_between_containers = original
    _original_copy_between.clear()
    for module, original in tuple(_original_copy_to.items()):
        module.copy_to_container = original
    _original_copy_to.clear()
    for module, original in tuple(_original_atomic_write.items()):
        module.atomic_write = original
    _original_atomic_write.clear()
    for arena_class, original in tuple(_original_build_image.items()):
        arena_class.build_image = original
    _original_build_image.clear()
    for tournament_class, original in tuple(_original_pvp_end.items()):
        tournament_class.end = original
    _original_pvp_end.clear()
    for environment_class, original in tuple(_original_environment_cleanup.items()):
        environment_class.cleanup = original
    _original_environment_cleanup.clear()
    if _original_logging_raise_exceptions is not None:
        logging.raiseExceptions = _original_logging_raise_exceptions
        _original_logging_raise_exceptions = None


def _make_harness_player(adapter_key: str):
    """Build a CodeClash Player subclass bound to a harness adapter."""
    cc = import_codeclash()
    adapter_cls = ADAPTERS[adapter_key]

    class HarnessPlayer(cc.Player):
        def __init__(self, config, environment, game_context):
            super().__init__(config, environment, game_context)
            cfg = self.config.get("config", {}) if isinstance(self.config, dict) else {}
            budget = cfg.get("budget", {})
            self._atv_core = HarnessPlayerCore(
                adapter=adapter_cls(),
                adapter_factory=adapter_cls,
                container=_DockerTreeContainer(
                    self.environment,
                    self._workdir(),
                    logs_root="/logs",
                ),
                bot_file=cfg.get("bot_file", "main.py"),
                goal=self.game_context.prompts.get("edit", "Improve the bot."),
                model=cfg.get("model", "auto"),
                budget=Budget(
                    max_turns=int(budget.get("max_turns", 10)),
                    max_seconds=int(budget.get("max_seconds", 300)),
                    max_tokens=int(budget.get("max_tokens", 200_000)),
                ),
                player_id=self.name,
                game=self.game_context.name,
                prompt_version=self.game_context.prompts.get(
                    "_version", "edit@1"
                ),
                adaptation=cfg.get("adaptation", "iterative"),
                adapter_version=cfg.get("adapter_version", "1.0.0"),
                harness_manifest_digest=cfg.get(
                    "harness_manifest_digest", "0" * 64
                ),
                harness_config_digest=cfg.get(
                    "harness_config_digest", "0" * 64
                ),
                model_policy_digest=cfg.get("model_policy_digest", "0" * 64),
                task_digest=cfg.get("task_digest", "0" * 64),
                prompt_digest=cfg.get("prompt_digest"),
                protocol_version=cfg.get("protocol_version", "atv.harness/v1"),
                manifest_capabilities=cfg.get("manifest_capabilities", {}),
            )
            self._metadata["atv"] = {
                "adaptation": self._atv_core.adaptation,
                "trial_unit": "tournament",
                "round_observation_unit": "nested-round",
                "requested_model": self._atv_core.model,
                "requested_model_verified": False,
                "rounds": {},
            }

        def run(self) -> None:
            round_number = int(self.game_context.round)
            self._atv_core.edit_turn(round_number=round_number)
            evidence = self._atv_core.last_round_evidence
            if evidence is not None:
                self._metadata["atv"]["rounds"][round_number] = evidence.to_dict()

        def _workdir(self) -> str:
            wd = getattr(self.game_context, "working_dir", None)
            return wd or "/workdir"

    HarnessPlayer.__name__ = f"HarnessPlayer_{adapter_key}"
    return HarnessPlayer


def _read_codeclash_text_tree(
    root: Path,
    captured_paths: tuple[str, ...],
) -> dict[str, str]:
    for relative in captured_paths:
        data = read_confined_regular_file(
            root,
            relative,
            max_bytes=MAX_CODECLASH_ARCHIVE_FILE_BYTES,
        )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            is_binary = True
        else:
            is_binary = "\x00" in text
        if is_binary:
            root.joinpath(*PurePosixPath(relative).parts).unlink()
    accepted = scan_captured_tree(
        root,
        max_files=MAX_PLAYER_TREE_FILES,
        max_total_bytes=MAX_PLAYER_TREE_TOTAL_BYTES,
        max_file_bytes=MAX_PLAYER_TREE_FILE_BYTES,
        max_entries=MAX_PLAYER_TREE_ENTRIES,
        max_directories=MAX_PLAYER_TREE_DIRECTORIES,
        allowed_text_suffixes=None,
    )
    return {
        item.relpath: read_confined_regular_file(
            root,
            item.relpath,
            max_bytes=MAX_PLAYER_TREE_FILE_BYTES,
        ).decode("utf-8")
        for item in accepted
    }


class _DockerTreeContainer:  # pragma: no cover - requires Docker
    """Adapts CodeClash's DockerEnvironment to players.TreeContainerLike (tree-level)."""

    def __init__(self, env, workdir: str, *, logs_root: str = "/logs"):
        self.env = env
        self.workdir = workdir
        self.logs_root = logs_root

    def read_tree(self) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "work"
            captured = _bounded_copy_from_container(
                self.env,
                self.workdir,
                dest,
                archive_limit=MAX_CODECLASH_ARCHIVE_BYTES,
                max_entries=MAX_CODECLASH_ARCHIVE_ENTRIES,
                max_directories=MAX_CODECLASH_ARCHIVE_DIRECTORIES,
                max_files=MAX_CODECLASH_ARCHIVE_FILES,
                max_total_bytes=MAX_CODECLASH_ARCHIVE_TOTAL_BYTES,
                max_file_bytes=MAX_CODECLASH_ARCHIVE_FILE_BYTES,
            )
            return _read_codeclash_text_tree(dest, captured)

    def write_tree(self, files: dict[str, str]) -> None:
        current = self.read_tree()
        for rel in sorted(set(current) - set(files)):
            command = f"rm -f -- {shlex.quote(f'{self.workdir}/{rel}')}"
            result = self.env.execute(
                {"command": command},
                cwd=self.workdir,
            )
            if result.get("returncode") != 0:
                raise RuntimeError(
                    f"CodeClash container deletion failed for {rel}: "
                    f"{result.get('output', '')}"
                )
        for rel, content in files.items():
            dest = f"{self.workdir}/{rel}"
            parent = str(PurePosixPath(dest).parent)
            result = self.env.execute(
                {"command": f"mkdir -p -- {shlex.quote(parent)}"},
                cwd=self.workdir,
            )
            if result.get("returncode") != 0:
                raise RuntimeError(
                    f"CodeClash container directory creation failed for {rel}: "
                    f"{result.get('output', '')}"
                )
            _write_bytes_to_container(self.env, dest, content.encode("utf-8"))

    def read_feedback(self, previous_round: int) -> dict[str, str]:
        """Read only the trusted round-log subtree CodeClash copied for this player."""
        source = f"{self.logs_root}/rounds/{previous_round}"
        out: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "round"
            try:
                captured = _bounded_copy_from_container(self.env, source, dest)
            except CaptureRejected:
                return {}
            for relative in sorted(captured):
                try:
                    data = read_confined_regular_file(
                        dest,
                        relative,
                        max_bytes=MAX_FILE_BYTES,
                    )
                    out[relative] = data.decode("utf-8")
                except (CaptureRejected, UnicodeDecodeError, OSError):
                    continue
        return out
