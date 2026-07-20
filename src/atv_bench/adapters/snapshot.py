"""Bounded, safe snapshot-diff capture for harness-edited repositories.

The base-tree-to-working-tree comparison captures committed, staged, and
unstaged tracked edits.  Untracked, non-ignored regular files are added
separately.  No untracked path is read until the confinement layer has rejected
links, junctions, hardlinks, special files, path traversal, and oversized data.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from atv_bench.capture import (
    MAX_FILE_BYTES,
    CaptureRejected,
    read_confined_regular_file,
)

BASE_TAG = "atv-base"
MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_PATH_LIST_BYTES = 512 * 1024
MAX_GIT_STDERR_BYTES = 64 * 1024
DEFAULT_GIT_TIMEOUT_SECONDS = 30.0


class SnapshotRejected(RuntimeError):
    """The repository could not be captured safely."""


class DiffLimitExceeded(SnapshotRejected):
    """The rendered diff exceeded the configured byte limit."""


class _BoundedBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.data = bytearray()
        self.total = 0
        self.overflow = False

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.overflow = True


def _drain(stream, buffer: _BoundedBuffer) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            buffer.feed(chunk)
    finally:
        stream.close()


def _git_environment() -> dict[str, str]:
    # Git gets only process-launch essentials. Global/system config, ambient
    # config injection, pagers, prompts, and system attributes are disabled.
    names = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "TMPDIR", "LANG", "LC_ALL",
    }
    env = {name: value for name, value in os.environ.items() if name.upper() in names}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_COUNT"] = "0"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    return env


def _git_command(
    executable: str,
    repo: Path,
    hooks_dir: str,
    args: tuple[str, ...],
) -> list[str]:
    # Repository config still has to be read for object-format/worktree basics,
    # so explicitly override every helper surface used by these commands.
    safe_config = (
        ("core.fsmonitor", "false"),
        ("core.hooksPath", hooks_dir),
        ("core.autocrlf", "input"),
        ("core.safecrlf", "false"),
        ("tag.gpgSign", "false"),
        ("commit.gpgSign", "false"),
        ("core.pager", "cat"),
        ("pager.diff", "false"),
        ("pager.tag", "false"),
        ("pager.ls-files", "false"),
        ("diff.external", ""),
        ("interactive.diffFilter", ""),
        ("core.attributesFile", os.devnull),
        ("core.excludesFile", os.devnull),
        ("credential.helper", ""),
        ("submodule.recurse", "false"),
        ("diff.submodule", "short"),
    )
    command = [executable, "--no-pager"]
    for key, value in safe_config:
        command.extend(["-c", f"{key}={value}"])
    command.extend(["-C", str(repo), *args])
    return command


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    max_stdout: int = MAX_DIFF_BYTES,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if executable is None:
        raise SnapshotRejected("git executable not found")
    disabled_hooks = tempfile.TemporaryDirectory(prefix="atv-git-disabled-hooks-")
    command = _git_command(executable, repo, disabled_hooks.name, args)
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except OSError as exc:
        disabled_hooks.cleanup()
        raise SnapshotRejected(f"could not execute git: {exc}") from exc

    try:
        assert proc.stdout is not None and proc.stderr is not None
        stdout = _BoundedBuffer(max_stdout)
        stderr = _BoundedBuffer(MAX_GIT_STDERR_BYTES)
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                )
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
        for reader in readers:
            reader.join(timeout=5)
    finally:
        disabled_hooks.cleanup()

    if timed_out:
        raise SnapshotRejected(
            f"git {' '.join(args[:2])} exceeded {timeout_seconds} seconds"
        )
    if stdout.overflow:
        raise DiffLimitExceeded(f"git output exceeded {max_stdout} bytes")
    if stderr.overflow:
        raise SnapshotRejected(f"git stderr exceeded {MAX_GIT_STDERR_BYTES} bytes")
    if check and returncode != 0:
        detail = bytes(stderr.data).decode("utf-8", errors="replace").strip()
        raise SnapshotRejected(
            f"git {' '.join(args[:2])} failed with exit {returncode}"
            + (f": {detail}" if detail else "")
        )
    return subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=bytes(stdout.data),
        stderr=bytes(stderr.data),
    )


def _decode(data: bytes, *, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotRejected(f"{label} was not valid UTF-8") from exc


def _resolve_base(repo: Path, base: str) -> str:
    if not isinstance(base, str) or not base.strip() or "\x00" in base:
        raise SnapshotRejected("invalid base revision")
    # Resolve once and pass only the resulting hex object id to `git diff`, so a
    # caller cannot smuggle command-line options through a revision string.
    resolved = _git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base}^{{commit}}",
        max_stdout=256,
    )
    value = _decode(resolved.stdout, label="base revision").strip()
    if not value or any(ch not in "0123456789abcdefABCDEF" for ch in value):
        raise SnapshotRejected("base revision did not resolve to an object id")
    return value


def seed_base(repo: Path) -> str:
    """Pin and return the clean seed commit used for later diff capture."""

    repo = Path(repo)
    sha = _resolve_base(repo, "HEAD")
    # Keep the object reachable even if an untrusted harness runs aggressive GC.
    _git(repo, "tag", "-f", BASE_TAG, sha, max_stdout=4096)
    return sha


def _quote_git_path(path: str) -> str:
    safe = all(0x21 <= ord(ch) < 0x7F and ch not in {'"', "\\"} for ch in path)
    return path if safe else json.dumps(path, ensure_ascii=False)


def _untracked_addition(rel: str, data: bytes) -> str:
    if b"\x00" in data:
        raise SnapshotRejected(f"binary untracked file not allowed: {rel}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotRejected(f"binary untracked file not allowed: {rel}") from exc
    rel = rel.replace("\\", "/")
    a_path = _quote_git_path(f"a/{rel}")
    b_path = _quote_git_path(f"b/{rel}")
    header = (
        f"diff --git {a_path} {b_path}\n"
        "new file mode 100644\n"
    )
    lines = text.splitlines(keepends=True)
    body = "".join(
        difflib.unified_diff(
            [],
            lines,
            fromfile="/dev/null",
            tofile=f"b/{rel}",
            lineterm="\n",
        )
    )
    if text and not text.endswith(("\n", "\r")):
        body += "\\ No newline at end of file\n"
    return header + body


def _append_bounded(chunks: list[str], chunk: str, used: int, limit: int) -> int:
    try:
        size = len(chunk.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SnapshotRejected("captured diff contains a non-UTF-8 path") from exc
    if used + size > limit:
        raise DiffLimitExceeded(f"captured diff exceeded {limit} bytes")
    chunks.append(chunk)
    return used + size


def _validate_changed_worktree_paths(repo: Path, base: str) -> None:
    """Reject changed tracked entries that are not confined regular files.

    Deletions have no working-tree object to inspect and are safe to represent.
    The diff filter therefore asks only for paths that exist in the resulting
    tree/worktree (including type changes and rename destinations).
    """

    changed = _git(
        repo,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--diff-filter=ACMRTUXB",
        base,
        "--",
        max_stdout=MAX_PATH_LIST_BYTES,
    )
    for raw in changed.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        try:
            read_confined_regular_file(repo, rel, max_bytes=MAX_FILE_BYTES)
        except CaptureRejected as exc:
            raise SnapshotRejected(f"unsafe changed path {rel!r}: {exc}") from exc


def capture_diff(
    repo: Path,
    base: str,
    *,
    max_bytes: int = MAX_DIFF_BYTES,
) -> str:
    """Capture committed, staged, unstaged, and untracked changes since ``base``."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    repo = Path(repo)
    resolved_base = _resolve_base(repo, base)
    _validate_changed_worktree_paths(repo, resolved_base)

    # `--no-ext-diff` and `--no-textconv` prevent repository configuration from
    # executing attacker-chosen helpers during trusted capture.
    tracked_proc = _git(
        repo,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        resolved_base,
        "--",
        max_stdout=max_bytes,
    )
    tracked = _decode(tracked_proc.stdout, label="tracked diff")
    chunks: list[str] = []
    used = _append_bounded(chunks, tracked, 0, max_bytes)

    others_proc = _git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        max_stdout=MAX_PATH_LIST_BYTES,
    )
    for raw in others_proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        remaining = max_bytes - used
        try:
            data = read_confined_regular_file(repo, rel, max_bytes=remaining)
        except CaptureRejected as exc:
            raise SnapshotRejected(f"unsafe untracked path {rel!r}: {exc}") from exc
        used = _append_bounded(chunks, _untracked_addition(rel, data), used, max_bytes)

    return "".join(chunks)
