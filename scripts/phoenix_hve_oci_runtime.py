"""Fail-closed OCI execution for Phoenix-versus-hve task trials.

This module is intentionally standalone so the task runner can opt into it without
weakening the existing local evidence format.  It:

* binds clean, pinned Phoenix and hve-core Git commits to two separate images;
* copies and verifies the exact locally installed ``@github/copilot`` package;
* builds Phoenix's Linux ``phoenix-mcp`` with ``Cargo.lock`` and ``--locked``;
* runs a named, resource-bounded, non-root container with one workspace mount;
* injects OAuth once over ``docker exec`` stdin without persisting it in Docker;
* forces all harness egress through a content-bound CONNECT allowlist proxy;
* preserves Docker inspect, network, proxy-log, and cleanup evidence; and
* returns the same six fields as ``compare_phoenix_hve.HarnessExecution``.

Each harness receives only an internal per-run Docker network.  A separate,
content-bound proxy is dual-homed to that network and a per-run bridge network,
and permits CONNECT only to the pinned Copilot model host allowlist.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from atv_bench.adapters.contract import capture_repo_diff, git_base
from scripts.compare_phoenix_hve import HarnessExecution


OCI_RUNTIME_SCHEMA = "atv.phoenix-hve-oci-runtime/v1"
OCI_IMAGE_SCHEMA = "atv.phoenix-hve-oci-image/v1"
OCI_PROXY_IMAGE_SCHEMA = "atv.phoenix-hve-connect-proxy-image/v1"
OCI_RUN_SCHEMA = "atv.phoenix-hve-oci-run/v1"
OCI_TRANSFORM_VERSION = "2026-07-21.9"
HARNESSES = ("phoenix", "hve")
CONTAINER_USER = "10001:10001"
CONTAINER_WORKSPACE = "/workspace"
COPILOT_HOME = "/run/copilot"
AUTH_DIRECTORY = "/home/runner/.atv-auth"
AUTH_TOKEN_FIFO = f"{AUTH_DIRECTORY}/token.fifo"
AUTH_START_FIFO = f"{AUTH_DIRECTORY}/start.fifo"
AUTH_ASKPASS = f"{AUTH_DIRECTORY}/github-askpass"
AUTH_READY = f"{AUTH_DIRECTORY}/ready"
PROXY_ALIAS = "atv-connect-proxy"
PROXY_PORT = 18080
NETWORK_POLICY = "internal-connect-proxy"
COPILOT_MODEL_HOSTS = (
    "api.githubcopilot.com",
    "api.business.githubcopilot.com",
    "api.enterprise.githubcopilot.com",
    "api.individual.githubcopilot.com",
)
EXPLICIT_PROXY_DENY_HOSTS = (
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PINNED_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_IMAGE_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_SAFE_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|credential)",
    re.IGNORECASE,
)


class OciRuntimeError(RuntimeError):
    """The OCI backend cannot continue without weakening isolation or evidence."""


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Docker resource limits, represented without locale-dependent unit strings."""

    cpus: float = 2.0
    memory_bytes: int = 4 * 1024 * 1024 * 1024
    pids: int = 256
    tmpfs_bytes: int = 256 * 1024 * 1024
    home_tmpfs_bytes: int = 512 * 1024 * 1024
    shm_bytes: int = 64 * 1024 * 1024

    def validate(self) -> None:
        if not 0.1 <= self.cpus <= 64:
            raise OciRuntimeError("cpus must be between 0.1 and 64")
        if self.memory_bytes < 128 * 1024 * 1024:
            raise OciRuntimeError("memory_bytes must be at least 128 MiB")
        if not 16 <= self.pids <= 4096:
            raise OciRuntimeError("pids must be between 16 and 4096")
        if self.tmpfs_bytes < 16 * 1024 * 1024:
            raise OciRuntimeError("tmpfs_bytes must be at least 16 MiB")
        if self.home_tmpfs_bytes < 16 * 1024 * 1024:
            raise OciRuntimeError("home_tmpfs_bytes must be at least 16 MiB")
        if self.shm_bytes < 16 * 1024 * 1024:
            raise OciRuntimeError("shm_bytes must be at least 16 MiB")


@dataclass(frozen=True, slots=True)
class OciBuildConfig:
    """Pinned inputs required to build or reuse both harness images."""

    phoenix_repo: Path
    phoenix_commit: str
    hve_repo: Path
    hve_commit: str
    copilot_package: Path
    runtime_base_image: str
    rust_builder_image: str
    evidence_dir: Path
    docker: str = "docker"
    host_node: str | None = None
    platform: str = "linux/amd64"
    image_namespace: str = "atv-bench/phoenix-hve"
    tool_compat_shim: bool = True


@dataclass(frozen=True, slots=True)
class GitSourceIdentity:
    harness: str
    repository: str
    checkout: Path
    commit: str
    git_tree: str
    tracked_listing_sha256: str
    remote: str

    def evidence(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "repository": self.repository,
            "checkout": str(self.checkout),
            "commit": self.commit,
            "git_tree": self.git_tree,
            "tracked_listing_sha256": self.tracked_listing_sha256,
            "remote": self.remote,
            "dirty": False,
        }


@dataclass(frozen=True, slots=True)
class CopilotPackageIdentity:
    root: Path
    version: str
    build_commit: str
    tree_sha256: str
    loader_sha256: str
    host_node_version: str
    host_version_output: str

    def evidence(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "package": "@github/copilot",
            "version": self.version,
            "build_commit": self.build_commit,
            "tree_sha256": self.tree_sha256,
            "loader_sha256": self.loader_sha256,
            "host_node_version": self.host_node_version,
            "host_version_output": self.host_version_output,
        }


@dataclass(frozen=True, slots=True)
class OciProxyImage:
    """A verified CONNECT proxy image built from the harness runtime base."""

    tag: str
    image_id: str
    platform: str
    runtime_base_image: str
    build_spec_sha256: str
    inspect_sha256: str
    script_sha256: str
    labels: Mapping[str, str]
    reused: bool

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": OCI_PROXY_IMAGE_SCHEMA,
            "tag": self.tag,
            "image_id": self.image_id,
            "platform": self.platform,
            "runtime_base_image": self.runtime_base_image,
            "build_spec_sha256": self.build_spec_sha256,
            "inspect_sha256": self.inspect_sha256,
            "script_sha256": self.script_sha256,
            "allowlist": list(COPILOT_MODEL_HOSTS),
            "explicit_denylist": list(EXPLICIT_PROXY_DENY_HOSTS),
            "labels": dict(sorted(self.labels.items())),
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class OciImage:
    """A verified, content-bound harness image."""

    harness: str
    tag: str
    image_id: str
    platform: str
    build_spec_sha256: str
    inspect_sha256: str
    source: GitSourceIdentity
    copilot: CopilotPackageIdentity
    labels: Mapping[str, str]
    parity: Mapping[str, Any]
    proxy: OciProxyImage
    reused: bool

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": OCI_IMAGE_SCHEMA,
            "harness": self.harness,
            "tag": self.tag,
            "image_id": self.image_id,
            "platform": self.platform,
            "build_spec_sha256": self.build_spec_sha256,
            "inspect_sha256": self.inspect_sha256,
            "source": self.source.evidence(),
            "copilot": self.copilot.evidence(),
            "labels": dict(sorted(self.labels.items())),
            "parity": dict(self.parity),
            "proxy": self.proxy.evidence(),
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class OciRunConfig:
    """Non-secret inputs for one isolated harness execution."""

    docker: str
    image: OciImage
    harness: str
    workspace: Path
    evidence_dir: Path
    run_id: str
    model: str
    max_ai_credits: int
    timeout_seconds: int
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    network: str = NETWORK_POLICY
    forbidden_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class _GitEntry:
    mode: str
    object_id: str
    path: str


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _write_json_atomic(path: Path, payload: Any) -> None:
    _write_bytes_atomic(path, _canonical_json_bytes(payload) + b"\n")


