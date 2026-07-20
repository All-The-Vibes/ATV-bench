"""Fail-closed, local-only launch verification evidence.

This module deliberately separates *verification evidence* from benchmark
execution.  It runs a fixed argv-only plan, records bounded command output and
content digests, and emits the ``atv.launch-proof/v1`` shape consumed by
``atv_bench.launch_audit``.  It never turns local implementation tests into an
official benchmark run, an external reproduction, or a human review.
"""
from __future__ import annotations

import dataclasses
import contextlib
import functools
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from atv_bench.adapters.contract import (
    CleanupStatus,
    _WindowsJob,
    _cleanup_descendants_after_exit,
    _resume_windows_process,
    _terminate_process_tree,
)
from atv_bench.capture import CaptureRejected, read_confined_regular_file


MANIFEST_SCHEMA = "atv.launch-evidence-manifest/v1"
PROOF_SCHEMA = "atv.launch-proof/v1"
COMMAND_SCHEMA = "atv.local-verification-command/v1"
PLAN_SCHEMA = "atv.local-verification-plan/v1"
RESUME_INDEX_SCHEMA = "atv.local-verification-resume-index/v1"
CANONICALIZATION_SCHEMA = "atv.local-verification-canonical-json/v1"
ENVIRONMENT_SCHEMA = "atv.local-verification-environment/v1"
INVOCATION_SCHEMA = "atv.local-verification-invocation/v1"
REPOSITORY_SCHEMA = "atv.local-verification-repository/v1"
STREAM_SCHEMA = "atv.local-verification-stream/v2"
TERMINATION_SCHEMA = "atv.local-verification-termination/v1"
TOOLCHAIN_SCHEMA = "atv.local-verification-toolchain/v1"
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_JUNIT_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 60
RESUME_FRESHNESS_DAYS = 7
MANIFEST_FRESHNESS_DAYS = 30
SAFE_OUTPUT_PREFIX = PurePosixPath("reports/local-verification")
GENERATED_SOURCE_EXCLUSIONS = ("docs/CREDIBILITY_STATUS.md",)
VERIFICATION_TEMP_DIRNAME = "atv-vfy"
FILESYSTEM_ID_LENGTH = 12
_EPHEMERAL_COMMAND_IDS = frozenset(
    {
        "wheel_venv_create",
        "wheel_install",
        "wheel_verify",
        "sdist_venv_create",
        "sdist_install",
        "sdist_verify",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMAND_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PYTHON_IMAGE = (
    "docker.io/library/python@sha256:"
    "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
)
PYTHON_IMAGE_DIGEST = PYTHON_IMAGE.rsplit("@sha256:", 1)[1]


class VerificationError(ValueError):
    """The evidence is unsafe, malformed, stale, or not adequately bound."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: Any, *, label: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"{label} is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON with no platform-specific whitespace."""

    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_descriptor(value: Any) -> dict[str, str]:
    return {
        "schema": CANONICALIZATION_SCHEMA,
        "algorithm": "sha256",
        "value": sha256_bytes(canonical_json_bytes(value)),
    }


def _validate_digest_descriptor(
    descriptor: Any,
    value: Any,
    *,
    label: str,
) -> str:
    if not isinstance(descriptor, Mapping):
        raise VerificationError(f"{label} canonical digest descriptor is absent")
    if descriptor.get("schema") != CANONICALIZATION_SCHEMA:
        raise VerificationError(f"{label} canonicalization schema is invalid")
    if descriptor.get("algorithm") != "sha256":
        raise VerificationError(f"{label} canonical digest algorithm is invalid")
    observed = descriptor.get("value")
    if not isinstance(observed, str) or not SHA256_RE.fullmatch(observed):
        raise VerificationError(f"{label} canonical digest is invalid")
    expected = _digest_descriptor(value)["value"]
    if observed != expected:
        raise VerificationError(
            f"{label} canonical digest mismatch: expected={expected} observed={observed}"
        )
    return observed


def _sha256_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise VerificationError(f"artifact is unreadable: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError(f"artifact is not a regular non-link file: {path}")
    if getattr(info, "st_nlink", 1) != 1:
        raise VerificationError(f"hardlinked artifact is not accepted: {path}")
    if info.st_size > max_bytes:
        raise VerificationError(
            f"artifact exceeds {max_bytes} bytes: {path} ({info.st_size})"
        )
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > max_bytes:
                    raise VerificationError(
                        f"artifact grew beyond {max_bytes} bytes: {path}"
                    )
                digest.update(chunk)
    except OSError as exc:
        raise VerificationError(f"artifact could not be hashed: {path}: {exc}") from exc
    return digest.hexdigest(), observed


def _atomic_write(path: Path, data: bytes) -> None:
    """Durably replace ``path`` from a same-directory temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=os.fspath(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_relative(path: Path, root: Path, *, label: str) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise VerificationError(f"{label} escapes the repository: {path}") from exc
    value = relative.as_posix()
    if not value or value == "." or ".." in PurePosixPath(value).parts:
        raise VerificationError(f"{label} is not a safe repository path: {path}")
    return value


def _validate_output_root(repo_root: Path, output_root: Path) -> Path:
    repo = repo_root.resolve()
    output = output_root if output_root.is_absolute() else repo / output_root
    output = output.resolve(strict=False)
    relative = _safe_relative(output, repo, label="verification output directory")
    parts = PurePosixPath(relative).parts
    prefix = SAFE_OUTPUT_PREFIX.parts
    if parts[: len(prefix)] != prefix:
        raise VerificationError(
            "verification output must remain under "
            f"{SAFE_OUTPUT_PREFIX.as_posix()}: {relative}"
        )
    cursor = repo
    for part in parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise VerificationError(
                f"verification output path contains a symlink: {cursor}"
            )
    output.mkdir(parents=True, exist_ok=True)
    return output


def _artifact_reference(
    repo_root: Path,
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> dict[str, Any]:
    digest, size = _sha256_file(path, max_bytes=max_bytes)
    return {
        "label": label,
        "artifact": _safe_relative(path, repo_root, label=label),
        "sha256": digest,
        "size_bytes": size,
    }


def _read_artifact(
    repo_root: Path,
    reference: Mapping[str, Any],
    *,
    label: str,
    max_bytes: int = MAX_JSON_BYTES,
) -> bytes:
    artifact = reference.get("artifact")
    digest = reference.get("sha256")
    if not isinstance(artifact, str) or not artifact:
        raise VerificationError(f"{label} has no artifact path")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise VerificationError(f"{label} has no valid lowercase SHA-256 digest")
    try:
        data = read_confined_regular_file(repo_root, artifact, max_bytes=max_bytes)
    except (CaptureRejected, OSError) as exc:
        raise VerificationError(f"{label} is not a confined regular file: {exc}") from exc
    observed = sha256_bytes(data)
    if observed != digest:
        raise VerificationError(
            f"{label} digest mismatch: expected={digest} observed={observed}"
        )
    return data


def _git(repo_root: Path, *argv: str, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    environment, _ = _environment_bundle(os.environ, spec=None)
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    raw = BoundedSubprocessExecutor(
        maximum_capture_bytes=max_bytes,
    ).execute(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            *argv,
        ],
        cwd=repo_root,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        max_output_bytes=max_bytes,
        env=environment,
    )
    if raw.error is not None or raw.timed_out or raw.exit_code is None:
        raise VerificationError(
            f"git {' '.join(argv)} could not complete safely: "
            f"{raw.error or 'no exit status'}"
        )
    if raw.exit_code != 0:
        detail = raw.stderr.decode("utf-8", errors="replace").strip()
        raise VerificationError(
            f"git {' '.join(argv)} failed with {raw.exit_code}: {detail}"
        )
    if raw.stdout_truncated or raw.stdout_total_bytes > max_bytes:
        raise VerificationError(f"git {' '.join(argv)} output exceeded {max_bytes} bytes")
    return raw.stdout


def _is_excluded(relative: str, excluded_paths: Sequence[str]) -> bool:
    value = PurePosixPath(relative)
    for excluded in excluded_paths:
        prefix = PurePosixPath(excluded)
        if value == prefix or prefix in value.parents:
            return True
    return False


def _repository_exclusions(output_relative: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys((output_relative, *GENERATED_SOURCE_EXCLUSIONS))
    )


def _decode_git_path_list(data: bytes) -> list[str]:
    return [
        entry.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for entry in data.split(b"\0")
        if entry
    ]


def _tracked_git_modes(repo_root: Path) -> dict[str, str]:
    modes: dict[str, str] = {}
    for entry in _git(repo_root, "ls-files", "-s", "-z", "--cached").split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_path = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) < 3:
            raise VerificationError("git ls-files returned a malformed index record")
        mode = fields[0].decode("ascii", errors="strict")
        relative = raw_path.decode(
            "utf-8", errors="surrogateescape"
        ).replace("\\", "/")
        modes[relative] = mode
    return modes


def _worktree_mode(path: Path, info: os.stat_result, tracked_mode: str | None) -> str:
    if tracked_mode:
        return tracked_mode
    if stat.S_ISLNK(info.st_mode):
        return "120000"
    if stat.S_ISREG(info.st_mode):
        executable = bool(
            stat.S_IMODE(info.st_mode)
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
        return "100755" if executable else "100644"
    return f"special:{stat.S_IFMT(info.st_mode)}"


def repository_snapshot(
    repo_root: Path | str,
    *,
    excluded_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Bind evidence to the exact repository, commit, and working tree bytes."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise VerificationError(f"repository root is not a directory: {root}")
    top = Path(
        _git(root, "rev-parse", "--show-toplevel").decode(
            "utf-8", errors="strict"
        ).strip()
    ).resolve()
    if top != root:
        raise VerificationError(
            f"repository root mismatch: requested={root} git={top}"
        )
    head = _git(root, "rev-parse", "HEAD").decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise VerificationError(f"git HEAD is not a full commit id: {head!r}")
    head_tree = (
        _git(root, "rev-parse", "HEAD^{tree}")
        .decode("ascii", errors="strict")
        .strip()
    )
    if not re.fullmatch(r"[0-9a-f]{40}", head_tree):
        raise VerificationError(f"git HEAD tree is not a full object id: {head_tree!r}")
    try:
        origin = (
            _git(root, "remote", "get-url", "origin", max_bytes=64 * 1024)
            .decode("utf-8", errors="strict")
            .strip()
        )
    except VerificationError:
        origin = ""
    workspace_id = sha256_bytes(
        os.path.normcase(os.fspath(root)).encode("utf-8", errors="surrogatepass")
    )
    if not origin:
        origin = f"local-worktree://{workspace_id}"

    listed = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    names = sorted(set(_decode_git_path_list(listed)))
    tracked_modes = _tracked_git_modes(root)
    digest = hashlib.sha256()
    included = 0
    for relative in names:
        if _is_excluded(relative, excluded_paths):
            continue
        path = root / Path(*PurePosixPath(relative).parts)
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        try:
            info = path.lstat()
        except OSError:
            digest.update(b"missing\0")
            digest.update((tracked_modes.get(relative) or "unknown").encode("ascii"))
            digest.update(b"\0")
            included += 1
            continue
        digest.update(
            _worktree_mode(path, info, tracked_modes.get(relative)).encode("ascii")
        )
        digest.update(b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"file\0")
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(128 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        else:
            digest.update(f"special:{stat.S_IFMT(info.st_mode)}".encode("ascii"))
        digest.update(b"\0")
        included += 1

    staged_paths = set(
        _decode_git_path_list(
            _git(
                root,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--diff-filter=ACDMRTUXB",
                "--",
            )
        )
    )
    worktree_paths = set(
        _decode_git_path_list(
            _git(
                root,
                "diff",
                "--name-only",
                "-z",
                "--diff-filter=ACDMRTUXB",
                "--",
            )
        )
    )
    untracked_paths = set(
        _decode_git_path_list(
            _git(root, "ls-files", "-z", "--others", "--exclude-standard")
        )
    )
    staged_paths = {
        path for path in staged_paths if not _is_excluded(path, excluded_paths)
    }
    worktree_paths = {
        path for path in worktree_paths if not _is_excluded(path, excluded_paths)
    }
    untracked_paths = {
        path for path in untracked_paths if not _is_excluded(path, excluded_paths)
    }
    dirty_paths = staged_paths | worktree_paths | untracked_paths
    dirty_state = {
        "staged": sorted(staged_paths),
        "worktree": sorted(worktree_paths),
        "untracked": sorted(untracked_paths),
    }
    tree_digest = digest.hexdigest()
    repository_id = sha256_bytes(
        canonical_json_bytes(
            {
                "origin": origin,
                "head": head,
                "head_tree": head_tree,
                "tree_digest": tree_digest,
            }
        )
    )
    return {
        "schema": REPOSITORY_SCHEMA,
        "origin": origin,
        "head": head,
        "head_tree": head_tree,
        "tree_digest": tree_digest,
        "repository_id": repository_id,
        "workspace_id": workspace_id,
        "dirty": bool(dirty_paths),
        "dirty_path_count": len(dirty_paths),
        "staged_path_count": len(staged_paths),
        "worktree_path_count": len(worktree_paths),
        "untracked_path_count": len(untracked_paths),
        "dirty_state_sha256": sha256_bytes(canonical_json_bytes(dirty_state)),
        "file_count": included,
        "excluded_paths": sorted(set(excluded_paths)),
    }


def _github_slug(origin: str) -> str | None:
    normalized = origin.strip().replace("\\", "/")
    match = re.search(
        r"(?:github\.com[:/])(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
        normalized,
        flags=re.IGNORECASE,
    )
    return match.group("slug") if match else None


@dataclasses.dataclass(frozen=True, slots=True)
class CommandSpec:
    id: str
    category: str
    description: str
    argv: tuple[str, ...]
    timeout_seconds: int
    modes: frozenset[str]
    pytest_targets: tuple[str, ...] = ()
    junit: bool = False
    docker: bool = False
    output_artifacts: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    cwd: str = "{REPO}"
    clear_pythonpath: bool = False

    def __post_init__(self) -> None:
        if not COMMAND_ID_RE.fullmatch(self.id):
            raise VerificationError(f"invalid fixed command id: {self.id!r}")
        if not self.argv or any(
            not isinstance(item, str)
            or not item
            or "\0" in item
            or "\r" in item
            or "\n" in item
            for item in self.argv
        ):
            raise VerificationError(f"{self.id} has unsafe argv tokens")
        if not self.modes or not self.modes <= {"quick", "full"}:
            raise VerificationError(f"{self.id} has invalid modes")
        if self.junit and "{JUNIT}" not in self.argv:
            raise VerificationError(f"{self.id} declares JUnit without {{JUNIT}}")
        if not self.junit and "{JUNIT}" in self.argv:
            raise VerificationError(f"{self.id} has an undeclared JUnit output")
        if self.cwd not in {"{REPO}", "{ISOLATED_CWD}"}:
            raise VerificationError(f"{self.id} has an unsafe fixed cwd token")

    def to_plan_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "pytest_targets": list(self.pytest_targets),
            "junit": self.junit,
            "docker": self.docker,
            "output_artifacts": list(self.output_artifacts),
            "dependencies": list(self.dependencies),
            "cwd": self.cwd,
            "clear_pythonpath": self.clear_pythonpath,
            "environment_policy": ENVIRONMENT_SCHEMA,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_plan_dict()))


def _pytest_command(
    id_: str,
    category: str,
    description: str,
    targets: Sequence[str],
    *,
    marker: str | None = None,
    timeout_seconds: int = 900,
    modes: Iterable[str] = ("quick", "full"),
    docker: bool = False,
) -> CommandSpec:
    argv = [
        "{PYTHON}",
        "-m",
        "pytest",
        "-q",
        "--strict-markers",
        "-p",
        "no:cacheprovider",
        "-o",
        "junit_family=xunit2",
        "--basetemp",
        "{BASETEMP}",
        *targets,
    ]
    if marker:
        argv.extend(("-m", marker))
    argv.extend(("--junitxml", "{JUNIT}"))
    return CommandSpec(
        id=id_,
        category=category,
        description=description,
        argv=tuple(argv),
        timeout_seconds=timeout_seconds,
        modes=frozenset(modes),
        pytest_targets=tuple(targets),
        junit=True,
        docker=docker,
    )


PACKAGE_VERIFY_CODE = (
    "import json;"
    "from importlib.resources import files;"
    "from atv_bench.protocol import SchemaStore;"
    "from atv_bench.protocol._embedded_schemas import embedded_schema_texts;"
    "texts=embedded_schema_texts();"
    "assert len(texts)==7;"
    "store=SchemaStore.from_texts(texts);"
    "assert str(store.directory)=='<embedded>';"
    "asset=files('atv_bench').joinpath('assets/codeclash-lightcycles.Dockerfile');"
    "assert asset.is_file();"
    "print(json.dumps({'embedded_schema_count':len(texts),'installed':True,"
    "'schema_source':str(store.directory),'codeclash_asset':True},"
    "sort_keys=True,separators=(',',':')))"
)
CODECLASH_ASSET_VERIFY_CODE = (
    "import inspect,json;"
    "from pathlib import Path;"
    "from atv_bench.codeclash_env import CODECLASH_PIN,import_codeclash,"
    "resolve_codeclash_source;"
    "import_codeclash();"
    "from codeclash.arenas.lightcycles.lightcycles import LightCyclesArena;"
    "source=resolve_codeclash_source();"
    "module=Path(inspect.getfile(LightCyclesArena)).resolve();"
    "asset=module.parent/'LightCycles.Dockerfile';"
    "assert source in module.parents and asset.is_file() and not asset.is_symlink();"
    "print(json.dumps({'arena_assets_verified':True,'codeclash_pin':CODECLASH_PIN},"
    "sort_keys=True,separators=(',',':')))"
)


_FIXED_COMMANDS: tuple[CommandSpec, ...] = (
    _pytest_command(
        "protocol_focused",
        "protocol",
        "Versioned schemas, canonical JSON, capability negotiation, and JSONL protocol.",
        ("tests/protocol",),
    ),
    _pytest_command(
        "eval_focused",
        "evaluation",
        "Trial identity, scheduler, grader, bundle, statistics, report, and export.",
        (
            "tests/test_eval_end_to_end.py",
            "tests/test_eval_grader_bundle.py",
            "tests/test_eval_protocol_export.py",
            "tests/test_eval_report.py",
            "tests/test_eval_stats.py",
            "tests/test_eval_tasks.py",
            "tests/test_eval_trial_scheduler.py",
        ),
        timeout_seconds=1500,
    ),
    _pytest_command(
        "security_focused",
        "security",
        "Credential/Responses gateways, provider backend, operator, confinement, OCI policy, and controller security.",
        (
            "tests/test_security_gateway.py",
            "tests/test_responses_gateway.py",
            "tests/test_openai_responses_backend.py",
            "tests/test_model_backed_operator.py",
            "tests/test_capture_hardening.py",
            "tests/test_snapshot_hardening.py",
            "tests/test_oci_runner.py",
            "tests/test_trial_controller.py",
        ),
        timeout_seconds=1500,
    ),
    _pytest_command(
        "oci_protocol_focused",
        "sandbox",
        "Interactive OCI authority, hard storage quota, and runner integration.",
        (
            "tests/test_oci_interactive.py",
            "tests/test_oci_storage_quota.py",
            "tests/test_oci_runner.py",
        ),
        marker="not integration",
        timeout_seconds=1500,
    ),
    _pytest_command(
        "signing_focused",
        "signing",
        "Public-key DSSE policy, role identity, replay, and tamper checks.",
        ("tests/test_security_signing.py",),
    ),
    _pytest_command(
        "adapter_focused",
        "adapter",
        "Command adapter conformance, bounded capture, and process-tree cleanup.",
        (
            "tests/test_adapter_conformance_v1.py",
            "tests/test_adapter_runtime_hardening.py",
            "tests/test_copilot_oci_wrapper.py",
            "tests/test_capture_hardening.py",
            "tests/test_snapshot_hardening.py",
        ),
        marker="not integration",
        timeout_seconds=1200,
    ),
    _pytest_command(
        "iterative_focused",
        "iterative",
        "Iterative CodeClash bridge and original-paper round semantics.",
        (
            "tests/test_codeclash_drift.py",
            "tests/test_comparison.py",
            "tests/test_phoenix_hve_case_study.py",
            "tests/test_integration.py",
            "tests/test_iterative_codeclash_bridge.py",
            "tests/test_iterative_paper_alignment.py",
        ),
        marker="not integration",
    ),
    _pytest_command(
        "task_focused",
        "tasks",
        "Pilot corpus diversity and all machine acceptance gates.",
        ("tests/test_pilot_task_suite.py", "tests/test_eval_tasks.py"),
        timeout_seconds=1200,
    ),
    _pytest_command(
        "cli_focused",
        "cli",
        "Local benchmark and League-executor CLI contracts without Docker integration.",
        ("tests/test_benchmark_cli.py", "tests/test_league_executor.py"),
        marker="not integration",
    ),
    _pytest_command(
        "governance_focused",
        "governance",
        "Fail-closed governance snapshot evaluator.",
        ("tests/test_governance_audit.py",),
    ),
    _pytest_command(
        "launch_focused",
        "launch",
        "Launch-audit definitions, proof validation, and rendering.",
        ("tests/test_launch_audit.py", "tests/test_verification_manifest.py"),
    ),
    _pytest_command(
        "embedded_schema",
        "packaging",
        "Embedded schema bytes are identical and usable offline.",
        (
            "tests/protocol/test_schemas_v1.py::"
            "test_embedded_wheel_fallback_is_byte_identical_and_usable",
        ),
    ),
    _pytest_command(
        "cp1252_cli",
        "cli",
        "Legacy Windows CP1252 CLI behavior.",
        ("tests/test_cli_windows_encoding.py",),
    ),
    _pytest_command(
        "actions_sha",
        "supply-chain",
        "Every third-party GitHub Action uses its reviewed full SHA.",
        (
            "tests/test_workflow_supply_chain.py::"
            "test_every_action_is_pinned_to_an_approved_full_sha",
        ),
    ),
    _pytest_command(
        "full_non_live",
        "full-suite",
        "Complete non-live, non-Docker, non-spike regression suite.",
        ("tests",),
        marker="not live and not integration and not spike",
        timeout_seconds=3600,
        modes=("full",),
    ),
    CommandSpec(
        id="docker_preflight",
        category="docker",
        description="Docker client and daemon version evidence.",
        argv=("docker", "version", "--format", "{{json .}}"),
        timeout_seconds=60,
        modes=frozenset({"full"}),
        docker=True,
    ),
    CommandSpec(
        id="docker_image",
        category="docker",
        description="Digest-pinned smoke image is cached and bound.",
        argv=(
            "docker",
            "image",
            "inspect",
            PYTHON_IMAGE,
            "--format",
            "{{json .}}",
        ),
        timeout_seconds=60,
        modes=frozenset({"full"}),
        docker=True,
    ),
    _pytest_command(
        "docker_oci_integration",
        "docker",
        "Real Docker interactive authority, quota, isolation, cleanup, and gateway tests.",
        (
            "tests/test_adapter_conformance_v1.py",
            "tests/test_iterative_codeclash_bridge.py",
            "tests/test_oci_interactive.py",
            "tests/test_oci_runner_integration.py",
            "tests/test_oci_storage_quota.py",
        ),
        marker="integration",
        timeout_seconds=1800,
        modes=("full",),
        docker=True,
    ),
    _pytest_command(
        "docker_control_plane_integration",
        "docker",
        "Real Docker model-free control-plane lifecycle.",
        ("tests/test_trial_controller_integration.py",),
        marker="integration",
        timeout_seconds=1800,
        modes=("full",),
        docker=True,
    ),
    _pytest_command(
        "docker_cli_integration",
        "docker",
        "Real Docker CLI plan/run/verify/analyze/reproduce lifecycle.",
        ("tests/test_benchmark_cli.py",),
        marker="integration",
        timeout_seconds=2400,
        modes=("full",),
        docker=True,
    ),
    CommandSpec(
        id="uv_sync_locked",
        category="packaging",
        description="Locked development and run extras install.",
        argv=("uv", "sync", "--locked", "--extra", "dev", "--extra", "run"),
        timeout_seconds=1800,
        modes=frozenset({"full"}),
    ),
    CommandSpec(
        id="codeclash_assets",
        category="packaging",
        description="Pinned CodeClash source assets are present and bound to arena classes.",
        argv=("{PYTHON}", "-c", CODECLASH_ASSET_VERIFY_CODE),
        timeout_seconds=120,
        modes=frozenset({"full"}),
        dependencies=("uv_sync_locked",),
    ),
    CommandSpec(
        id="package_build",
        category="packaging",
        description="Build one wheel and one source distribution.",
        argv=("uv", "build", "--wheel", "--sdist", "--out-dir", "{DIST_DIR}"),
        timeout_seconds=900,
        modes=frozenset({"full"}),
        output_artifacts=("wheel", "sdist"),
    ),
    CommandSpec(
        id="wheel_venv_create",
        category="packaging",
        description="Create a fresh wheel installation environment.",
        argv=("uv", "venv", "{WHEEL_VENV}", "--python", "{PYTHON}", "--seed"),
        timeout_seconds=300,
        modes=frozenset({"full"}),
        dependencies=("package_build",),
    ),
    CommandSpec(
        id="wheel_install",
        category="packaging",
        description="Install the built wheel with dependencies into a fresh environment.",
        argv=(
            "uv",
            "pip",
            "install",
            "--python",
            "{WHEEL_PYTHON}",
            "--link-mode",
            "copy",
            "{WHEEL}",
        ),
        timeout_seconds=900,
        modes=frozenset({"full"}),
        dependencies=("package_build", "wheel_venv_create"),
    ),
    CommandSpec(
        id="wheel_verify",
        category="packaging",
        description="Import the wheel and load all embedded schemas.",
        argv=("{WHEEL_PYTHON}", "-c", PACKAGE_VERIFY_CODE),
        timeout_seconds=120,
        modes=frozenset({"full"}),
        dependencies=("wheel_install",),
        cwd="{ISOLATED_CWD}",
        clear_pythonpath=True,
    ),
    CommandSpec(
        id="sdist_venv_create",
        category="packaging",
        description="Create a fresh source-distribution installation environment.",
        argv=("uv", "venv", "{SDIST_VENV}", "--python", "{PYTHON}", "--seed"),
        timeout_seconds=300,
        modes=frozenset({"full"}),
        dependencies=("package_build",),
    ),
    CommandSpec(
        id="sdist_install",
        category="packaging",
        description="Install the built source distribution with dependencies.",
        argv=(
            "uv",
            "pip",
            "install",
            "--python",
            "{SDIST_PYTHON}",
            "--link-mode",
            "copy",
            "{SDIST}",
        ),
        timeout_seconds=1200,
        modes=frozenset({"full"}),
        dependencies=("package_build", "sdist_venv_create"),
    ),
    CommandSpec(
        id="sdist_verify",
        category="packaging",
        description="Import the source distribution and load all embedded schemas.",
        argv=("{SDIST_PYTHON}", "-c", PACKAGE_VERIFY_CODE),
        timeout_seconds=120,
        modes=frozenset({"full"}),
        dependencies=("sdist_install",),
        cwd="{ISOLATED_CWD}",
        clear_pythonpath=True,
    ),
)
_FIXED_BY_ID = {spec.id: spec for spec in _FIXED_COMMANDS}
if len(_FIXED_BY_ID) != len(_FIXED_COMMANDS):  # pragma: no cover - import tripwire
    raise RuntimeError("fixed verification command ids must be unique")


def build_verification_plan(mode: str) -> tuple[CommandSpec, ...]:
    if mode not in {"quick", "full"}:
        raise VerificationError("verification mode must be 'quick' or 'full'")
    selected = [spec for spec in _FIXED_COMMANDS if mode in spec.modes]
    if mode == "quick":
        return tuple(selected)
    order = {
        command_id: index
        for index, command_id in enumerate(
            (
                "uv_sync_locked",
                "codeclash_assets",
                "full_non_live",
                "protocol_focused",
                "eval_focused",
                "security_focused",
                "oci_protocol_focused",
                "signing_focused",
                "adapter_focused",
                "iterative_focused",
                "task_focused",
                "cli_focused",
                "governance_focused",
                "launch_focused",
                "embedded_schema",
                "cp1252_cli",
                "actions_sha",
                "package_build",
                "wheel_venv_create",
                "wheel_install",
                "wheel_verify",
                "sdist_venv_create",
                "sdist_install",
                "sdist_verify",
                "docker_preflight",
                "docker_image",
                "docker_oci_integration",
                "docker_control_plane_integration",
                "docker_cli_integration",
            )
        )
    }
    return tuple(sorted(selected, key=lambda spec: order[spec.id]))


def plan_document(mode: str) -> dict[str, Any]:
    commands = [spec.to_plan_dict() for spec in build_verification_plan(mode)]
    payload = {"schema": PLAN_SCHEMA, "mode": mode, "commands": commands}
    return {**payload, "digest": sha256_bytes(canonical_json_bytes(payload))}


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    command_id: str
    test_ids: tuple[str, ...] = ()
    artifact_labels: tuple[str, ...] = ()
    require_zero_skips: bool = False
    require_docker_daemon: bool = False
    require_docker_image: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class GateRule:
    gate_id: str
    claims: Mapping[str, Any]
    requirements: tuple[EvidenceRequirement, ...]
    modes: frozenset[str] = frozenset({"quick", "full"})
    forced_block_reason: str | None = None


TRIAL_UNIT_TESTS = (
    "tests/test_eval_stats.py::"
    "test_tasks_not_trials_are_the_bootstrap_and_macro_average_unit",
    "tests/test_eval_stats.py::"
    "test_nested_game_or_round_cannot_be_constructed_as_trial_evidence",
    "tests/test_eval_trial_scheduler.py::"
    "test_attempt_identity_binds_fresh_workspace_and_attempt_number",
)
SCHEMA_TESTS = (
    "tests/protocol/test_schemas_v1.py::"
    "test_all_v1_schemas_are_draft_2020_12_meta_valid_and_use_offline_ids",
    "tests/protocol/test_schemas_v1.py::"
    "test_valid_documents_cover_every_public_schema",
    "tests/protocol/test_schemas_v1.py::"
    "test_security_critical_objects_reject_unknown_fields",
    "tests/protocol/test_schemas_v1.py::"
    "test_bundle_contents_digest_verifies_and_tampering_fails",
)
SCHEDULER_TESTS = (
    "tests/test_eval_trial_scheduler.py::"
    "test_schedule_is_fully_crossed_and_paired_by_block",
    "tests/test_eval_trial_scheduler.py::"
    "test_schedule_changes_with_seed",
    "tests/test_eval_trial_scheduler.py::"
    "test_rotation_balances_every_harness_across_every_order_position",
    "tests/test_eval_trial_scheduler.py::"
    "test_worker_assignments_are_deterministic_and_balanced",
)
SIGNING_TESTS = (
    "tests/test_security_signing.py::"
    "test_public_policy_verifies_deterministic_dsse_without_private_key",
    "tests/test_security_signing.py::"
    "test_wrong_role_unknown_and_revoked_keys_fail",
    "tests/test_security_signing.py::"
    "test_signature_tamper_replay_and_digest_mismatch_fail",
)
TASK_GATE_TESTS = (
    "tests/test_pilot_task_suite.py::"
    "test_all_pilot_tasks_conform_and_pass_every_machine_gate",
    "tests/test_pilot_task_suite.py::"
    "test_oracle_alternative_exploit_mutation_and_noop_are_content_distinct",
    "tests/test_eval_tasks.py::"
    "test_every_smoke_task_passes_all_machine_acceptance_gates",
)
HIDDEN_GRADER_UNIT_TEST = (
    "tests/test_oci_runner.py::"
    "test_harness_and_grader_are_separate_with_hidden_inputs_late_and_no_secrets"
)
HIDDEN_GRADER_DOCKER_TEST = (
    "tests/test_oci_runner_integration.py::"
    "test_real_engine_networkless_harness_then_hidden_grader"
)
CANCELLATION_TESTS = (
    "tests/test_adapter_runtime_hardening.py::test_timeout_kills_the_full_process_tree",
    "tests/test_adapter_runtime_hardening.py::"
    "test_cancellation_kills_the_full_process_tree",
)
DOCKER_TIMEOUT_TEST = (
    "tests/test_oci_runner_integration.py::"
    "test_real_engine_timeout_force_removes_exact_container_before_grader"
)
DOCKER_EXISTENCE_PROBE_TEST = (
    "tests/test_oci_runner_integration.py::"
    "test_real_docker_container_existence_distinguishes_not_found_from_daemon_failure"
)
DOCKER_GATEWAY_TEST = (
    "tests/test_oci_runner_integration.py::"
    "test_real_internal_network_requires_exact_named_gateway_sidecar"
)
INTERACTIVE_DOCKER_TEST = (
    "tests/test_oci_interactive.py::"
    "test_real_cached_oci_interactive_roundtrip_edits_workspace"
)
SHARED_CONFORMANCE_DOCKER_TESTS = (
    "tests/test_adapter_conformance_v1.py::"
    "test_process_and_oci_pass_the_same_behavioral_conformance_cases[no_edit]",
    "tests/test_adapter_conformance_v1.py::"
    "test_process_and_oci_pass_the_same_behavioral_conformance_cases[single_file]",
    "tests/test_adapter_conformance_v1.py::"
    "test_process_and_oci_pass_the_same_behavioral_conformance_cases[multi_file]",
)
CODECLASH_COPY_DOCKER_TEST = (
    "tests/test_iterative_codeclash_bridge.py::"
    "test_real_docker_codeclash_copy_excludes_git_and_cache_trees"
)
CODECLASH_TOURNAMENT_DOCKER_TEST = (
    "tests/test_iterative_codeclash_bridge.py::"
    "test_real_codeclash_lightcycles_runs_persistent_model_free_round"
)
HARD_QUOTA_DOCKER_TESTS = (
    "tests/test_oci_runner_integration.py::"
    "test_real_engine_hard_quota_reports_aggregate_enospc_and_grades_output",
    "tests/test_oci_storage_quota.py::"
    "test_real_docker_quota_is_aggregate_across_workspace_artifacts_and_temp",
    "tests/test_oci_storage_quota.py::"
    "test_real_docker_output_links_cannot_bypass_verified_capture[symlink]",
    "tests/test_oci_storage_quota.py::"
    "test_real_docker_output_links_cannot_bypass_verified_capture[hardlink]",
)
RESOURCE_BOMB_DOCKER_TESTS = (
    "tests/test_oci_runner_integration.py::"
    "test_real_engine_pid_bomb_is_contained_and_cleaned",
    "tests/test_oci_runner_integration.py::"
    "test_real_engine_memory_bomb_is_oom_contained_and_cleaned",
    "tests/test_oci_runner_integration.py::"
    "test_real_engine_output_bomb_is_streamed_bounded_and_cleaned",
)
DOCKER_REPRODUCTION_TESTS = (
    "tests/test_benchmark_cli.py::"
    "test_real_docker_local_plan_run_verify_analyze_reproduce_and_smoke",
    "tests/test_benchmark_cli.py::test_offline_verify_detects_tampering",
)
EXPECTED_DOCKER_CASES: Mapping[str, tuple[str, ...]] = {
    "docker_oci_integration": (
        *SHARED_CONFORMANCE_DOCKER_TESTS,
        CODECLASH_COPY_DOCKER_TEST,
        CODECLASH_TOURNAMENT_DOCKER_TEST,
        INTERACTIVE_DOCKER_TEST,
        DOCKER_EXISTENCE_PROBE_TEST,
        HIDDEN_GRADER_DOCKER_TEST,
        DOCKER_TIMEOUT_TEST,
        *HARD_QUOTA_DOCKER_TESTS,
        *RESOURCE_BOMB_DOCKER_TESTS,
        DOCKER_GATEWAY_TEST,
    ),
    "docker_control_plane_integration": (
        "tests/test_trial_controller_integration.py::"
        "test_real_docker_model_free_full_control_plane[repair-config]",
        "tests/test_trial_controller_integration.py::"
        "test_real_docker_model_free_full_control_plane[cross-file-total]",
        "tests/test_trial_controller_integration.py::"
        "test_real_docker_model_free_full_control_plane[repair-config-max-output]",
    ),
    "docker_cli_integration": DOCKER_REPRODUCTION_TESTS,
}


def _claims(gate_id: str) -> dict[str, Any]:
    from atv_bench.launch_audit import required_claims_for_gate

    return required_claims_for_gate(gate_id)


def gate_rules() -> tuple[GateRule, ...]:
    rules: list[GateRule] = []

    def add(
        gate_id: str,
        *requirements: EvidenceRequirement,
        modes: Iterable[str] = ("quick", "full"),
        claims: Mapping[str, Any] | None = None,
        forced_block_reason: str | None = None,
    ) -> None:
        rules.append(
            GateRule(
                gate_id,
                dict(claims) if claims is not None else _claims(gate_id),
                tuple(requirements),
                frozenset(modes),
                forced_block_reason,
            )
        )

    trial_requirements = (
        EvidenceRequirement("eval_focused", TRIAL_UNIT_TESTS),
        EvidenceRequirement(
            "iterative_focused",
            (
                "tests/test_iterative_paper_alignment.py::"
                "test_match_record_never_promotes_nested_rounds_to_trials",
            ),
        ),
    )
    add("launch.independent_trial", *trial_requirements)
    add("release.experiment.trial_unit", *trial_requirements)

    schema_requirements = (
        EvidenceRequirement("protocol_focused", SCHEMA_TESTS),
        EvidenceRequirement(
            "embedded_schema",
            (
                "tests/protocol/test_schemas_v1.py::"
                "test_embedded_wheel_fallback_is_byte_identical_and_usable",
            ),
        ),
    )
    add("launch.versioned_protocol_task", *schema_requirements)
    add("release.protocol.schemas", *schema_requirements)

    schedule = EvidenceRequirement("eval_focused", SCHEDULER_TESTS)
    add("launch.paired_schedule", schedule)
    add("release.experiment.paired", schedule)
    add(
        "launch.winner_rule",
        EvidenceRequirement(
            "eval_focused",
            (
                "tests/test_eval_report.py::"
                "test_two_immutable_policies_with_consistent_direction_produce_winner",
                "tests/test_eval_report.py::"
                "test_two_policy_equivalence_and_incident_state_suppress_winner",
                "tests/test_eval_protocol_export.py::"
                "test_two_smoke_tasks_cannot_export_official_rankable_winner",
            ),
        ),
    )
    add(
        "release.analysis.winner",
        EvidenceRequirement(
            "eval_focused",
            (
                "tests/test_eval_report.py::"
                "test_two_immutable_policies_with_consistent_direction_produce_winner",
                "tests/test_eval_report.py::"
                "test_two_policy_equivalence_and_incident_state_suppress_winner",
            ),
        ),
    )
    add(
        "release.tasks.deterministic",
        EvidenceRequirement(
            "eval_focused",
            (
                "tests/test_eval_grader_bundle.py::"
                "test_file_assertion_grader_is_deterministic_and_does_not_execute_output",
                "tests/test_eval_tasks.py::"
                "test_nondeterministic_grader_makes_task_ineligible",
            ),
        ),
    )
    add(
        "release.experiment.model_budget",
        EvidenceRequirement(
            "eval_focused",
            (
                "tests/test_eval_trial_scheduler.py::"
                "test_budget_and_trial_spec_are_immutable_and_content_addressed",
                "tests/test_eval_stats.py::"
                "test_from_trial_uses_full_policy_and_budget_identities",
                "tests/test_eval_stats.py::"
                "test_controlled_analysis_refuses_to_pool_models_or_budgets",
            ),
        ),
    )
    add(
        "release.experiment.infrastructure",
        EvidenceRequirement(
            "eval_focused",
            (
                "tests/test_eval_trial_scheduler.py::"
                "test_retry_preserves_assignment_but_requires_new_attempt_and_nonce",
                "tests/test_eval_stats.py::"
                "test_infrastructure_failures_are_reported_but_never_scored_as_losses",
            ),
        ),
        EvidenceRequirement(
            "security_focused",
            (
                "tests/test_trial_controller.py::"
                "test_cleanup_failure_is_infrastructure_and_retryable",
            ),
        ),
    )
    add(
        "release.protocol.unknown_versions",
        EvidenceRequirement(
            "protocol_focused",
            (
                "tests/protocol/test_schemas_v1.py::"
                "test_unknown_protocol_versions_fail_closed",
            ),
        ),
    )
    secret_isolation = (
        EvidenceRequirement(
            "security_focused",
            (
                "tests/test_security_gateway.py::"
                "test_provider_canary_is_absent_from_every_harness_visible_surface",
                "tests/test_oci_runner.py::"
                "test_harness_and_grader_are_separate_with_hidden_inputs_late_and_no_secrets",
            ),
        ),
        EvidenceRequirement(
            "adapter_focused",
            (
                "tests/test_adapter_runtime_hardening.py::"
                "test_environment_is_default_deny_with_explicit_allowlist",
                "tests/test_adapter_runtime_hardening.py::"
                "test_command_adapter_does_not_inherit_secret_without_opt_in",
            ),
        ),
    )
    add("launch.secret_isolation", *secret_isolation)
    add("release.security.no_credentials", *secret_isolation)

    add(
        "release.repository.actions_pinned",
        EvidenceRequirement(
            "actions_sha",
            (
                "tests/test_workflow_supply_chain.py::"
                "test_every_action_is_pinned_to_an_approved_full_sha",
            ),
        ),
    )
    add(
        "release.tasks.gates",
        EvidenceRequirement("task_focused", TASK_GATE_TESTS),
    )
    add(
        "launch.contamination_retraction",
        EvidenceRequirement("governance_focused"),
        claims={
            "contamination_policy_published": True,
            "retraction_policy_published": True,
            "reviewed": False,
        },
        forced_block_reason=(
            "Repository text and offline tests do not prove an independent policy "
            "review; the local fallback must not infer human review."
        ),
    )

    docker_common = (
        EvidenceRequirement("docker_preflight", require_docker_daemon=True),
        EvidenceRequirement("docker_image", require_docker_image=True),
    )
    process_oci = (
        EvidenceRequirement(
            "adapter_focused",
            (
                "tests/test_adapter_conformance_v1.py::"
                "test_process_and_oci_share_canonical_contract_parity_where_supported",
            ),
        ),
        EvidenceRequirement(
            "oci_protocol_focused",
            (
                "tests/test_oci_runner.py::"
                "test_trusted_protocol_session_preserves_controller_authority",
            ),
        ),
        EvidenceRequirement(
            "docker_oci_integration",
            (*SHARED_CONFORMANCE_DOCKER_TESTS, INTERACTIVE_DOCKER_TEST),
            require_zero_skips=True,
        ),
        EvidenceRequirement(
            "docker_control_plane_integration",
            EXPECTED_DOCKER_CASES["docker_control_plane_integration"],
            require_zero_skips=True,
        ),
        *docker_common,
    )
    add("launch.process_oci_conformance", *process_oci, modes=("full",))
    add("release.protocol.process_oci", *process_oci, modes=("full",))

    runner_resources = (
        EvidenceRequirement(
            "adapter_focused",
            (
                "tests/test_adapter_runtime_hardening.py::"
                "test_stdout_and_stderr_are_streamed_into_bounded_tail_buffers",
                "tests/test_adapter_runtime_hardening.py::"
                "test_timeout_kills_the_full_process_tree",
            ),
        ),
        EvidenceRequirement(
            "oci_protocol_focused",
            (
                "tests/test_oci_storage_quota.py::"
                "test_quota_argv_uses_one_volume_subpaths_and_disables_default_shm",
                "tests/test_oci_storage_quota.py::"
                "test_explicit_hard_quota_capability_failure_closes_before_start",
                "tests/test_oci_storage_quota.py::"
                "test_auto_mode_does_not_fallback_after_hard_quota_verification_failure",
                "tests/test_oci_storage_quota.py::"
                "test_unverifiable_named_volume_cleanup_fails_closed",
            ),
        ),
        EvidenceRequirement(
            "docker_oci_integration",
            HARD_QUOTA_DOCKER_TESTS,
            require_zero_skips=True,
        ),
        *docker_common,
    )
    add("launch.ephemeral_runner", *runner_resources, modes=("full",))
    add(
        "release.security.bombs",
        EvidenceRequirement(
            "adapter_focused",
            (
                "tests/test_adapter_runtime_hardening.py::"
                "test_stdout_and_stderr_are_streamed_into_bounded_tail_buffers",
                "tests/test_adapter_runtime_hardening.py::"
                "test_timeout_kills_the_full_process_tree",
            ),
        ),
        EvidenceRequirement(
            "oci_protocol_focused",
            (
                "tests/test_oci_storage_quota.py::"
                "test_quota_argv_uses_one_volume_subpaths_and_disables_default_shm",
            ),
        ),
        EvidenceRequirement(
            "docker_oci_integration",
            (*HARD_QUOTA_DOCKER_TESTS, *RESOURCE_BOMB_DOCKER_TESTS),
            require_zero_skips=True,
        ),
        *docker_common,
        modes=("full",),
    )
    add(
        "launch.no_silent_failure",
        EvidenceRequirement(
            "launch_focused",
            (
                "tests/test_launch_audit.py::"
                "test_failure_registry_has_no_unmitigated_critical_silent_rows",
            ),
        ),
        EvidenceRequirement(
            "adapter_focused",
            (
                "tests/test_adapter_runtime_hardening.py::"
                "test_environment_is_default_deny_with_explicit_allowlist",
                "tests/test_adapter_runtime_hardening.py::"
                "test_timeout_kills_the_full_process_tree",
            ),
        ),
        EvidenceRequirement(
            "oci_protocol_focused",
            (
                "tests/test_oci_interactive.py::"
                "test_stdout_pollution_partial_and_invalid_utf8_are_rejected",
                "tests/test_oci_storage_quota.py::"
                "test_unverifiable_named_volume_cleanup_fails_closed",
            ),
        ),
        EvidenceRequirement(
            "docker_oci_integration",
            (
                CODECLASH_COPY_DOCKER_TEST,
                CODECLASH_TOURNAMENT_DOCKER_TEST,
                DOCKER_EXISTENCE_PROBE_TEST,
            ),
            require_zero_skips=True,
        ),
        *docker_common,
        modes=("full",),
    )
    add(
        "release.security.filesystem",
        EvidenceRequirement(
            "security_focused",
            (
                "tests/test_capture_hardening.py::test_hardlink_is_rejected",
                "tests/test_capture_hardening.py::test_fifo_is_rejected_as_special_file",
                "tests/test_capture_hardening.py::test_windows_junction_is_rejected",
                "tests/test_snapshot_hardening.py::"
                "test_untracked_symlink_is_rejected_without_reading_target",
            ),
        ),
        EvidenceRequirement(
            "docker_oci_integration",
            (
                "tests/test_oci_storage_quota.py::"
                "test_real_docker_output_links_cannot_bypass_verified_capture[symlink]",
                "tests/test_oci_storage_quota.py::"
                "test_real_docker_output_links_cannot_bypass_verified_capture[hardlink]",
            ),
            require_zero_skips=True,
        ),
        *docker_common,
        modes=("full",),
    )
    hidden = (
        EvidenceRequirement("security_focused", (HIDDEN_GRADER_UNIT_TEST,)),
        EvidenceRequirement(
            "docker_oci_integration",
            (HIDDEN_GRADER_DOCKER_TEST,),
            require_zero_skips=True,
        ),
        *docker_common,
    )
    add("launch.hidden_grader", *hidden, modes=("full",))
    add("release.security.hidden_tests", *hidden, modes=("full",))

    add(
        "release.protocol.cancellation",
        EvidenceRequirement("adapter_focused", CANCELLATION_TESTS),
        EvidenceRequirement(
            "docker_oci_integration",
            (DOCKER_TIMEOUT_TEST,),
            require_zero_skips=True,
        ),
        *docker_common,
        modes=("full",),
    )
    add(
        "release.security.gateway_egress",
        EvidenceRequirement(
            "security_focused",
            (
                "tests/test_oci_runner.py::"
                "test_gateway_network_requires_exact_declared_peer_set",
            ),
        ),
        EvidenceRequirement(
            "docker_oci_integration",
            (DOCKER_GATEWAY_TEST,),
            require_zero_skips=True,
        ),
        *docker_common,
        modes=("full",),
    )

    packaging = (
        EvidenceRequirement("uv_sync_locked"),
        EvidenceRequirement("codeclash_assets"),
        EvidenceRequirement(
            "package_build", artifact_labels=("wheel", "sdist")
        ),
        EvidenceRequirement("wheel_venv_create"),
        EvidenceRequirement("wheel_install"),
        EvidenceRequirement("wheel_verify"),
        EvidenceRequirement("sdist_venv_create"),
        EvidenceRequirement("sdist_install"),
        EvidenceRequirement("sdist_verify"),
    )
    add(
        "launch.packaging_cp1252",
        *packaging,
        EvidenceRequirement(
            "cp1252_cli",
            (
                "tests/test_cli_windows_encoding.py::"
                "test_cli_commands_do_not_crash_on_cp1252",
            ),
        ),
        modes=("full",),
    )
    add(
        "release.repository.clean_packages",
        *packaging[1:],
        modes=("full",),
    )
    add(
        "release.publication.reproduction_command",
        EvidenceRequirement(
            "docker_cli_integration",
            DOCKER_REPRODUCTION_TESTS,
            require_zero_skips=True,
        ),
        *docker_common,
        modes=("full",),
    )
    return tuple(rules)


_GATE_RULES = gate_rules()
_GATE_RULE_BY_ID = {rule.gate_id: rule for rule in _GATE_RULES}


CLAIMS_NOT_MADE: Mapping[str, str] = {
    "launch.model_attestation": (
        "No live provider receipt verifies the requested and resolved model identity."
    ),
    "launch.signed_bundle": (
        "Local DSSE implementation tests are not an official signed benchmark bundle "
        "or official run."
    ),
    "launch.task_portfolio": (
        "Machine fixtures do not constitute independent human task review."
    ),
    "release.tasks.human_review": (
        "No independent human reviewer record is inferred from machine tests."
    ),
    "release.tasks.split": (
        "Local public fixtures do not prove private-task secrecy or a rotation split."
    ),
    "release.tasks.contamination": (
        "No human contamination review of every task is claimed."
    ),
    "launch.five_trials": "No official five-trial-per-cell ledger is supplied.",
    "launch.clustered_uncertainty": (
        "Algorithm tests are not a published analysis over an accepted immutable run."
    ),
    "launch.external_reproduction": (
        "A local rerun is not a non-affiliated external reproduction."
    ),
    "release.publication.external_reproduction": (
        "A local rerun is not a non-affiliated external reproduction."
    ),
    "launch.live_governance": (
        "Live governance is accepted only from an explicitly supplied fresh GitHub "
        "REST audit, never inferred from repository files."
    ),
    "launch.immutable_release": (
        "The runner never infers an immutable signed release or tag from local tests."
    ),
    "release.repository.signed_tag": (
        "The runner never claims a signed immutable tag without live external evidence."
    ),
    "release.security.independent_review": (
        "Automated tests are not an independent human security review."
    ),
    "release.security.signatures": (
        "Local key fixtures test DSSE mechanics but do not verify production workload "
        "identities or an official signed run."
    ),
    "release.analysis.independent_review": (
        "Automated tests are not an independent statistical review."
    ),
    "release.repository.uv_run": (
        "A locked sync in the active dirty worktree is not a clean-checkout proof."
    ),
}


@dataclasses.dataclass(frozen=True, slots=True)
class RawExecution:
    argv: tuple[str, ...]
    started_at: str
    finished_at: str
    duration_ms: int
    exit_code: int | None
    timed_out: bool
    stdout: bytes
    stderr: bytes
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    error: str | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_redactions: tuple[str, ...] = ()
    stderr_redactions: tuple[str, ...] = ()


def _sanitize_raw_execution(
    raw: RawExecution,
    *,
    repo_root: Path,
    output_root: Path,
) -> RawExecution:
    stdout, stdout_redactions = _sanitize_public_bytes(
        raw.stdout,
        repo_root=repo_root,
        output_root=output_root,
    )
    stderr, stderr_redactions = _sanitize_public_bytes(
        raw.stderr,
        repo_root=repo_root,
        output_root=output_root,
    )
    error = raw.error
    if error is not None:
        error, _ = _sanitize_public_text(
            error,
            repo_root=repo_root,
            output_root=output_root,
        )
    stdout_total = raw.stdout_total_bytes if raw.stdout_truncated else len(stdout)
    stderr_total = raw.stderr_total_bytes if raw.stderr_truncated else len(stderr)
    stdout_digest = (
        raw.stdout_sha256 if raw.stdout_truncated else sha256_bytes(stdout)
    )
    stderr_digest = (
        raw.stderr_sha256 if raw.stderr_truncated else sha256_bytes(stderr)
    )
    return dataclasses.replace(
        raw,
        stdout=stdout,
        stderr=stderr,
        stdout_total_bytes=stdout_total,
        stderr_total_bytes=stderr_total,
        error=error,
        stdout_sha256=stdout_digest,
        stderr_sha256=stderr_digest,
        stdout_redactions=stdout_redactions,
        stderr_redactions=stderr_redactions,
    )


class CommandExecutor(Protocol):
    def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
    ) -> RawExecution:
        ...


class _BoundedBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = max(1024, limit)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.digest = hashlib.sha256()

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        self.digest.update(data)
        if len(self.head) < self.head_limit:
            take = min(self.head_limit - len(self.head), len(data))
            self.head.extend(data[:take])
            data = data[take:]
        if data:
            self.tail.extend(data)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def render(self) -> bytes:
        if not self.truncated:
            return bytes(self.head + self.tail)
        marker = (
            b"\n...[ATV output truncated; total_bytes="
            + str(self.total).encode("ascii")
            + b"]...\n"
        )
        allowance = max(0, self.limit - len(marker))
        head = bytes(self.head[: allowance // 2])
        tail = bytes(self.tail[-(allowance - len(head)) :])
        return head + marker + tail

    @property
    def sha256(self) -> str:
        return self.digest.hexdigest()


class BoundedSubprocessExecutor:
    """Run argv directly while draining output into bounded head/tail buffers."""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        maximum_capture_bytes: int = MAX_CAPTURE_BYTES,
    ) -> None:
        if maximum_capture_bytes <= 0:
            raise VerificationError("maximum subprocess capture must be positive")
        self.popen_factory = popen_factory
        self.now = now
        self.monotonic = monotonic
        self.maximum_capture_bytes = maximum_capture_bytes

    def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
    ) -> RawExecution:
        if isinstance(argv, (str, bytes)) or not argv:
            raise VerificationError("command argv must be a non-empty token sequence")
        if timeout_seconds <= 0:
            raise VerificationError("command timeout must be positive")
        if (
            max_output_bytes <= 0
            or max_output_bytes > self.maximum_capture_bytes
        ):
            raise VerificationError(
                "command output limit must be within "
                f"1..{self.maximum_capture_bytes} bytes"
            )
        tokens = tuple(argv)
        if any(
            not isinstance(token, str)
            or not token
            or "\0" in token
            or "\r" in token
            or "\n" in token
            for token in tokens
        ):
            raise VerificationError("command argv contains an unsafe token")
        started = self.now()
        start_tick = self.monotonic()
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        try:
            process = self.popen_factory(
                list(tokens),
                cwd=os.fspath(cwd),
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError as exc:
            finished = self.now()
            stderr = str(exc).encode("utf-8", errors="replace")
            return RawExecution(
                tokens,
                _iso(started),
                _iso(finished),
                max(0, round((self.monotonic() - start_tick) * 1000)),
                None,
                False,
                b"",
                stderr,
                0,
                len(stderr),
                False,
                False,
                f"could not execute {tokens[0]!r}: {exc}",
                sha256_bytes(b""),
                sha256_bytes(stderr),
            )

        job = _WindowsJob(process) if os.name == "nt" else None
        if os.name == "nt":
            if job is None or job.handle is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                finished = self.now()
                stderr = b"could not establish a Windows Job Object execution cell"
                return RawExecution(
                    tokens,
                    _iso(started),
                    _iso(finished),
                    max(0, round((self.monotonic() - start_tick) * 1000)),
                    process.returncode,
                    False,
                    b"",
                    stderr,
                    0,
                    len(stderr),
                    False,
                    False,
                    stderr.decode("ascii"),
                    sha256_bytes(b""),
                    sha256_bytes(stderr),
                )
            try:
                _resume_windows_process(process)
            except OSError as exc:
                job.terminate_and_confirm(timeout=5)
                job.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                finished = self.now()
                stderr = f"could not resume verification execution cell: {exc}".encode(
                    "utf-8", errors="replace"
                )
                return RawExecution(
                    tokens,
                    _iso(started),
                    _iso(finished),
                    max(0, round((self.monotonic() - start_tick) * 1000)),
                    process.returncode,
                    False,
                    b"",
                    stderr,
                    0,
                    len(stderr),
                    False,
                    False,
                    stderr.decode("utf-8", errors="replace"),
                    sha256_bytes(b""),
                    sha256_bytes(stderr),
                )

        stdout_buffer = _BoundedBuffer(max_output_bytes)
        stderr_buffer = _BoundedBuffer(max_output_bytes)

        def drain(stream: Any, target: _BoundedBuffer) -> None:
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        return
                    target.feed(chunk)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        assert process.stdout is not None and process.stderr is not None
        threads = (
            threading.Thread(
                target=drain, args=(process.stdout, stdout_buffer), daemon=True
            ),
            threading.Thread(
                target=drain, args=(process.stderr, stderr_buffer), daemon=True
            ),
        )
        for thread in threads:
            thread.start()
        timed_out = False
        error = None
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            error = f"command exceeded {timeout_seconds} seconds"
            cleanup = _terminate_process_tree(
                process,
                job=job,
                grace_seconds=5.0,
            )
        else:
            cleanup = _cleanup_descendants_after_exit(process, job=job)
        if cleanup.status is CleanupStatus.FAILED:
            cleanup_error = cleanup.error or "execution-cell cleanup could not be confirmed"
            error = f"{error}; {cleanup_error}" if error else cleanup_error
        try:
            for thread in threads:
                thread.join(timeout=30)
            if any(thread.is_alive() for thread in threads):
                for stream in (process.stdout, process.stderr):
                    try:
                        stream.close()
                    except OSError:
                        pass
                for thread in threads:
                    thread.join(timeout=1)
                capture_error = "output pipe did not close after execution-cell cleanup"
                error = f"{error}; {capture_error}" if error else capture_error
        finally:
            if job is not None:
                job.close()
        finished = self.now()
        return RawExecution(
            tokens,
            _iso(started),
            _iso(finished),
            max(0, round((self.monotonic() - start_tick) * 1000)),
            process.returncode,
            timed_out,
            stdout_buffer.render(),
            stderr_buffer.render(),
            stdout_buffer.total,
            stderr_buffer.total,
            stdout_buffer.truncated,
            stderr_buffer.truncated,
            error,
            stdout_buffer.sha256,
            stderr_buffer.sha256,
        )


def _test_file_from_classname(classname: str) -> str:
    parts = classname.split(".")
    if not parts or parts[0] != "tests":
        raise VerificationError(
            f"JUnit testcase classname is outside the repository tests: {classname!r}"
        )
    return "/".join(parts) + ".py"


def _base_test_id(node_id: str) -> str:
    file_name, separator, test_name = node_id.partition("::")
    if not separator:
        return node_id
    return f"{file_name}::{test_name.split('[', 1)[0]}"


def _target_allows_test(target: str, node_id: str) -> bool:
    test_file = node_id.split("::", 1)[0]
    normalized = target.replace("\\", "/")
    if "::" in normalized:
        return _base_test_id(node_id) == _base_test_id(normalized)
    if normalized.endswith(".py"):
        return test_file == normalized
    prefix = normalized.rstrip("/") + "/"
    return test_file.startswith(prefix)


def _sanitize_junit_metadata(
    path: Path,
    *,
    repo_root: Path | None = None,
    output_root: Path | None = None,
) -> tuple[str, ...]:
    try:
        data = path.read_bytes()
        if len(data) > MAX_JUNIT_BYTES:
            raise VerificationError("JUnit XML exceeds the evidence size limit")
        root = ET.fromstring(data)
    except (OSError, ET.ParseError) as exc:
        raise VerificationError(
            f"JUnit XML is unreadable or malformed: {path}: {exc}"
        ) from exc
    removed: list[str] = []
    changed = False
    for suite in root.iter("testsuite"):
        if "hostname" in suite.attrib:
            suite.attrib.pop("hostname", None)
            removed.append("testsuite.hostname")
            changed = True
    for element in root.iter():
        for name, value in tuple(element.attrib.items()):
            sanitized, _ = _sanitize_public_text(
                value,
                repo_root=repo_root,
                output_root=output_root,
            )
            if sanitized != value:
                element.set(name, sanitized)
                removed.append("xml.host-identifiers")
                changed = True
        if element.text:
            sanitized, _ = _sanitize_public_text(
                element.text,
                repo_root=repo_root,
                output_root=output_root,
            )
            if sanitized != element.text:
                element.text = sanitized
                removed.append("xml.host-identifiers")
                changed = True
        if element.tail:
            sanitized, _ = _sanitize_public_text(
                element.tail,
                repo_root=repo_root,
                output_root=output_root,
            )
            if sanitized != element.tail:
                element.tail = sanitized
                removed.append("xml.host-identifiers")
                changed = True
    if changed:
        sanitized = ET.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )
        _atomic_write(path, sanitized)
    return tuple(sorted(set(removed)))


def parse_junit(
    path: Path,
    *,
    command_started_at: str,
    command_finished_at: str,
    allowed_targets: Sequence[str],
) -> dict[str, Any]:
    digest, size = _sha256_file(path, max_bytes=MAX_JUNIT_BYTES)
    try:
        data = path.read_bytes()
        root = ET.fromstring(data)
    except (OSError, ET.ParseError) as exc:
        raise VerificationError(f"JUnit XML is unreadable or malformed: {path}: {exc}") from exc
    if root.tag not in {"testsuites", "testsuite"}:
        raise VerificationError(f"JUnit root must be testsuites/testsuite, got {root.tag!r}")
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if any("hostname" in suite.attrib for suite in suites):
        raise VerificationError("JUnit XML contains unsanitized host metadata")
    timestamps = [
        suite.get("timestamp") for suite in suites if suite.get("timestamp")
    ]
    if not timestamps:
        raise VerificationError("JUnit XML has no testsuite timestamp")
    generated = min(_parse_time(item, label="JUnit timestamp") for item in timestamps)
    started = _parse_time(command_started_at, label="command start")
    finished = _parse_time(command_finished_at, label="command finish")
    tolerance = 300
    if generated.timestamp() < started.timestamp() - tolerance:
        raise VerificationError("JUnit XML predates the command that allegedly created it")
    if generated.timestamp() > finished.timestamp() + tolerance:
        raise VerificationError("JUnit XML is future-dated relative to its command")
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError as exc:
        raise VerificationError(f"JUnit mtime is unavailable: {path}: {exc}") from exc
    if modified.timestamp() < started.timestamp() - tolerance:
        raise VerificationError("JUnit file mtime predates the command")
    if modified.timestamp() > finished.timestamp() + tolerance:
        raise VerificationError("JUnit file mtime is after the command evidence window")

    cases: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()
    failures = errors = skipped = 0
    duration = 0.0
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname")
        name = testcase.get("name")
        if not classname or not name:
            raise VerificationError("JUnit testcase lacks classname or name")
        node_id = f"{_test_file_from_classname(classname)}::{name}"
        if node_id in seen_node_ids:
            raise VerificationError(f"JUnit contains duplicate testcase identity: {node_id}")
        seen_node_ids.add(node_id)
        if not any(_target_allows_test(target, node_id) for target in allowed_targets):
            raise VerificationError(
                f"JUnit testcase {node_id!r} is outside the fixed command targets"
            )
        outcome = "passed"
        message = ""
        if testcase.find("error") is not None:
            outcome = "error"
            errors += 1
            message = testcase.find("error").get("message", "")[:1024]  # type: ignore[union-attr]
        elif testcase.find("failure") is not None:
            outcome = "failed"
            failures += 1
            message = testcase.find("failure").get("message", "")[:1024]  # type: ignore[union-attr]
        elif testcase.find("skipped") is not None:
            outcome = "skipped"
            skipped += 1
            message = testcase.find("skipped").get("message", "")[:1024]  # type: ignore[union-attr]
        try:
            elapsed = float(testcase.get("time", "0"))
        except ValueError:
            elapsed = 0.0
        duration += elapsed
        cases.append(
            {
                "node_id": node_id,
                "base_node_id": _base_test_id(node_id),
                "outcome": outcome,
                "duration_seconds": round(elapsed, 6),
                "message": message,
            }
        )
    cases.sort(key=lambda item: (item["node_id"], item["outcome"]))
    total = len(cases)
    return {
        "artifact": path,
        "sha256": digest,
        "size_bytes": size,
        "generated_at": _iso(generated),
        "tests": total,
        "passed": total - failures - errors - skipped,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "duration_seconds": round(duration, 6),
        "testcases": cases,
    }


def _source_artifacts(
    repo_root: Path, spec: CommandSpec
) -> list[dict[str, Any]]:
    files: set[Path] = set()
    for target in spec.pytest_targets:
        source = target.split("::", 1)[0].replace("\\", "/")
        candidate = repo_root / Path(*PurePosixPath(source).parts)
        if candidate.is_dir():
            files.update(path for path in candidate.rglob("*.py") if path.is_file())
        elif candidate.is_file():
            files.add(candidate)
        else:
            raise VerificationError(
                f"fixed pytest target is missing for {spec.id}: {source}"
            )
    return [
        _artifact_reference(
            repo_root,
            path,
            label="test-source",
            max_bytes=4 * 1024 * 1024,
        )
        for path in sorted(files)
    ]


def _assert_required_test_exists(repo_root: Path, node_id: str) -> None:
    file_name, separator, function = _base_test_id(node_id).partition("::")
    if not separator or not function.startswith("test_"):
        raise VerificationError(f"required test id is malformed: {node_id}")
    path = repo_root / Path(*PurePosixPath(file_name).parts)
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"required test source is unreadable: {node_id}: {exc}") from exc
    if re.search(rf"(?m)^\s*(?:async\s+)?def\s+{re.escape(function)}\s*\(", text) is None:
        raise VerificationError(
            f"required test function is absent from bound source: {node_id}"
        )


_FORCED_ENVIRONMENT: Mapping[str, str] = {
    "CI": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "NO_COLOR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8:replace",
    "PYTHONUTF8": "1",
    "TZ": "UTC",
}
_PYTHON_ISOLATION_ENV = frozenset(
    {
        "ATV_BENCH_SCHEMA_DIR",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)
_VERIFICATION_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "CONTAINER_HOST",
        "CURL_CA_BUNDLE",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "GIT_CONFIG_NOSYSTEM",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "UV_CACHE_DIR",
        "UV_LINK_MODE",
        "UV_NO_PROGRESS",
        "UV_PYTHON",
        "VIRTUAL_ENV",
        "WINDIR",
        "XDG_RUNTIME_DIR",
    }
)


def _normalize_environment_names(
    environment: Mapping[str, str],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in environment.items():
        name = str(raw_name)
        if not name or "=" in name or "\0" in name:
            raise VerificationError(f"environment contains an invalid name: {name!r}")
        value = str(raw_value)
        if "\0" in value:
            raise VerificationError(f"environment variable {name!r} contains NUL")
        key = name.upper() if os.name == "nt" else name
        normalized[key] = value
    return normalized


def _environment_bundle(
    source: Mapping[str, str],
    *,
    spec: CommandSpec | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    source_environment = _normalize_environment_names(source)
    forced = _normalize_environment_names(_FORCED_ENVIRONMENT)
    allowlisted = {
        name.upper() if os.name == "nt" else name
        for name in _VERIFICATION_ENV_ALLOWLIST
    }
    environment = {
        name: value
        for name, value in source_environment.items()
        if name in allowlisted
    }
    cleared = set(
        _PYTHON_ISOLATION_ENV
        if spec is not None and spec.clear_pythonpath
        else ()
    )
    if os.name == "nt":
        cleared = {name.upper() for name in cleared}
    for name in cleared:
        environment.pop(name, None)
    environment.update(forced)
    inherited = sorted(set(environment) - set(forced))
    forced_value_sha256 = {
        name: sha256_bytes(
            value.encode("utf-8", errors="surrogatepass")
        )
        for name, value in sorted(forced.items())
    }
    payload = {
        "schema": ENVIRONMENT_SCHEMA,
        "inheritance_mode": "allowlist",
        "allowlisted_names": sorted(allowlisted),
        "inherited_names": inherited,
        "forced": dict(sorted(forced.items())),
        "cleared_names": sorted(cleared),
        "forced_value_sha256": forced_value_sha256,
    }
    policy = {
        **payload,
        "digest": _digest_descriptor(payload),
    }
    return environment, policy


def _validate_environment_policy(
    policy: Any,
    *,
    spec: CommandSpec | None,
) -> str:
    if not isinstance(policy, Mapping):
        raise VerificationError("command environment policy is absent")
    required = {
        "schema",
        "inheritance_mode",
        "allowlisted_names",
        "inherited_names",
        "forced",
        "cleared_names",
        "forced_value_sha256",
        "digest",
    }
    if required - policy.keys():
        raise VerificationError("command environment policy is incomplete")
    if policy.get("schema") != ENVIRONMENT_SCHEMA:
        raise VerificationError("command environment policy schema is invalid")
    inherited = policy.get("inherited_names")
    inheritance_mode = policy.get("inheritance_mode")
    allowlisted_names = policy.get("allowlisted_names")
    forced = policy.get("forced")
    cleared = policy.get("cleared_names")
    hashes = policy.get("forced_value_sha256")
    if (
        inheritance_mode != "allowlist"
        or not isinstance(allowlisted_names, list)
        or allowlisted_names != sorted(set(allowlisted_names))
        or not isinstance(inherited, list)
        or inherited != sorted(set(inherited))
        or not all(isinstance(name, str) and name for name in inherited)
        or not isinstance(forced, Mapping)
        or not isinstance(cleared, list)
        or cleared != sorted(set(cleared))
        or not isinstance(hashes, Mapping)
    ):
        raise VerificationError("command environment policy fields are malformed")
    expected_forced = _normalize_environment_names(_FORCED_ENVIRONMENT)
    if dict(forced) != dict(sorted(expected_forced.items())):
        raise VerificationError("command environment forced values differ from policy")
    expected_cleared = set(
        _PYTHON_ISOLATION_ENV if spec is not None and spec.clear_pythonpath else ()
    )
    if os.name == "nt":
        expected_cleared = {name.upper() for name in expected_cleared}
    if set(cleared) != expected_cleared:
        raise VerificationError("command environment clear-list differs from policy")
    expected_allowlisted = {
        name.upper() if os.name == "nt" else name
        for name in _VERIFICATION_ENV_ALLOWLIST
    }
    if set(allowlisted_names) != expected_allowlisted:
        raise VerificationError("command environment allowlist differs from policy")
    if not set(inherited).issubset(expected_allowlisted):
        raise VerificationError("command inherited names exceed the allowlist")
    effective_names = set(inherited) | set(forced)
    if set(hashes) != set(forced) or effective_names & set(cleared):
        raise VerificationError("command environment forced-value binding is invalid")
    for name, digest in hashes.items():
        if not isinstance(name, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(
            digest
        ):
            raise VerificationError("command environment forced-value hash is invalid")
    for name, value in expected_forced.items():
        expected_hash = sha256_bytes(
            value.encode("utf-8", errors="surrogatepass")
        )
        if hashes.get(name) != expected_hash:
            raise VerificationError(
                f"command environment forced-value hash differs for {name}"
            )
    payload = {key: policy[key] for key in required if key != "digest"}
    return _validate_digest_descriptor(
        policy.get("digest"),
        payload,
        label="command environment policy",
    )


def _derive_command_environment_policy(
    run_policy: Mapping[str, Any],
    *,
    spec: CommandSpec,
) -> dict[str, Any]:
    _validate_environment_policy(run_policy, spec=None)
    forced = dict(run_policy["forced"])
    allowlisted_names = list(run_policy["allowlisted_names"])
    inherited = set(run_policy["inherited_names"])
    hashes = dict(run_policy["forced_value_sha256"])
    cleared = set(_PYTHON_ISOLATION_ENV if spec.clear_pythonpath else ())
    if os.name == "nt":
        cleared = {name.upper() for name in cleared}
    inherited.difference_update(cleared)
    payload = {
        "schema": ENVIRONMENT_SCHEMA,
        "inheritance_mode": "allowlist",
        "allowlisted_names": allowlisted_names,
        "inherited_names": sorted(inherited),
        "forced": forced,
        "cleared_names": sorted(cleared),
        "forced_value_sha256": dict(sorted(hashes.items())),
    }
    return {**payload, "digest": _digest_descriptor(payload)}


def _platform_record() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }


def _execution_context(
    platform_record: Mapping[str, Any],
    tool_versions: Mapping[str, Any],
    environment_policy: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "platform": dict(platform_record),
        "tool_versions": dict(tool_versions),
        "environment_policy": dict(environment_policy),
    }
    return {
        **payload,
        "digest": sha256_bytes(canonical_json_bytes(payload)),
    }


def _dependency_file_fingerprints(repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative in ("pyproject.toml", "uv.lock"):
        path = repo_root / relative
        if not path.is_file():
            rows.append({"path": relative, "present": False})
            continue
        digest, size = _sha256_file(path, max_bytes=MAX_PACKAGE_BYTES)
        rows.append(
            {
                "path": relative,
                "present": True,
                "sha256": digest,
                "size_bytes": size,
            }
        )
    return rows


def _package_version_fingerprints() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in ("atv-bench", "cryptography", "jsonschema", "pytest", "typer"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def _hash_tool_executable(path: Path) -> dict[str, Any]:
    display_path = _redact_host_path(path)
    try:
        info = path.stat()
    except OSError as exc:
        error, _ = _sanitize_public_text(str(exc))
        return {
            "path": display_path,
            "present": False,
            "error": error[:512],
        }
    row: dict[str, Any] = {
        "path": display_path,
        "present": path.is_file(),
        "size_bytes": info.st_size,
    }
    if not path.is_file():
        return row
    if info.st_size > MAX_EXECUTABLE_BYTES:
        row["sha256"] = None
        row["hash_error"] = f"executable exceeds {MAX_EXECUTABLE_BYTES} bytes"
        return row
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        error, _ = _sanitize_public_text(str(exc))
        row["sha256"] = None
        row["hash_error"] = error[:512]
    else:
        row["sha256"] = digest.hexdigest()
    return row


def _redact_host_path(value: str | os.PathLike[str]) -> str:
    absolute = os.path.abspath(os.fspath(value))
    candidates = (
        (os.path.abspath(os.fspath(Path.home())), "$HOME"),
        (os.path.abspath(tempfile.gettempdir()), "$TEMP"),
        (
            os.path.abspath(os.environ.get("SYSTEMROOT", ""))
            if os.environ.get("SYSTEMROOT")
            else "",
            "$SYSTEMROOT",
        ),
    )
    normalized = os.path.normcase(absolute)
    for base, label in candidates:
        if not base:
            continue
        normalized_base = os.path.normcase(base.rstrip("\\/"))
        if normalized == normalized_base:
            return label
        if normalized.startswith(normalized_base + os.sep):
            suffix = absolute[len(base.rstrip("\\/")) :].lstrip("\\/")
            return label + (f"/{suffix.replace(os.sep, '/')}" if suffix else "")
    return absolute.replace("\\", "/")


def _public_redaction_pairs(
    *,
    repo_root: Path | None = None,
    output_root: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for value, label in (
        (os.fspath(output_root.resolve()) if output_root is not None else "", "$EVIDENCE"),
        (os.fspath(repo_root.resolve()) if repo_root is not None else "", "$REPO"),
        (os.environ.get("USERPROFILE", ""), "$HOME"),
        (os.fspath(Path.home()), "$HOME"),
        (os.environ.get("TEMP", ""), "$TEMP"),
        (tempfile.gettempdir(), "$TEMP"),
        (os.environ.get("SYSTEMROOT", ""), "$SYSTEMROOT"),
    ):
        if not value:
            continue
        absolute = os.path.abspath(value).rstrip("\\/")
        for variant in {
            absolute,
            absolute.replace("\\", "/"),
            absolute.replace("\\", "\\\\"),
            "\\\\?\\" + absolute,
        }:
            if variant:
                candidates.append((variant, label))
    unique = {
        (value.casefold(), label): (value, label)
        for value, label in candidates
    }
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )


def _sanitize_public_text(
    value: str,
    *,
    repo_root: Path | None = None,
    output_root: Path | None = None,
) -> tuple[str, tuple[str, ...]]:
    text = str(value)
    redactions: set[str] = set()
    for candidate, label in _public_redaction_pairs(
        repo_root=repo_root,
        output_root=output_root,
    ):
        updated, count = re.subn(
            re.escape(candidate),
            label,
            text,
            flags=re.IGNORECASE,
        )
        if count:
            redactions.add(label)
            text = updated
    for raw, label in (
        (os.environ.get("USERNAME", ""), "$USER"),
        (os.environ.get("COMPUTERNAME", ""), "$HOST"),
    ):
        if not raw or len(raw) < 3:
            continue
        updated, count = re.subn(
            rf"(?<![A-Za-z0-9]){re.escape(raw)}(?![A-Za-z0-9])",
            label,
            text,
            flags=re.IGNORECASE,
        )
        if count:
            redactions.add(label)
            text = updated
    for name, raw in os.environ.items():
        upper = name.upper()
        if (
            len(raw) < 6
            or not any(
                marker in upper
                for marker in (
                    "CREDENTIAL",
                    "KEY",
                    "PASSWORD",
                    "SECRET",
                    "TOKEN",
                )
            )
        ):
            continue
        updated, count = re.subn(
            re.escape(raw),
            "$SECRET",
            text,
            flags=re.IGNORECASE,
        )
        if count:
            redactions.add("$SECRET")
            text = updated
    return text, tuple(sorted(redactions))


def _sanitize_public_bytes(
    value: bytes,
    *,
    repo_root: Path | None = None,
    output_root: Path | None = None,
) -> tuple[bytes, tuple[str, ...]]:
    text = value.decode("utf-8", errors="replace")
    sanitized, redactions = _sanitize_public_text(
        text,
        repo_root=repo_root,
        output_root=output_root,
    )
    return sanitized.encode("utf-8"), redactions


def _tool_executable_fingerprint(
    command: str,
    *,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    requested = _redact_host_path(command) if os.path.isabs(command) else command
    if os.path.isabs(command):
        resolved = command
    else:
        resolved = shutil.which(command, path=environment.get("PATH"))
    if not resolved:
        return {"requested": requested, "resolved": None, "present": False}
    return {
        "requested": requested,
        "resolved": _redact_host_path(os.path.abspath(resolved)),
        **_hash_tool_executable(Path(resolved)),
    }


def _toolchain_payload(
    *,
    repo_root: Path,
    tools: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": TOOLCHAIN_SCHEMA,
        "tools": {name: tools[name] for name in sorted(tools)},
        "dependency_files": _dependency_file_fingerprints(repo_root),
        "python_packages": _package_version_fingerprints(),
    }


def _toolchain_record(
    *,
    repo_root: Path,
    tools: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _toolchain_payload(repo_root=repo_root, tools=tools)
    return {**payload, "digest": _digest_descriptor(payload)}


def _validate_toolchain_record(
    record: Any,
    *,
    repo_root: Path,
) -> str:
    if not isinstance(record, Mapping) or record.get("schema") != TOOLCHAIN_SCHEMA:
        raise VerificationError("toolchain fingerprint schema is invalid")
    tools = record.get("tools")
    dependencies = record.get("dependency_files")
    packages = record.get("python_packages")
    if not isinstance(tools, Mapping) or not isinstance(dependencies, list) or not isinstance(
        packages, Mapping
    ):
        raise VerificationError("toolchain fingerprint is incomplete")
    if set(tools) != {"python", "pytest", "uv", "git", "docker"}:
        raise VerificationError("toolchain fingerprint tool set is invalid")
    for name, row in tools.items():
        if not isinstance(row, Mapping):
            raise VerificationError(f"toolchain fingerprint for {name} is malformed")
        exit_code = row.get("exit_code")
        if isinstance(exit_code, bool) or (
            exit_code is not None and not isinstance(exit_code, int)
        ):
            raise VerificationError(f"toolchain exit code for {name} is invalid")
        for field in ("stdout_sha256", "stderr_sha256"):
            digest = row.get(field)
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise VerificationError(f"toolchain {field} for {name} is invalid")
    expected_dependencies = _dependency_file_fingerprints(repo_root)
    if dependencies != expected_dependencies:
        raise VerificationError("dependency lock/config fingerprints changed")
    payload = {
        "schema": record["schema"],
        "tools": dict(tools),
        "dependency_files": dependencies,
        "python_packages": dict(packages),
    }
    return _validate_digest_descriptor(
        record.get("digest"),
        payload,
        label="toolchain fingerprint",
    )


def _repository_path_key(repository: Mapping[str, Any]) -> str:
    value = repository.get("repository_id")
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise VerificationError("repository id is invalid for filesystem paths")
    return value[:FILESYSTEM_ID_LENGTH]


def _verification_temp_prefix() -> Path:
    return (
        Path(tempfile.gettempdir()).resolve() / VERIFICATION_TEMP_DIRNAME
    ).resolve()


def _verification_temp_root(
    output_root: Path,
    repository: Mapping[str, Any],
) -> Path:
    prefix = _verification_temp_prefix()
    output_key = sha256_bytes(
        os.path.normcase(os.fspath(output_root.resolve())).encode(
            "utf-8",
            errors="surrogatepass",
        )
    )[:FILESYSTEM_ID_LENGTH]
    target = (
        prefix / f"{output_key}-{_repository_path_key(repository)}"
    ).resolve()
    if prefix not in target.parents:
        raise VerificationError(
            f"verification temp path escaped its prefix: {target}"
        )
    return target


def _pytest_basetemp(
    output_root: Path,
    repository: Mapping[str, Any],
    spec: CommandSpec,
) -> Path:
    command_key = sha256_bytes(spec.id.encode("ascii"))[:FILESYSTEM_ID_LENGTH]
    return _verification_temp_root(output_root, repository) / "p" / command_key


def _safe_remove_verification_temp(path: Path) -> None:
    prefix = _verification_temp_prefix()
    target = path.resolve()
    if target == prefix or prefix not in target.parents:
        raise VerificationError(
            f"refusing to remove path outside verification temp root: {target}"
        )
    if path.exists() and path.is_symlink():
        raise VerificationError(
            f"refusing to remove symlinked verification temp path: {path}"
        )
    if path.exists():
        def make_writable_and_retry(
            operation: Callable[..., Any],
            failing_path: str,
            error_info: tuple[type[BaseException], BaseException, Any],
        ) -> None:
            error = error_info[1]
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(failing_path, stat.S_IWRITE | stat.S_IREAD)
            operation(failing_path)

        for attempt in range(5):
            try:
                shutil.rmtree(path, onerror=make_writable_and_retry)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))


@contextlib.contextmanager
def _verification_run_lock(output_root: Path) -> Iterable[None]:
    lock_path = output_root / ".run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise VerificationError(
                f"another local verification run owns {lock_path}"
            ) from exc
        locked = True
        metadata = canonical_json_bytes(
            {
                "schema": "atv.local-verification-lock/v1",
                "pid": os.getpid(),
                "acquired_at": _iso(_utc_now()),
            }
        )
        handle.seek(0)
        handle.truncate()
        handle.write(metadata)
        handle.flush()
        yield
    finally:
        if locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _locked_verification_run(
    function: Callable[..., "VerificationOutcome"],
) -> Callable[..., "VerificationOutcome"]:
    @functools.wraps(function)
    def wrapped(
        self: "LocalVerificationRunner",
        *args: Any,
        **kwargs: Any,
    ) -> "VerificationOutcome":
        with _verification_run_lock(self.output_root):
            return function(self, *args, **kwargs)

    return wrapped


def _normalize_argv(
    argv: Sequence[str],
    *,
    repo_root: Path,
    output_root: Path,
    python_executable: str,
) -> list[str]:
    root = os.path.normcase(os.fspath(repo_root.resolve()))
    output = os.path.normcase(os.fspath(output_root.resolve()))
    python = os.path.normcase(os.path.abspath(python_executable))
    verification_temp = os.path.normcase(
        os.fspath(_verification_temp_prefix())
    )
    normalized: list[str] = []
    for token in argv:
        comparable = os.path.normcase(os.path.abspath(token)) if os.path.isabs(token) else ""
        if comparable == python:
            normalized.append("$PYTHON")
            continue
        raw = token.replace("\\", "/")
        token_case = os.path.normcase(os.path.abspath(token)) if os.path.isabs(token) else ""
        if token_case and (
            token_case == verification_temp
            or token_case.startswith(verification_temp + os.sep)
        ):
            suffix = token_case[len(verification_temp) :].lstrip("\\/")
            parts = Path(suffix).parts
            # The first component is a run key derived from the output path and
            # repository id. It is intentionally omitted from canonical evidence.
            stable_parts = parts[1:] if parts else ()
            stable_suffix = "/".join(stable_parts)
            normalized.append(
                "$VERIFY_TMP" + (f"/{stable_suffix}" if stable_suffix else "")
            )
        elif token_case and (token_case == output or token_case.startswith(output + os.sep)):
            suffix = token_case[len(output) :].lstrip("\\/")
            normalized.append("$EVIDENCE" + (f"/{suffix.replace(os.sep, '/')}" if suffix else ""))
        elif token_case and (token_case == root or token_case.startswith(root + os.sep)):
            suffix = token_case[len(root) :].lstrip("\\/")
            normalized.append("$REPO" + (f"/{suffix.replace(os.sep, '/')}" if suffix else ""))
        else:
            normalized.append(raw)
    return normalized


def _resolve_tokens(
    spec: CommandSpec,
    *,
    repo_root: Path,
    output_root: Path,
    repository: Mapping[str, Any],
    python_executable: str,
) -> tuple[str, ...]:
    build_root = output_root / "build" / _repository_path_key(repository)
    dist_dir = build_root / "dist"
    temp_root = _verification_temp_root(output_root, repository)
    wheel_venv = temp_root / "wv"
    sdist_venv = temp_root / "sv"
    wheel_python = wheel_venv / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    sdist_python = sdist_venv / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    work = output_root / "work" / _repository_path_key(repository) / spec.id
    junit_path = work / "junit.xml"
    basetemp = _pytest_basetemp(output_root, repository, spec)

    def one_package(pattern: str, label: str) -> Path:
        matches = sorted(path for path in dist_dir.glob(pattern) if path.is_file())
        if len(matches) != 1:
            raise VerificationError(
                f"{spec.id} requires exactly one {label} in {dist_dir}; found {len(matches)}"
            )
        _sha256_file(matches[0], max_bytes=MAX_PACKAGE_BYTES)
        return matches[0]

    replacements: dict[str, str | Callable[[], str]] = {
        "{PYTHON}": os.path.abspath(python_executable),
        "{JUNIT}": os.fspath(junit_path),
        "{BASETEMP}": os.fspath(basetemp),
        "{DIST_DIR}": os.fspath(dist_dir),
        "{WHEEL_VENV}": os.fspath(wheel_venv),
        "{SDIST_VENV}": os.fspath(sdist_venv),
        "{WHEEL_PYTHON}": os.fspath(wheel_python),
        "{SDIST_PYTHON}": os.fspath(sdist_python),
        "{WHEEL}": lambda: os.fspath(one_package("*.whl", "wheel")),
        "{SDIST}": lambda: os.fspath(one_package("*.tar.gz", "sdist")),
    }
    resolved: list[str] = []
    for token in spec.argv:
        replacement = replacements.get(token)
        if replacement is None:
            if token.startswith("{{") and token.endswith("}}"):
                resolved.append(token)
            elif token.startswith("{") and token.endswith("}"):
                raise VerificationError(f"{spec.id} uses an unknown fixed token: {token}")
            else:
                resolved.append(token)
        elif callable(replacement):
            resolved.append(replacement())
        else:
            resolved.append(replacement)
    return tuple(resolved)


def _resolve_cwd(
    spec: CommandSpec,
    *,
    repo_root: Path,
    output_root: Path,
    repository: Mapping[str, Any],
) -> Path:
    if spec.cwd == "{REPO}":
        return repo_root
    if spec.cwd == "{ISOLATED_CWD}":
        command_key = sha256_bytes(spec.id.encode("ascii"))[
            :FILESYSTEM_ID_LENGTH
        ]
        target = (
            _verification_temp_root(output_root, repository)
            / "i"
            / command_key
        )
        target.mkdir(parents=True, exist_ok=True)
        return target
    raise VerificationError(f"{spec.id} has an unknown cwd token")


def _diagnostic(
    *,
    problem: str,
    cause: str,
    fix: str,
    evidence: Sequence[str],
) -> dict[str, Any]:
    return {
        "Problem": problem,
        "Cause": cause,
        "Fix": fix,
        "Evidence": list(evidence),
    }


def format_diagnostic(diagnostic: Mapping[str, Any]) -> str:
    evidence = diagnostic.get("Evidence")
    evidence_rows = evidence if isinstance(evidence, list) else [str(evidence)]
    return "\n".join(
        (
            f"Problem: {diagnostic.get('Problem', 'unknown')}",
            f"Cause: {diagnostic.get('Cause', 'unknown')}",
            f"Fix: {diagnostic.get('Fix', 'unknown')}",
            "Evidence: " + ("; ".join(map(str, evidence_rows)) or "none"),
        )
    )


def _docker_evidence(spec: CommandSpec, raw: RawExecution, junit: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not spec.docker:
        return None
    evidence: dict[str, Any] = {
        "command_id": spec.id,
        "exit_code": raw.exit_code,
        "timed_out": raw.timed_out,
    }
    text = raw.stdout.decode("utf-8", errors="replace").strip()
    if spec.id == "docker_preflight":
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            document = {}
        server = document.get("Server") if isinstance(document, Mapping) else None
        client = document.get("Client") if isinstance(document, Mapping) else None
        evidence.update(
            {
                "daemon_reachable": bool(
                    raw.exit_code == 0
                    and isinstance(server, Mapping)
                    and server.get("Version")
                ),
                "server_version": (
                    str(server.get("Version"))
                    if isinstance(server, Mapping) and server.get("Version")
                    else None
                ),
                "client_version": (
                    str(client.get("Version"))
                    if isinstance(client, Mapping) and client.get("Version")
                    else None
                ),
            }
        )
    elif spec.id == "docker_image":
        try:
            image_document = json.loads(text)
        except json.JSONDecodeError:
            image_document = {}
        if isinstance(image_document, list):
            repo_digests = image_document
            image_id = operating_system = architecture = None
        elif isinstance(image_document, Mapping):
            repo_digests = image_document.get("RepoDigests", [])
            image_id = image_document.get("Id")
            operating_system = image_document.get("Os")
            architecture = image_document.get("Architecture")
        else:
            repo_digests = []
            image_id = operating_system = architecture = None
        if not isinstance(repo_digests, list):
            repo_digests = []
        normalized_digests = sorted(str(item) for item in repo_digests)
        evidence.update(
            {
                "image": PYTHON_IMAGE,
                "expected_digest": PYTHON_IMAGE_DIGEST,
                "image_id": str(image_id) if image_id else None,
                "os": str(operating_system) if operating_system else None,
                "architecture": str(architecture) if architecture else None,
                "repo_digests": normalized_digests,
                "image_digest_verified": bool(
                    raw.exit_code == 0
                    and not raw.timed_out
                    and any(
                    str(item).endswith("@sha256:" + PYTHON_IMAGE_DIGEST)
                    for item in normalized_digests
                    )
                ),
            }
        )
    elif junit is not None:
        evidence.update(
            {
                "integration": True,
                "tests": junit.get("tests"),
                "skipped": junit.get("skipped"),
                "failures": junit.get("failures"),
                "errors": junit.get("errors"),
            }
        )
    return evidence


def _full_stream_sha256(
    raw: RawExecution,
    *,
    stream: str,
) -> str:
    captured = raw.stdout if stream == "stdout" else raw.stderr
    total = raw.stdout_total_bytes if stream == "stdout" else raw.stderr_total_bytes
    truncated = raw.stdout_truncated if stream == "stdout" else raw.stderr_truncated
    declared = raw.stdout_sha256 if stream == "stdout" else raw.stderr_sha256
    if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
        return declared
    if not truncated and total == len(captured):
        return sha256_bytes(captured)
    raise VerificationError(
        f"{stream} was truncated without a full-stream SHA-256 digest"
    )


def _stream_record(
    repo_root: Path,
    path: Path,
    *,
    label: str,
    raw: RawExecution,
) -> dict[str, Any]:
    captured = raw.stdout if label == "stdout" else raw.stderr
    total = raw.stdout_total_bytes if label == "stdout" else raw.stderr_total_bytes
    truncated = raw.stdout_truncated if label == "stdout" else raw.stderr_truncated
    redactions = (
        raw.stdout_redactions if label == "stdout" else raw.stderr_redactions
    )
    reference = _artifact_reference(
        repo_root,
        path,
        label=label,
        max_bytes=MAX_CAPTURE_BYTES + 4096,
    )
    if reference["size_bytes"] != len(captured):
        raise VerificationError(f"{label} capture changed before it was recorded")
    return {
        "schema": STREAM_SCHEMA,
        **reference,
        "capture_sha256": reference["sha256"],
        "stream_sha256": _full_stream_sha256(raw, stream=label),
        "capture_limit_bytes": MAX_CAPTURE_BYTES,
        "capture_strategy": "head-tail",
        "total_bytes": total,
        "truncated": truncated,
        "redactions": list(redactions),
    }


def _termination_record(
    raw: RawExecution,
    *,
    status: str,
    blocked: bool = False,
) -> dict[str, Any]:
    if blocked:
        kind = "blocked"
    elif raw.timed_out:
        kind = "timed_out"
    elif raw.exit_code is None:
        kind = "spawn_error"
    else:
        kind = "exited"
    return {
        "schema": TERMINATION_SCHEMA,
        "kind": kind,
        "exit_code": raw.exit_code,
        "timed_out": raw.timed_out,
        "status": status,
        "error": raw.error,
    }


def _invocation_record(
    *,
    argv: Sequence[str],
    cwd: str,
    environment_digest: str,
    timeout_seconds: int,
    max_output_bytes: int,
) -> dict[str, Any]:
    payload = {
        "schema": INVOCATION_SCHEMA,
        "argv": list(argv),
        "cwd": cwd,
        "environment_digest": environment_digest,
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "shell": False,
        "stdin": "devnull",
    }
    return {**payload, "digest": _digest_descriptor(payload)}


def _command_canonical_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "canonical_digest",
            "started_at",
            "finished_at",
            "duration_ms",
            "diagnostic",
        }
    }
    junit = payload.get("junit")
    if isinstance(junit, Mapping):
        normalized_junit = {
            key: value
            for key, value in junit.items()
            if key not in {"generated_at", "duration_seconds"}
        }
        cases = normalized_junit.get("testcases")
        if isinstance(cases, list):
            normalized_junit["testcases"] = [
                {
                    key: value
                    for key, value in case.items()
                    if key != "duration_seconds"
                }
                if isinstance(case, Mapping)
                else case
                for case in cases
            ]
        payload["junit"] = normalized_junit
    return payload


def _proof_canonical_payload(proof: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in proof.items()
        if key not in {"canonical_digest", "generated_at"}
    }


def _manifest_canonical_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    commands = manifest.get("commands")
    command_rows: dict[str, Any] = {}
    if isinstance(commands, Mapping):
        for command_id, reference in sorted(commands.items()):
            if isinstance(reference, Mapping):
                command_rows[str(command_id)] = {
                    "canonical_sha256": reference.get("canonical_sha256"),
                    "status": reference.get("status"),
                    "exit_code": reference.get("exit_code"),
                }
    proof = manifest.get("proof")
    proof_row = (
        {"canonical_sha256": proof.get("canonical_sha256")}
        if isinstance(proof, Mapping)
        else None
    )
    proof_records = manifest.get("proofs")
    normalized_proofs: dict[str, Any] = {}
    if isinstance(proof_records, Mapping):
        for gate_id, reference in sorted(proof_records.items()):
            if isinstance(reference, Mapping):
                normalized_proofs[str(gate_id)] = {
                    "canonical_sha256": reference.get("canonical_sha256"),
                    "command": reference.get("command"),
                    "exit_code": reference.get("exit_code"),
                }
    return {
        key: value
        for key, value in {
            **dict(manifest),
            "commands": command_rows,
            "proof": proof_row,
            "proofs": normalized_proofs,
        }.items()
        if key not in {"canonical_digest", "generated_at"}
    }


def _record_status(
    raw: RawExecution, junit: Mapping[str, Any] | None, source_changed: bool
) -> str:
    if source_changed:
        return "failed"
    if raw.timed_out:
        return "timed_out"
    if raw.error or raw.exit_code is None:
        return "error"
    if raw.exit_code != 0:
        return "failed"
    if junit is not None:
        if junit["failures"] or junit["errors"]:
            return "failed"
        if junit["tests"] and junit["skipped"] == junit["tests"]:
            return "skipped"
    return "passed"


def _command_diagnostic(
    spec: CommandSpec,
    *,
    status: str,
    raw: RawExecution,
    junit: Mapping[str, Any] | None,
    source_changed: bool,
) -> dict[str, Any]:
    evidence = [
        f"argv_id={spec.id}",
        f"exit_code={raw.exit_code!r}",
        f"duration_ms={raw.duration_ms}",
    ]
    if junit is not None:
        evidence.append(
            "junit="
            f"{junit['tests']} tests/{junit['failures']} failures/"
            f"{junit['errors']} errors/{junit['skipped']} skips"
        )
    if status == "passed":
        return _diagnostic(
            problem="No command failure was observed.",
            cause="The fixed argv command exited successfully and its evidence parsed.",
            fix="No local fix is indicated by this command.",
            evidence=evidence,
        )
    if source_changed:
        cause = "The repository source snapshot changed while verification was running."
        fix = "Revert unintended source changes and rerun from a stable worktree."
    elif status == "skipped":
        cause = "Every collected test was skipped because a required runtime prerequisite was unavailable."
        fix = "Provide the exact prerequisite (for example Docker daemon and cached digest-pinned image) and rerun."
    elif raw.timed_out:
        cause = raw.error or "The command exceeded its fixed timeout."
        fix = "Resolve the hang or prerequisite issue; do not increase trust from this run."
    else:
        cause = raw.error or f"The fixed command exited with {raw.exit_code!r}."
        fix = "Inspect the bounded stdout/stderr artifacts, fix the failure, and rerun."
    return _diagnostic(
        problem=f"Verification command {spec.id} did not produce passing evidence.",
        cause=cause,
        fix=fix,
        evidence=evidence,
    )


def _command_cache_key(
    spec: CommandSpec,
    repository: Mapping[str, Any],
    plan_digest: str,
    execution_context_digest: str,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "command_spec_digest": spec.digest,
                "repository_id": repository["repository_id"],
                "workspace_id": repository["workspace_id"],
                "plan_digest": plan_digest,
                "execution_context_digest": execution_context_digest,
            }
        )
    )


def _read_json_bytes(data: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be a JSON object")
    return value


def _load_resume_index(output_root: Path) -> dict[str, Any]:
    path = output_root / "resume-index.json"
    if not path.is_file():
        return {"schema": RESUME_INDEX_SCHEMA, "entries": {}}
    try:
        document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"schema": RESUME_INDEX_SCHEMA, "entries": {}}
    if (
        not isinstance(document, dict)
        or document.get("schema") != RESUME_INDEX_SCHEMA
        or not isinstance(document.get("entries"), dict)
    ):
        return {"schema": RESUME_INDEX_SCHEMA, "entries": {}}
    return document


def _write_resume_index(output_root: Path, index: Mapping[str, Any]) -> None:
    _atomic_write(output_root / "resume-index.json", canonical_json_bytes(index))


def _safe_remove_tree(path: Path, *, output_root: Path) -> None:
    output = output_root.resolve()
    target = path.resolve(strict=False)
    try:
        relative = target.relative_to(output)
    except ValueError as exc:
        raise VerificationError(f"refusing to remove path outside evidence root: {path}") from exc
    if not relative.parts:
        raise VerificationError("refusing to remove the evidence root itself")
    if path.exists() and path.is_symlink():
        raise VerificationError(f"refusing to remove a symlinked build path: {path}")
    if path.exists():
        shutil.rmtree(path)


def _prepare_command_paths(
    spec: CommandSpec,
    *,
    output_root: Path,
    repository: Mapping[str, Any],
    resume: bool,
) -> None:
    work = output_root / "work" / _repository_path_key(repository) / spec.id
    if work.exists() and not resume:
        _safe_remove_tree(work, output_root=output_root)
    work.mkdir(parents=True, exist_ok=True)
    if spec.junit:
        basetemp = _pytest_basetemp(output_root, repository, spec)
        if not resume:
            _safe_remove_verification_temp(basetemp)
        basetemp.parent.mkdir(parents=True, exist_ok=True)
    if spec.id == "package_build" and not resume:
        dist = output_root / "build" / _repository_path_key(repository) / "dist"
        if dist.exists():
            _safe_remove_tree(dist, output_root=output_root)
        dist.mkdir(parents=True, exist_ok=True)
    if spec.id in {"wheel_venv_create", "sdist_venv_create"} and not resume:
        name = "wv" if spec.id.startswith("wheel") else "sv"
        target = _verification_temp_root(output_root, repository) / name
        _safe_remove_verification_temp(target)


def _collect_package_artifacts(
    spec: CommandSpec,
    *,
    repo_root: Path,
    output_root: Path,
    repository: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not spec.output_artifacts:
        return []
    dist = output_root / "build" / _repository_path_key(repository) / "dist"
    rows: list[dict[str, Any]] = []
    for label, pattern in (("wheel", "*.whl"), ("sdist", "*.tar.gz")):
        if label not in spec.output_artifacts:
            continue
        matches = sorted(path for path in dist.glob(pattern) if path.is_file())
        if len(matches) != 1:
            raise VerificationError(
                f"{spec.id} produced {len(matches)} {label} artifacts; exactly one is required"
            )
        rows.append(
            _artifact_reference(
                repo_root,
                matches[0],
                label=label,
                max_bytes=MAX_PACKAGE_BYTES,
            )
        )
    return rows


def _command_artifact_paths(
    output_root: Path, repository: Mapping[str, Any], spec: CommandSpec
) -> tuple[Path, Path, Path]:
    work = output_root / "work" / _repository_path_key(repository) / spec.id
    return work / "stdout.bin", work / "stderr.bin", work / "junit.xml"


def _validate_source_references(
    repo_root: Path, references: Sequence[Mapping[str, Any]]
) -> None:
    for index, reference in enumerate(references):
        _read_artifact(
            repo_root,
            reference,
            label=f"test source {index}",
            max_bytes=4 * 1024 * 1024,
        )


def _validate_stream_record(
    repo_root: Path,
    reference: Any,
    *,
    label: str,
) -> bytes:
    if not isinstance(reference, Mapping):
        raise VerificationError(f"{label} artifact reference is absent")
    if reference.get("schema") != STREAM_SCHEMA:
        raise VerificationError(f"{label} stream schema is invalid")
    data = _read_artifact(
        repo_root,
        reference,
        label=label,
        max_bytes=MAX_CAPTURE_BYTES + 4096,
    )
    if reference.get("capture_sha256") != reference.get("sha256"):
        raise VerificationError(f"{label} capture digest binding is invalid")
    stream_sha256 = reference.get("stream_sha256")
    if not isinstance(stream_sha256, str) or not SHA256_RE.fullmatch(stream_sha256):
        raise VerificationError(f"{label} full-stream digest is invalid")
    total = reference.get("total_bytes")
    truncated = reference.get("truncated")
    redactions = reference.get("redactions")
    if isinstance(total, bool) or not isinstance(total, int) or total < len(data):
        raise VerificationError(f"{label} total byte count is invalid")
    if not isinstance(truncated, bool):
        raise VerificationError(f"{label} truncation flag is invalid")
    if (
        not isinstance(redactions, list)
        or redactions != sorted(set(redactions))
        or not set(redactions).issubset(
            {
                "$EVIDENCE",
                "$HOME",
                "$HOST",
                "$REPO",
                "$SECRET",
                "$SYSTEMROOT",
                "$TEMP",
                "$USER",
            }
        )
    ):
        raise VerificationError(f"{label} redaction metadata is invalid")
    if reference.get("capture_limit_bytes") != MAX_CAPTURE_BYTES:
        raise VerificationError(f"{label} capture limit differs from policy")
    if reference.get("capture_strategy") != "head-tail":
        raise VerificationError(f"{label} capture strategy differs from policy")
    if truncated:
        if total <= len(data) or len(data) > MAX_CAPTURE_BYTES:
            raise VerificationError(f"{label} truncated capture accounting is invalid")
    elif total != len(data) or stream_sha256 != sha256_bytes(data):
        raise VerificationError(f"{label} complete stream digest/accounting is invalid")
    return data


def _validate_invocation_record(
    invocation: Any,
    *,
    argv: Sequence[str],
    cwd: str,
    environment_digest: str,
    spec: CommandSpec,
) -> None:
    if not isinstance(invocation, Mapping):
        raise VerificationError(f"{spec.id} invocation record is absent")
    expected = _invocation_record(
        argv=argv,
        cwd=cwd,
        environment_digest=environment_digest,
        timeout_seconds=spec.timeout_seconds,
        max_output_bytes=MAX_CAPTURE_BYTES,
    )
    if invocation != expected:
        raise VerificationError(f"{spec.id} invocation differs from the fixed policy")


def _validate_termination_record(
    record: Mapping[str, Any],
    *,
    spec: CommandSpec,
    junit: Mapping[str, Any] | None,
) -> None:
    timed_out = record.get("timed_out")
    exit_code = record.get("exit_code")
    error = record.get("error")
    status = record.get("status")
    source_changed = record.get("source_changed")
    if not isinstance(timed_out, bool):
        raise VerificationError(f"command timeout flag is invalid for {spec.id}")
    if isinstance(exit_code, bool) or (
        exit_code is not None and not isinstance(exit_code, int)
    ):
        raise VerificationError(f"command exit code is invalid for {spec.id}")
    if error is not None and not isinstance(error, str):
        raise VerificationError(f"command error field is invalid for {spec.id}")
    if not isinstance(source_changed, bool):
        raise VerificationError(f"command source-changed flag is invalid for {spec.id}")
    if status not in {
        "passed",
        "failed",
        "skipped",
        "timed_out",
        "error",
        "blocked",
    }:
        raise VerificationError(f"command status is invalid for {spec.id}")
    termination = record.get("termination")
    if not isinstance(termination, Mapping) or termination.get("schema") != TERMINATION_SCHEMA:
        raise VerificationError(f"command termination record is invalid for {spec.id}")
    expected_kind = (
        "blocked"
        if status == "blocked"
        else "timed_out"
        if timed_out
        else "spawn_error"
        if exit_code is None
        else "exited"
    )
    expected_termination = {
        "schema": TERMINATION_SCHEMA,
        "kind": expected_kind,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "status": status,
        "error": error,
    }
    if dict(termination) != expected_termination:
        raise VerificationError(f"command termination semantics were forged for {spec.id}")
    if status == "blocked":
        if exit_code is not None or timed_out:
            raise VerificationError(f"blocked command termination is invalid for {spec.id}")
        return
    raw = RawExecution(
        argv=tuple(),
        started_at=str(record.get("started_at")),
        finished_at=str(record.get("finished_at")),
        duration_ms=int(record.get("duration_ms", 0)),
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=b"",
        stderr=b"",
        stdout_total_bytes=0,
        stderr_total_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        error=error,
    )
    expected_status = _record_status(raw, junit, source_changed)
    if status != expected_status:
        raise VerificationError(
            f"command exit/timeout status is inconsistent for {spec.id}: "
            f"expected={expected_status} observed={status}"
        )


def _validate_command_record(
    repo_root: Path,
    record: Mapping[str, Any],
    *,
    spec: CommandSpec,
    repository: Mapping[str, Any],
    plan_digest: str,
    execution_context_digest: str,
    run_environment_policy: Mapping[str, Any],
    output_root: Path,
    python_executable: str,
    now: datetime,
) -> dict[str, Any]:
    required = {
        "schema",
        "command_id",
        "command_spec_digest",
        "plan_digest",
        "execution_context_digest",
        "repository",
        "argv",
        "cwd",
        "environment",
        "invocation",
        "started_at",
        "finished_at",
        "duration_ms",
        "exit_code",
        "timed_out",
        "error",
        "source_changed",
        "status",
        "termination",
        "stdout",
        "stderr",
        "artifacts",
        "test_sources",
        "diagnostic",
        "canonical_digest",
    }
    missing = sorted(required - record.keys())
    if missing:
        raise VerificationError(
            f"command evidence {spec.id} is missing fields: {', '.join(missing)}"
        )
    if record.get("schema") != COMMAND_SCHEMA or record.get("command_id") != spec.id:
        raise VerificationError(f"command evidence identity mismatch for {spec.id}")
    if record.get("command_spec_digest") != spec.digest:
        raise VerificationError(f"command spec digest mismatch for {spec.id}")
    if record.get("plan_digest") != plan_digest:
        raise VerificationError(f"plan digest mismatch for {spec.id}")
    if record.get("execution_context_digest") != execution_context_digest:
        raise VerificationError(f"execution context changed for {spec.id}")
    bound = record.get("repository")
    if not isinstance(bound, Mapping):
        raise VerificationError(f"command repository binding is absent for {spec.id}")
    for field in (
        "schema",
        "origin",
        "head",
        "head_tree",
        "tree_digest",
        "repository_id",
        "workspace_id",
        "dirty",
        "dirty_path_count",
        "staged_path_count",
        "worktree_path_count",
        "untracked_path_count",
        "dirty_state_sha256",
        "file_count",
        "excluded_paths",
    ):
        if bound.get(field) != repository.get(field):
            raise VerificationError(
                f"cross-repository or stale command evidence for {spec.id}: {field}"
            )
    finished = _parse_time(record.get("finished_at"), label=f"{spec.id} finish")
    started = _parse_time(record.get("started_at"), label=f"{spec.id} start")
    if finished < started:
        raise VerificationError(f"command finish precedes start for {spec.id}")
    age_days = (now - finished).total_seconds() / 86400
    if age_days < 0 or age_days > RESUME_FRESHNESS_DAYS:
        raise VerificationError(
            f"command evidence is stale or future-dated for {spec.id}: {age_days:.2f} days"
        )
    if not isinstance(record.get("duration_ms"), int) or record["duration_ms"] < 0:
        raise VerificationError(f"command duration is invalid for {spec.id}")
    try:
        expected_resolved = _resolve_tokens(
            spec,
            repo_root=repo_root,
            output_root=output_root,
            repository=repository,
            python_executable=python_executable,
        )
        expected_argv = _normalize_argv(
            expected_resolved,
            repo_root=repo_root,
            output_root=output_root,
            python_executable=python_executable,
        )
    except VerificationError:
        if record.get("status") != "blocked":
            raise
        expected_argv = list(spec.argv)
    if record.get("argv") != expected_argv:
        raise VerificationError(
            f"command argv differs from the fixed allowlist for {spec.id}"
        )
    expected_cwd = _resolve_cwd(
        spec,
        repo_root=repo_root,
        output_root=output_root,
        repository=repository,
    )
    expected_cwd_normalized = _normalize_argv(
        (os.fspath(expected_cwd),),
        repo_root=repo_root,
        output_root=output_root,
        python_executable=python_executable,
    )[0]
    if record.get("cwd") != expected_cwd_normalized:
        raise VerificationError(f"command cwd differs from the fixed plan for {spec.id}")
    environment_digest = _validate_environment_policy(
        record.get("environment"),
        spec=spec,
    )
    expected_environment_policy = _derive_command_environment_policy(
        run_environment_policy,
        spec=spec,
    )
    if record.get("environment") != expected_environment_policy:
        raise VerificationError(f"{spec.id} environment differs from the run policy")
    _validate_invocation_record(
        record.get("invocation"),
        argv=expected_argv,
        cwd=expected_cwd_normalized,
        environment_digest=environment_digest,
        spec=spec,
    )
    _validate_stream_record(
        repo_root,
        record.get("stdout"),
        label=f"{spec.id} stdout",
    )
    _validate_stream_record(
        repo_root,
        record.get("stderr"),
        label=f"{spec.id} stderr",
    )
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        raise VerificationError(f"{spec.id} artifacts must be an array")
    for index, reference in enumerate(artifacts):
        if not isinstance(reference, Mapping):
            raise VerificationError(f"{spec.id} artifact {index} is not an object")
        maximum = (
            MAX_PACKAGE_BYTES
            if reference.get("label") in {"wheel", "sdist"}
            else MAX_JSON_BYTES
        )
        _read_artifact(
            repo_root,
            reference,
            label=f"{spec.id} artifact {index}",
            max_bytes=maximum,
        )
    sources = record.get("test_sources")
    if not isinstance(sources, list):
        raise VerificationError(f"{spec.id} test_sources must be an array")
    _validate_source_references(repo_root, sources)

    junit_record = record.get("junit")
    if spec.junit:
        if not isinstance(junit_record, Mapping):
            if record.get("status") in {"passed", "skipped"}:
                raise VerificationError(f"{spec.id} is missing its JUnit artifact")
        else:
            sanitized_fields = junit_record.get("sanitized_fields")
            if (
                not isinstance(sanitized_fields, list)
                or sanitized_fields != sorted(set(sanitized_fields))
                or not set(sanitized_fields).issubset(
                    {"testsuite.hostname", "xml.host-identifiers"}
                )
            ):
                raise VerificationError(
                    f"{spec.id} JUnit sanitization metadata is invalid"
                )
            data = _read_artifact(
                repo_root,
                junit_record,
                label=f"{spec.id} JUnit",
                max_bytes=MAX_JUNIT_BYTES,
            )
            junit_path = repo_root / str(junit_record["artifact"])
            parsed = parse_junit(
                junit_path,
                command_started_at=str(record["started_at"]),
                command_finished_at=str(record["finished_at"]),
                allowed_targets=spec.pytest_targets,
            )
            if parsed["sha256"] != sha256_bytes(data):
                raise VerificationError(f"{spec.id} JUnit changed while validating")
            for field in (
                "tests",
                "passed",
                "failures",
                "errors",
                "skipped",
                "testcases",
            ):
                if junit_record.get(field) != parsed.get(field):
                    raise VerificationError(
                        f"{spec.id} JUnit summary field was forged or stale: {field}"
                    )
            docker_error = _docker_case_error(spec.id, junit_record)
            if docker_error and record.get("status") == "passed":
                raise VerificationError(docker_error)
    elif junit_record is not None:
        raise VerificationError(f"{spec.id} unexpectedly contains JUnit evidence")
    _validate_termination_record(
        record,
        spec=spec,
        junit=junit_record if isinstance(junit_record, Mapping) else None,
    )
    _validate_digest_descriptor(
        record.get("canonical_digest"),
        _command_canonical_payload(record),
        label=f"command evidence {spec.id}",
    )
    return dict(record)


def _test_outcomes(record: Mapping[str, Any], required_id: str) -> list[str]:
    junit = record.get("junit")
    if not isinstance(junit, Mapping):
        return []
    return [
        str(row.get("outcome"))
        for row in junit.get("testcases", [])
        if isinstance(row, Mapping)
        and row.get("base_node_id") == _base_test_id(required_id)
    ]


def _docker_case_error(
    command_id: str, junit: Mapping[str, Any] | None
) -> str | None:
    expected = EXPECTED_DOCKER_CASES.get(command_id)
    if expected is None:
        return None
    if not isinstance(junit, Mapping):
        return f"{command_id} has no JUnit evidence for its fixed Docker cases"
    rows = junit.get("testcases")
    if not isinstance(rows, list):
        return f"{command_id} JUnit testcases are absent"
    observed = {
        str(row.get("node_id")): str(row.get("outcome"))
        for row in rows
        if isinstance(row, Mapping)
    }
    if set(observed) != set(expected):
        return (
            f"{command_id} Docker testcase set changed: "
            f"expected={sorted(expected)!r} observed={sorted(observed)!r}"
        )
    nonpassing = sorted(
        node_id for node_id, outcome in observed.items() if outcome != "passed"
    )
    if nonpassing:
        return (
            f"{command_id} Docker cases did not all pass: "
            + ", ".join(nonpassing)
        )
    return None


def _assessment(
    rule: GateRule,
    command_records: Mapping[str, Mapping[str, Any]],
    *,
    mode: str,
) -> dict[str, Any]:
    problems: list[str] = []
    blockers: list[str] = []
    evidence: list[dict[str, Any]] = []
    requirements = list(rule.requirements)
    if mode == "full":
        requirements.append(EvidenceRequirement("full_non_live"))
    for requirement in requirements:
        record = command_records.get(requirement.command_id)
        if record is None:
            blockers.append(f"missing command evidence: {requirement.command_id}")
            continue
        evidence_row: dict[str, Any] = {
            "command_id": requirement.command_id,
            "command_artifact": record.get("_artifact"),
            "command_sha256": record.get("_sha256"),
            "argv": record.get("argv"),
            "exit_code": record.get("exit_code"),
            "status": record.get("status"),
            "required_test_ids": list(requirement.test_ids),
        }
        junit = record.get("junit")
        if isinstance(junit, Mapping):
            evidence_row["junit"] = {
                "artifact": junit.get("artifact"),
                "sha256": junit.get("sha256"),
                "tests": junit.get("tests"),
                "failures": junit.get("failures"),
                "errors": junit.get("errors"),
                "skipped": junit.get("skipped"),
            }
        evidence.append(evidence_row)
        if record.get("exit_code") != 0 or record.get("status") not in {
            "passed",
            "skipped",
        }:
            problems.append(
                f"{requirement.command_id} did not exit successfully "
                f"(status={record.get('status')}, exit={record.get('exit_code')!r})"
            )
            continue
        if record.get("status") == "skipped":
            blockers.append(f"{requirement.command_id} skipped every test")
        if requirement.require_zero_skips:
            if not isinstance(junit, Mapping) or junit.get("skipped") != 0:
                blockers.append(
                    f"{requirement.command_id} has skipped integration evidence"
                )
        for test_id in requirement.test_ids:
            _assert_required_test_exists(Path(str(record["_repo_root"])), test_id)
            outcomes = _test_outcomes(record, test_id)
            if not outcomes:
                problems.append(
                    f"{requirement.command_id} lacks exact required test {test_id}"
                )
            elif any(outcome in {"failed", "error"} for outcome in outcomes):
                problems.append(f"required test failed: {test_id}")
            elif any(outcome == "skipped" for outcome in outcomes):
                blockers.append(f"required test skipped: {test_id}")
            elif any(outcome != "passed" for outcome in outcomes):
                problems.append(f"required test has unknown outcome: {test_id}")
        labels = {
            row.get("label")
            for row in record.get("artifacts", [])
            if isinstance(row, Mapping)
        }
        missing_labels = sorted(set(requirement.artifact_labels) - labels)
        if missing_labels:
            problems.append(
                f"{requirement.command_id} lacks artifacts: {', '.join(missing_labels)}"
            )
        docker = record.get("docker")
        if requirement.require_docker_daemon and (
            not isinstance(docker, Mapping)
            or docker.get("daemon_reachable") is not True
        ):
            blockers.append("Docker daemon evidence is unavailable")
        if requirement.require_docker_image and (
            not isinstance(docker, Mapping)
            or docker.get("image_digest_verified") is not True
        ):
            blockers.append("digest-pinned Docker image evidence is unavailable")

    if problems:
        result = "failed"
        problem = "Required local verification evidence failed or was inconsistent."
        cause = "; ".join(problems)
        fix = "Fix the failing command or evidence binding and rerun the fixed plan."
    elif blockers:
        result = "blocked"
        problem = "The gate lacks complete executable evidence."
        cause = "; ".join(blockers)
        fix = "Provide the missing prerequisite and rerun; do not promote this gate."
    elif rule.forced_block_reason:
        result = "blocked"
        problem = "The local evidence is intentionally insufficient for this claim."
        cause = rule.forced_block_reason
        fix = "Supply independently verified external evidence; do not infer it from local tests."
    else:
        result = "passed"
        problem = "No mapped verification failure was observed."
        cause = "Every exact mapped command and test id passed with required artifact digests."
        fix = "No local implementation fix is indicated for this gate."
    return {
        "result": result,
        "claims": dict(rule.claims),
        **_diagnostic(
            problem=problem,
            cause=cause,
            fix=fix,
            evidence=[
                f"{row['command_id']}:{row.get('command_sha256')}"
                for row in evidence
            ],
        ),
        "mapped_evidence": evidence,
    }


def _proof_command(rule: GateRule, records: Mapping[str, Mapping[str, Any]]) -> str:
    mapped = []
    for requirement in rule.requirements:
        record = records.get(requirement.command_id)
        if record is not None:
            mapped.append(
                {
                    "command_id": requirement.command_id,
                    "argv": record.get("argv"),
                    "test_ids": list(requirement.test_ids),
                }
            )
    return json.dumps(mapped, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_governance_input(
    path: Path,
    *,
    expected_repository: str | None,
    now: datetime,
) -> tuple[Mapping[str, Any], bytes]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise VerificationError(f"governance JSON is unreadable: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError("governance JSON must be a regular non-link file")
    if getattr(info, "st_nlink", 1) != 1:
        raise VerificationError("hardlinked governance JSON is not accepted")
    if info.st_size > MAX_JSON_BYTES:
        raise VerificationError("governance JSON exceeds the evidence size limit")
    data = path.read_bytes()
    document = _read_json_bytes(data, label="governance JSON")
    if document.get("schema_version") != 1:
        raise VerificationError("governance JSON schema_version must equal 1")
    if document.get("source") != "github-rest-via-gh":
        raise VerificationError(
            "governance JSON source must equal github-rest-via-gh"
        )
    repository = document.get("repository")
    if (
        not isinstance(repository, str)
        or not GITHUB_REPOSITORY_RE.fullmatch(repository)
    ):
        raise VerificationError("governance JSON repository identity is invalid")
    if expected_repository and repository.casefold() != expected_repository.casefold():
        raise VerificationError(
            "governance JSON belongs to a different repository: "
            f"expected={expected_repository} observed={repository}"
        )
    generated = _parse_time(document.get("generated_at"), label="governance generated_at")
    age_days = (now - generated).total_seconds() / 86400
    if age_days < 0 or age_days > 7:
        raise VerificationError(
            f"governance JSON is stale or future-dated: {age_days:.2f} days"
        )
    findings = document.get("findings")
    if not isinstance(findings, list):
        raise VerificationError("governance JSON findings must be an array")
    return document, data


@dataclasses.dataclass(frozen=True, slots=True)
class VerificationOutcome:
    mode: str
    manifest_path: Path
    manifest_sha256: str
    manifest_canonical_sha256: str
    proof_path: Path
    proof_sha256: str
    proof_canonical_sha256: str
    audit_path: Path
    audit_sha256: str
    command_counts: Mapping[str, int]
    gate_counts: Mapping[str, int]
    plan_succeeded: bool
    launch_ready: bool


class LocalVerificationRunner:
    """Execute the immutable local verification plan and write bound evidence."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        output_root: Path | str = SAFE_OUTPUT_PREFIX.as_posix(),
        mode: str = "quick",
        governance_json: Path | str | None = None,
        resume: bool = False,
        executor: CommandExecutor | None = None,
        now: Callable[[], datetime] = _utc_now,
        python_executable: str = sys.executable,
        tool_versions: Mapping[str, Any] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.output_root = _validate_output_root(
            self.repo_root, Path(output_root)
        )
        self.output_relative = _safe_relative(
            self.output_root, self.repo_root, label="verification output"
        )
        self.mode = mode
        self.plan = build_verification_plan(mode)
        self.plan_document = plan_document(mode)
        self.resume = resume
        self.executor = executor or BoundedSubprocessExecutor(now=now)
        self.now = now
        self.python_executable = python_executable
        self.source_environment = dict(os.environ)
        self.tool_versions_override = (
            dict(tool_versions) if tool_versions is not None else None
        )
        self.governance_path = (
            Path(governance_json).resolve() if governance_json is not None else None
        )

    def _environment(self, spec: CommandSpec | None = None) -> dict[str, str]:
        environment, _ = _environment_bundle(
            self.source_environment,
            spec=spec,
        )
        return environment

    def _environment_policy(
        self, spec: CommandSpec | None = None
    ) -> dict[str, Any]:
        _, policy = _environment_bundle(
            self.source_environment,
            spec=spec,
        )
        return policy

    def _tool_versions(self) -> dict[str, Any]:
        if self.tool_versions_override is not None:
            if self.tool_versions_override.get("schema") == TOOLCHAIN_SCHEMA:
                _validate_toolchain_record(
                    self.tool_versions_override,
                    repo_root=self.repo_root,
                )
                return dict(self.tool_versions_override)
            environment_policy = self._environment_policy()
            tools: dict[str, Any] = {}
            for name in ("python", "pytest", "uv", "git", "docker"):
                supplied = self.tool_versions_override.get(name)
                if not isinstance(supplied, Mapping):
                    raise VerificationError(
                        f"tool version override is missing {name}"
                    )
                row = dict(supplied)
                output_digest = row.get("output_sha256")
                if not isinstance(output_digest, str) or not SHA256_RE.fullmatch(
                    output_digest
                ):
                    output_digest = sha256_bytes(
                        str(row.get("version") or "").encode(
                            "utf-8", errors="surrogatepass"
                        )
                    )
                row.setdefault("stdout_sha256", output_digest)
                row.setdefault("stderr_sha256", sha256_bytes(b""))
                row.setdefault("timed_out", False)
                row.setdefault("environment_digest", environment_policy["digest"]["value"])
                row.setdefault("executable", {"present": None, "override": True})
                tools[name] = row
            return _toolchain_record(repo_root=self.repo_root, tools=tools)
        probes = {
            "python": (self.python_executable, "--version"),
            "pytest": (self.python_executable, "-m", "pytest", "--version"),
            "uv": ("uv", "--version"),
            "git": ("git", "--version"),
            "docker": ("docker", "version", "--format", "{{json .}}"),
        }
        rows: dict[str, Any] = {}
        environment = self._environment()
        environment_policy = self._environment_policy()
        for name, argv in probes.items():
            result = self.executor.execute(
                argv,
                cwd=self.repo_root,
                timeout_seconds=30,
                max_output_bytes=64 * 1024,
                env=environment,
            )
            result = _sanitize_raw_execution(
                result,
                repo_root=self.repo_root,
                output_root=self.output_root,
            )
            output = (result.stdout or result.stderr).decode(
                "utf-8", errors="replace"
            ).strip()
            stdout_sha256 = _full_stream_sha256(result, stream="stdout")
            stderr_sha256 = _full_stream_sha256(result, stream="stderr")
            rows[name] = {
                "argv": _normalize_argv(
                    argv,
                    repo_root=self.repo_root,
                    output_root=self.output_root,
                    python_executable=self.python_executable,
                ),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "version": output.splitlines()[0][:512] if output else None,
                "output_sha256": sha256_bytes(result.stdout + b"\0" + result.stderr),
                "stdout_sha256": stdout_sha256,
                "stderr_sha256": stderr_sha256,
                "stdout_total_bytes": result.stdout_total_bytes,
                "stderr_total_bytes": result.stderr_total_bytes,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "environment_digest": environment_policy["digest"]["value"],
                "executable": _tool_executable_fingerprint(
                    argv[0],
                    environment=environment,
                ),
            }
        return _toolchain_record(repo_root=self.repo_root, tools=rows)

    def _cached_record(
        self,
        *,
        spec: CommandSpec,
        repository: Mapping[str, Any],
        execution_context_digest: str,
        run_environment_policy: Mapping[str, Any],
        index: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        if (
            not self.resume
            or spec.docker
            or spec.id in _EPHEMERAL_COMMAND_IDS
        ):
            return None
        key = _command_cache_key(
            spec,
            repository,
            self.plan_document["digest"],
            execution_context_digest,
        )
        entries = index.get("entries")
        reference = entries.get(key) if isinstance(entries, Mapping) else None
        if not isinstance(reference, Mapping):
            return None
        try:
            data = _read_artifact(
                self.repo_root,
                reference,
                label=f"resume command {spec.id}",
            )
            record = _read_json_bytes(data, label=f"resume command {spec.id}")
            validated = _validate_command_record(
                self.repo_root,
                record,
                spec=spec,
                repository=repository,
                plan_digest=self.plan_document["digest"],
                execution_context_digest=execution_context_digest,
                run_environment_policy=run_environment_policy,
                output_root=self.output_root,
                python_executable=self.python_executable,
                now=now,
            )
        except VerificationError:
            return None
        junit = validated.get("junit")
        if (
            validated.get("status") != "passed"
            or validated.get("exit_code") != 0
            or (
                isinstance(junit, Mapping)
                and int(junit.get("skipped", 0)) != 0
            )
        ):
            return None
        validated["_artifact"] = reference["artifact"]
        validated["_sha256"] = reference["sha256"]
        validated["_repo_root"] = os.fspath(self.repo_root)
        validated["_resumed"] = True
        return validated

    def _blocked_record(
        self,
        spec: CommandSpec,
        *,
        repository: Mapping[str, Any],
        execution_context_digest: str,
        reason: str,
    ) -> dict[str, Any]:
        instant = _iso(self.now())
        reason, stderr_redactions = _sanitize_public_text(
            reason,
            repo_root=self.repo_root,
            output_root=self.output_root,
        )
        stdout_path, stderr_path, _ = _command_artifact_paths(
            self.output_root, repository, spec
        )
        stdout = b""
        stderr = reason.encode("utf-8", errors="replace")
        _atomic_write(stdout_path, stdout)
        _atomic_write(stderr_path, stderr)
        try:
            resolved = _resolve_tokens(
                spec,
                repo_root=self.repo_root,
                output_root=self.output_root,
                repository=repository,
                python_executable=self.python_executable,
            )
        except VerificationError:
            normalized_argv = list(spec.argv)
        else:
            normalized_argv = _normalize_argv(
                resolved,
                repo_root=self.repo_root,
                output_root=self.output_root,
                python_executable=self.python_executable,
            )
        command_cwd = _resolve_cwd(
            spec,
            repo_root=self.repo_root,
            output_root=self.output_root,
            repository=repository,
        )
        normalized_cwd = _normalize_argv(
            (os.fspath(command_cwd),),
            repo_root=self.repo_root,
            output_root=self.output_root,
            python_executable=self.python_executable,
        )[0]
        environment_policy = self._environment_policy(spec)
        raw = RawExecution(
            argv=tuple(normalized_argv),
            started_at=instant,
            finished_at=instant,
            duration_ms=0,
            exit_code=None,
            timed_out=False,
            stdout=stdout,
            stderr=stderr,
            stdout_total_bytes=0,
            stderr_total_bytes=len(stderr),
            stdout_truncated=False,
            stderr_truncated=False,
            error=reason,
            stdout_sha256=sha256_bytes(stdout),
            stderr_sha256=sha256_bytes(stderr),
            stderr_redactions=stderr_redactions,
        )
        record = {
            "schema": COMMAND_SCHEMA,
            "command_id": spec.id,
            "category": spec.category,
            "description": spec.description,
            "command_spec_digest": spec.digest,
            "plan_digest": self.plan_document["digest"],
            "execution_context_digest": execution_context_digest,
            "repository": dict(repository),
            "argv": normalized_argv,
            "cwd": normalized_cwd,
            "environment": environment_policy,
            "invocation": _invocation_record(
                argv=normalized_argv,
                cwd=normalized_cwd,
                environment_digest=environment_policy["digest"]["value"],
                timeout_seconds=spec.timeout_seconds,
                max_output_bytes=MAX_CAPTURE_BYTES,
            ),
            "started_at": instant,
            "finished_at": instant,
            "duration_ms": 0,
            "exit_code": None,
            "timed_out": False,
            "error": reason,
            "source_changed": False,
            "status": "blocked",
            "termination": _termination_record(
                raw,
                status="blocked",
                blocked=True,
            ),
            "stdout": _stream_record(
                self.repo_root,
                stdout_path,
                label="stdout",
                raw=raw,
            ),
            "stderr": _stream_record(
                self.repo_root,
                stderr_path,
                label="stderr",
                raw=raw,
            ),
            "junit": None,
            "artifacts": [],
            "test_sources": _source_artifacts(self.repo_root, spec),
            "docker": None,
            "platform": _platform_record(),
            "diagnostic": _diagnostic(
                problem=f"Verification command {spec.id} was not executed.",
                cause=reason,
                fix="Resolve the prerequisite and rerun the fixed plan.",
                evidence=[f"command_id={spec.id}"],
            ),
        }
        return self._store_command_record(record, spec, repository)

    def _store_command_record(
        self,
        record: Mapping[str, Any],
        spec: CommandSpec,
        repository: Mapping[str, Any],
    ) -> dict[str, Any]:
        stored = dict(record)
        stored["canonical_digest"] = _digest_descriptor(
            _command_canonical_payload(stored)
        )
        data = canonical_json_bytes(stored)
        digest = sha256_bytes(data)
        path = self.output_root / "objects" / f"{digest}.json"
        _atomic_write(path, data)
        result = dict(stored)
        result["_artifact"] = _safe_relative(
            path, self.repo_root, label=f"{spec.id} command object"
        )
        result["_sha256"] = digest
        result["_repo_root"] = os.fspath(self.repo_root)
        result["_resumed"] = False
        return result

    def _execute_one(
        self,
        spec: CommandSpec,
        *,
        repository: Mapping[str, Any],
        execution_context_digest: str,
        completed: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        failed_dependencies = [
            dependency
            for dependency in spec.dependencies
            if completed.get(dependency, {}).get("exit_code") != 0
            or completed.get(dependency, {}).get("status") != "passed"
        ]
        if failed_dependencies:
            return self._blocked_record(
                spec,
                repository=repository,
                execution_context_digest=execution_context_digest,
                reason=(
                    "required command dependencies did not pass: "
                    + ", ".join(failed_dependencies)
                ),
            )
        _prepare_command_paths(
            spec,
            output_root=self.output_root,
            repository=repository,
            resume=self.resume,
        )
        try:
            resolved = _resolve_tokens(
                spec,
                repo_root=self.repo_root,
                output_root=self.output_root,
                repository=repository,
                python_executable=self.python_executable,
            )
        except VerificationError as exc:
            return self._blocked_record(
                spec,
                repository=repository,
                execution_context_digest=execution_context_digest,
                reason=str(exc),
            )
        command_cwd = _resolve_cwd(
            spec,
            repo_root=self.repo_root,
            output_root=self.output_root,
            repository=repository,
        )
        stdout_path, stderr_path, junit_path = _command_artifact_paths(
            self.output_root, repository, spec
        )
        if spec.junit and junit_path.exists():
            junit_path.unlink()
        test_sources = _source_artifacts(self.repo_root, spec)
        raw = self.executor.execute(
            resolved,
            cwd=command_cwd,
            timeout_seconds=spec.timeout_seconds,
            max_output_bytes=MAX_CAPTURE_BYTES,
            env=self._environment(spec),
        )
        if spec.junit:
            try:
                _safe_remove_verification_temp(
                    _pytest_basetemp(self.output_root, repository, spec)
                )
            except VerificationError as exc:
                raw = dataclasses.replace(
                    raw,
                    error=f"pytest temporary-directory cleanup failed: {exc}",
                )
        raw = _sanitize_raw_execution(
            raw,
            repo_root=self.repo_root,
            output_root=self.output_root,
        )
        _atomic_write(stdout_path, raw.stdout)
        _atomic_write(stderr_path, raw.stderr)
        after = repository_snapshot(
            self.repo_root,
            excluded_paths=_repository_exclusions(self.output_relative),
        )
        source_changed = any(
            after.get(field) != repository.get(field)
            for field in ("origin", "head", "tree_digest", "repository_id")
        )
        junit: dict[str, Any] | None = None
        if spec.junit and junit_path.is_file():
            try:
                sanitized_fields = _sanitize_junit_metadata(
                    junit_path,
                    repo_root=self.repo_root,
                    output_root=self.output_root,
                )
                parsed = parse_junit(
                    junit_path,
                    command_started_at=raw.started_at,
                    command_finished_at=raw.finished_at,
                    allowed_targets=spec.pytest_targets,
                )
            except VerificationError as exc:
                raw = dataclasses.replace(raw, error=str(exc))
            else:
                junit = {
                    **{
                        key: value
                        for key, value in parsed.items()
                        if key != "artifact"
                    },
                    "label": "junit",
                    "sanitized_fields": list(sanitized_fields),
                    "artifact": _safe_relative(
                        junit_path, self.repo_root, label=f"{spec.id} JUnit"
                    ),
                }
        artifacts: list[dict[str, Any]] = []
        docker_case_error = _docker_case_error(spec.id, junit)
        if docker_case_error:
            raw = dataclasses.replace(raw, error=docker_case_error)
        if raw.exit_code == 0 and not raw.timed_out:
            try:
                artifacts = _collect_package_artifacts(
                    spec,
                    repo_root=self.repo_root,
                    output_root=self.output_root,
                    repository=repository,
                )
            except VerificationError as exc:
                raw = dataclasses.replace(raw, error=str(exc))
        raw = _sanitize_raw_execution(
            raw,
            repo_root=self.repo_root,
            output_root=self.output_root,
        )
        status = _record_status(raw, junit, source_changed)
        normalized_argv = _normalize_argv(
            resolved,
            repo_root=self.repo_root,
            output_root=self.output_root,
            python_executable=self.python_executable,
        )
        normalized_cwd = _normalize_argv(
            (os.fspath(command_cwd),),
            repo_root=self.repo_root,
            output_root=self.output_root,
            python_executable=self.python_executable,
        )[0]
        environment_policy = self._environment_policy(spec)
        record = {
            "schema": COMMAND_SCHEMA,
            "command_id": spec.id,
            "category": spec.category,
            "description": spec.description,
            "command_spec_digest": spec.digest,
            "plan_digest": self.plan_document["digest"],
            "execution_context_digest": execution_context_digest,
            "repository": dict(repository),
            "argv": normalized_argv,
            "cwd": normalized_cwd,
            "environment": environment_policy,
            "invocation": _invocation_record(
                argv=normalized_argv,
                cwd=normalized_cwd,
                environment_digest=environment_policy["digest"]["value"],
                timeout_seconds=spec.timeout_seconds,
                max_output_bytes=MAX_CAPTURE_BYTES,
            ),
            "started_at": raw.started_at,
            "finished_at": raw.finished_at,
            "duration_ms": raw.duration_ms,
            "exit_code": raw.exit_code,
            "timed_out": raw.timed_out,
            "error": raw.error,
            "source_changed": source_changed,
            "status": status,
            "termination": _termination_record(raw, status=status),
            "stdout": _stream_record(
                self.repo_root,
                stdout_path,
                label="stdout",
                raw=raw,
            ),
            "stderr": _stream_record(
                self.repo_root,
                stderr_path,
                label="stderr",
                raw=raw,
            ),
            "junit": junit,
            "artifacts": artifacts,
            "test_sources": test_sources,
            "docker": _docker_evidence(spec, raw, junit),
            "platform": _platform_record(),
            "diagnostic": _command_diagnostic(
                spec,
                status=status,
                raw=raw,
                junit=junit,
                source_changed=source_changed,
            ),
        }
        return self._store_command_record(record, spec, repository)

    @_locked_verification_run
    def run(self) -> VerificationOutcome:
        run_started = self.now()
        repository = repository_snapshot(
            self.repo_root,
            excluded_paths=_repository_exclusions(self.output_relative),
        )
        platform_record = _platform_record()
        tool_versions = self._tool_versions()
        run_environment_policy = self._environment_policy()
        execution_context = _execution_context(
            platform_record,
            tool_versions,
            run_environment_policy,
        )
        verification_temp_root = _verification_temp_root(
            self.output_root,
            repository,
        )
        if not self.resume:
            _safe_remove_verification_temp(verification_temp_root)
        governance_document: Mapping[str, Any] | None = None
        governance_reference: dict[str, Any] | None = None
        if self.governance_path is not None:
            governance_document, governance_bytes = _validate_governance_input(
                self.governance_path,
                expected_repository=_github_slug(str(repository["origin"])),
                now=run_started,
            )
            governance_digest = sha256_bytes(governance_bytes)
            governance_copy = (
                self.output_root / "governance" / f"{governance_digest}.json"
            )
            _atomic_write(governance_copy, governance_bytes)
            governance_reference = _artifact_reference(
                self.repo_root,
                governance_copy,
                label="live-governance-json",
                max_bytes=MAX_JSON_BYTES,
            )

        index = _load_resume_index(self.output_root)
        command_records: dict[str, dict[str, Any]] = {}
        for spec in self.plan:
            cached = self._cached_record(
                spec=spec,
                repository=repository,
                execution_context_digest=execution_context["digest"],
                run_environment_policy=run_environment_policy,
                index=index,
                now=run_started,
            )
            record = cached or self._execute_one(
                spec,
                repository=repository,
                execution_context_digest=execution_context["digest"],
                completed=command_records,
            )
            command_records[spec.id] = record
            key = _command_cache_key(
                spec,
                repository,
                self.plan_document["digest"],
                execution_context["digest"],
            )
            index.setdefault("entries", {})[key] = {
                "artifact": record["_artifact"],
                "sha256": record["_sha256"],
            }
            _write_resume_index(self.output_root, index)

        _safe_remove_verification_temp(verification_temp_root)
        generated = self.now()
        assessments: dict[str, dict[str, Any]] = {}
        for rule in _GATE_RULES:
            if self.mode in rule.modes:
                assessments[rule.gate_id] = _assessment(
                    rule, command_records, mode=self.mode
                )
        proof_document = {
            "schema": PROOF_SCHEMA,
            "generated_at": _iso(generated),
            "mode": self.mode,
            "repository": dict(repository),
            "plan_digest": self.plan_document["digest"],
            "gates": assessments,
            "claims_not_made": dict(CLAIMS_NOT_MADE),
        }
        proof_document["canonical_digest"] = _digest_descriptor(
            _proof_canonical_payload(proof_document)
        )
        proof_canonical_digest = proof_document["canonical_digest"]["value"]
        proof_bytes = canonical_json_bytes(proof_document)
        proof_digest = sha256_bytes(proof_bytes)
        proof_path = self.output_root / "proofs" / f"{proof_digest}.json"
        _atomic_write(proof_path, proof_bytes)
        proof_relative = _safe_relative(
            proof_path, self.repo_root, label="launch proof"
        )

        proof_records: dict[str, Any] = {}
        for gate_id, assessment in assessments.items():
            rule = _GATE_RULE_BY_ID[gate_id]
            exit_codes = [
                command_records[requirement.command_id].get("exit_code")
                for requirement in rule.requirements
                if requirement.command_id in command_records
            ]
            failed_codes = [
                code
                for code in exit_codes
                if isinstance(code, int) and not isinstance(code, bool) and code != 0
            ]
            proof_records[gate_id] = {
                "artifact": proof_relative,
                "sha256": proof_digest,
                "canonical_sha256": proof_canonical_digest,
                "command": _proof_command(rule, command_records),
                "exit_code": (
                    0
                    if assessment["result"] in {"passed", "blocked"}
                    else (failed_codes[0] if failed_codes else 1)
                ),
            }

        command_references = {
            command_id: {
                "artifact": record["_artifact"],
                "sha256": record["_sha256"],
                "canonical_sha256": record["canonical_digest"]["value"],
                "status": record["status"],
                "exit_code": record["exit_code"],
                "resumed": bool(record.get("_resumed")),
            }
            for command_id, record in sorted(command_records.items())
        }
        command_counts = {
            status: sum(
                record["status"] == status for record in command_records.values()
            )
            for status in (
                "passed",
                "failed",
                "skipped",
                "timed_out",
                "error",
                "blocked",
            )
        }
        gate_counts = {
            status: sum(
                assessment["result"] == status
                for assessment in assessments.values()
            )
            for status in ("passed", "failed", "blocked")
        }
        manifest_document = {
            "schema": MANIFEST_SCHEMA,
            "generated_at": _iso(generated),
            "mode": self.mode,
            "repository": dict(repository),
            "output_root": self.output_relative,
            "plan": self.plan_document,
            "platform": platform_record,
            "tool_versions": tool_versions,
            "environment_policy": run_environment_policy,
            "execution_context_digest": execution_context["digest"],
            "commands": command_references,
            "proof": {
                "artifact": proof_relative,
                "sha256": proof_digest,
                "canonical_sha256": proof_canonical_digest,
            },
            "proofs": proof_records,
            "governance_evidence": governance_reference,
            "claims_not_made": dict(CLAIMS_NOT_MADE),
            "summary": {
                "commands": command_counts,
                "gates": gate_counts,
                "local_verification_only": True,
                "official_run_claimed": False,
            },
        }
        manifest_document["canonical_digest"] = _digest_descriptor(
            _manifest_canonical_payload(manifest_document)
        )
        manifest_canonical_digest = manifest_document["canonical_digest"]["value"]
        manifest_bytes = canonical_json_bytes(manifest_document)
        manifest_digest = sha256_bytes(manifest_bytes)
        manifest_path = (
            self.output_root / "manifests" / f"{manifest_digest}.json"
        )
        _atomic_write(manifest_path, manifest_bytes)
        _atomic_write(self.output_root / "evidence-manifest.json", manifest_bytes)

        validated = validate_evidence_manifest(
            self.repo_root,
            manifest_path,
            now=generated,
        )
        if validated.get("schema") != MANIFEST_SCHEMA:  # pragma: no cover
            raise VerificationError("newly generated evidence failed validation")

        from atv_bench.launch_audit import audit_launch, render_json

        report = audit_launch(
            self.repo_root,
            audit_date=generated.date().isoformat(),
            governance=governance_document,
            evidence_manifest=manifest_document,
        )
        audit_bytes = render_json(report).encode("utf-8")
        audit_digest = sha256_bytes(audit_bytes)
        audit_path = self.output_root / "audits" / f"{audit_digest}.json"
        _atomic_write(audit_path, audit_bytes)
        _atomic_write(self.output_root / "launch-audit.json", audit_bytes)

        plan_succeeded = all(
            record.get("status") == "passed"
            and record.get("exit_code") == 0
            and not (
                _FIXED_BY_ID[command_id].docker
                and isinstance(record.get("junit"), Mapping)
                and record["junit"].get("skipped") != 0
            )
            for command_id, record in command_records.items()
        )
        return VerificationOutcome(
            mode=self.mode,
            manifest_path=manifest_path,
            manifest_sha256=manifest_digest,
            manifest_canonical_sha256=manifest_canonical_digest,
            proof_path=proof_path,
            proof_sha256=proof_digest,
            proof_canonical_sha256=proof_canonical_digest,
            audit_path=audit_path,
            audit_sha256=audit_digest,
            command_counts=command_counts,
            gate_counts=gate_counts,
            plan_succeeded=plan_succeeded,
            launch_ready=report.launch_ready,
        )


def _load_manifest(
    repo_root: Path, manifest: Path | str | Mapping[str, Any]
) -> tuple[Mapping[str, Any], Path | None]:
    if isinstance(manifest, Mapping):
        return manifest, None
    path = Path(manifest)
    if not path.is_absolute():
        path = repo_root / path
    digest, _ = _sha256_file(path, max_bytes=MAX_JSON_BYTES)
    if (
        path.parent.name == "manifests"
        and SHA256_RE.fullmatch(path.stem)
        and path.stem != digest
    ):
        raise VerificationError(
            f"content-addressed manifest filename does not match bytes: {path}"
        )
    return _read_json_bytes(path.read_bytes(), label="evidence manifest"), path


def validate_evidence_manifest(
    repo_root: Path | str,
    manifest: Path | str | Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    """Validate repository binding, all artifact digests, and gate derivations."""

    root = Path(repo_root).resolve()
    document, _ = _load_manifest(root, manifest)
    if document.get("schema") != MANIFEST_SCHEMA:
        raise VerificationError(
            f"manifest schema must equal {MANIFEST_SCHEMA}; booleans alone are not evidence"
        )
    required = {
        "generated_at",
        "mode",
        "repository",
        "output_root",
        "plan",
        "platform",
        "tool_versions",
        "environment_policy",
        "execution_context_digest",
        "commands",
        "proof",
        "proofs",
        "claims_not_made",
        "summary",
        "canonical_digest",
    }
    missing = sorted(required - document.keys())
    if missing:
        raise VerificationError(
            "manifest is missing artifact-bearing fields: " + ", ".join(missing)
        )
    current_time = (now or _utc_now()).astimezone(timezone.utc)
    generated = _parse_time(document.get("generated_at"), label="manifest generated_at")
    age_days = (current_time - generated).total_seconds() / 86400
    if age_days < 0 or age_days > MANIFEST_FRESHNESS_DAYS:
        raise VerificationError(
            f"manifest is stale or future-dated: {age_days:.2f} days"
        )
    mode = document.get("mode")
    if mode not in {"quick", "full"}:
        raise VerificationError("manifest mode is invalid")
    output_root = document.get("output_root")
    if not isinstance(output_root, str):
        raise VerificationError("manifest output_root is absent")
    validated_output = _validate_output_root(root, root / output_root)
    expected_plan = plan_document(str(mode))
    if document.get("plan") != expected_plan:
        raise VerificationError(
            "manifest plan is not the fixed allowlisted argv plan"
        )
    platform_record = document.get("platform")
    tool_versions = document.get("tool_versions")
    environment_policy = document.get("environment_policy")
    if not isinstance(platform_record, Mapping) or not isinstance(
        tool_versions, Mapping
    ):
        raise VerificationError("manifest execution context is incomplete")
    _validate_toolchain_record(tool_versions, repo_root=root)
    _validate_environment_policy(environment_policy, spec=None)
    execution_context = _execution_context(
        platform_record,
        tool_versions,
        environment_policy,
    )
    if document.get("execution_context_digest") != execution_context["digest"]:
        raise VerificationError("manifest execution context digest is invalid")
    bound_repository = document.get("repository")
    if not isinstance(bound_repository, Mapping):
        raise VerificationError("manifest repository binding is absent")
    current_repository = repository_snapshot(
        root,
        excluded_paths=_repository_exclusions(output_root),
    )
    for field in current_repository:
        if bound_repository.get(field) != current_repository.get(field):
            raise VerificationError(
                f"manifest is stale or belongs to another repository: {field}"
            )

    commands = document.get("commands")
    if not isinstance(commands, Mapping):
        raise VerificationError("manifest commands must be an object")
    allowed = {spec.id: spec for spec in build_verification_plan(str(mode))}
    if set(commands) != set(allowed):
        raise VerificationError(
            "manifest command set differs from the fixed allowlisted plan"
        )
    command_records: dict[str, dict[str, Any]] = {}
    for command_id, reference in commands.items():
        if not isinstance(reference, Mapping):
            raise VerificationError(f"command reference is not an object: {command_id}")
        data = _read_artifact(
            root, reference, label=f"command object {command_id}"
        )
        record = _read_json_bytes(data, label=f"command object {command_id}")
        validated = _validate_command_record(
            root,
            record,
            spec=allowed[command_id],
            repository=current_repository,
            plan_digest=expected_plan["digest"],
            execution_context_digest=execution_context["digest"],
            run_environment_policy=environment_policy,
            output_root=validated_output,
            python_executable=sys.executable,
            now=current_time,
        )
        if reference.get("status") != validated.get("status"):
            raise VerificationError(f"command status mismatch: {command_id}")
        if reference.get("exit_code") != validated.get("exit_code"):
            raise VerificationError(f"command exit-code mismatch: {command_id}")
        canonical = validated.get("canonical_digest")
        if (
            not isinstance(canonical, Mapping)
            or reference.get("canonical_sha256") != canonical.get("value")
        ):
            raise VerificationError(f"command canonical digest mismatch: {command_id}")
        validated["_artifact"] = reference["artifact"]
        validated["_sha256"] = reference["sha256"]
        validated["_repo_root"] = os.fspath(root)
        command_records[command_id] = validated

    proof_reference = document.get("proof")
    if not isinstance(proof_reference, Mapping):
        raise VerificationError("manifest proof reference is absent")
    proof_data = _read_artifact(root, proof_reference, label="launch proof")
    proof = _read_json_bytes(proof_data, label="launch proof")
    if proof.get("schema") != PROOF_SCHEMA:
        raise VerificationError("launch proof schema is invalid")
    proof_canonical_digest = _validate_digest_descriptor(
        proof.get("canonical_digest"),
        _proof_canonical_payload(proof),
        label="launch proof",
    )
    if proof_reference.get("canonical_sha256") != proof_canonical_digest:
        raise VerificationError("launch proof canonical reference is invalid")
    if proof.get("generated_at") != document.get("generated_at"):
        raise VerificationError("launch proof and manifest timestamps differ")
    if proof.get("mode") != mode or proof.get("plan_digest") != expected_plan["digest"]:
        raise VerificationError("launch proof plan binding is invalid")
    proof_repository = proof.get("repository")
    if not isinstance(proof_repository, Mapping):
        raise VerificationError("launch proof repository binding is absent")
    for field in ("repository_id", "workspace_id", "head", "tree_digest", "origin"):
        if proof_repository.get(field) != current_repository.get(field):
            raise VerificationError(f"launch proof cross-repository mismatch: {field}")
    expected_assessments = {
        rule.gate_id: _assessment(rule, command_records, mode=str(mode))
        for rule in _GATE_RULES
        if mode in rule.modes
    }
    if proof.get("gates") != expected_assessments:
        raise VerificationError(
            "launch proof gate results were forged or are not derivable from exact evidence"
        )
    if proof.get("claims_not_made") != dict(CLAIMS_NOT_MADE):
        raise VerificationError("launch proof removed or changed required claim disclaimers")

    proof_records = document.get("proofs")
    if not isinstance(proof_records, Mapping):
        raise VerificationError(
            "manifest proofs must be artifact records; pass booleans are rejected"
        )
    if set(proof_records) != set(expected_assessments):
        raise VerificationError("manifest proof gate set is incomplete or forged")
    for gate_id, reference in proof_records.items():
        if not isinstance(reference, Mapping):
            raise VerificationError(
                f"gate {gate_id} uses a boolean/non-object instead of an artifact"
            )
        if reference.get("artifact") != proof_reference.get("artifact"):
            raise VerificationError(f"gate {gate_id} points to a different proof artifact")
        if reference.get("sha256") != proof_reference.get("sha256"):
            raise VerificationError(f"gate {gate_id} proof digest mismatch")
        if reference.get("canonical_sha256") != proof_canonical_digest:
            raise VerificationError(f"gate {gate_id} proof canonical digest mismatch")
        command = reference.get("command")
        exit_code = reference.get("exit_code")
        if not isinstance(command, str) or not command:
            raise VerificationError(f"gate {gate_id} lacks exact command mapping")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise VerificationError(f"gate {gate_id} has an invalid proof exit code")
        if expected_assessments[gate_id]["result"] == "passed" and exit_code != 0:
            raise VerificationError(
                f"gate {gate_id} claims pass from a non-successful command"
            )

    governance = document.get("governance_evidence")
    if governance is not None:
        if not isinstance(governance, Mapping):
            raise VerificationError("governance evidence is not an artifact reference")
        governance_data = _read_artifact(
            root, governance, label="governance evidence"
        )
        temporary = validated_output / ".governance-validation.json"
        _atomic_write(temporary, governance_data)
        try:
            _validate_governance_input(
                temporary,
                expected_repository=_github_slug(str(current_repository["origin"])),
                now=current_time,
            )
        finally:
            temporary.unlink(missing_ok=True)
    summary = document.get("summary")
    if not isinstance(summary, Mapping):
        raise VerificationError("manifest summary is not an object")
    if (
        summary.get("local_verification_only") is not True
        or summary.get("official_run_claimed") is not False
    ):
        raise VerificationError("manifest attempts to promote local evidence to an official run")
    if document.get("claims_not_made") != dict(CLAIMS_NOT_MADE):
        raise VerificationError("manifest removed or changed required claim disclaimers")
    expected_command_counts = {
        status: sum(record.get("status") == status for record in command_records.values())
        for status in (
            "passed",
            "failed",
            "skipped",
            "timed_out",
            "error",
            "blocked",
        )
    }
    expected_gate_counts = {
        status: sum(
            assessment.get("result") == status
            for assessment in expected_assessments.values()
        )
        for status in ("passed", "failed", "blocked")
    }
    if summary.get("commands") != expected_command_counts:
        raise VerificationError("manifest command summary was forged")
    if summary.get("gates") != expected_gate_counts:
        raise VerificationError("manifest gate summary was forged")
    _validate_digest_descriptor(
        document.get("canonical_digest"),
        _manifest_canonical_payload(document),
        label="evidence manifest",
    )
    return document


__all__ = [
    "BoundedSubprocessExecutor",
    "CLAIMS_NOT_MADE",
    "COMMAND_SCHEMA",
    "CommandExecutor",
    "CommandSpec",
    "GateRule",
    "LocalVerificationRunner",
    "MANIFEST_SCHEMA",
    "PLAN_SCHEMA",
    "PROOF_SCHEMA",
    "RawExecution",
    "VerificationError",
    "VerificationOutcome",
    "build_verification_plan",
    "canonical_json_bytes",
    "format_diagnostic",
    "gate_rules",
    "parse_junit",
    "plan_document",
    "repository_snapshot",
    "sha256_bytes",
    "validate_evidence_manifest",
]