def _run_bytes(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int | float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.run(
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if check and process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="replace")[:4096]
        raise OciRuntimeError(
            f"command failed ({process.returncode}): {argv[0]} "
            f"{' '.join(argv[1:4])}: {stderr}"
        )
    return process


def _run_text(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int | float | None = None,
) -> str:
    return (
        _run_bytes(argv, cwd=cwd, timeout=timeout)
        .stdout.decode("utf-8", errors="strict")
        .strip()
    )


def _git_bytes(repo: Path, *args: str) -> bytes:
    return _run_bytes(["git", "-C", str(repo), *args], timeout=120).stdout


def _git_text(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", errors="strict").strip()


def _normalize_remote(value: str) -> str:
    remote = value.strip().replace("\\", "/")
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("ssh://git@github.com/"):
        remote = remote.removeprefix("ssh://git@github.com/")
    else:
        remote = re.sub(r"^https?://github\.com/", "", remote, flags=re.IGNORECASE)
    return remote.removesuffix(".git").strip("/").casefold()


def inspect_source(
    repo: Path,
    *,
    harness: str,
    expected_commit: str,
) -> GitSourceIdentity:
    """Validate a clean checkout at the exact expected commit and GitHub origin."""

    if harness not in HARNESSES:
        raise OciRuntimeError(f"unsupported harness: {harness}")
    if not _COMMIT_RE.fullmatch(expected_commit):
        raise OciRuntimeError(f"{harness} commit must be a full lowercase SHA-1")
    checkout = repo.resolve()
    if not checkout.is_dir():
        raise OciRuntimeError(f"{harness} checkout does not exist: {checkout}")
    expected_repository = (
        "all-the-vibes/atv-phoenix" if harness == "phoenix" else "microsoft/hve-core"
    )
    if harness == "phoenix":
        required = (checkout / "Cargo.toml", checkout / "Cargo.lock")
        if not all(path.is_file() for path in required):
            raise OciRuntimeError("Phoenix checkout lacks Cargo.toml or Cargo.lock")
    elif not (checkout / "plugins" / "hve-core").is_dir():
        raise OciRuntimeError("hve checkout lacks plugins/hve-core")

    status = _git_bytes(
        checkout,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status:
        raise OciRuntimeError(f"{harness} checkout is dirty")
    head = _git_text(checkout, "rev-parse", "HEAD").casefold()
    if head != expected_commit:
        raise OciRuntimeError(
            f"{harness} checkout is at {head}, expected {expected_commit}"
        )
    remote = _git_text(checkout, "remote", "get-url", "origin")
    if _normalize_remote(remote) != expected_repository:
        raise OciRuntimeError(
            f"{harness} origin is {remote!r}, expected {expected_repository}"
        )
    tree = _git_text(checkout, "rev-parse", "HEAD^{tree}").casefold()
    listing = _git_bytes(
        checkout,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "HEAD",
    )
    return GitSourceIdentity(
        harness=harness,
        repository=expected_repository,
        checkout=checkout,
        commit=head,
        git_tree=tree,
        tracked_listing_sha256=_sha256_bytes(listing),
        remote=remote,
    )


def _tree_rows(root: Path) -> list[list[Any]]:
    base = root.resolve()
    if not base.is_dir():
        raise OciRuntimeError(f"tree root is not a directory: {base}")
    rows: list[list[Any]] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise OciRuntimeError(f"cannot scan tree {directory}: {exc}") from exc
        for entry in entries:
            child_relative = relative / entry.name
            canonical = child_relative.as_posix()
            path = Path(entry.path)
            if entry.is_symlink():
                target = os.readlink(path)
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(base):
                    raise OciRuntimeError(
                        f"tree symlink escapes package root: {canonical} -> {target}"
                    )
                rows.append(["l", canonical, target])
            elif entry.is_dir(follow_symlinks=False):
                rows.append(["d", canonical])
                visit(path, child_relative)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                rows.append(["f", canonical, size, _sha256_file(path)])
            else:
                raise OciRuntimeError(f"unsupported special file in tree: {path}")

    visit(base, PurePosixPath())
    return rows


def tree_sha256(root: Path) -> str:
    """Return the path/type/content digest also recomputed inside each image."""

    return _sha256_bytes(_canonical_json_bytes(_tree_rows(root)))


def _copy_tree_exact(source: Path, destination: Path) -> None:
    source = source.resolve()
    if destination.exists():
        raise OciRuntimeError(f"copy destination already exists: {destination}")
    destination.mkdir(parents=True)

    def copy_directory(src: Path, dst: Path) -> None:
        for entry in sorted(os.scandir(src), key=lambda item: item.name):
            src_path = Path(entry.path)
            dst_path = dst / entry.name
            if entry.is_symlink():
                target = os.readlink(src_path)
                resolved = (src_path.parent / target).resolve()
                if not resolved.is_relative_to(source):
                    raise OciRuntimeError(
                        f"Copilot symlink escapes package root: {src_path}"
                    )
                try:
                    os.symlink(
                        target,
                        dst_path,
                        target_is_directory=resolved.is_dir(),
                    )
                except OSError as exc:
                    raise OciRuntimeError(
                        "exact Copilot symlink parity cannot be preserved"
                    ) from exc
            elif entry.is_dir(follow_symlinks=False):
                dst_path.mkdir()
                copy_directory(src_path, dst_path)
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(src_path, dst_path)
            else:
                raise OciRuntimeError(
                    f"unsupported special file in Copilot package: {src_path}"
                )

    copy_directory(source, destination)
    if tree_sha256(destination) != tree_sha256(source):
        raise OciRuntimeError(
            "Copilot package copy did not preserve exact tree content"
        )


def inspect_copilot_package(
    package_root: Path,
    *,
    host_node: str | None = None,
) -> CopilotPackageIdentity:
    """Bind the exact local Copilot package and its host-observed version output."""

    root = package_root.resolve()
    package_json = root / "package.json"
    loader = root / "npm-loader.js"
    if not package_json.is_file() or not loader.is_file():
        raise OciRuntimeError(
            "Copilot package must contain package.json and npm-loader.js"
        )
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRuntimeError(f"invalid Copilot package.json: {exc}") from exc
    if package.get("name") != "@github/copilot":
        raise OciRuntimeError("local package is not @github/copilot")
    version = package.get("version")
    metadata = package.get("buildMetadata")
    build_commit = metadata.get("gitCommit") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or not version:
        raise OciRuntimeError("Copilot package has no exact version")
    if not isinstance(build_commit, str) or not build_commit:
        raise OciRuntimeError("Copilot package has no buildMetadata.gitCommit")
    node = host_node or shutil.which("node")
    if not node:
        raise OciRuntimeError("Node.js is required to attest local Copilot")
    node_version = _run_text([node, "--version"], timeout=30)
    match = re.fullmatch(r"v(\d+)(?:\.\d+){2}", node_version)
    if not match or int(match.group(1)) < 22:
        raise OciRuntimeError("Copilot parity requires Node.js 22 or newer")
    version_run = _run_bytes([node, str(loader), "--version"], timeout=60)
    if version_run.stderr.strip():
        raise OciRuntimeError("local Copilot --version emitted stderr")
    version_output = version_run.stdout.decode("utf-8", errors="strict").strip()
    if not version_output or len(version_output) > 4096:
        raise OciRuntimeError("local Copilot --version output is missing or excessive")
    return CopilotPackageIdentity(
        root=root,
        version=version,
        build_commit=build_commit,
        tree_sha256=tree_sha256(root),
        loader_sha256=_sha256_file(loader),
        host_node_version=node_version,
        host_version_output=version_output,
    )


def _git_index(repo: Path) -> dict[str, _GitEntry]:
    payload = _git_bytes(repo, "ls-files", "--stage", "-z")
    entries: dict[str, _GitEntry] = {}
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        try:
            header, encoded_path = raw.split(b"\t", 1)
            mode, object_id, stage = header.decode("ascii").split()
            path = encoded_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise OciRuntimeError("could not parse Git index") from exc
        canonical = PurePosixPath(path)
        if (
            canonical.is_absolute()
            or ".." in canonical.parts
            or canonical.as_posix() != path
        ):
            raise OciRuntimeError(f"non-canonical Git path: {path!r}")
        if stage != "0":
            raise OciRuntimeError(f"unmerged Git index entry: {path}")
        if mode not in {"100644", "100755", "120000"}:
            raise OciRuntimeError(f"unsupported Git mode {mode} at {path}")
        if path in entries:
            raise OciRuntimeError(f"duplicate Git index entry: {path}")
        entries[path] = _GitEntry(mode=mode, object_id=object_id, path=path)
    if not entries:
        raise OciRuntimeError(f"Git checkout has no tracked files: {repo}")
    return entries


def _normalize_git_target(source_path: str, target: str) -> str:
    if not target or "\x00" in target or "\\" in target:
        raise OciRuntimeError(f"invalid Git symlink target at {source_path}")
    pointer = PurePosixPath(target)
    if pointer.is_absolute():
        raise OciRuntimeError(f"absolute Git symlink target at {source_path}")
    parts: list[str] = []
    for part in (PurePosixPath(source_path).parent / pointer).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise OciRuntimeError(f"Git symlink escapes checkout at {source_path}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise OciRuntimeError(f"Git symlink resolves to checkout root at {source_path}")
    return PurePosixPath(*parts).as_posix()


def _write_materialized_file(path: Path, payload: bytes, *, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_dir() or path.read_bytes() != payload:
            raise OciRuntimeError(f"materialized asset collision: {path}")
        return
    path.write_bytes(payload)
    path.chmod(0o755 if executable else 0o644)


def _git_blob(repo: Path, entry: _GitEntry) -> bytes:
    payload = _git_bytes(repo, "cat-file", "blob", entry.object_id)
    header = f"blob {len(payload)}\0".encode("ascii")
    object_id = hashlib.sha1(header + payload).hexdigest()
    if object_id != entry.object_id:
        raise OciRuntimeError(f"Git blob verification failed at {entry.path}")
    return payload


def _materialize_git_node(
    repo: Path,
    index: Mapping[str, _GitEntry],
    source_path: str,
    destination: Path,
    *,
    stack: tuple[str, ...],
) -> None:
    if source_path in stack:
        raise OciRuntimeError(
            f"Git symlink cycle: {' -> '.join((*stack, source_path))}"
        )
    entry = index.get(source_path)
    if entry is not None:
        payload = _git_blob(repo, entry)
        if entry.mode == "120000":
            try:
                pointer = payload.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise OciRuntimeError(
                    f"could not read Git symlink pointer: {source_path}"
                ) from exc
            target = _normalize_git_target(source_path, pointer)
            _materialize_git_node(
                repo,
                index,
                target,
                destination,
                stack=(*stack, source_path),
            )
            return
        _write_materialized_file(
            destination,
            payload,
            executable=entry.mode == "100755",
        )
        return

    prefix = f"{source_path}/" if source_path else ""
    children = [path for path in index if path.startswith(prefix)]
    if not children:
        raise OciRuntimeError(f"Git symlink target is not tracked: {source_path}")
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(children):
        relative = PurePosixPath(child.removeprefix(prefix))
        _materialize_git_node(
            repo,
            index,
            child,
            destination.joinpath(*relative.parts),
            stack=stack,
        )


def _materialize_git_subtree(
    repo: Path,
    destination: Path,
    *,
    source_prefix: str = "",
) -> None:
    if destination.exists():
        raise OciRuntimeError(f"materialization destination exists: {destination}")
    index = _git_index(repo)
    _materialize_git_node(
        repo,
        index,
        source_prefix,
        destination,
        stack=(),
    )


def _shim_agent_tools(path: Path) -> dict[str, str]:
    before = path.read_bytes()
    try:
        lines = before.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OciRuntimeError(f"agent is not UTF-8: {path}") from exc
    if len(lines) < 3 or lines[0].strip() != "---":
        raise OciRuntimeError(f"agent has no YAML frontmatter: {path}")
    try:
        end = next(
            index for index in range(1, len(lines)) if lines[index].strip() == "---"
        )
    except StopIteration as exc:
        raise OciRuntimeError(f"agent frontmatter is unterminated: {path}") from exc
    replacement = "tools: ['*']"
    tool_rows = [index for index in range(1, end) if lines[index].startswith("tools:")]
    if tool_rows:
        lines[tool_rows[0]] = replacement
        for index in reversed(tool_rows[1:]):
            del lines[index]
    else:
        lines.insert(end, replacement)
    after = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(after)
    return {
        "path": path.as_posix(),
        "before_sha256": _sha256_bytes(before),
        "after_sha256": _sha256_bytes(after),
        "change": "frontmatter tools allowlist only",
    }


def _prepare_phoenix_assets(
    source: GitSourceIdentity,
    destination: Path,
    *,
    tool_compat_shim: bool,
) -> dict[str, Any]:
    destination.mkdir(parents=True)
    copilot_home = destination / "copilot-home"
    agents = copilot_home / "agents"
    skills = copilot_home / "skills"
    agents.mkdir(parents=True)
    skills.mkdir(parents=True)
    index = _git_index(source.checkout)
    source_agent = index.get("dist/phoenix.agent.md")
    if source_agent is None or source_agent.mode == "120000":
        raise OciRuntimeError("Phoenix checkout lacks dist/phoenix.agent.md")
    try:
        agent = _git_blob(source.checkout, source_agent).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OciRuntimeError("Phoenix agent is not UTF-8") from exc
    if "__PHOENIX_BIN__" not in agent:
        raise OciRuntimeError("Phoenix agent lacks __PHOENIX_BIN__ binding")
    agent_path = agents / "phoenix.agent.md"
    agent_path.write_text(
        agent.replace("__PHOENIX_BIN__", "/opt/harness/bin/phoenix-mcp"),
        encoding="utf-8",
        newline="\n",
    )
    shim = _shim_agent_tools(agent_path) if tool_compat_shim else None
    if shim is not None:
        shim["path"] = agent_path.relative_to(destination).as_posix()

    skill_roots = sorted(
        {
            PurePosixPath(path).parts[1]
            for path in index
            if path.startswith("skills/")
            and len(PurePosixPath(path).parts) >= 3
            and PurePosixPath(path).name == "SKILL.md"
        }
    )
    if not skill_roots:
        raise OciRuntimeError("Phoenix checkout has no tracked skills")
    for skill in skill_roots:
        _materialize_git_node(
            source.checkout,
            index,
            f"skills/{skill}",
            skills / skill,
            stack=(),
        )
    (copilot_home / "config.json").write_text(
        json.dumps({"disabled_skills": []}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (copilot_home / "mcp-config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "phoenix": {
                        "type": "stdio",
                        "command": "/opt/harness/bin/phoenix-mcp",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "asset_tree_sha256": tree_sha256(copilot_home),
        "staging_tree_sha256": tree_sha256(destination),
        "tool_compatibility_shim": shim,
        "skill_count": len(skill_roots),
    }


def _prepare_hve_assets(
    source: GitSourceIdentity,
    destination: Path,
    *,
    tool_compat_shim: bool,
) -> dict[str, Any]:
    plugin = destination / "plugin"
    destination.mkdir(parents=True)
    _materialize_git_subtree(
        source.checkout,
        plugin,
        source_prefix="plugins/hve-core",
    )
    candidates: list[Path] = []
    for path in sorted(plugin.rglob("*.md")):
        try:
            header = path.read_text(encoding="utf-8").splitlines()[:20]
        except (OSError, UnicodeDecodeError):
            continue
        if any(line.strip().casefold() == "name: rpi agent" for line in header):
            candidates.append(path)
    if len(candidates) != 1:
        raise OciRuntimeError(
            f"expected one materialized hve-core RPI agent, found {len(candidates)}"
        )
    shim = _shim_agent_tools(candidates[0]) if tool_compat_shim else None
    if shim is not None:
        shim["path"] = candidates[0].relative_to(destination).as_posix()
    return {
        "asset_tree_sha256": tree_sha256(plugin),
        "staging_tree_sha256": tree_sha256(destination),
        "tool_compatibility_shim": shim,
        "rpi_agent": candidates[0].relative_to(plugin).as_posix(),
    }


_VERIFY_IMAGE_MJS = r"""import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

function sha256(data) {
  return crypto.createHash("sha256").update(data).digest("hex");
}

function visit(root, directory, relative, rows) {
  const names = fs.readdirSync(directory).sort();
  for (const name of names) {
    const absolute = path.join(directory, name);
    const rel = relative ? `${relative}/${name}` : name;
    const info = fs.lstatSync(absolute);
    if (info.isSymbolicLink()) {
      rows.push(["l", rel, fs.readlinkSync(absolute)]);
    } else if (info.isDirectory()) {
      rows.push(["d", rel]);
      visit(root, absolute, rel, rows);
    } else if (info.isFile()) {
      const bytes = fs.readFileSync(absolute);
      rows.push(["f", rel, bytes.length, sha256(bytes)]);
    } else {
      throw new Error(`unsupported file type: ${absolute}`);
    }
  }
}

const root = "/opt/copilot";
const rows = [];
visit(root, root, "", rows);
const harness = process.env.ATV_HARNESS;
if (harness !== "phoenix" && harness !== "hve") {
  throw new Error(`unexpected harness identity: ${harness}`);
}
const assetRoot = harness === "phoenix"
  ? "/opt/harness/copilot-home"
  : "/opt/harness/plugin";
const assetRows = [];
visit(assetRoot, assetRoot, "", assetRows);
const loader = fs.readFileSync(`${root}/npm-loader.js`);
const packageJson = JSON.parse(fs.readFileSync(`${root}/package.json`, "utf8"));
const version = spawnSync(
  process.execPath,
  [`${root}/npm-loader.js`, "--version"],
  {
    encoding: "utf8",
    env: {
      ...process.env,
      COPILOT_AUTO_UPDATE: "false",
      HOME: "/home/runner",
      NO_COLOR: "1",
    },
  },
);
const versionStderr = version.stderr.trim();
const benignVersionStderr = versionStderr
  ? versionStderr.split(/\r?\n/).every(
      (line) => /^Package extraction took \d+ms$/.test(line.trim()),
    )
  : true;
if (version.status !== 0 || version.signal !== null || !benignVersionStderr) {
  throw new Error(
    `Copilot Linux self-test failed: status=${version.status} ` +
      `signal=${version.signal} stderr=${version.stderr}`,
  );
}
const tools = spawnSync(
  "/bin/sh",
  ["-c", "command -v node && command -v git && command -v python3 && command -v cp"],
  { encoding: "utf8" },
);
if (tools.status !== 0 || tools.signal !== null || tools.stderr.trim()) {
  throw new Error(
    `Runtime tool self-test failed: status=${tools.status} ` +
      `signal=${tools.signal} stderr=${tools.stderr}`,
  );
}
const envProxy = spawnSync(process.execPath, ["--use-env-proxy", "--version"], {
  encoding: "utf8",
});
if (
  envProxy.status !== 0 ||
  envProxy.signal !== null ||
  envProxy.stderr.trim()
) {
  throw new Error(
    `Node env-proxy self-test failed: status=${envProxy.status} ` +
      `signal=${envProxy.signal} stderr=${envProxy.stderr}`,
  );
}
console.log(JSON.stringify({
  platform: process.platform,
  arch: process.arch,
  node_version: process.version,
  package_version: packageJson.version,
  build_commit: packageJson.buildMetadata?.gitCommit ?? null,
  harness,
  tree_sha256: sha256(Buffer.from(JSON.stringify(rows))),
  asset_tree_sha256: sha256(Buffer.from(JSON.stringify(assetRows))),
  loader_sha256: sha256(loader),
  entrypoint_sha256: sha256(fs.readFileSync("/opt/atv/entrypoint.sh")),
  feed_token_sha256: sha256(fs.readFileSync("/opt/atv/feed-token.sh")),
  start_agent_sha256: sha256(fs.readFileSync("/opt/atv/start-agent.sh")),
  node_use_env_proxy_supported: true,
  version_output: version.stdout.trim(),
  version_stderr: versionStderr,
  runtime_tools: tools.stdout.trim().split(/\r?\n/).map((item) => item.trim()),
  phoenix_mcp_sha256: harness === "phoenix"
    ? sha256(fs.readFileSync("/opt/harness/bin/phoenix-mcp"))
    : null,
  other_harness_assets_present: harness === "phoenix"
    ? fs.existsSync("/opt/harness/plugin")
    : (
      fs.existsSync("/opt/harness/bin/phoenix-mcp") ||
      fs.existsSync("/opt/harness/copilot-home/agents/phoenix.agent.md")
    ),
}));
"""


_ENTRYPOINT_SH = r"""#!/bin/sh
set -eu
umask 077
test "${COPILOT_HOME:-}" = "/run/copilot"
mkdir -p "$COPILOT_HOME"
cp -R /opt/harness/copilot-home/. "$COPILOT_HOME"/
git config --global --add safe.directory /workspace
auth_dir="/home/runner/.atv-auth"
token_fifo="$auth_dir/token.fifo"
start_fifo="$auth_dir/start.fifo"
askpass="$auth_dir/github-askpass"
ready="$auth_dir/ready"
mkdir -p "$auth_dir"
chmod 0700 "$auth_dir"
mkfifo -m 0600 "$token_fifo" "$start_fifo"
cat > "$askpass" <<'ATV_ASKPASS'
#!/bin/sh
set -eu
auth_dir="/home/runner/.atv-auth"
token_fifo="$auth_dir/token.fifo"
askpass="$auth_dir/github-askpass"
cleanup() {
  rm -f "$token_fifo" "$askpass"
}
trap cleanup EXIT HUP INT TERM
test -p "$token_fifo"
IFS= read -r token < "$token_fifo"
test -n "$token"
printf '%s\n' "$token"
unset token
ATV_ASKPASS
chmod 0700 "$askpass"
: > "$ready"
IFS= read -r signal < "$start_fifo"
test "$signal" = "start"
rm -f "$start_fifo" "$ready"
test -p "$token_fifo"
test -x "$askpass"
export GITHUB_ASKPASS="$askpass"
export COPILOT_API_URL="https://api.githubcopilot.com"
exec node --use-env-proxy /opt/copilot/npm-loader.js "$@"
"""


_FEED_TOKEN_SH = r"""#!/bin/sh
set -eu
umask 077
auth_dir="/home/runner/.atv-auth"
token_fifo="$auth_dir/token.fifo"
askpass="$auth_dir/github-askpass"
ready="$auth_dir/ready"
test -f "$ready"
test -p "$token_fifo"
test -x "$askpass"
IFS= read -r token
test -n "$token"
printf '%s\n' "$token" > "$token_fifo"
unset token
remaining=200
while [ "$remaining" -gt 0 ]; do
  if [ ! -e "$token_fifo" ] && [ ! -e "$askpass" ]; then
    exit 0
  fi
  remaining=$((remaining - 1))
  sleep 0.1
done
exit 71
"""


_START_AGENT_SH = r"""#!/bin/sh
set -eu
start_fifo="/home/runner/.atv-auth/start.fifo"
ready="/home/runner/.atv-auth/ready"
test -f "$ready"
test -p "$start_fifo"
printf '%s\n' start > "$start_fifo"
"""


_CONNECT_PROXY_PY = r"""#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import selectors
import socket
import socketserver
import sys
import time

ALLOWED_HOSTS = frozenset(
    {
        "api.githubcopilot.com",
        "api.business.githubcopilot.com",
        "api.enterprise.githubcopilot.com",
        "api.individual.githubcopilot.com",
    }
)
EXPLICIT_DENY_HOSTS = frozenset(
    {"github.com", "api.github.com", "raw.githubusercontent.com"}
)
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 18080
MAX_HEADER_BYTES = 32768
CONNECT_TIMEOUT_SECONDS = 15
IDLE_TIMEOUT_SECONDS = 60


def emit(**payload):
    payload["timestamp_unix"] = time.time()
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def response(client, status, reason):
    client.sendall(
        f"HTTP/1.1 {status} {reason}\r\n"
        "Connection: close\r\n"
        "Content-Length: 0\r\n\r\n".encode("ascii")
    )


def read_headers(client):
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = client.recv(4096)
        if not chunk:
            raise ValueError("client_closed_before_headers")
        payload.extend(chunk)
        if len(payload) > MAX_HEADER_BYTES:
            raise ValueError("headers_too_large")
    return bytes(payload)


def parse_authority(authority):
    if any(character in authority for character in "\r\n\t /\\@"):
        raise ValueError("invalid_authority")
    if authority.startswith("["):
        raise ValueError("ip_literal_not_allowed")
    host, separator, raw_port = authority.rpartition(":")
    if not separator or not host or not raw_port.isdigit():
        raise ValueError("missing_host_or_port")
    normalized = host.rstrip(".").casefold()
    if not normalized or len(normalized) > 253:
        raise ValueError("invalid_host")
    return normalized, int(raw_port)


def public_addresses(host, port):
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for family, socktype, protocol, _, sockaddr in rows:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            continue
        yield family, socktype, protocol, sockaddr, str(address)


def connect_upstream(host, port):
    errors = []
    for family, socktype, protocol, sockaddr, address in public_addresses(host, port):
        upstream = socket.socket(family, socktype, protocol)
        upstream.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            upstream.connect(sockaddr)
            upstream.settimeout(None)
            return upstream, address
        except OSError as exc:
            errors.append(type(exc).__name__)
            upstream.close()
    raise OSError("no_public_upstream:" + ",".join(errors[-4:]))


def relay(client, upstream):
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            events = selector.select(IDLE_TIMEOUT_SECONDS)
            if not events:
                return
            for key, _ in events:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    finally:
        selector.close()


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(CONNECT_TIMEOUT_SECONDS)
        host = None
        port = None
        try:
            headers = read_headers(client)
            request_line = headers.split(b"\r\n", 1)[0].decode("ascii", "strict")
            parts = request_line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                response(client, 405, "Method Not Allowed")
                emit(event="connect", allowed=False, reason="connect_only")
                return
            host, port = parse_authority(parts[1])
            if port != 443:
                response(client, 403, "Forbidden")
                emit(
                    event="connect",
                    allowed=False,
                    host=host,
                    port=port,
                    reason="port_not_allowlisted",
                )
                return
            if host not in ALLOWED_HOSTS:
                reason = (
                    "explicit_deny"
                    if host in EXPLICIT_DENY_HOSTS
                    else "host_not_allowlisted"
                )
                response(client, 403, "Forbidden")
                emit(
                    event="connect",
                    allowed=False,
                    host=host,
                    port=port,
                    reason=reason,
                )
                return
            upstream, address = connect_upstream(host, port)
            try:
                response(client, 200, "Connection Established")
                emit(
                    event="connect",
                    allowed=True,
                    host=host,
                    port=port,
                    upstream_address=address,
                )
                client.settimeout(None)
                relay(client, upstream)
            finally:
                upstream.close()
        except (OSError, UnicodeError, ValueError) as exc:
            try:
                response(client, 502, "Bad Gateway")
            except OSError:
                pass
            emit(
                event="connect",
                allowed=False,
                host=host,
                port=port,
                reason=type(exc).__name__,
            )


class ThreadedProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


with ThreadedProxy((LISTEN_HOST, LISTEN_PORT), ConnectHandler) as server:
    emit(
        event="ready",
        listen=f"{LISTEN_HOST}:{LISTEN_PORT}",
        allowlist=sorted(ALLOWED_HOSTS),
        explicit_denylist=sorted(EXPLICIT_DENY_HOSTS),
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        sys.exit(0)
"""


def _dockerfile(harness: str) -> str:
    if harness == "phoenix":
        return """# syntax=docker/dockerfile:1.7
ARG RUST_BUILDER_IMAGE
ARG RUNTIME_BASE_IMAGE
FROM ${RUST_BUILDER_IMAGE} AS phoenix-builder
WORKDIR /build/phoenix
COPY phoenix-src/ ./
ENV CARGO_INCREMENTAL=0 SOURCE_DATE_EPOCH=0
RUN cargo build --locked --release --bin phoenix-mcp \
 && test -x target/release/phoenix-mcp

FROM ${RUNTIME_BASE_IMAGE}
RUN test ! -e /opt/harness \
 && test ! -e /opt/copilot \
 && test ! -e /opt/atv \
 && groupadd --gid 10001 atv-runner \
 && useradd --uid 10001 --gid 10001 --no-create-home \
    --home-dir /home/runner --shell /bin/sh atv-runner
WORKDIR /workspace
COPY copilot-package/ /opt/copilot/
COPY verify-image.mjs /opt/atv/verify-image.mjs
COPY entrypoint.sh /opt/atv/entrypoint.sh
COPY feed-token.sh /opt/atv/feed-token.sh
COPY start-agent.sh /opt/atv/start-agent.sh
COPY phoenix-assets/copilot-home/ /opt/harness/copilot-home/
COPY --from=phoenix-builder --chmod=0555 \
  /build/phoenix/target/release/phoenix-mcp /opt/harness/bin/phoenix-mcp
RUN chmod 0555 /opt/atv /opt/atv/entrypoint.sh \
    /opt/atv/feed-token.sh /opt/atv/start-agent.sh \
 && chmod 0444 /opt/atv/verify-image.mjs
ENV ATV_HARNESS=phoenix \
    COPILOT_HOME=/run/copilot \
    COPILOT_AUTO_UPDATE=false \
    HOME=/home/runner \
    USERPROFILE=/home/runner \
    NO_COLOR=1 \
    TMPDIR=/tmp
USER 10001:10001
ENTRYPOINT ["/opt/atv/entrypoint.sh"]
"""
    if harness == "hve":
        return """# syntax=docker/dockerfile:1.7
ARG RUNTIME_BASE_IMAGE
FROM ${RUNTIME_BASE_IMAGE}
RUN test ! -e /opt/harness \
 && test ! -e /opt/copilot \
 && test ! -e /opt/atv \
 && groupadd --gid 10001 atv-runner \
 && useradd --uid 10001 --gid 10001 --no-create-home \
    --home-dir /home/runner --shell /bin/sh atv-runner
WORKDIR /workspace
COPY copilot-package/ /opt/copilot/
COPY verify-image.mjs /opt/atv/verify-image.mjs
COPY entrypoint.sh /opt/atv/entrypoint.sh
COPY feed-token.sh /opt/atv/feed-token.sh
COPY start-agent.sh /opt/atv/start-agent.sh
COPY hve-assets/plugin/ /opt/harness/plugin/
RUN mkdir -p /opt/harness/copilot-home \
 && printf '%s\\n' '{"disabled_skills":[]}' \
    > /opt/harness/copilot-home/config.json \
 && chmod 0555 /opt/atv /opt/atv/entrypoint.sh \
    /opt/atv/feed-token.sh /opt/atv/start-agent.sh \
 && chmod 0444 /opt/atv/verify-image.mjs
ENV ATV_HARNESS=hve \
    COPILOT_HOME=/run/copilot \
    COPILOT_AUTO_UPDATE=false \
    HOME=/home/runner \
    USERPROFILE=/home/runner \
    NO_COLOR=1 \
    TMPDIR=/tmp
USER 10001:10001
ENTRYPOINT ["/opt/atv/entrypoint.sh"]
"""
    raise OciRuntimeError(f"unsupported harness: {harness}")


def _proxy_dockerfile() -> str:
    return """# syntax=docker/dockerfile:1.7
ARG RUNTIME_BASE_IMAGE
FROM ${RUNTIME_BASE_IMAGE}
ARG ATV_PROXY_SCRIPT_SHA256
RUN test ! -e /opt/atv-proxy \
 && groupadd --gid 10001 atv-proxy \
 && useradd --uid 10001 --gid 10001 --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin atv-proxy \
 && mkdir -p /opt/atv-proxy
COPY --chmod=0555 connect-proxy.py /opt/atv-proxy/connect-proxy.py
RUN python3 -c \
    'import hashlib, pathlib, sys; p=pathlib.Path(sys.argv[1]); \
assert hashlib.sha256(p.read_bytes()).hexdigest() == sys.argv[2]' \
    /opt/atv-proxy/connect-proxy.py "$ATV_PROXY_SCRIPT_SHA256"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/nonexistent
USER 10001:10001
WORKDIR /
ENTRYPOINT ["python3", "/opt/atv-proxy/connect-proxy.py"]
"""


def _validate_build_config(config: OciBuildConfig) -> None:
    if not _PINNED_IMAGE_RE.fullmatch(config.runtime_base_image):
        raise OciRuntimeError("runtime_base_image must be pinned by sha256 digest")
    if not _PINNED_IMAGE_RE.fullmatch(config.rust_builder_image):
        raise OciRuntimeError("rust_builder_image must be pinned by sha256 digest")
    if config.platform not in {"linux/amd64", "linux/arm64"}:
        raise OciRuntimeError("platform must be linux/amd64 or linux/arm64")
    if not _IMAGE_NAMESPACE_RE.fullmatch(config.image_namespace):
        raise OciRuntimeError("image_namespace is not a canonical lowercase name")
    if not config.docker:
        raise OciRuntimeError("docker executable is required")
    if config.phoenix_repo.resolve() == config.hve_repo.resolve():
        raise OciRuntimeError("Phoenix and hve must use different checkouts")
    evidence = config.evidence_dir.resolve()
    protected = (
        config.phoenix_repo.resolve(),
        config.hve_repo.resolve(),
        config.copilot_package.resolve(),
    )
    if any(evidence == root or evidence.is_relative_to(root) for root in protected):
        raise OciRuntimeError("image evidence directory cannot be inside an input tree")


def _image_spec(
    config: OciBuildConfig,
    *,
    harness: str,
    source: GitSourceIdentity,
    copilot: CopilotPackageIdentity,
    asset_metadata: Mapping[str, Any],
    dockerfile: str,
) -> dict[str, Any]:
    return {
        "schema": OCI_IMAGE_SCHEMA,
        "transform_version": OCI_TRANSFORM_VERSION,
        "harness": harness,
        "platform": config.platform,
        "runtime_base_image": config.runtime_base_image,
        "rust_builder_image": (
            config.rust_builder_image if harness == "phoenix" else None
        ),
        "source": {
            "repository": source.repository,
            "commit": source.commit,
            "git_tree": source.git_tree,
            "tracked_listing_sha256": source.tracked_listing_sha256,
        },
        "copilot": {
            "version": copilot.version,
            "build_commit": copilot.build_commit,
            "tree_sha256": copilot.tree_sha256,
            "loader_sha256": copilot.loader_sha256,
            "host_node_version": copilot.host_node_version,
            "host_version_output": copilot.host_version_output,
        },
        "assets": dict(asset_metadata),
        "dockerfile_sha256": _sha256_bytes(dockerfile.encode("utf-8")),
        "entrypoint_sha256": _sha256_bytes(_ENTRYPOINT_SH.encode("utf-8")),
        "feed_token_sha256": _sha256_bytes(_FEED_TOKEN_SH.encode("utf-8")),
        "start_agent_sha256": _sha256_bytes(_START_AGENT_SH.encode("utf-8")),
        "node_use_env_proxy_supported": True,
        "auth_transport": "docker-exec-stdin-to-one-shot-github-askpass-fifo",
        "phoenix_build": (
            {
                "command": [
                    "cargo",
                    "build",
                    "--locked",
                    "--release",
                    "--bin",
                    "phoenix-mcp",
                ],
                "cargo_lock_sha256": _sha256_bytes(
                    _git_blob(
                        source.checkout,
                        _git_index(source.checkout)["Cargo.lock"],
                    )
                ),
            }
            if harness == "phoenix"
            else None
        ),
        "final_image_contains_only_selected_harness_assets": True,
    }


def _image_labels(
    spec: Mapping[str, Any],
    *,
    source: GitSourceIdentity,
    copilot: CopilotPackageIdentity,
) -> dict[str, str]:
    spec_digest = _sha256_bytes(_canonical_json_bytes(spec))
    labels = {
        "org.atvbench.schema": OCI_IMAGE_SCHEMA,
        "org.atvbench.transform": OCI_TRANSFORM_VERSION,
        "org.atvbench.harness": source.harness,
        "org.atvbench.source.repository": source.repository,
        "org.atvbench.source.commit": source.commit,
        "org.atvbench.source.tree": source.git_tree,
        "org.atvbench.source.listing-sha256": source.tracked_listing_sha256,
        "org.atvbench.copilot.version": copilot.version,
        "org.atvbench.copilot.build-commit": copilot.build_commit,
        "org.atvbench.copilot.tree-sha256": copilot.tree_sha256,
        "org.atvbench.copilot.loader-sha256": copilot.loader_sha256,
        "org.atvbench.build-spec-sha256": spec_digest,
        "org.opencontainers.image.revision": source.commit,
    }
    return labels


def _image_tag(namespace: str, harness: str, spec_digest: str) -> str:
    return f"{namespace}-{harness}:{spec_digest[:24]}"


def _inspect_image_optional(docker: str, reference: str) -> dict[str, Any] | None:
    result = _run_bytes(
        [docker, "image", "inspect", reference],
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").casefold()
        if "no such image" in detail or "not found" in detail:
            return None
        raise OciRuntimeError(f"docker image inspect failed: {detail[:2048]}")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRuntimeError("docker image inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise OciRuntimeError("docker image inspect returned an unexpected shape")
    if not isinstance(payload[0], dict):
        raise OciRuntimeError("docker image inspect entry is not an object")
    return payload[0]


def _validate_image_inspect(
    inspect: Mapping[str, Any],
    *,
    harness: str,
    tag: str,
    labels: Mapping[str, str],
    platform: str,
) -> None:
    config = inspect.get("Config")
    if not isinstance(config, dict):
        raise OciRuntimeError("image inspect lacks Config")
    actual_labels = config.get("Labels")
    if not isinstance(actual_labels, dict):
        raise OciRuntimeError("image inspect lacks labels")
    mismatches = {
        key: {"expected": value, "actual": actual_labels.get(key)}
        for key, value in labels.items()
        if actual_labels.get(key) != value
    }
    if mismatches:
        raise OciRuntimeError(
            f"stale or substituted {harness} image labels: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )
    expected_arch = platform.split("/", 1)[1]
    if inspect.get("Os") != "linux" or inspect.get("Architecture") != expected_arch:
        raise OciRuntimeError(f"{harness} image platform does not match {platform}")
    image_id = inspect.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_id
    ):
        raise OciRuntimeError(f"{harness} image has no immutable image ID")
    if config.get("User") != CONTAINER_USER:
        raise OciRuntimeError(f"{harness} image does not default to {CONTAINER_USER}")
    if config.get("WorkingDir") != CONTAINER_WORKSPACE:
        raise OciRuntimeError(f"{harness} image has the wrong working directory")
    if config.get("Entrypoint") != ["/opt/atv/entrypoint.sh"]:
        raise OciRuntimeError(f"{harness} image has an unexpected entrypoint")
    environment = config.get("Env")
    if not isinstance(environment, list) or f"ATV_HARNESS={harness}" not in environment:
        raise OciRuntimeError(f"{harness} image lacks its harness identity")
    forbidden_environment = {
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "CONTAINER_HOST",
        "PODMAN_HOST",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ASKPASS",
    }
    inherited_keys = {
        row.split("=", 1)[0]
        for row in environment
        if isinstance(row, str) and "=" in row
    }
    if forbidden_environment & inherited_keys:
        raise OciRuntimeError(f"{harness} image inherits a container-engine endpoint")
    if config.get("Volumes"):
        raise OciRuntimeError(f"{harness} image declares implicit volumes")
    repository_tags = inspect.get("RepoTags")
    if isinstance(repository_tags, list) and tag not in repository_tags:
        raise OciRuntimeError(f"{harness} image is not bound to expected tag {tag}")


def _prepare_context(
    context: Path,
    *,
    harness: str,
    source: GitSourceIdentity,
    copilot: CopilotPackageIdentity,
    assets: Path,
    dockerfile: str,
) -> None:
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text(
        dockerfile,
        encoding="utf-8",
        newline="\n",
    )
    (context / "verify-image.mjs").write_text(
        _VERIFY_IMAGE_MJS,
        encoding="utf-8",
        newline="\n",
    )
    entrypoint = context / "entrypoint.sh"
    entrypoint.write_text(_ENTRYPOINT_SH, encoding="utf-8", newline="\n")
    entrypoint.chmod(0o755)
    feed_token = context / "feed-token.sh"
    feed_token.write_text(_FEED_TOKEN_SH, encoding="utf-8", newline="\n")
    feed_token.chmod(0o755)
    start_agent = context / "start-agent.sh"
    start_agent.write_text(_START_AGENT_SH, encoding="utf-8", newline="\n")
    start_agent.chmod(0o755)
    _copy_tree_exact(copilot.root, context / "copilot-package")
    if harness == "phoenix":
        _copy_tree_exact(assets, context / "phoenix-assets")
        _materialize_git_subtree(source.checkout, context / "phoenix-src")
    else:
        _copy_tree_exact(assets, context / "hve-assets")


def _build_image(
    config: OciBuildConfig,
    *,
    harness: str,
    tag: str,
    labels: Mapping[str, str],
    context: Path,
) -> tuple[bytes, bytes]:
    argv = [
        config.docker,
        "build",
        "--pull=false",
        "--progress=plain",
        "--platform",
        config.platform,
        "--file",
        str(context / "Dockerfile"),
        "--tag",
        tag,
        "--build-arg",
        f"RUNTIME_BASE_IMAGE={config.runtime_base_image}",
    ]
    if harness == "phoenix":
        argv += [
            "--build-arg",
            f"RUST_BUILDER_IMAGE={config.rust_builder_image}",
        ]
    for key, value in sorted(labels.items()):
        argv += ["--label", f"{key}={value}"]
    argv.append(str(context))
    result = _run_bytes(argv, timeout=3600, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-8192:]
        raise OciRuntimeError(f"Docker build failed for {harness}: {detail}")
    return result.stdout, result.stderr


def _proxy_image_spec(
    config: OciBuildConfig,
    *,
    dockerfile: str,
) -> dict[str, Any]:
    return {
        "schema": OCI_PROXY_IMAGE_SCHEMA,
        "transform_version": OCI_TRANSFORM_VERSION,
        "platform": config.platform,
        "runtime_base_image": config.runtime_base_image,
        "script_sha256": _sha256_bytes(_CONNECT_PROXY_PY.encode("utf-8")),
        "dockerfile_sha256": _sha256_bytes(dockerfile.encode("utf-8")),
        "listen_port": PROXY_PORT,
        "allowlist": list(COPILOT_MODEL_HOSTS),
        "explicit_denylist": list(EXPLICIT_PROXY_DENY_HOSTS),
        "connect_only": True,
        "public_resolved_addresses_only": True,
    }


def _proxy_image_labels(spec: Mapping[str, Any]) -> dict[str, str]:
    return {
        "org.atvbench.schema": OCI_PROXY_IMAGE_SCHEMA,
        "org.atvbench.transform": OCI_TRANSFORM_VERSION,
        "org.atvbench.role": "connect-proxy",
        "org.atvbench.proxy.script-sha256": str(spec["script_sha256"]),
        "org.atvbench.proxy.allowlist-sha256": _sha256_bytes(
            _canonical_json_bytes(spec["allowlist"])
        ),
        "org.atvbench.build-spec-sha256": _sha256_bytes(_canonical_json_bytes(spec)),
    }


def _validate_proxy_image_inspect(
    inspect: Mapping[str, Any],
    *,
    tag: str,
    labels: Mapping[str, str],
    platform: str,
) -> None:
    config = inspect.get("Config")
    if not isinstance(config, dict):
        raise OciRuntimeError("proxy image inspect lacks Config")
    actual_labels = config.get("Labels")
    if not isinstance(actual_labels, dict):
        raise OciRuntimeError("proxy image inspect lacks labels")
    mismatches = {
        key: {"expected": value, "actual": actual_labels.get(key)}
        for key, value in labels.items()
        if actual_labels.get(key) != value
    }
    if mismatches:
        raise OciRuntimeError(
            "stale or substituted proxy image labels: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )
    expected_arch = platform.split("/", 1)[1]
    if inspect.get("Os") != "linux" or inspect.get("Architecture") != expected_arch:
        raise OciRuntimeError(f"proxy image platform does not match {platform}")
    image_id = inspect.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_id
    ):
        raise OciRuntimeError("proxy image has no immutable image ID")
    if config.get("User") != CONTAINER_USER:
        raise OciRuntimeError(f"proxy image does not default to {CONTAINER_USER}")
    if config.get("WorkingDir") != "/":
        raise OciRuntimeError("proxy image has the wrong working directory")
    if config.get("Entrypoint") != [
        "python3",
        "/opt/atv-proxy/connect-proxy.py",
    ]:
        raise OciRuntimeError("proxy image has an unexpected entrypoint")
    environment = config.get("Env")
    if not isinstance(environment, list):
        raise OciRuntimeError("proxy image lacks a deterministic environment")
    environment_keys = {
        row.split("=", 1)[0]
        for row in environment
        if isinstance(row, str) and "=" in row
    }
    forbidden = {
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ASKPASS",
        "DOCKER_HOST",
        "CONTAINER_HOST",
    }
    if forbidden & environment_keys:
        raise OciRuntimeError("proxy image inherits a secret or container endpoint")
    if config.get("Volumes"):
        raise OciRuntimeError("proxy image declares implicit volumes")
    repository_tags = inspect.get("RepoTags")
    if isinstance(repository_tags, list) and tag not in repository_tags:
        raise OciRuntimeError("proxy image is not bound to its expected tag")


def _verify_proxy_image_content(
    docker: str,
    tag: str,
    *,
    expected_script_sha256: str,
) -> None:
    probe = _run_bytes(
        [
            docker,
            "run",
            "--rm",
            "--read-only",
            "--user",
            CONTAINER_USER,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            tag,
            "-c",
            (
                "import hashlib,pathlib;"
                "print(hashlib.sha256(pathlib.Path("
                "'/opt/atv-proxy/connect-proxy.py').read_bytes()).hexdigest())"
            ),
        ],
        timeout=120,
        check=False,
    )
    if probe.returncode != 0:
        detail = probe.stderr.decode("utf-8", errors="replace")[:2048]
        raise OciRuntimeError(f"proxy image content probe failed: {detail}")
    actual = probe.stdout.decode("ascii", errors="strict").strip()
    if actual != expected_script_sha256:
        raise OciRuntimeError("proxy image script hash does not match the build spec")


def _build_or_reuse_proxy_image(
    config: OciBuildConfig,
    *,
    root: Path,
) -> OciProxyImage:
    dockerfile = _proxy_dockerfile()
    spec = _proxy_image_spec(config, dockerfile=dockerfile)
    labels = _proxy_image_labels(spec)
    spec_digest = labels["org.atvbench.build-spec-sha256"]
    tag = _image_tag(config.image_namespace, "connect-proxy", spec_digest)
    inspect = _inspect_image_optional(config.docker, tag)
    reused = inspect is not None
    build_stdout = b""
    build_stderr = b""
    if inspect is None:
        context = root / "connect-proxy-context"
        context.mkdir(parents=True)
        (context / "Dockerfile").write_text(
            dockerfile,
            encoding="utf-8",
            newline="\n",
        )
        (context / "connect-proxy.py").write_text(
            _CONNECT_PROXY_PY,
            encoding="utf-8",
            newline="\n",
        )
        argv = [
            config.docker,
            "build",
            "--pull=false",
            "--progress=plain",
            "--platform",
            config.platform,
            "--file",
            str(context / "Dockerfile"),
            "--tag",
            tag,
            "--build-arg",
            f"RUNTIME_BASE_IMAGE={config.runtime_base_image}",
            "--build-arg",
            f"ATV_PROXY_SCRIPT_SHA256={spec['script_sha256']}",
        ]
        for key, value in sorted(labels.items()):
            argv += ["--label", f"{key}={value}"]
        argv.append(str(context))
        build = _run_bytes(argv, timeout=1800, check=False)
        build_stdout = build.stdout
        build_stderr = build.stderr
        if build.returncode != 0:
            detail = build.stderr.decode("utf-8", errors="replace")[-8192:]
            raise OciRuntimeError(f"Docker proxy build failed: {detail}")
        inspect = _inspect_image_optional(config.docker, tag)
        if inspect is None:
            raise OciRuntimeError("Docker proxy build completed but image is missing")
    _validate_proxy_image_inspect(
        inspect,
        tag=tag,
        labels=labels,
        platform=config.platform,
    )
    _verify_proxy_image_content(
        config.docker,
        tag,
        expected_script_sha256=str(spec["script_sha256"]),
    )
    inspect_bytes = _canonical_json_bytes(inspect)
    proxy = OciProxyImage(
        tag=tag,
        image_id=str(inspect["Id"]),
        platform=config.platform,
        runtime_base_image=config.runtime_base_image,
        build_spec_sha256=spec_digest,
        inspect_sha256=_sha256_bytes(inspect_bytes),
        script_sha256=str(spec["script_sha256"]),
        labels=labels,
        reused=reused,
    )
    evidence = (
        config.evidence_dir
        / "images"
        / (
            "connect-proxy-"
            f"{spec_digest[:12]}-"
            f"{proxy.image_id.removeprefix('sha256:')[:12]}"
        )
    )
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(evidence / "build-spec.json", spec)
    _write_json_atomic(evidence / "image-inspect.json", inspect)
    _write_json_atomic(evidence / "image.json", proxy.evidence())
    if not reused:
        _write_bytes_atomic(evidence / "docker-build.stdout", build_stdout)
        _write_bytes_atomic(evidence / "docker-build.stderr", build_stderr)
    return proxy


def _parity_probe_argv(docker: str, tag: str) -> list[str]:
    return [
        docker,
        "run",
        "--rm",
        "--read-only",
        "--user",
        CONTAINER_USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--cpus",
        "0.5",
        "--memory",
        str(512 * 1024 * 1024),
        "--memory-swap",
        str(512 * 1024 * 1024),
        "--network",
        "none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=268435456",
        "--tmpfs",
        "/home/runner:rw,exec,nosuid,nodev,size=536870912",
        "--env",
        "HOME=/home/runner",
        "--env",
        "COPILOT_AUTO_UPDATE=false",
        "--entrypoint",
        "node",
        tag,
        "/opt/atv/verify-image.mjs",
    ]


def _verify_image_parity(
    docker: str,
    image: str,
    copilot: CopilotPackageIdentity,
    *,
    harness: str,
    asset_metadata: Mapping[str, Any],
    platform: str,
) -> dict[str, Any]:
    result = _run_bytes(_parity_probe_argv(docker, image), timeout=180, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:4096]
        raise OciRuntimeError(f"Linux Copilot parity probe failed: {detail}")
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="strict").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRuntimeError(
            "Linux Copilot parity probe returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise OciRuntimeError("Linux Copilot parity probe did not return an object")
    expected = {
        "platform": "linux",
        "arch": ("x64" if platform.split("/", 1)[1] == "amd64" else "arm64"),
        "harness": harness,
        "node_version": copilot.host_node_version,
        "package_version": copilot.version,
        "build_commit": copilot.build_commit,
        "tree_sha256": copilot.tree_sha256,
        "asset_tree_sha256": asset_metadata["asset_tree_sha256"],
        "loader_sha256": copilot.loader_sha256,
        "entrypoint_sha256": _sha256_bytes(_ENTRYPOINT_SH.encode("utf-8")),
        "feed_token_sha256": _sha256_bytes(_FEED_TOKEN_SH.encode("utf-8")),
        "start_agent_sha256": _sha256_bytes(_START_AGENT_SH.encode("utf-8")),
    }
    mismatches = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise OciRuntimeError(
            "exact Linux Copilot parity could not be proven: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )
    linux_version_output = payload.get("version_output")
    if not isinstance(linux_version_output, str) or (
        copilot.version not in linux_version_output
        and linux_version_output != copilot.host_version_output
    ):
        raise OciRuntimeError(
            "Linux Copilot self-test output is not bound to the frozen package "
            "version or the host-observed banner"
        )
    if payload.get("other_harness_assets_present") is not False:
        raise OciRuntimeError("final image contains assets from the other harness")
    phoenix_mcp_sha256 = payload.get("phoenix_mcp_sha256")
    if harness == "phoenix":
        if not isinstance(phoenix_mcp_sha256, str) or not _SHA256_RE.fullmatch(
            phoenix_mcp_sha256
        ):
            raise OciRuntimeError("Phoenix image lacks a hashable Linux phoenix-mcp")
    elif phoenix_mcp_sha256 is not None:
        raise OciRuntimeError("hve image unexpectedly contains phoenix-mcp")
    runtime_tools = payload.get("runtime_tools")
    if not isinstance(runtime_tools, list) or len(runtime_tools) != 4:
        raise OciRuntimeError("runtime image must provide node, git, python3, and cp")
    if not all(
        isinstance(item, str) and item.startswith("/") for item in runtime_tools
    ):
        raise OciRuntimeError("runtime tool paths are not absolute")
    return {
        **expected,
        "host_version_output": copilot.host_version_output,
        "linux_version_output": linux_version_output,
        "linux_version_stderr": payload.get("version_stderr", ""),
        "platform_specific_version_banner_allowed": True,
        "runtime_tools": runtime_tools,
        "phoenix_mcp_sha256": phoenix_mcp_sha256,
        "other_harness_assets_present": False,
        "verified": True,
        "probe_network": "none",
        "probe_read_only_root": True,
        "probe_non_root": True,
    }


def build_or_reuse_images(config: OciBuildConfig) -> dict[str, OciImage]:
    """Build or strictly reuse two content-bound, payload-verified images."""

    _validate_build_config(config)
    sources = {
        "phoenix": inspect_source(
            config.phoenix_repo,
            harness="phoenix",
            expected_commit=config.phoenix_commit,
        ),
        "hve": inspect_source(
            config.hve_repo,
            harness="hve",
            expected_commit=config.hve_commit,
        ),
    }
    copilot = inspect_copilot_package(
        config.copilot_package,
        host_node=config.host_node,
    )
    results: dict[str, OciImage] = {}
    config.evidence_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="atv-oci-image-inputs-") as temporary:
        root = Path(temporary)
        proxy = _build_or_reuse_proxy_image(config, root=root)
        for harness in HARNESSES:
            source = sources[harness]
            assets = root / f"{harness}-assets"
            if harness == "phoenix":
                asset_metadata = _prepare_phoenix_assets(
                    source,
                    assets,
                    tool_compat_shim=config.tool_compat_shim,
                )
            else:
                asset_metadata = _prepare_hve_assets(
                    source,
                    assets,
                    tool_compat_shim=config.tool_compat_shim,
                )
            dockerfile = _dockerfile(harness)
            spec = _image_spec(
                config,
                harness=harness,
                source=source,
                copilot=copilot,
                asset_metadata=asset_metadata,
                dockerfile=dockerfile,
            )
            labels = _image_labels(spec, source=source, copilot=copilot)
            spec_digest = labels["org.atvbench.build-spec-sha256"]
            tag = _image_tag(config.image_namespace, harness, spec_digest)
            inspect = _inspect_image_optional(config.docker, tag)
            reused = inspect is not None
            build_stdout = b""
            build_stderr = b""
            if inspect is None:
                context = root / f"{harness}-context"
                _prepare_context(
                    context,
                    harness=harness,
                    source=source,
                    copilot=copilot,
                    assets=assets,
                    dockerfile=dockerfile,
                )
                build_stdout, build_stderr = _build_image(
                    config,
                    harness=harness,
                    tag=tag,
                    labels=labels,
                    context=context,
                )
                inspect = _inspect_image_optional(config.docker, tag)
                if inspect is None:
                    raise OciRuntimeError(
                        f"Docker build completed but {harness} image is missing"
                    )
            _validate_image_inspect(
                inspect,
                harness=harness,
                tag=tag,
                labels=labels,
                platform=config.platform,
            )
            parity = _verify_image_parity(
                config.docker,
                tag,
                copilot,
                harness=harness,
                asset_metadata=asset_metadata,
                platform=config.platform,
            )
            inspect_bytes = _canonical_json_bytes(inspect)
            image_id = str(inspect["Id"])
            image = OciImage(
                harness=harness,
                tag=tag,
                image_id=image_id,
                platform=config.platform,
                build_spec_sha256=spec_digest,
                inspect_sha256=_sha256_bytes(inspect_bytes),
                source=source,
                copilot=copilot,
                labels=labels,
                parity=parity,
                proxy=proxy,
                reused=reused,
            )
            image_evidence = (
                config.evidence_dir
                / "images"
                / f"{harness}-{spec_digest[:12]}-{image_id.removeprefix('sha256:')[:12]}"
            )
            image_evidence.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(image_evidence / "build-spec.json", spec)
            _write_json_atomic(image_evidence / "image-inspect.json", inspect)
            _write_json_atomic(image_evidence / "copilot-parity.json", parity)
            _write_json_atomic(
                image_evidence / "source-identity.json", source.evidence()
            )
            _write_json_atomic(image_evidence / "image.json", image.evidence())
            if not reused:
                _write_bytes_atomic(
                    image_evidence / "docker-build.stdout", build_stdout
                )
                _write_bytes_atomic(
                    image_evidence / "docker-build.stderr", build_stderr
                )
            results[harness] = image
    return results


def _safe_component(value: str, *, limit: int = 48) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("-", value).strip("._-")
    return (cleaned or "run")[:limit]


def _container_name(config: OciRunConfig) -> str:
    digest = _sha256_bytes(
        _canonical_json_bytes(
            {
                "run_id": config.run_id,
                "harness": config.harness,
                "image": config.image.image_id,
                "workspace": str(config.workspace.resolve()),
            }
        )
    )
    return f"atv-{config.harness}-{_safe_component(config.run_id)}-{digest[:12]}"


def _run_resource_names(config: OciRunConfig) -> dict[str, str]:
    digest = _sha256_bytes(
        _canonical_json_bytes(
            {
                "run_id": config.run_id,
                "harness": config.harness,
                "image": config.image.image_id,
                "proxy_image": config.image.proxy.image_id,
                "workspace": str(config.workspace.resolve()),
            }
        )
    )
    component = _safe_component(config.run_id, limit=24)
    suffix = digest[:12]
    return {
        "harness_container": _container_name(config),
        "proxy_container": f"atv-proxy-{component}-{suffix}",
        "internal_network": f"atv-int-{component}-{suffix}",
        "egress_network": f"atv-eg-{component}-{suffix}",
    }


def _workspace_mount_value(workspace: Path) -> str:
    value = str(workspace.resolve())
    if "," in value or "\n" in value or "\r" in value or "\x00" in value:
        raise OciRuntimeError("workspace path cannot be represented safely as --mount")
    return (
        f"type=bind,source={value},target={CONTAINER_WORKSPACE},"
        "bind-propagation=rprivate"
    )


def _copilot_command(config: OciRunConfig, goal: str) -> list[str]:
    if not goal or "\x00" in goal:
        raise OciRuntimeError("goal must be non-empty and contain no NUL")
    argv: list[str] = []
    if config.harness == "hve":
        argv += ["--plugin-dir", "/opt/harness/plugin"]
    argv += [
        "-C",
        CONTAINER_WORKSPACE,
        "-p",
        goal,
        "--agent",
        "phoenix" if config.harness == "phoenix" else "hve-core:rpi-agent",
        "--allow-all-tools",
        "--no-ask-user",
        "--output-format",
        "json",
        "--stream",
        "off",
        "--model",
        config.model,
        "--max-ai-credits",
        str(config.max_ai_credits),
        "--disable-builtin-mcps",
        "--no-remote",
        "--no-remote-export",
        "--no-auto-update",
        "--no-color",
        "--plain-diff",
        "--log-level",
        "error",
        "--secret-env-vars=GITHUB_ASKPASS",
    ]
    return argv


def _container_create_argv(
    config: OciRunConfig,
    *,
    goal: str,
    container_name: str,
    internal_network: str,
) -> list[str]:
    limits = config.limits
    proxy_url = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    argv = [
        config.docker,
        "create",
        "--name",
        container_name,
        "--init",
        "--read-only",
        "--user",
        CONTAINER_USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(limits.pids),
        "--cpus",
        format(limits.cpus, "g"),
        "--memory",
        str(limits.memory_bytes),
        "--memory-swap",
        str(limits.memory_bytes),
        "--shm-size",
        str(limits.shm_bytes),
        "--ulimit",
        "nofile=1024:1024",
        "--network",
        internal_network,
        "--ipc",
        "none",
        "--add-host",
        "host.docker.internal:127.0.0.1",
        "--add-host",
        "gateway.docker.internal:127.0.0.1",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}",
        "--tmpfs",
        f"{COPILOT_HOME}:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}",
        "--tmpfs",
        f"/home/runner:rw,exec,nosuid,nodev,size={limits.home_tmpfs_bytes}",
        "--mount",
        _workspace_mount_value(config.workspace),
        "--workdir",
        CONTAINER_WORKSPACE,
        "--env",
        f"COPILOT_HOME={COPILOT_HOME}",
        "--env",
        "COPILOT_AUTO_UPDATE=false",
        "--env",
        "HOME=/home/runner",
        "--env",
        "USERPROFILE=/home/runner",
        "--env",
        "NO_COLOR=1",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        f"HTTP_PROXY={proxy_url}",
        "--env",
        f"HTTPS_PROXY={proxy_url}",
        "--env",
        f"http_proxy={proxy_url}",
        "--env",
        f"https_proxy={proxy_url}",
        "--env",
        "NO_PROXY=localhost,127.0.0.1,::1",
        "--env",
        "no_proxy=localhost,127.0.0.1,::1",
        "--env",
        "NODE_USE_ENV_PROXY=1",
        "--label",
        f"org.atvbench.run-schema={OCI_RUN_SCHEMA}",
        "--label",
        f"org.atvbench.run-id={config.run_id}",
        "--label",
        f"org.atvbench.harness={config.harness}",
        config.image.tag,
        *_copilot_command(config, goal),
    ]
    _assert_secure_create_argv(
        argv,
        workspace=config.workspace,
        network=internal_network,
    )
    return argv


def _option_values(argv: Sequence[str], option: str) -> list[str]:
    values: list[str] = []
    for index, item in enumerate(argv):
        if item == option:
            if index + 1 >= len(argv):
                raise OciRuntimeError(f"missing value for {option}")
            values.append(argv[index + 1])
        elif item.startswith(f"{option}="):
            values.append(item.split("=", 1)[1])
    return values


def _assert_secure_create_argv(
    argv: Sequence[str],
    *,
    workspace: Path,
    network: str,
) -> None:
    required_flags = {"--init", "--read-only"}
    missing = sorted(flag for flag in required_flags if flag not in argv)
    if missing:
        raise OciRuntimeError(f"container argv lacks required flags: {missing}")
    forbidden_flags = {
        "--privileged",
        "--volume",
        "-v",
        "--device",
        "--pid=host",
        "--network=host",
    }
    if any(item in forbidden_flags for item in argv):
        raise OciRuntimeError("container argv contains a forbidden Docker flag")
    if _option_values(argv, "--user") != [CONTAINER_USER]:
        raise OciRuntimeError("container must run as the fixed non-root user")
    if _option_values(argv, "--cap-drop") != ["ALL"]:
        raise OciRuntimeError("container must drop all Linux capabilities")
    if _option_values(argv, "--security-opt") != ["no-new-privileges:true"]:
        raise OciRuntimeError("container must set no-new-privileges")
    if _option_values(argv, "--network") != [network]:
        raise OciRuntimeError("Copilot execution network is not the per-run network")
    if network in {"bridge", "host", "none"}:
        raise OciRuntimeError("Copilot execution must use a named internal network")
    if set(_option_values(argv, "--add-host")) != {
        "host.docker.internal:127.0.0.1",
        "gateway.docker.internal:127.0.0.1",
    }:
        raise OciRuntimeError("Docker Desktop host aliases are not both masked")
    mounts = _option_values(argv, "--mount")
    if mounts != [_workspace_mount_value(workspace)]:
        raise OciRuntimeError("container must have exactly one workspace bind mount")
    if _option_values(argv, "--env-file"):
        raise OciRuntimeError("container must not use an env file")
    environment = _option_values(argv, "--env")
    environment_by_key = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in environment
        if "=" in item
    }
    proxy_url = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if environment_by_key.get(key) != proxy_url:
            raise OciRuntimeError("container proxy environment is incomplete")
    forbidden_environment = {
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ASKPASS",
    }
    if forbidden_environment & set(environment_by_key):
        raise OciRuntimeError("container argv persists an authentication secret")
    if "--secret-env-vars=GITHUB_ASKPASS" not in argv:
        raise OciRuntimeError("Copilot must strip GITHUB_ASKPASS from tool processes")
    serialized = "\n".join(argv).casefold()
    if "docker.sock" in serialized:
        raise OciRuntimeError("container argv exposes the Docker socket")
    if any(
        "source=" in item and str(workspace.resolve()) not in item for item in mounts
    ):
        raise OciRuntimeError("container argv exposes a non-workspace source")


def _network_labels(config: OciRunConfig, *, role: str) -> dict[str, str]:
    return {
        "org.atvbench.run-schema": OCI_RUN_SCHEMA,
        "org.atvbench.run-id": config.run_id,
        "org.atvbench.harness": config.harness,
        "org.atvbench.network-role": role,
    }


def _network_create_argv(
    config: OciRunConfig,
    *,
    name: str,
    role: str,
    internal: bool,
) -> list[str]:
    argv = [
        config.docker,
        "network",
        "create",
        "--driver",
        "bridge",
    ]
    if internal:
        argv.append("--internal")
    for key, value in sorted(_network_labels(config, role=role).items()):
        argv += ["--label", f"{key}={value}"]
    argv.append(name)
    return argv


def _proxy_container_create_argv(
    config: OciRunConfig,
    *,
    container_name: str,
    egress_network: str,
) -> list[str]:
    memory = 256 * 1024 * 1024
    argv = [
        config.docker,
        "create",
        "--name",
        container_name,
        "--init",
        "--read-only",
        "--user",
        CONTAINER_USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--cpus",
        "0.5",
        "--memory",
        str(memory),
        "--memory-swap",
        str(memory),
        "--shm-size",
        str(32 * 1024 * 1024),
        "--ulimit",
        "nofile=256:256",
        "--network",
        egress_network,
        "--ipc",
        "none",
        "--add-host",
        "host.docker.internal:127.0.0.1",
        "--add-host",
        "gateway.docker.internal:127.0.0.1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=33554432",
        "--label",
        f"org.atvbench.run-schema={OCI_RUN_SCHEMA}",
        "--label",
        f"org.atvbench.run-id={config.run_id}",
        "--label",
        "org.atvbench.role=connect-proxy",
        config.image.proxy.tag,
    ]
    if _option_values(argv, "--mount") or _option_values(argv, "--env-file"):
        raise OciRuntimeError("proxy container must have no mounts or env files")
    return argv


def _inspect_container_optional(docker: str, reference: str) -> dict[str, Any] | None:
    result = _run_bytes(
        [docker, "container", "inspect", reference],
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").casefold()
        if "no such container" in detail or "not found" in detail:
            return None
        raise OciRuntimeError(f"docker container inspect failed: {detail[:2048]}")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRuntimeError("docker container inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise OciRuntimeError("docker container inspect returned an unexpected shape")
    if not isinstance(payload[0], dict):
        raise OciRuntimeError("docker container inspect entry is not an object")
    return payload[0]


def _inspect_network_optional(docker: str, reference: str) -> dict[str, Any] | None:
    result = _run_bytes(
        [docker, "network", "inspect", reference],
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").casefold()
        if "no such network" in detail or "not found" in detail:
            return None
        raise OciRuntimeError(f"docker network inspect failed: {detail[:2048]}")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRuntimeError("docker network inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise OciRuntimeError("docker network inspect returned an unexpected shape")
    if not isinstance(payload[0], dict):
        raise OciRuntimeError("docker network inspect entry is not an object")
    return payload[0]


def _validate_network_inspect(
    inspect: Mapping[str, Any],
    *,
    config: OciRunConfig,
    name: str,
    role: str,
    internal: bool,
) -> None:
    if inspect.get("Name") != name:
        raise OciRuntimeError(f"{role} network name mismatch")
    if inspect.get("Driver") != "bridge":
        raise OciRuntimeError(f"{role} network is not a bridge")
    if inspect.get("Internal") is not internal:
        raise OciRuntimeError(f"{role} network internal flag mismatch")
    if inspect.get("Ingress") is True or inspect.get("Attachable") is True:
        raise OciRuntimeError(f"{role} network is unexpectedly shared")
    labels = inspect.get("Labels")
    if not isinstance(labels, dict):
        raise OciRuntimeError(f"{role} network lacks ownership labels")
    expected = _network_labels(config, role=role)
    mismatches = {
        key: {"expected": value, "actual": labels.get(key)}
        for key, value in expected.items()
        if labels.get(key) != value
    }
    if mismatches:
        raise OciRuntimeError(
            f"{role} network ownership mismatch: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )


def _network_member_names(inspect: Mapping[str, Any]) -> set[str]:
    containers = inspect.get("Containers")
    if containers is None or containers == {}:
        return set()
    if not isinstance(containers, dict):
        raise OciRuntimeError("network inspect has invalid container membership")
    names: set[str] = set()
    for row in containers.values():
        if not isinstance(row, dict) or not isinstance(row.get("Name"), str):
            raise OciRuntimeError("network inspect has an invalid endpoint")
        names.add(str(row["Name"]))
    return names


def _assert_no_token_metadata(
    payload: Mapping[str, Any],
    *,
    token: str,
    context: str,
) -> None:
    serialized = _canonical_json_bytes(payload)
    if token.encode("utf-8") in serialized:
        raise OciRuntimeError(f"{context} persists the OAuth token")


def _docker_desktop_source_candidates(workspace: Path) -> set[str]:
    resolved = str(workspace.resolve()).replace("\\", "/")
    candidates = {resolved.casefold()}
    match = re.fullmatch(r"([a-zA-Z]):/(.*)", resolved)
    if match:
        drive, suffix = match.groups()
        candidates.update(
            {
                f"/run/desktop/mnt/host/{drive.casefold()}/{suffix}".casefold(),
                f"/host_mnt/{drive.casefold()}/{suffix}".casefold(),
                f"/mnt/{drive.casefold()}/{suffix}".casefold(),
            }
        )
    return candidates


def _validate_container_inspect(
    inspect: Mapping[str, Any],
    *,
    config: OciRunConfig,
    container_name: str,
    internal_network: str,
    token: str,
    expect_running: bool | None,
) -> None:
    _assert_no_token_metadata(inspect, token=token, context="container inspect")
    if inspect.get("Image") != config.image.image_id:
        raise OciRuntimeError("container image ID does not match the verified image")
    if inspect.get("Name") not in {container_name, f"/{container_name}"}:
        raise OciRuntimeError("container inspect name mismatch")
    container_config = inspect.get("Config")
    host = inspect.get("HostConfig")
    if not isinstance(container_config, dict) or not isinstance(host, dict):
        raise OciRuntimeError("container inspect lacks Config or HostConfig")
    if container_config.get("User") != CONTAINER_USER:
        raise OciRuntimeError("container is not configured as the non-root user")
    if container_config.get("WorkingDir") != CONTAINER_WORKSPACE:
        raise OciRuntimeError("container working directory is not /workspace")
    environment = container_config.get("Env")
    if not isinstance(environment, list):
        raise OciRuntimeError("container inspect lacks environment")
    environment_by_key = {
        row.split("=", 1)[0]: row.split("=", 1)[1]
        for row in environment
        if isinstance(row, str) and "=" in row
    }
    forbidden_environment = {
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ASKPASS",
    }
    if forbidden_environment & set(environment_by_key):
        raise OciRuntimeError("container inspect contains an authentication env var")
    proxy_url = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if environment_by_key.get(key) != proxy_url:
            raise OciRuntimeError("container inspect lacks the forced CONNECT proxy")
    command = container_config.get("Cmd")
    if (
        not isinstance(command, list)
        or "--secret-env-vars=GITHUB_ASKPASS" not in command
    ):
        raise OciRuntimeError("container command does not protect GITHUB_ASKPASS")
    if host.get("ReadonlyRootfs") is not True:
        raise OciRuntimeError("container root filesystem is not read-only")
    if host.get("Init") is not True:
        raise OciRuntimeError("container init process is not enabled")
    if set(host.get("CapDrop") or []) != {"ALL"}:
        raise OciRuntimeError("container did not drop all capabilities")
    if "no-new-privileges:true" not in set(host.get("SecurityOpt") or []):
        raise OciRuntimeError("container lacks no-new-privileges")
    if host.get("Privileged") is True:
        raise OciRuntimeError("container is privileged")
    if host.get("AutoRemove") is True:
        raise OciRuntimeError("container auto-removal would destroy inspect evidence")
    if host.get("NetworkMode") != internal_network:
        raise OciRuntimeError("container is not using the per-run internal network")
    if host.get("IpcMode") != "none":
        raise OciRuntimeError("container IPC mode is not none")
    if host.get("PidMode") not in {"", None}:
        raise OciRuntimeError("container PID namespace is not private")
    if host.get("UTSMode") not in {"", None}:
        raise OciRuntimeError("container UTS namespace is not private")
    if host.get("PublishAllPorts") is True or host.get("PortBindings"):
        raise OciRuntimeError("container publishes network ports")
    if int(host.get("PidsLimit") or 0) != config.limits.pids:
        raise OciRuntimeError("container PIDs limit mismatch")
    if int(host.get("Memory") or 0) != config.limits.memory_bytes:
        raise OciRuntimeError("container memory limit mismatch")
    if int(host.get("MemorySwap") or 0) != config.limits.memory_bytes:
        raise OciRuntimeError("container memory-swap limit mismatch")
    if int(host.get("ShmSize") or 0) != config.limits.shm_bytes:
        raise OciRuntimeError("container shared-memory limit mismatch")
    expected_nano_cpus = int(config.limits.cpus * 1_000_000_000)
    if int(host.get("NanoCpus") or 0) != expected_nano_cpus:
        raise OciRuntimeError("container CPU limit mismatch")
    if host.get("Devices"):
        raise OciRuntimeError("container exposes host devices")
    if host.get("Binds"):
        raise OciRuntimeError("container has legacy bind mounts")
    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, dict) or set(tmpfs) != {
        "/tmp",
        COPILOT_HOME,
        "/home/runner",
    }:
        raise OciRuntimeError("container tmpfs set is incomplete or excessive")
    expected_tmpfs_sizes = {
        "/tmp": config.limits.tmpfs_bytes,
        COPILOT_HOME: config.limits.tmpfs_bytes,
        "/home/runner": config.limits.home_tmpfs_bytes,
    }
    for destination, expected_size in expected_tmpfs_sizes.items():
        options = {
            item.strip() for item in str(tmpfs[destination]).split(",") if item.strip()
        }
        required = {"rw", "nosuid", "nodev"}
        required.add("exec" if destination == "/home/runner" else "noexec")
        if not required.issubset(options):
            raise OciRuntimeError(f"tmpfs hardening mismatch at {destination}")
        if f"size={expected_size}" not in options:
            raise OciRuntimeError(f"tmpfs size mismatch at {destination}")
    ulimits = host.get("Ulimits")
    if not isinstance(ulimits, list) or not any(
        isinstance(item, dict)
        and item.get("Name") == "nofile"
        and item.get("Soft") == 1024
        and item.get("Hard") == 1024
        for item in ulimits
    ):
        raise OciRuntimeError("container file-descriptor limit mismatch")
    extra_hosts = set(host.get("ExtraHosts") or [])
    if not {
        "host.docker.internal:127.0.0.1",
        "gateway.docker.internal:127.0.0.1",
    }.issubset(extra_hosts):
        raise OciRuntimeError("container inspect does not show masked host aliases")
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 1:
        raise OciRuntimeError("container does not have exactly one persistent mount")
    mount = mounts[0]
    if not isinstance(mount, dict):
        raise OciRuntimeError("container mount inspect entry is invalid")
    if (
        mount.get("Type") != "bind"
        or mount.get("Destination") != CONTAINER_WORKSPACE
        or mount.get("RW") is not True
    ):
        raise OciRuntimeError("container workspace mount is not the sole RW bind")
    source = str(mount.get("Source", "")).replace("\\", "/").casefold()
    if source not in _docker_desktop_source_candidates(config.workspace):
        raise OciRuntimeError("container bind source is not the intended workspace")
    if "docker.sock" in source:
        raise OciRuntimeError("container mount exposes the Docker socket")
    network_settings = inspect.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        raise OciRuntimeError("container inspect lacks NetworkSettings")
    networks = network_settings.get("Networks")
    if not isinstance(networks, dict) or set(networks) != {internal_network}:
        raise OciRuntimeError("harness has a network path that bypasses the proxy")
    state = inspect.get("State")
    if not isinstance(state, dict):
        raise OciRuntimeError("container inspect lacks State")
    if expect_running is True and state.get("Running") is not True:
        raise OciRuntimeError("container was expected to be running")
    if expect_running is False and state.get("Running") is not False:
        raise OciRuntimeError("container was expected to have exited")


def _validate_proxy_container_inspect(
    inspect: Mapping[str, Any],
    *,
    config: OciRunConfig,
    container_name: str,
    internal_network: str,
    egress_network: str,
    token: str,
    expect_running: bool | None,
) -> None:
    _assert_no_token_metadata(inspect, token=token, context="proxy inspect")
    if inspect.get("Image") != config.image.proxy.image_id:
        raise OciRuntimeError("proxy container image ID mismatch")
    if inspect.get("Name") not in {container_name, f"/{container_name}"}:
        raise OciRuntimeError("proxy container name mismatch")
    container_config = inspect.get("Config")
    host = inspect.get("HostConfig")
    if not isinstance(container_config, dict) or not isinstance(host, dict):
        raise OciRuntimeError("proxy inspect lacks Config or HostConfig")
    if container_config.get("User") != CONTAINER_USER:
        raise OciRuntimeError("proxy container is not non-root")
    environment = container_config.get("Env")
    if not isinstance(environment, list):
        raise OciRuntimeError("proxy inspect lacks environment")
    environment_keys = {
        row.split("=", 1)[0]
        for row in environment
        if isinstance(row, str) and "=" in row
    }
    if {
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ASKPASS",
    } & environment_keys:
        raise OciRuntimeError("proxy inspect contains an authentication env var")
    if host.get("ReadonlyRootfs") is not True or host.get("Init") is not True:
        raise OciRuntimeError("proxy root or init hardening mismatch")
    if set(host.get("CapDrop") or []) != {"ALL"}:
        raise OciRuntimeError("proxy did not drop all capabilities")
    if "no-new-privileges:true" not in set(host.get("SecurityOpt") or []):
        raise OciRuntimeError("proxy lacks no-new-privileges")
    if host.get("Privileged") is True or host.get("AutoRemove") is True:
        raise OciRuntimeError("proxy lifecycle hardening mismatch")
    if host.get("NetworkMode") != egress_network:
        raise OciRuntimeError("proxy primary network is not per-run egress")
    if host.get("IpcMode") != "none":
        raise OciRuntimeError("proxy IPC namespace is not private")
    if host.get("PidMode") not in {"", None}:
        raise OciRuntimeError("proxy PID namespace is not private")
    if host.get("UTSMode") not in {"", None}:
        raise OciRuntimeError("proxy UTS namespace is not private")
    if host.get("PublishAllPorts") is True or host.get("PortBindings"):
        raise OciRuntimeError("proxy publishes a host port")
    if int(host.get("PidsLimit") or 0) != 64:
        raise OciRuntimeError("proxy PID limit mismatch")
    if int(host.get("Memory") or 0) != 256 * 1024 * 1024:
        raise OciRuntimeError("proxy memory limit mismatch")
    if int(host.get("MemorySwap") or 0) != 256 * 1024 * 1024:
        raise OciRuntimeError("proxy memory-swap limit mismatch")
    if int(host.get("ShmSize") or 0) != 32 * 1024 * 1024:
        raise OciRuntimeError("proxy shared-memory limit mismatch")
    if int(host.get("NanoCpus") or 0) != 500_000_000:
        raise OciRuntimeError("proxy CPU limit mismatch")
    if host.get("Devices") or host.get("Binds"):
        raise OciRuntimeError("proxy exposes a host device or bind mount")
    mounts = inspect.get("Mounts")
    if mounts is not None and mounts != []:
        raise OciRuntimeError("proxy container must have no persistent mounts")
    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, dict) or set(tmpfs) != {"/tmp"}:
        raise OciRuntimeError("proxy tmpfs set is incomplete or excessive")
    tmp_options = {
        item.strip() for item in str(tmpfs["/tmp"]).split(",") if item.strip()
    }
    if not {"rw", "noexec", "nosuid", "nodev", "size=33554432"}.issubset(tmp_options):
        raise OciRuntimeError("proxy tmpfs hardening mismatch")
    ulimits = host.get("Ulimits")
    if not isinstance(ulimits, list) or not any(
        isinstance(item, dict)
        and item.get("Name") == "nofile"
        and item.get("Soft") == 256
        and item.get("Hard") == 256
        for item in ulimits
    ):
        raise OciRuntimeError("proxy file-descriptor limit mismatch")
    extra_hosts = set(host.get("ExtraHosts") or [])
    if not {
        "host.docker.internal:127.0.0.1",
        "gateway.docker.internal:127.0.0.1",
    }.issubset(extra_hosts):
        raise OciRuntimeError("proxy inspect does not show masked host aliases")
    network_settings = inspect.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        raise OciRuntimeError("proxy inspect lacks NetworkSettings")
    networks = network_settings.get("Networks")
    if not isinstance(networks, dict) or set(networks) != {
        internal_network,
        egress_network,
    }:
        raise OciRuntimeError("proxy is not exactly dual-homed")
    internal_endpoint = networks.get(internal_network)
    aliases = (
        internal_endpoint.get("Aliases")
        if isinstance(internal_endpoint, dict)
        else None
    )
    if not isinstance(aliases, list) or PROXY_ALIAS not in aliases:
        raise OciRuntimeError("proxy lacks its internal DNS alias")
    state = inspect.get("State")
    if not isinstance(state, dict):
        raise OciRuntimeError("proxy inspect lacks State")
    if expect_running is True and state.get("Running") is not True:
        raise OciRuntimeError("proxy was expected to be running")
    if expect_running is False and state.get("Running") is not False:
        raise OciRuntimeError("proxy was expected to have exited")


def _validate_network_membership(
    internal_inspect: Mapping[str, Any],
    egress_inspect: Mapping[str, Any],
    *,
    internal_expected: set[str],
    egress_expected: set[str],
) -> None:
    if _network_member_names(internal_inspect) != internal_expected:
        raise OciRuntimeError("internal network membership is not exact")
    if _network_member_names(egress_inspect) != egress_expected:
        raise OciRuntimeError("egress network membership is not exact")


def _redact_inspect(payload: Mapping[str, Any], token: str) -> dict[str, Any]:
    _assert_no_token_metadata(payload, token=token, context="inspect evidence")
    return copy.deepcopy(dict(payload))


def _validate_run_config(config: OciRunConfig) -> str:
    if config.harness not in HARNESSES:
        raise OciRuntimeError(f"unsupported harness: {config.harness}")
    if config.image.harness != config.harness:
        raise OciRuntimeError("run harness does not match verified image")
    if config.network != NETWORK_POLICY:
        raise OciRuntimeError("only the internal CONNECT-proxy policy is supported")
    if config.image.proxy.platform != config.image.platform:
        raise OciRuntimeError("proxy and harness image platforms differ")
    if config.image.proxy.script_sha256 != _sha256_bytes(
        _CONNECT_PROXY_PY.encode("utf-8")
    ):
        raise OciRuntimeError("proxy script is not bound to this runtime module")
    if not config.model or "\x00" in config.model:
        raise OciRuntimeError("model must be explicit and contain no NUL")
    if config.max_ai_credits <= 0 or config.timeout_seconds <= 0:
        raise OciRuntimeError("credits and timeout must be positive")
    if not config.run_id or "\x00" in config.run_id:
        raise OciRuntimeError("run_id must be explicit and contain no NUL")
    if len(config.run_id) > 256:
        raise OciRuntimeError("run_id is too long")
    config.limits.validate()
    workspace = config.workspace.resolve()
    if not workspace.is_dir():
        raise OciRuntimeError(f"workspace does not exist: {workspace}")
    if not (workspace / ".git").exists():
        raise OciRuntimeError("fresh task workspace must be a Git repository")
    status = _git_bytes(
        workspace,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status:
        raise OciRuntimeError("task workspace is not fresh and clean")
    base = git_base(str(workspace))
    if base is None:
        raise OciRuntimeError("task workspace has no readable HEAD")
    evidence = config.evidence_dir.resolve()
    if evidence == workspace or evidence.is_relative_to(workspace):
        raise OciRuntimeError("run evidence cannot be mounted inside the workspace")
    protected = (
        config.image.source.checkout.resolve(),
        config.image.copilot.root.resolve(),
        *(root.resolve() for root in config.forbidden_roots),
    )
    for root in protected:
        if (
            workspace == root
            or workspace.is_relative_to(root)
            or root.is_relative_to(workspace)
        ):
            raise OciRuntimeError(
                f"workspace overlaps a source, sibling, or grader root: {root}"
            )
    if evidence.exists():
        if not evidence.is_dir():
            raise OciRuntimeError("run evidence path is not a directory")
        if any(evidence.iterdir()):
            raise OciRuntimeError("run evidence directory is not empty")
    evidence.mkdir(parents=True, exist_ok=True)
    return base


def _redact_secret_bytes(payload: bytes, token: str) -> tuple[bytes, bool]:
    secret = token.encode("utf-8")
    if secret not in payload:
        return payload, False
    return payload.replace(secret, b"<redacted-copilot-token>"), True


def _create_network(
    config: OciRunConfig,
    *,
    name: str,
    role: str,
    internal: bool,
) -> dict[str, Any]:
    create = _run_bytes(
        _network_create_argv(
            config,
            name=name,
            role=role,
            internal=internal,
        ),
        timeout=120,
    )
    network_id = create.stdout.decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
        raise OciRuntimeError(f"docker returned an invalid {role} network ID")
    inspect = _inspect_network_optional(config.docker, name)
    if inspect is None:
        raise OciRuntimeError(f"{role} network disappeared after creation")
    _validate_network_inspect(
        inspect,
        config=config,
        name=name,
        role=role,
        internal=internal,
    )
    return inspect


def _wait_for_proxy_ready(
    config: OciRunConfig,
    *,
    proxy_container: str,
    timeout_seconds: float = 30,
) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + timeout_seconds
    last_stdout = b""
    last_stderr = b""
    while time.monotonic() < deadline:
        inspect = _inspect_container_optional(config.docker, proxy_container)
        if inspect is None:
            raise OciRuntimeError("proxy disappeared while waiting for readiness")
        state = inspect.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise OciRuntimeError("proxy exited before readiness")
        logs = _run_bytes(
            [config.docker, "logs", proxy_container],
            timeout=30,
            check=False,
        )
        last_stdout, last_stderr = logs.stdout, logs.stderr
        if logs.returncode != 0:
            raise OciRuntimeError("proxy logs could not be read")
        for raw_line in logs.stdout.splitlines():
            with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                row = json.loads(raw_line.decode("utf-8", errors="strict"))
                if isinstance(row, dict) and row.get("event") == "ready":
                    return logs.stdout, logs.stderr
        time.sleep(0.1)
    raise OciRuntimeError(
        "proxy readiness timed out: "
        f"stdout={last_stdout[-512:]!r} stderr={last_stderr[-512:]!r}"
    )


def _wait_for_auth_ready(
    config: OciRunConfig,
    *,
    container_id: str,
    timeout_seconds: float = 30,
) -> None:
    probe = (
        f"test -f {AUTH_READY} && "
        f"test -p {AUTH_TOKEN_FIFO} && "
        f"test -p {AUTH_START_FIFO} && "
        f"test -x {AUTH_ASKPASS}"
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _run_bytes(
            [config.docker, "exec", container_id, "sh", "-eu", "-c", probe],
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return
        inspect = _inspect_container_optional(config.docker, container_id)
        if inspect is None:
            raise OciRuntimeError("harness disappeared before auth bootstrap")
        state = inspect.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise OciRuntimeError("harness exited before auth bootstrap")
        time.sleep(0.1)
    raise OciRuntimeError("harness auth FIFO/helper did not become ready")


def _start_token_feeder(
    config: OciRunConfig,
    *,
    container_id: str,
    token: str,
) -> tuple[subprocess.Popen[bytes], list[str]]:
    argv = [
        config.docker,
        "exec",
        "--interactive",
        container_id,
        "/opt/atv/feed-token.sh",
    ]
    if token in "\n".join(argv):
        raise OciRuntimeError("OAuth token was placed in docker exec argv")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if process.stdin is None:
        process.kill()
        raise OciRuntimeError("docker exec token feeder has no stdin pipe")
    try:
        process.stdin.write(token.encode("utf-8") + b"\n")
        process.stdin.flush()
    finally:
        process.stdin.close()
        process.stdin = None
    if process.poll() not in {None, 0}:
        raise OciRuntimeError("docker exec token feeder failed before agent start")
    return process, argv


def _finish_token_feeder(
    process: subprocess.Popen[bytes],
    *,
    argv: Sequence[str],
    token: str,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=10)
        raise OciRuntimeError(
            "one-shot GITHUB_ASKPASS did not consume and remove the token FIFO"
        ) from exc
    stdout = process.stdout.read() if process.stdout is not None else b""
    stderr = process.stderr.read() if process.stderr is not None else b""
    if token.encode("utf-8") in stdout or token.encode("utf-8") in stderr:
        raise OciRuntimeError("token feeder emitted the OAuth token")
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1024:]
        raise OciRuntimeError(f"token feeder failed with {returncode}: {detail}")
    return {
        "argv": list(argv),
        "stdin_only": True,
        "token_in_argv": False,
        "token_in_environment": False,
        "returncode": returncode,
        "stdout_sha256": _sha256_bytes(stdout),
        "stderr_sha256": _sha256_bytes(stderr),
        "fifo_removed": True,
        "askpass_removed": True,
        "secret_env_vars": ["GITHUB_ASKPASS"],
    }


def _signal_agent_start(
    config: OciRunConfig,
    *,
    container_id: str,
) -> None:
    result = _run_bytes(
        [config.docker, "exec", container_id, "/opt/atv/start-agent.sh"],
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1024:]
        raise OciRuntimeError(f"agent start signal failed: {detail}")


def _wait_for_container_exit(
    config: OciRunConfig,
    *,
    container_id: str,
) -> tuple[int | None, bool, dict[str, Any]]:
    argv = [config.docker, "wait", container_id]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill = _run_bytes(
            [config.docker, "kill", "--signal", "KILL", container_id],
            timeout=60,
            check=False,
        )
        if kill.returncode != 0:
            raise OciRuntimeError("timed-out harness could not be killed")
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise OciRuntimeError(
                "docker wait did not finish after harness kill"
            ) from exc
    exit_code: int | None = None
    if process.returncode == 0:
        raw_exit_code = stdout.decode("ascii", errors="strict").strip()
        if re.fullmatch(r"-?[0-9]+", raw_exit_code):
            exit_code = int(raw_exit_code)
    if process.returncode != 0 or exit_code is None:
        detail = stderr.decode("utf-8", errors="replace")[-1024:]
        raise OciRuntimeError(f"docker wait returned invalid evidence: {detail}")
    return (
        exit_code,
        timed_out,
        {
            "argv": argv,
            "returncode": process.returncode,
            "stdout_sha256": _sha256_bytes(stdout),
            "stderr_sha256": _sha256_bytes(stderr),
            "timed_out": timed_out,
            "exit_code": exit_code,
        },
    )


def _container_logs(
    docker: str,
    container_id: str,
) -> tuple[bytes, bytes]:
    logs = _run_bytes(
        [docker, "logs", container_id],
        timeout=120,
        check=False,
    )
    if logs.returncode != 0:
        detail = logs.stderr.decode("utf-8", errors="replace")[-1024:]
        raise OciRuntimeError(f"docker logs failed: {detail}")
    return logs.stdout, logs.stderr


def _validate_proxy_logs(stdout: bytes, stderr: bytes) -> list[dict[str, Any]]:
    if stderr.strip():
        raise OciRuntimeError("CONNECT proxy wrote unexpected stderr")
    rows: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        try:
            row = json.loads(raw_line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OciRuntimeError("CONNECT proxy emitted non-JSON evidence") from exc
        if not isinstance(row, dict):
            raise OciRuntimeError("CONNECT proxy log row is not an object")
        rows.append(row)
    ready = [row for row in rows if row.get("event") == "ready"]
    if len(ready) != 1:
        raise OciRuntimeError("CONNECT proxy readiness evidence is not unique")
    if ready[0].get("allowlist") != sorted(COPILOT_MODEL_HOSTS):
        raise OciRuntimeError("CONNECT proxy logged an unexpected allowlist")
    for row in rows:
        if row.get("event") != "connect" or row.get("allowed") is not True:
            continue
        if row.get("host") not in COPILOT_MODEL_HOSTS or row.get("port") != 443:
            raise OciRuntimeError("CONNECT proxy allowed a non-Copilot destination")
    return rows


def _owned_container(
    inspect: Mapping[str, Any],
    *,
    config: OciRunConfig,
    role: str,
) -> bool:
    container_config = inspect.get("Config")
    labels = (
        container_config.get("Labels") if isinstance(container_config, dict) else None
    )
    if not isinstance(labels, dict):
        return False
    return (
        labels.get("org.atvbench.run-schema") == OCI_RUN_SCHEMA
        and labels.get("org.atvbench.run-id") == config.run_id
        and (
            labels.get("org.atvbench.harness") == config.harness
            if role == "harness"
            else labels.get("org.atvbench.role") == "connect-proxy"
        )
    )


def _owned_network(
    inspect: Mapping[str, Any],
    *,
    config: OciRunConfig,
    role: str,
) -> bool:
    labels = inspect.get("Labels")
    if not isinstance(labels, dict):
        return False
    return all(
        labels.get(key) == value
        for key, value in _network_labels(config, role=role).items()
    )


def _cleanup_run_resources(
    config: OciRunConfig,
    *,
    names: Mapping[str, str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    specs = (
        ("container", "harness", names["harness_container"]),
        ("container", "proxy", names["proxy_container"]),
        ("network", "internal", names["internal_network"]),
        ("network", "egress", names["egress_network"]),
    )
    all_confirmed = True
    for kind, role, name in specs:
        inspect_error: str | None = None
        try:
            before = (
                _inspect_container_optional(config.docker, name)
                if kind == "container"
                else _inspect_network_optional(config.docker, name)
            )
        except BaseException as exc:
            before = None
            inspect_error = f"{type(exc).__name__}: {exc}"
        owned = False
        if before is not None:
            owned = (
                _owned_container(before, config=config, role=role)
                if kind == "container"
                else _owned_network(before, config=config, role=role)
            )
        remove_result: subprocess.CompletedProcess[bytes] | None = None
        if before is not None and owned:
            command = (
                [config.docker, "container", "rm", "--force", name]
                if kind == "container"
                else [config.docker, "network", "rm", name]
            )
            remove_result = _run_bytes(command, timeout=120, check=False)
        try:
            after = (
                _inspect_container_optional(config.docker, name)
                if kind == "container"
                else _inspect_network_optional(config.docker, name)
            )
            confirmed_absent = after is None
        except BaseException as exc:
            confirmed_absent = False
            inspect_error = (
                f"{inspect_error}; {type(exc).__name__}: {exc}"
                if inspect_error
                else f"{type(exc).__name__}: {exc}"
            )
        all_confirmed = all_confirmed and confirmed_absent
        rows.append(
            {
                "kind": kind,
                "role": role,
                "name": name,
                "present_before": before is not None,
                "ownership_confirmed": owned if before is not None else None,
                "remove_exit_code": (
                    remove_result.returncode if remove_result is not None else None
                ),
                "remove_stdout_sha256": (
                    _sha256_bytes(remove_result.stdout)
                    if remove_result is not None
                    else None
                ),
                "remove_stderr_sha256": (
                    _sha256_bytes(remove_result.stderr)
                    if remove_result is not None
                    else None
                ),
                "confirmed_absent": confirmed_absent,
                "inspect_error": inspect_error,
            }
        )
    return {
        "resources": rows,
        "all_confirmed_absent": all_confirmed,
    }


def run_harness(
    config: OciRunConfig,
    *,
    goal: str,
    token: str,
) -> HarnessExecution:
    """Run one harness with stdin-only auth and proxy-only network egress."""

    base = _validate_run_config(config)
    evidence = config.evidence_dir.resolve()
    if not token or any(character in token for character in "\r\n\x00"):
        raise OciRuntimeError("Copilot token is empty or invalid")

    current_image_inspect = _inspect_image_optional(config.docker, config.image.tag)
    if current_image_inspect is None:
        raise OciRuntimeError("verified harness image is no longer present")
    _validate_image_inspect(
        current_image_inspect,
        harness=config.harness,
        tag=config.image.tag,
        labels=config.image.labels,
        platform=config.image.platform,
    )
    if current_image_inspect.get("Id") != config.image.image_id:
        raise OciRuntimeError(
            "verified harness image tag now resolves to another image"
        )
    current_proxy_image_inspect = _inspect_image_optional(
        config.docker,
        config.image.proxy.tag,
    )
    if current_proxy_image_inspect is None:
        raise OciRuntimeError("verified CONNECT proxy image is no longer present")
    _validate_proxy_image_inspect(
        current_proxy_image_inspect,
        tag=config.image.proxy.tag,
        labels=config.image.proxy.labels,
        platform=config.image.proxy.platform,
    )
    if current_proxy_image_inspect.get("Id") != config.image.proxy.image_id:
        raise OciRuntimeError("verified CONNECT proxy tag resolves to another image")
    _verify_proxy_image_content(
        config.docker,
        config.image.proxy.tag,
        expected_script_sha256=config.image.proxy.script_sha256,
    )
    _write_json_atomic(
        evidence / "image-inspect.pre-run.json",
        current_image_inspect,
    )
    _write_json_atomic(
        evidence / "proxy-image-inspect.pre-run.json",
        current_proxy_image_inspect,
    )
    pre_run_image_inspect_sha256 = _sha256_bytes(
        _canonical_json_bytes(current_image_inspect)
    )
    pre_run_proxy_image_inspect_sha256 = _sha256_bytes(
        _canonical_json_bytes(current_proxy_image_inspect)
    )

    names = _run_resource_names(config)
    preexisting = {
        "harness_container": _inspect_container_optional(
            config.docker,
            names["harness_container"],
        ),
        "proxy_container": _inspect_container_optional(
            config.docker,
            names["proxy_container"],
        ),
        "internal_network": _inspect_network_optional(
            config.docker,
            names["internal_network"],
        ),
        "egress_network": _inspect_network_optional(
            config.docker,
            names["egress_network"],
        ),
    }
    collisions = sorted(key for key, value in preexisting.items() if value is not None)
    if collisions:
        raise OciRuntimeError(
            "refusing to remove or reuse pre-existing run resources: "
            + ", ".join(collisions)
        )

    started = time.monotonic()
    primary_error: BaseException | None = None
    token_feeder: subprocess.Popen[bytes] | None = None
    token_feeder_argv: list[str] = []
    auth_evidence: dict[str, Any] | None = None
    cleanup_evidence: dict[str, Any] | None = None
    create_argv: list[str] = []
    proxy_create_argv: list[str] = []
    container_id: str | None = None
    proxy_container_id: str | None = None
    inspect_before: dict[str, Any] | None = None
    inspect_running: dict[str, Any] | None = None
    inspect_after: dict[str, Any] | None = None
    proxy_inspect_before: dict[str, Any] | None = None
    proxy_inspect_after: dict[str, Any] | None = None
    internal_network_inspect: dict[str, Any] | None = None
    egress_network_inspect: dict[str, Any] | None = None
    stdout = b""
    stderr = b""
    proxy_stdout = b""
    proxy_stderr = b""
    proxy_log_rows: list[dict[str, Any]] = []
    timed_out = False
    exit_code: int | None = None
    wait_evidence: dict[str, Any] | None = None

    try:
        internal_network_inspect = _create_network(
            config,
            name=names["internal_network"],
            role="internal",
            internal=True,
        )
        egress_network_inspect = _create_network(
            config,
            name=names["egress_network"],
            role="egress",
            internal=False,
        )
        _write_json_atomic(
            evidence / "network-internal.created.json",
            internal_network_inspect,
        )
        _write_json_atomic(
            evidence / "network-egress.created.json",
            egress_network_inspect,
        )

        proxy_create_argv = _proxy_container_create_argv(
            config,
            container_name=names["proxy_container"],
            egress_network=names["egress_network"],
        )
        if token in "\n".join(proxy_create_argv):
            raise OciRuntimeError("OAuth token was placed in proxy create argv")
        proxy_create = _run_bytes(proxy_create_argv, timeout=180)
        proxy_container_id = proxy_create.stdout.decode(
            "ascii", errors="strict"
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", proxy_container_id):
            raise OciRuntimeError("docker create returned an invalid proxy ID")
        connect = _run_bytes(
            [
                config.docker,
                "network",
                "connect",
                "--alias",
                PROXY_ALIAS,
                names["internal_network"],
                proxy_container_id,
            ],
            timeout=120,
            check=False,
        )
        if connect.returncode != 0:
            detail = connect.stderr.decode("utf-8", errors="replace")[-1024:]
            raise OciRuntimeError(f"proxy internal-network attach failed: {detail}")
        proxy_inspect_before = _inspect_container_optional(
            config.docker,
            proxy_container_id,
        )
        if proxy_inspect_before is None:
            raise OciRuntimeError("proxy disappeared before initial inspect")
        _validate_proxy_container_inspect(
            proxy_inspect_before,
            config=config,
            container_name=names["proxy_container"],
            internal_network=names["internal_network"],
            egress_network=names["egress_network"],
            token=token,
            expect_running=False,
        )
        _write_json_atomic(
            evidence / "proxy-inspect.before.json",
            _redact_inspect(proxy_inspect_before, token),
        )
        _write_json_atomic(
            evidence / "proxy-create-command.json",
            {
                "argv": proxy_create_argv,
                "sha256": _sha256_bytes(_canonical_json_bytes(proxy_create_argv)),
                "mount_count": 0,
                "secret_environment_count": 0,
            },
        )
        proxy_start = _run_bytes(
            [config.docker, "start", proxy_container_id],
            timeout=120,
            check=False,
        )
        if proxy_start.returncode != 0:
            detail = proxy_start.stderr.decode("utf-8", errors="replace")[-1024:]
            raise OciRuntimeError(f"CONNECT proxy failed to start: {detail}")
        proxy_stdout, proxy_stderr = _wait_for_proxy_ready(
            config,
            proxy_container=proxy_container_id,
        )
        proxy_log_rows = _validate_proxy_logs(proxy_stdout, proxy_stderr)
        _write_bytes_atomic(evidence / "proxy.ready.jsonl", proxy_stdout)
        proxy_inspect_after = _inspect_container_optional(
            config.docker,
            proxy_container_id,
        )
        if proxy_inspect_after is None:
            raise OciRuntimeError("proxy disappeared after readiness")
        _validate_proxy_container_inspect(
            proxy_inspect_after,
            config=config,
            container_name=names["proxy_container"],
            internal_network=names["internal_network"],
            egress_network=names["egress_network"],
            token=token,
            expect_running=True,
        )
        internal_network_inspect = _inspect_network_optional(
            config.docker,
            names["internal_network"],
        )
        egress_network_inspect = _inspect_network_optional(
            config.docker,
            names["egress_network"],
        )
        if internal_network_inspect is None or egress_network_inspect is None:
            raise OciRuntimeError("per-run network disappeared before harness start")
        _validate_network_inspect(
            internal_network_inspect,
            config=config,
            name=names["internal_network"],
            role="internal",
            internal=True,
        )
        _validate_network_inspect(
            egress_network_inspect,
            config=config,
            name=names["egress_network"],
            role="egress",
            internal=False,
        )
        _validate_network_membership(
            internal_network_inspect,
            egress_network_inspect,
            internal_expected={names["proxy_container"]},
            egress_expected={names["proxy_container"]},
        )

        create_argv = _container_create_argv(
            config,
            goal=goal,
            container_name=names["harness_container"],
            internal_network=names["internal_network"],
        )
        if token in "\n".join(create_argv):
            raise OciRuntimeError("OAuth token was placed in docker create argv")
        create = _run_bytes(create_argv, timeout=180)
        container_id = create.stdout.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise OciRuntimeError("docker create returned an invalid container ID")
        inspect_before = _inspect_container_optional(config.docker, container_id)
        if inspect_before is None:
            raise OciRuntimeError("container disappeared before initial inspect")
        _validate_container_inspect(
            inspect_before,
            config=config,
            container_name=names["harness_container"],
            internal_network=names["internal_network"],
            token=token,
            expect_running=False,
        )
        _write_json_atomic(
            evidence / "container-inspect.before.json",
            _redact_inspect(inspect_before, token),
        )
        _write_json_atomic(
            evidence / "create-command.json",
            {
                "argv": create_argv,
                "sha256": _sha256_bytes(_canonical_json_bytes(create_argv)),
                "env_file_used": False,
                "token_in_argv": False,
                "secret_env_vars": ["GITHUB_ASKPASS"],
            },
        )
        start = _run_bytes(
            [config.docker, "start", container_id],
            timeout=120,
            check=False,
        )
        if start.returncode != 0:
            detail = start.stderr.decode("utf-8", errors="replace")[-1024:]
            raise OciRuntimeError(f"harness bootstrap failed to start: {detail}")
        _wait_for_auth_ready(config, container_id=container_id)
        inspect_running = _inspect_container_optional(config.docker, container_id)
        if inspect_running is None:
            raise OciRuntimeError("container disappeared before token delivery")
        _validate_container_inspect(
            inspect_running,
            config=config,
            container_name=names["harness_container"],
            internal_network=names["internal_network"],
            token=token,
            expect_running=True,
        )
        internal_network_inspect = _inspect_network_optional(
            config.docker,
            names["internal_network"],
        )
        egress_network_inspect = _inspect_network_optional(
            config.docker,
            names["egress_network"],
        )
        if internal_network_inspect is None or egress_network_inspect is None:
            raise OciRuntimeError("per-run network disappeared before token delivery")
        _validate_network_membership(
            internal_network_inspect,
            egress_network_inspect,
            internal_expected={
                names["proxy_container"],
                names["harness_container"],
            },
            egress_expected={names["proxy_container"]},
        )

        token_feeder, token_feeder_argv = _start_token_feeder(
            config,
            container_id=container_id,
            token=token,
        )
        _signal_agent_start(config, container_id=container_id)
        auth_evidence = _finish_token_feeder(
            token_feeder,
            argv=token_feeder_argv,
            token=token,
        )
        token_feeder = None
        _write_json_atomic(evidence / "auth-transport.json", auth_evidence)

        exit_code, timed_out, wait_evidence = _wait_for_container_exit(
            config,
            container_id=container_id,
        )
        stdout, stderr = _container_logs(config.docker, container_id)
        inspect_after = _inspect_container_optional(config.docker, container_id)
        if inspect_after is None:
            raise OciRuntimeError("container disappeared before final inspect evidence")
        _validate_container_inspect(
            inspect_after,
            config=config,
            container_name=names["harness_container"],
            internal_network=names["internal_network"],
            token=token,
            expect_running=False,
        )
        state_exit_code = inspect_after.get("State", {}).get("ExitCode")
        if state_exit_code is None or int(state_exit_code) != exit_code:
            raise OciRuntimeError("docker wait and inspect exit codes disagree")
        _write_json_atomic(
            evidence / "container-inspect.after.json",
            _redact_inspect(inspect_after, token),
        )
        _write_json_atomic(evidence / "wait.json", wait_evidence)

        proxy_inspect_after = _inspect_container_optional(
            config.docker,
            proxy_container_id,
        )
        if proxy_inspect_after is None:
            raise OciRuntimeError("proxy disappeared before final evidence")
        _validate_proxy_container_inspect(
            proxy_inspect_after,
            config=config,
            container_name=names["proxy_container"],
            internal_network=names["internal_network"],
            egress_network=names["egress_network"],
            token=token,
            expect_running=True,
        )
        proxy_stdout, proxy_stderr = _container_logs(
            config.docker,
            proxy_container_id,
        )
        proxy_log_rows = _validate_proxy_logs(proxy_stdout, proxy_stderr)
        _write_bytes_atomic(evidence / "proxy.jsonl", proxy_stdout)
        _write_bytes_atomic(evidence / "proxy.stderr.log", proxy_stderr)
        _write_json_atomic(
            evidence / "proxy-inspect.after.json",
            _redact_inspect(proxy_inspect_after, token),
        )

        internal_network_inspect = _inspect_network_optional(
            config.docker,
            names["internal_network"],
        )
        egress_network_inspect = _inspect_network_optional(
            config.docker,
            names["egress_network"],
        )
        if internal_network_inspect is None or egress_network_inspect is None:
            raise OciRuntimeError("per-run network disappeared before final evidence")
        _validate_network_inspect(
            internal_network_inspect,
            config=config,
            name=names["internal_network"],
            role="internal",
            internal=True,
        )
        _validate_network_inspect(
            egress_network_inspect,
            config=config,
            name=names["egress_network"],
            role="egress",
            internal=False,
        )
        _validate_network_membership(
            internal_network_inspect,
            egress_network_inspect,
            # Docker removes an exited container from the network's active
            # Containers map before the container object itself is removed.
            internal_expected={names["proxy_container"]},
            egress_expected={names["proxy_container"]},
        )
        _write_json_atomic(
            evidence / "network-internal.final.json",
            internal_network_inspect,
        )
        _write_json_atomic(
            evidence / "network-egress.final.json",
            egress_network_inspect,
        )
    except BaseException as exc:
        primary_error = exc
    finally:
        if token_feeder is not None and token_feeder.poll() is None:
            token_feeder.kill()
            with contextlib.suppress(BaseException):
                token_feeder.wait(timeout=10)
            feeder_error = OciRuntimeError(
                "token feeder required forced termination; FIFO cleanup is unproven"
            )
            primary_error = (
                feeder_error
                if primary_error is None
                else OciRuntimeError(f"{primary_error}; {feeder_error}")
            )
        if proxy_container_id is not None:
            try:
                proxy_stdout, proxy_stderr = _container_logs(
                    config.docker,
                    proxy_container_id,
                )
                proxy_log_rows = _validate_proxy_logs(
                    proxy_stdout,
                    proxy_stderr,
                )
                _write_bytes_atomic(evidence / "proxy.finally.jsonl", proxy_stdout)
                _write_bytes_atomic(
                    evidence / "proxy.finally.stderr.log",
                    proxy_stderr,
                )
            except BaseException as exc:
                log_error = OciRuntimeError(
                    "final CONNECT proxy logs were not captured and validated: "
                    f"{type(exc).__name__}: {exc}"
                )
                primary_error = (
                    log_error
                    if primary_error is None
                    else OciRuntimeError(f"{primary_error}; {log_error}")
                )
        if container_id is not None:
            try:
                pending_stdout, pending_stderr = _container_logs(
                    config.docker,
                    container_id,
                )
                _write_bytes_atomic(
                    evidence / "container.finally.stdout.log",
                    pending_stdout,
                )
                _write_bytes_atomic(
                    evidence / "container.finally.stderr.log",
                    pending_stderr,
                )
            except BaseException as exc:
                log_error = OciRuntimeError(
                    f"final harness logs were not captured: {type(exc).__name__}: {exc}"
                )
                primary_error = (
                    log_error
                    if primary_error is None
                    else OciRuntimeError(f"{primary_error}; {log_error}")
                )
        for filename, reference, is_proxy in (
            ("container-inspect.finally.json", container_id, False),
            ("proxy-inspect.finally.json", proxy_container_id, True),
        ):
            if reference is None:
                continue
            with contextlib.suppress(BaseException):
                pending = _inspect_container_optional(config.docker, reference)
                if pending is not None:
                    if is_proxy:
                        _assert_no_token_metadata(
                            pending,
                            token=token,
                            context="proxy final inspect",
                        )
                    else:
                        _assert_no_token_metadata(
                            pending,
                            token=token,
                            context="harness final inspect",
                        )
                    _write_json_atomic(evidence / filename, pending)
        try:
            cleanup_evidence = _cleanup_run_resources(config, names=names)
        except BaseException as exc:
            cleanup_evidence = {
                "resources": [],
                "all_confirmed_absent": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        _write_json_atomic(evidence / "cleanup.json", cleanup_evidence)
        if not cleanup_evidence.get("all_confirmed_absent"):
            cleanup_error = OciRuntimeError(
                "harness/proxy/network cleanup was not fully confirmed"
            )
            primary_error = (
                cleanup_error
                if primary_error is None
                else OciRuntimeError(f"{primary_error}; {cleanup_error}")
            )

    if primary_error is not None:
        if isinstance(primary_error, OciRuntimeError):
            raise primary_error
        raise OciRuntimeError(
            f"OCI harness execution failed: {type(primary_error).__name__}: "
            f"{primary_error}"
        ) from primary_error
    if (
        inspect_before is None
        or inspect_after is None
        or proxy_inspect_after is None
        or auth_evidence is None
        or cleanup_evidence is None
        or exit_code is None
    ):
        raise OciRuntimeError("OCI execution completed without mandatory evidence")

    duration = time.monotonic() - started
    stdout, stdout_redacted = _redact_secret_bytes(stdout, token)
    stderr, stderr_redacted = _redact_secret_bytes(stderr, token)
    proxy_stdout, proxy_stdout_redacted = _redact_secret_bytes(proxy_stdout, token)
    proxy_stderr, proxy_stderr_redacted = _redact_secret_bytes(proxy_stderr, token)
    if proxy_stdout_redacted or proxy_stderr_redacted:
        raise OciRuntimeError("CONNECT proxy logs contained the OAuth token")
    diff = capture_repo_diff(str(config.workspace), base)
    if timed_out:
        status = "timeout"
    elif exit_code != 0:
        status = "error"
    elif diff.strip():
        status = "ok"
    else:
        status = "no_edit"
    _write_bytes_atomic(evidence / "stdout.jsonl", stdout)
    _write_bytes_atomic(evidence / "stderr.log", stderr)
    _write_bytes_atomic(evidence / "workspace.diff", diff.encode("utf-8"))
    _write_bytes_atomic(evidence / "proxy.jsonl", proxy_stdout)
    _write_bytes_atomic(evidence / "proxy.stderr.log", proxy_stderr)
    artifacts = {
        "stdout.jsonl": {
            "sha256": _sha256_bytes(stdout),
            "bytes": len(stdout),
            "secret_redaction_applied": stdout_redacted,
        },
        "stderr.log": {
            "sha256": _sha256_bytes(stderr),
            "bytes": len(stderr),
            "secret_redaction_applied": stderr_redacted,
        },
        "workspace.diff": {
            "sha256": _sha256_bytes(diff.encode("utf-8")),
            "bytes": len(diff.encode("utf-8")),
        },
        "proxy.jsonl": {
            "sha256": _sha256_bytes(proxy_stdout),
            "bytes": len(proxy_stdout),
            "rows": len(proxy_log_rows),
            "secret_redaction_applied": False,
        },
        "proxy.stderr.log": {
            "sha256": _sha256_bytes(proxy_stderr),
            "bytes": len(proxy_stderr),
            "secret_redaction_applied": False,
        },
    }
    inspect_before_sha = _sha256_bytes(
        _canonical_json_bytes(_redact_inspect(inspect_before, token))
    )
    inspect_after_sha = _sha256_bytes(
        _canonical_json_bytes(_redact_inspect(inspect_after, token))
    )
    proxy_inspect_sha = _sha256_bytes(
        _canonical_json_bytes(_redact_inspect(proxy_inspect_after, token))
    )
    manifest = {
        "schema": OCI_RUN_SCHEMA,
        "runtime_schema": OCI_RUNTIME_SCHEMA,
        "rankable": False,
        "run_id": config.run_id,
        "harness": config.harness,
        "model": config.model,
        "max_ai_credits": config.max_ai_credits,
        "timeout_seconds": config.timeout_seconds,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": duration,
        "workspace_base": base,
        "image": config.image.evidence(),
        "pre_run_image_inspect_sha256": pre_run_image_inspect_sha256,
        "pre_run_proxy_image_inspect_sha256": (pre_run_proxy_image_inspect_sha256),
        "container": {
            "name": names["harness_container"],
            "id": container_id,
            "inspect_before_sha256": inspect_before_sha,
            "inspect_after_sha256": inspect_after_sha,
            "removed": True,
            "removal_confirmed": True,
        },
        "proxy": {
            "name": names["proxy_container"],
            "id": proxy_container_id,
            "inspect_after_sha256": proxy_inspect_sha,
            "image": config.image.proxy.evidence(),
            "removed": True,
            "removal_confirmed": True,
        },
        "auth": auth_evidence,
        "security": {
            "one_named_harness_container_per_run": True,
            "only_workspace_bind_mounted": True,
            "sibling_workspace_mounted": False,
            "hidden_grader_mounted": False,
            "docker_socket_mounted": False,
            "read_only_root": True,
            "non_root_user": CONTAINER_USER,
            "cap_drop_all": True,
            "no_new_privileges": True,
            "bounded_cpu_memory_pids_tmpfs": True,
            "copilot_native_module_cache_exec_tmpfs": "/home/runner",
            "token_in_docker_env_config_or_argv": False,
            "token_transport": "docker-exec-stdin-to-one-shot-fifo",
            "github_askpass_removed_before_agent_completion": True,
            "github_askpass_stripped_from_tool_processes": True,
            "host_aliases_masked_to_loopback": [
                "host.docker.internal",
                "gateway.docker.internal",
            ],
            "cleanup_confirmed_before_return": True,
        },
        "network": {
            "policy": NETWORK_POLICY,
            "harness_network": {
                "name": names["internal_network"],
                "internal": True,
                "direct_egress": False,
            },
            "proxy_egress_network": {
                "name": names["egress_network"],
                "internal": False,
                "attached_containers": [names["proxy_container"]],
            },
            "proxy_alias": PROXY_ALIAS,
            "proxy_port": PROXY_PORT,
            "connect_allowlist": list(COPILOT_MODEL_HOSTS),
            "explicit_denylist": list(EXPLICIT_PROXY_DENY_HOSTS),
            "all_other_hosts_denied": True,
            "cleanup_confirmed": cleanup_evidence["all_confirmed_absent"],
        },
        "cleanup": cleanup_evidence,
        "artifacts": artifacts,
    }
    _write_json_atomic(evidence / "oci-run.json", manifest)
    return HarnessExecution(
        status=status,
        exit_code=exit_code,
        duration_seconds=duration,
        stdout=stdout,
        stderr=stderr,
        diff=diff,
    )


__all__ = [
    "CopilotPackageIdentity",
    "GitSourceIdentity",
    "HarnessExecution",
    "OciBuildConfig",
    "OciImage",
    "OciProxyImage",
    "OciRunConfig",
    "OciRuntimeError",
    "ResourceLimits",
    "build_or_reuse_images",
    "inspect_copilot_package",
    "inspect_source",
    "run_harness",
    "tree_sha256",
]
