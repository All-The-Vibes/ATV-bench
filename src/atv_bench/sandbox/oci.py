"""Digest-pinned OCI trial execution for Docker- and Podman-compatible engines.

The runner deliberately exposes a small engine protocol so security and lifecycle
behavior can be tested without a daemon.  The CLI implementation never invokes a
host shell.  It runs a harness container first, destroys its gateway capability and
container, and only then starts a separate networkless grader with hidden inputs.

This module records controller-observed runtime evidence.  ``runtime_verified`` means
the engine's image/container inspection matched the requested policy; it is not a
signed attestation and never implies ``official_verified``.
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from atv_bench.capture import CaptureRejected, read_confined_regular_file
from atv_bench.eval import (
    GradingTrustTier,
    RunnerLifecycleReceipt,
    TaskPackage,
    TrialAttempt,
)
from atv_bench.protocol import (
    ProtocolError,
    ProtocolSession,
    ProtocolTranscript,
    canonical_json_bytes,
    sha256_bytes,
)
from atv_bench.security import CredentialBroker, OpaqueTrialHandle

RUNNER_SCHEMA = "atv.oci-runner-evidence/v1"
RUNNER_ID = "atv-oci-runner/v1"
DEFAULT_USER = "65534:65534"
DEFAULT_ENGINE_TIMEOUT_SECONDS = 30.0
MAX_ENGINE_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_INSPECT_BYTES = 4 * 1024 * 1024
_IMAGE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._/-]+(?::[0-9]+/[A-Za-z0-9._/-]+)?)"
    r"@sha256:(?P<digest>[0-9a-f]{64})$"
)
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VOLUME_SUBPATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTAINER_NOT_FOUND_MARKERS = (
    "no such object",
    "no such container",
    "no container with name or id",
)
_VOLUME_NOT_FOUND_MARKERS = (
    "no such volume",
    "no volume with name or id",
)
_HARD_QUOTA_ENFORCEMENT = "single-size-limited-tmpfs-volume-subpaths"
_HARD_QUOTA_MINIMUM_BYTES = 4096
_HARD_QUOTA_HARNESS_ROOT = "/atv-harness-quota"
_HARD_QUOTA_GRADER_ROOT = "/atv-grader-quota"
_HARD_QUOTA_HARNESS_SUBPATHS = ("workspace", "artifacts", "tmp")
_HARD_QUOTA_GRADER_SUBPATHS = ("output", "tmp")
_FORBIDDEN_NETWORKS = {
    "",
    "bridge",
    "default",
    "host",
    "none",
    "slirp4netns",
    "pasta",
}
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


class OciRunnerError(RuntimeError):
    """Base class for OCI runner failures."""


class ImageReferenceError(OciRunnerError, ValueError):
    """An OCI image was not pinned by immutable sha256 digest."""


class NetworkPolicyError(OciRunnerError, ValueError):
    """A network policy was broad, mutable, or internally inconsistent."""


class ImageRolePolicyError(OciRunnerError, ValueError):
    """Task, harness, and execution image roles are not credibly bound."""


class EngineUnavailableError(OciRunnerError):
    """No usable OCI engine client/daemon is available."""


class OciTrialStatus(str, enum.Enum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    NONZERO_EXIT = "nonzero_exit"
    PROTOCOL_ERROR = "protocol_error"
    INVALID_OUTPUT = "invalid_output"
    POLICY_MISMATCH = "policy_mismatch"
    STORAGE_FAILED = "storage_failed"
    CLEANUP_FAILED = "cleanup_failed"
    GRADER_FAILED = "grader_failed"
    ENGINE_ERROR = "engine_error"


class OciNetworkMode(str, enum.Enum):
    NONE = "none"
    MODEL_GATEWAY_ONLY = "model-gateway-only"


class OciTrack(str, enum.Enum):
    CONTROLLED = "controlled"
    SYSTEMS = "systems"


class OciStorageMode(str, enum.Enum):
    AUTO = "auto"
    HARD_QUOTA = "hard-quota"
    BIND_MONITOR = "bind-monitor"


class CleanupStatus(str, enum.Enum):
    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ContainerPhase(str, enum.Enum):
    VOLUME_KEEPER = "volume-keeper"
    SEED = "seed"
    HARNESS = "harness"
    OUTPUT_CAPTURE = "output-capture"
    GRADER = "grader"


@dataclasses.dataclass(frozen=True, slots=True)
class DigestPinnedImage:
    reference: str
    name: str
    digest: str

    @classmethod
    def parse(cls, value: str) -> "DigestPinnedImage":
        if not isinstance(value, str) or not value or value != value.strip():
            raise ImageReferenceError("image reference must be non-empty and trimmed")
        if any(character in value for character in ("\x00", "\r", "\n", " ", "\t")):
            raise ImageReferenceError("image reference contains unsafe whitespace")
        match = _IMAGE_RE.fullmatch(value)
        if match is None:
            raise ImageReferenceError(
                "image must use name@sha256:<64 lowercase hex characters>"
            )
        name = match.group("name")
        # A registry port is allowed before the final slash. A tag on the final
        # repository component remains mutable and is rejected even with @digest.
        if ":" in name.rsplit("/", 1)[-1]:
            raise ImageReferenceError("tag-qualified images are not accepted")
        return cls(reference=value, name=name, digest=f"sha256:{match.group('digest')}")

    def to_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "name": self.name,
            "requested_digest": self.digest,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class OciResourcePolicy:
    wall_time_ms: int
    memory_bytes: int
    cpu_millis: int
    pids_limit: int
    storage_bytes: int
    stdout_bytes: int
    stderr_bytes: int
    artifact_bytes: int
    tmpfs_bytes: int

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")

    @classmethod
    def from_budget_limits(
        cls,
        limits: Mapping[str, Any],
        *,
        cpu_millis: int = 1_000,
    ) -> "OciResourcePolicy":
        storage = int(limits["storage_bytes"])
        return cls(
            wall_time_ms=int(limits["wall_time_ms"]),
            memory_bytes=int(limits["memory_bytes"]),
            cpu_millis=cpu_millis,
            pids_limit=int(limits["pids"]),
            storage_bytes=storage,
            stdout_bytes=int(limits["stdout_bytes"]),
            stderr_bytes=int(limits["stderr_bytes"]),
            artifact_bytes=int(limits["artifact_bytes"]),
            tmpfs_bytes=min(storage, 64 * 1024 * 1024),
        )

    @property
    def cpus_arg(self) -> str:
        whole, remainder = divmod(self.cpu_millis, 1_000)
        return f"{whole}.{remainder:03d}".rstrip("0").rstrip(".")

    def to_dict(self) -> dict[str, int]:
        return {
            "wall_time_ms": self.wall_time_ms,
            "memory_bytes": self.memory_bytes,
            "cpu_millis": self.cpu_millis,
            "pids_limit": self.pids_limit,
            "storage_bytes": self.storage_bytes,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "artifact_bytes": self.artifact_bytes,
            "tmpfs_bytes": self.tmpfs_bytes,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class OciNetworkPolicy:
    mode: OciNetworkMode
    network_name: str | None = None
    dns_servers: tuple[str, ...] = ()
    allowed_gateway_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, OciNetworkMode):
            object.__setattr__(self, "mode", OciNetworkMode(self.mode))
        object.__setattr__(self, "dns_servers", tuple(self.dns_servers))
        object.__setattr__(
            self,
            "allowed_gateway_identities",
            tuple(self.allowed_gateway_identities),
        )
        if self.mode is OciNetworkMode.NONE:
            if (
                self.network_name is not None
                or self.dns_servers
                or self.allowed_gateway_identities
            ):
                raise NetworkPolicyError(
                    "network none cannot name a network, DNS server, or gateway"
                )
            return
        if self.mode is not OciNetworkMode.MODEL_GATEWAY_ONLY:
            raise NetworkPolicyError("unsupported OCI network mode")
        name = self.network_name
        if (
            not isinstance(name, str)
            or name.lower() in _FORBIDDEN_NETWORKS
            or name.startswith(("container:", "service:"))
            or name.startswith("-")
            or any(character in name for character in ("\x00", "\r", "\n", " ", "\t"))
        ):
            raise NetworkPolicyError(
                "model-gateway-only requires a declared pre-created private network"
            )
        if self.dns_servers:
            raise NetworkPolicyError(
                "model-gateway-only forbids broad/custom DNS; use engine-internal "
                "name resolution on the isolated network"
            )
        if not self.allowed_gateway_identities or any(
            not isinstance(identity, str)
            or not identity
            or identity.startswith("-")
            or any(
                character in identity
                for character in ("\x00", "\r", "\n", " ", "\t")
            )
            for identity in self.allowed_gateway_identities
        ):
            raise NetworkPolicyError(
                "model-gateway-only requires explicit allowed gateway identities"
            )
        if len(set(self.allowed_gateway_identities)) != len(
            self.allowed_gateway_identities
        ):
            raise NetworkPolicyError("allowed gateway identities must be unique")

    @classmethod
    def none(cls) -> "OciNetworkPolicy":
        return cls(OciNetworkMode.NONE)

    @classmethod
    def model_gateway_only(
        cls,
        network_name: str,
        *,
        allowed_gateway_identities: Sequence[str],
    ) -> "OciNetworkPolicy":
        return cls(
            OciNetworkMode.MODEL_GATEWAY_ONLY,
            network_name=network_name,
            dns_servers=(),
            allowed_gateway_identities=tuple(allowed_gateway_identities),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "network_name": self.network_name,
            "dns_servers": list(self.dns_servers),
            "allowed_gateway_identities": list(
                self.allowed_gateway_identities
            ),
            "dns_policy": (
                "disabled"
                if self.mode is OciNetworkMode.NONE
                else "engine-internal-only"
            ),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class MountSpec:
    source: Path | str
    destination: str
    read_only: bool
    mount_type: str = "bind"
    volume_subpath: str | None = None
    volume_nocopy: bool = False

    def __post_init__(self) -> None:
        if self.mount_type not in {"bind", "volume"}:
            raise ValueError("mount_type must be 'bind' or 'volume'")
        if self.mount_type == "bind":
            if self.volume_subpath is not None or self.volume_nocopy:
                raise ValueError("bind mounts cannot use volume-only options")
            source: Path | str = Path(self.source).resolve(strict=True)
            if any(
                character in str(source) for character in ("\x00", "\r", "\n", ",")
            ):
                raise ValueError("mount source contains an unsafe character")
        else:
            source = str(self.source)
            if _VOLUME_NAME_RE.fullmatch(source) is None:
                raise ValueError("volume mount source is unsafe")
            if self.volume_subpath is not None and (
                _VOLUME_SUBPATH_RE.fullmatch(self.volume_subpath) is None
            ):
                raise ValueError("volume subpath must be one safe relative component")
            if not isinstance(self.volume_nocopy, bool):
                raise TypeError("volume_nocopy must be a boolean")
        destination = PurePosixPath(self.destination)
        if (
            not destination.is_absolute()
            or ".." in destination.parts
            or self.destination == "/"
            or any(character in self.destination for character in ("\x00", "\r", "\n", ","))
        ):
            raise ValueError("mount destination must be a safe absolute container path")
        object.__setattr__(self, "source", source)

    @classmethod
    def volume(
        cls,
        name: str,
        destination: str,
        read_only: bool,
        *,
        subpath: str | None = None,
        no_copy: bool = False,
    ) -> "MountSpec":
        return cls(
            source=name,
            destination=destination,
            read_only=read_only,
            mount_type="volume",
            volume_subpath=subpath,
            volume_nocopy=no_copy,
        )

    @property
    def is_bind(self) -> bool:
        return self.mount_type == "bind"

    @property
    def host_path(self) -> Path | None:
        return self.source if isinstance(self.source, Path) else None

    def argv_value(self) -> str:
        value = (
            f"type={self.mount_type},src={self.source},dst={self.destination}"
        )
        if self.read_only:
            value += ",readonly"
        if self.volume_subpath is not None:
            value += f",volume-subpath={self.volume_subpath}"
        if self.volume_nocopy:
            value += ",volume-nocopy"
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.mount_type,
            "source": str(self.source),
            "destination": self.destination,
            "read_only": self.read_only,
            "volume_subpath": self.volume_subpath,
            "volume_nocopy": self.volume_nocopy,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ContainerSpec:
    phase: ContainerPhase
    name: str
    image: DigestPinnedImage
    command: tuple[str, ...]
    mounts: tuple[MountSpec, ...]
    resources: OciResourcePolicy
    network: OciNetworkPolicy
    working_directory: str
    user: str = DEFAULT_USER
    env_file: Path | None = None
    environment_names: tuple[str, ...] = ()
    detached: bool = False
    stdin_open: bool = False
    tmpfs_mounts: tuple[tuple[str, str], ...] = ()
    ipc_mode: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "environment_names", tuple(self.environment_names))
        object.__setattr__(self, "tmpfs_mounts", tuple(self.tmpfs_mounts))
        if _CONTAINER_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("container name is unsafe")
        if not self.command or any(
            not isinstance(item, str) or "\x00" in item for item in self.command
        ):
            raise ValueError("container command must be a non-empty argv vector")
        if self.user in {"", "0", "0:0", "root", "root:root"}:
            raise ValueError("container user must be non-root")
        workdir = PurePosixPath(self.working_directory)
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise ValueError("container working directory must be absolute and confined")
        destinations = [mount.destination for mount in self.mounts]
        if len(destinations) != len(set(destinations)):
            raise ValueError("container mount destinations must be unique")
        if self.ipc_mode != "none":
            raise ValueError("container IPC must be disabled")
        for name in self.environment_names:
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise ValueError(f"unsafe container environment name: {name!r}")
        tmpfs_destinations: set[str] = set(destinations) | {"/tmp"}
        for destination, options in self.tmpfs_mounts:
            path = PurePosixPath(destination)
            if (
                not path.is_absolute()
                or destination == "/"
                or ".." in path.parts
                or any(
                    character in destination + options
                    for character in ("\x00", "\r", "\n")
                )
            ):
                raise ValueError("container tmpfs mount is unsafe")
            if destination in tmpfs_destinations:
                raise ValueError(
                    "container mount and tmpfs destinations must be unique"
                )
            tmpfs_destinations.add(destination)
        if self.env_file is not None:
            path = Path(self.env_file).resolve(strict=True)
            if not path.is_file():
                raise ValueError("container env_file must be a regular file")
            object.__setattr__(self, "env_file", path)

    def policy_dict(self) -> dict[str, Any]:
        writable_mounts = [mount for mount in self.mounts if not mount.read_only]
        writable_volume_mounts = [
            mount for mount in writable_mounts if mount.mount_type == "volume"
        ]
        hard_quota_requested = bool(writable_mounts) and (
            len(writable_mounts) == len(writable_volume_mounts)
            and len({str(mount.source) for mount in writable_volume_mounts}) == 1
            and all(
                mount.volume_subpath is not None and mount.volume_nocopy
                for mount in writable_volume_mounts
            )
            and any(mount.destination == "/tmp" for mount in writable_volume_mounts)
        )
        return {
            "phase": self.phase.value,
            "name": self.name,
            "image": self.image.to_dict(),
            "command": list(self.command),
            "mounts": [mount.to_dict() for mount in self.mounts],
            "resources": self.resources.to_dict(),
            "network": self.network.to_dict(),
            "working_directory": self.working_directory,
            "user": self.user,
            "read_only_rootfs": True,
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "storage_enforcement": {
                "aggregate": (
                    _HARD_QUOTA_ENFORCEMENT
                    if hard_quota_requested
                    else "best-effort-controller-polling"
                ),
                "hard_aggregate_quota_requested": hard_quota_requested,
                "hard_aggregate_quota": hard_quota_requested,
                "per_file_rlimit_fsize": True,
                "official_eligible_if_runtime_verified_and_cleaned": (
                    hard_quota_requested
                ),
            },
            "environment_names": list(self.environment_names),
            "env_file": str(self.env_file) if self.env_file else None,
            "detached": self.detached,
            "stdin_open": self.stdin_open,
            "ipc_mode": self.ipc_mode,
            "tmpfs_mounts": {
                destination: options
                for destination, options in self.tmpfs_mounts
            },
        }


def build_run_argv(engine_executable: str, spec: ContainerSpec) -> tuple[str, ...]:
    """Build a Docker/Podman-compatible shell-free ``run`` argv."""

    argv = [
        engine_executable,
        "run",
        "--pull",
        "never",
        "--name",
        spec.name,
        "--user",
        spec.user,
        "--workdir",
        spec.working_directory,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--ipc",
        spec.ipc_mode,
        "--pids-limit",
        str(spec.resources.pids_limit),
        "--memory",
        str(spec.resources.memory_bytes),
        "--cpus",
        spec.resources.cpus_arg,
        "--ulimit",
        f"fsize={spec.resources.storage_bytes}:{spec.resources.storage_bytes}",
        "--ulimit",
        "core=0:0",
    ]
    if not any(mount.destination == "/tmp" for mount in spec.mounts):
        argv.extend(
            [
                "--tmpfs",
                (
                    "/tmp:rw,noexec,nosuid,nodev,"
                    f"size={spec.resources.tmpfs_bytes},mode=1777"
                ),
            ]
        )
    if spec.detached:
        argv.append("--detach")
    if spec.stdin_open:
        argv.append("--interactive")
    for destination, options in spec.tmpfs_mounts:
        argv.extend(["--tmpfs", f"{destination}:{options}"])
    if spec.network.mode is OciNetworkMode.NONE:
        argv.extend(["--network", "none"])
    else:
        assert spec.network.network_name is not None
        argv.extend(["--network", spec.network.network_name])
        for address in spec.network.dns_servers:
            argv.extend(["--dns", address])
    if spec.env_file is not None:
        argv.extend(["--env-file", str(spec.env_file)])
    for mount in spec.mounts:
        argv.extend(["--mount", mount.argv_value()])
    argv.append(spec.image.reference)
    argv.extend(spec.command)
    return tuple(argv)


@dataclasses.dataclass(frozen=True, slots=True)
class VolumeSpec:
    purpose: str
    name: str
    size_bytes: int
    subpaths: tuple[str, ...] = ()
    driver: str = "local"
    filesystem_type: str = "tmpfs"
    device: str = "tmpfs"

    def __post_init__(self) -> None:
        object.__setattr__(self, "subpaths", tuple(self.subpaths))
        if not self.purpose or any(
            character in self.purpose for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("volume purpose must be non-empty and safe")
        if _VOLUME_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("volume name is unsafe")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise TypeError("volume size_bytes must be an integer")
        if self.size_bytes < _HARD_QUOTA_MINIMUM_BYTES:
            raise ValueError(
                "hard-quota volume size_bytes must be at least one memory page"
            )
        if (
            not self.subpaths
            or len(self.subpaths) != len(set(self.subpaths))
            or any(_VOLUME_SUBPATH_RE.fullmatch(value) is None for value in self.subpaths)
        ):
            raise ValueError(
                "hard-quota volumes require unique safe relative subpaths"
            )
        if self.driver != "local":
            raise ValueError("hard-quota volumes require the local driver")
        if self.filesystem_type != "tmpfs" or self.device != "tmpfs":
            raise ValueError("hard-quota volumes require tmpfs type and device")

    @property
    def mount_options(self) -> str:
        # The explicit owner/mode keeps the volume writable by the fixed
        # non-root runtime user even when several local-driver tmpfs volumes
        # are held open concurrently by the trusted keeper container.
        return (
            f"size={self.size_bytes},mode=0700,"
            f"uid={DEFAULT_USER.split(':', 1)[0]},gid={DEFAULT_USER.split(':', 1)[1]}"
        )

    def create_argv(self, engine_executable: str) -> tuple[str, ...]:
        return (
            engine_executable,
            "volume",
            "create",
            "--driver",
            self.driver,
            "--opt",
            f"type={self.filesystem_type}",
            "--opt",
            f"device={self.device}",
            "--opt",
            f"o={self.mount_options}",
            self.name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "subpaths": list(self.subpaths),
            "driver": self.driver,
            "options": {
                "type": self.filesystem_type,
                "device": self.device,
                "o": self.mount_options,
            },
        }


@dataclasses.dataclass(frozen=True, slots=True)
class VolumeInspection:
    name: str
    driver: str | None
    options: Mapping[str, str]
    mountpoint: str | None
    scope: str | None

    def mismatches(self, spec: VolumeSpec) -> tuple[str, ...]:
        mismatches: list[str] = []
        if self.name != spec.name:
            mismatches.append("volume_name")
        if self.driver != spec.driver:
            mismatches.append("volume_driver")
        if self.scope != "local":
            mismatches.append("volume_scope")
        expected_options = {
            "type": spec.filesystem_type,
            "device": spec.device,
            "o": spec.mount_options,
        }
        if dict(self.options) != expected_options:
            mismatches.append("volume_options")
        if (
            not self.mountpoint
            or "\x00" in self.mountpoint
            or not PurePosixPath(self.mountpoint).is_absolute()
        ):
            mismatches.append("volume_mountpoint")
        return tuple(mismatches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "driver": self.driver,
            "options": dict(sorted(self.options.items())),
            "mountpoint": self.mountpoint,
            "scope": self.scope,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class EngineIdentity:
    kind: str
    executable: str
    version: str
    executable_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class ImageInspection:
    reference: str
    requested_digest: str
    resolved_digest: str | None
    image_id: str | None
    repo_digests: tuple[str, ...]
    verified: bool
    declared_volumes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "requested_digest": self.requested_digest,
            "resolved_digest": self.resolved_digest,
            "image_id": self.image_id,
            "repo_digests": list(self.repo_digests),
            "verified": self.verified,
            "declared_volumes": list(self.declared_volumes),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class NetworkEndpoint:
    container_id: str
    identity: str
    endpoint_id: str | None = None
    ipv4_address: str | None = None
    ipv6_address: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class NetworkInspection:
    name: str
    network_id: str | None
    driver: str | None
    internal: bool
    endpoints: tuple[NetworkEndpoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "network_id": self.network_id,
            "driver": self.driver,
            "internal": self.internal,
            "endpoints": [endpoint.to_dict() for endpoint in self.endpoints],
        }

    @property
    def endpoint_identities(self) -> tuple[str, ...]:
        return tuple(sorted(endpoint.identity for endpoint in self.endpoints))


@dataclasses.dataclass(frozen=True, slots=True)
class InspectedMount:
    source: str
    destination: str
    read_only: bool
    mount_type: str = "bind"
    volume_subpath: str | None = None
    volume_nocopy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.mount_type,
            "source": self.source,
            "destination": self.destination,
            "read_only": self.read_only,
            "volume_subpath": self.volume_subpath,
            "volume_nocopy": self.volume_nocopy,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ContainerInspection:
    name: str
    user: str
    read_only_rootfs: bool
    cap_drop: tuple[str, ...]
    no_new_privileges: bool
    pids_limit: int | None
    memory_bytes: int | None
    nano_cpus: int | None
    file_size_limit: int | None
    storage_size: str | None
    network_mode: str
    ipc_mode: str
    dns_servers: tuple[str, ...]
    tmpfs: Mapping[str, str]
    mounts: tuple[InspectedMount, ...]
    environment_names: tuple[str, ...]
    stdin_open: bool

    @classmethod
    def expected(cls, spec: ContainerSpec) -> "ContainerInspection":
        return cls(
            name=spec.name,
            user=spec.user,
            read_only_rootfs=True,
            cap_drop=("ALL",),
            no_new_privileges=True,
            pids_limit=spec.resources.pids_limit,
            memory_bytes=spec.resources.memory_bytes,
            nano_cpus=spec.resources.cpu_millis * 1_000_000,
            file_size_limit=spec.resources.storage_bytes,
            storage_size=None,
            network_mode=(
                "none"
                if spec.network.mode is OciNetworkMode.NONE
                else str(spec.network.network_name)
            ),
            ipc_mode=spec.ipc_mode,
            dns_servers=spec.network.dns_servers,
            tmpfs={
                **(
                    {}
                    if any(mount.destination == "/tmp" for mount in spec.mounts)
                    else {
                        "/tmp": (
                            "rw,noexec,nosuid,nodev,"
                            f"size={spec.resources.tmpfs_bytes},mode=1777"
                        )
                    }
                ),
                **dict(spec.tmpfs_mounts),
            },
            mounts=tuple(
                InspectedMount(
                    source=str(mount.source),
                    destination=mount.destination,
                    read_only=mount.read_only,
                    mount_type=mount.mount_type,
                    volume_subpath=mount.volume_subpath,
                    volume_nocopy=mount.volume_nocopy,
                )
                for mount in spec.mounts
            ),
            environment_names=spec.environment_names,
            stdin_open=spec.stdin_open,
        )

    def mismatches(self, spec: ContainerSpec) -> tuple[str, ...]:
        mismatches: list[str] = []
        if self.name.lstrip("/") != spec.name:
            mismatches.append("container_name")
        if self.user != spec.user or self.user in {"", "0", "0:0", "root"}:
            mismatches.append("non_root_user")
        if not self.read_only_rootfs:
            mismatches.append("read_only_rootfs")
        if "ALL" not in {value.upper() for value in self.cap_drop}:
            mismatches.append("cap_drop_all")
        if not self.no_new_privileges:
            mismatches.append("no_new_privileges")
        if self.pids_limit != spec.resources.pids_limit:
            mismatches.append("pids_limit")
        if self.memory_bytes != spec.resources.memory_bytes:
            mismatches.append("memory_limit")
        if self.nano_cpus != spec.resources.cpu_millis * 1_000_000:
            mismatches.append("cpu_limit")
        if self.file_size_limit != spec.resources.storage_bytes:
            mismatches.append("file_size_limit")
        expected_network = (
            "none"
            if spec.network.mode is OciNetworkMode.NONE
            else str(spec.network.network_name)
        )
        if self.network_mode != expected_network:
            mismatches.append("network_mode")
        if self.ipc_mode != spec.ipc_mode:
            mismatches.append("ipc_mode")
        if tuple(self.dns_servers) != tuple(spec.network.dns_servers):
            mismatches.append("dns_policy")
        expected_tmpfs = {
            **(
                {}
                if any(mount.destination == "/tmp" for mount in spec.mounts)
                else {
                    "/tmp": (
                        "rw,noexec,nosuid,nodev,"
                        f"size={spec.resources.tmpfs_bytes},mode=1777"
                    )
                }
            ),
            **dict(spec.tmpfs_mounts),
        }
        if set(self.tmpfs) != set(expected_tmpfs) or any(
            {
                token.strip()
                for token in str(self.tmpfs[destination]).split(",")
                if token.strip()
            }
            != {
                token.strip()
                for token in expected.split(",")
                if token.strip()
            }
            for destination, expected in expected_tmpfs.items()
            if destination in self.tmpfs
        ):
            mismatches.append("tmpfs")
        expected_mounts = {
            (
                mount.mount_type,
                (
                    os.path.normcase(str(mount.source))
                    if mount.mount_type == "bind"
                    else str(mount.source)
                ),
                mount.destination,
                mount.read_only,
                mount.volume_subpath,
                mount.volume_nocopy,
            )
            for mount in spec.mounts
        }
        actual_mounts = {
            (
                mount.mount_type,
                (
                    os.path.normcase(mount.source)
                    if mount.mount_type == "bind"
                    else mount.source
                ),
                mount.destination,
                mount.read_only,
                mount.volume_subpath,
                mount.volume_nocopy,
            )
            for mount in self.mounts
        }
        if actual_mounts != expected_mounts:
            mismatches.append("mount_policy")
        if not set(spec.environment_names).issubset(self.environment_names):
            mismatches.append("environment_policy")
        if self.stdin_open is not spec.stdin_open:
            mismatches.append("stdin_open")
        return tuple(mismatches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "user": self.user,
            "read_only_rootfs": self.read_only_rootfs,
            "cap_drop": list(self.cap_drop),
            "no_new_privileges": self.no_new_privileges,
            "pids_limit": self.pids_limit,
            "memory_bytes": self.memory_bytes,
            "nano_cpus": self.nano_cpus,
            "file_size_limit": self.file_size_limit,
            "storage_size": self.storage_size,
            "network_mode": self.network_mode,
            "ipc_mode": self.ipc_mode,
            "dns_servers": list(self.dns_servers),
            "tmpfs": dict(sorted(self.tmpfs.items())),
            "mounts": [mount.to_dict() for mount in self.mounts],
            "environment_names": list(self.environment_names),
            "stdin_open": self.stdin_open,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class EngineRunResult:
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "duration_ms": self.duration_ms,
            "stdout_total_bytes": self.stdout_total_bytes,
            "stderr_total_bytes": self.stderr_total_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_sha256": sha256_bytes(self.stdout),
            "stderr_sha256": sha256_bytes(self.stderr),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class CleanupEvidence:
    container_name: str
    attempted: bool
    status: CleanupStatus
    remove_argv: tuple[str, ...]
    remove_exit_code: int | None
    confirmed_absent: bool
    error: str | None = None

    @property
    def succeeded(self) -> bool | None:
        if self.status is CleanupStatus.NOT_ATTEMPTED:
            return None
        return self.status is CleanupStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_name": self.container_name,
            "attempted": self.attempted,
            "status": self.status.value,
            "succeeded": self.succeeded,
            "remove_argv": list(self.remove_argv),
            "remove_exit_code": self.remove_exit_code,
            "confirmed_absent": self.confirmed_absent,
            "error": self.error,
        }


def _cleanup_not_attempted(name: str) -> CleanupEvidence:
    return CleanupEvidence(
        container_name=name,
        attempted=False,
        status=CleanupStatus.NOT_ATTEMPTED,
        remove_argv=(),
        remove_exit_code=None,
        confirmed_absent=False,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class VolumeCleanupEvidence:
    volume_name: str
    attempted: bool
    status: CleanupStatus
    remove_argv: tuple[str, ...]
    remove_exit_code: int | None
    confirmed_absent: bool
    error: str | None = None

    @property
    def succeeded(self) -> bool | None:
        if self.status is CleanupStatus.NOT_ATTEMPTED:
            return None
        return self.status is CleanupStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "volume_name": self.volume_name,
            "attempted": self.attempted,
            "status": self.status.value,
            "succeeded": self.succeeded,
            "remove_argv": list(self.remove_argv),
            "remove_exit_code": self.remove_exit_code,
            "confirmed_absent": self.confirmed_absent,
            "error": self.error,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class VolumeLifecycleEvidence:
    spec: VolumeSpec
    create: EngineRunResult
    inspection: VolumeInspection | None
    inspection_mismatches: tuple[str, ...]
    cleanup: VolumeCleanupEvidence

    @property
    def quota_verified(self) -> bool:
        return (
            self.create.exit_code == 0
            and not self.create.timed_out
            and not self.create.cancelled
            and self.inspection is not None
            and not self.inspection_mismatches
        )

    @property
    def lifecycle_verified(self) -> bool:
        return (
            self.quota_verified
            and self.cleanup.status is CleanupStatus.SUCCEEDED
            and self.cleanup.confirmed_absent
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "create": self.create.to_dict(),
            "inspection": self.inspection.to_dict() if self.inspection else None,
            "inspection_mismatches": list(self.inspection_mismatches),
            "quota_verified": self.quota_verified,
            "cleanup": self.cleanup.to_dict(),
            "lifecycle_verified": self.lifecycle_verified,
        }


class OciEngine(Protocol):
    executable: str

    def identity(self) -> EngineIdentity: ...

    def inspect_image(self, image: DigestPinnedImage) -> ImageInspection: ...

    def inspect_network(self, name: str) -> NetworkInspection: ...

    def hard_quota_volume_capability(self) -> tuple[bool, str]: ...

    def create_volume(self, spec: VolumeSpec) -> EngineRunResult: ...

    def inspect_volume(self, name: str) -> VolumeInspection: ...

    def remove_volume(self, name: str, *, force: bool) -> EngineRunResult: ...

    def volume_exists(self, name: str) -> bool: ...

    def copy_to_container(
        self,
        name: str,
        source: Path,
        destination: str,
    ) -> EngineRunResult: ...

    def exec_container(
        self,
        name: str,
        command: Sequence[str],
        *,
        user: str,
        working_directory: str,
        resources: OciResourcePolicy,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
    ) -> EngineRunResult: ...

    def run_container(
        self,
        spec: ContainerSpec,
        *,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
    ) -> EngineRunResult: ...

    def inspect_container(self, name: str) -> ContainerInspection: ...

    def remove_container(self, name: str, *, force: bool) -> EngineRunResult: ...

    def container_exists(self, name: str) -> bool: ...


class _BoundedBytes:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.data = bytearray()
        self.total = 0
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True


def _drain(stream, sink: _BoundedBytes) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            sink.feed(chunk)
    finally:
        stream.close()


def _feed_stdin(stream, data: bytes) -> None:
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = stream.write(view[offset : offset + 64 * 1024])
            if written is None:
                written = 0
            offset += written
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        stream.close()


def _engine_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENGINE_ENV_ALLOWLIST and "\x00" not in value
    }


def _terminate_client_process(proc: subprocess.Popen[bytes]) -> None:
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


class CliOciEngine:
    """Shell-free Docker/Podman CLI implementation of :class:`OciEngine`."""

    def __init__(self, executable: str) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise EngineUnavailableError(f"OCI engine executable not found: {executable}")
        self.executable = resolved
        base = Path(resolved).name.lower()
        self.kind = "podman" if "podman" in base else "docker"

    @classmethod
    def auto(cls) -> "CliOciEngine":
        for candidate in ("docker", "podman"):
            if shutil.which(candidate):
                return cls(candidate)
        raise EngineUnavailableError("neither docker nor podman is installed")

    def _execute(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
    ) -> EngineRunResult:
        command = tuple(str(value) for value in argv)
        if not command or command[0] != self.executable:
            raise ValueError("engine argv must begin with the resolved engine executable")
        if any("\x00" in value for value in command):
            raise ValueError("engine argv cannot contain NUL")
        stdout = _BoundedBytes(stdout_limit)
        stderr = _BoundedBytes(stderr_limit)
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            start_new_session = True
        started = time.monotonic()
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_engine_environment(),
            shell=False,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        assert proc.stdout is not None and proc.stderr is not None
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True),
        ]
        writer: threading.Thread | None = None
        if stdin_data is not None:
            assert proc.stdin is not None
            writer = threading.Thread(
                target=_feed_stdin,
                args=(proc.stdin, stdin_data),
                daemon=True,
            )
            writer.start()
        for reader in readers:
            reader.start()
        deadline = started + timeout_seconds
        timed_out = False
        cancelled = False
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate_client_process(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_client_process(proc)
                break
            try:
                proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        for reader in readers:
            reader.join(timeout=5)
        if writer is not None:
            writer.join(timeout=5)
        return EngineRunResult(
            argv=command,
            exit_code=proc.poll(),
            timed_out=timed_out,
            cancelled=cancelled,
            duration_ms=max(0, int((time.monotonic() - started) * 1_000)),
            stdout=bytes(stdout.data),
            stderr=bytes(stderr.data),
            stdout_total_bytes=stdout.total,
            stderr_total_bytes=stderr.total,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
        )

    def identity(self) -> EngineIdentity:
        result = self._execute(
            [self.executable, "--version"],
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
        )
        if result.exit_code != 0:
            raise EngineUnavailableError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or f"{self.kind} --version failed"
            )
        digest = None
        try:
            digest = hashlib.sha256(Path(self.executable).read_bytes()).hexdigest()
        except OSError:
            pass
        return EngineIdentity(
            kind=self.kind,
            executable=self.executable,
            version=result.stdout.decode("utf-8", errors="replace").strip(),
            executable_sha256=digest,
        )

    def daemon_status(self) -> tuple[bool, str]:
        result = self._execute(
            [self.executable, "info"],
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=256 * 1024,
            stderr_limit=256 * 1024,
        )
        if result.exit_code == 0:
            return True, "daemon reachable"
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return False, detail or "engine daemon is not reachable"

    def _inspect_json(self, argv: Sequence[str]) -> Mapping[str, Any]:
        result = self._execute(
            argv,
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=MAX_IMAGE_INSPECT_BYTES,
            stderr_limit=512 * 1024,
        )
        if result.exit_code != 0 or result.stdout_truncated:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise OciRunnerError(detail or f"engine inspect failed: {list(argv)!r}")
        try:
            value = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OciRunnerError("engine inspect did not return valid UTF-8 JSON") from exc
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            return value[0]
        if isinstance(value, dict):
            return value
        raise OciRunnerError("engine inspect returned an unexpected JSON shape")

    def _object_exists(
        self,
        argv: Sequence[str],
        *,
        object_kind: str,
        object_name: str,
        not_found_markers: Sequence[str],
    ) -> bool:
        result = self._execute(
            argv,
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
        )
        if (
            result.timed_out
            or result.cancelled
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            conditions = []
            if result.timed_out:
                conditions.append("timed out")
            if result.cancelled:
                conditions.append("was cancelled")
            if result.stdout_truncated or result.stderr_truncated:
                conditions.append("returned truncated output")
            raise OciRunnerError(
                f"{object_kind} existence probe failed for {object_name!r}: "
                + ", ".join(conditions)
            )
        if result.exit_code == 0:
            return True

        combined = (result.stdout + b"\n" + result.stderr).decode(
            "utf-8",
            errors="replace",
        )
        normalized = combined.casefold()
        if any(marker in normalized for marker in not_found_markers):
            return False
        detail = combined.strip() or f"engine exited with code {result.exit_code}"
        raise OciRunnerError(
            f"{object_kind} existence probe failed for {object_name!r}: {detail}"
        )

    def inspect_image(self, image: DigestPinnedImage) -> ImageInspection:
        data = self._inspect_json(
            [self.executable, "image", "inspect", image.reference]
        )
        config = data.get("Config") if isinstance(data.get("Config"), dict) else {}
        raw_declared_volumes = config.get("Volumes")
        declared_volumes = tuple(
            sorted(
                str(value)
                for value in (
                    raw_declared_volumes.keys()
                    if isinstance(raw_declared_volumes, dict)
                    else ()
                )
            )
        )
        repo_digests = tuple(
            str(value)
            for value in (data.get("RepoDigests") or ())
            if isinstance(value, str)
        )
        digest_values = {
            value.split("@", 1)[-1] for value in repo_digests if "@" in value
        }
        direct = data.get("Digest")
        if isinstance(direct, str):
            digest_values.add(direct)
        resolved = image.digest if image.digest in digest_values else None
        return ImageInspection(
            reference=image.reference,
            requested_digest=image.digest,
            resolved_digest=resolved,
            image_id=str(data.get("Id")) if data.get("Id") else None,
            repo_digests=repo_digests,
            verified=resolved == image.digest,
            declared_volumes=declared_volumes,
        )

    def inspect_network(self, name: str) -> NetworkInspection:
        data = self._inspect_json([self.executable, "network", "inspect", name])
        endpoints: list[NetworkEndpoint] = []
        containers = data.get("Containers")
        if isinstance(containers, dict):
            for container_id, value in sorted(containers.items()):
                if not isinstance(value, dict):
                    continue
                endpoints.append(
                    NetworkEndpoint(
                        container_id=str(container_id),
                        identity=str(value.get("Name") or container_id),
                        endpoint_id=(
                            str(value.get("EndpointID"))
                            if value.get("EndpointID")
                            else None
                        ),
                        ipv4_address=(
                            str(value.get("IPv4Address"))
                            if value.get("IPv4Address")
                            else None
                        ),
                        ipv6_address=(
                            str(value.get("IPv6Address"))
                            if value.get("IPv6Address")
                            else None
                        ),
                    )
                )
        return NetworkInspection(
            name=str(data.get("Name") or name),
            network_id=str(data.get("Id")) if data.get("Id") else None,
            driver=str(data.get("Driver")) if data.get("Driver") else None,
            internal=bool(data.get("Internal")),
            endpoints=tuple(endpoints),
        )

    def hard_quota_volume_capability(self) -> tuple[bool, str]:
        if self.kind != "docker":
            return (
                False,
                "hard aggregate quota requires Docker volume-subpath semantics; "
                f"engine kind {self.kind!r} is not verified",
            )
        daemon = self._execute(
            [self.executable, "info", "--format", "{{.OSType}}"],
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
        )
        if daemon.exit_code != 0 or daemon.stdout_truncated:
            detail = daemon.stderr.decode("utf-8", errors="replace").strip()
            return False, detail or "Docker daemon type could not be verified"
        operating_system = daemon.stdout.decode(
            "utf-8", errors="replace"
        ).strip().lower()
        if operating_system != "linux":
            return (
                False,
                "hard aggregate quota requires a Linux Docker daemon "
                f"(observed {operating_system or '<empty>'})",
            )
        return (
            True,
            (
                "Linux Docker daemon supports local-driver tmpfs named volumes; "
                "runtime volume, subpath-mount, and statfs verification is required"
            ),
        )

    def create_volume(self, spec: VolumeSpec) -> EngineRunResult:
        return self._execute(
            spec.create_argv(self.executable),
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=256 * 1024,
            stderr_limit=256 * 1024,
        )

    def inspect_volume(self, name: str) -> VolumeInspection:
        if _VOLUME_NAME_RE.fullmatch(name) is None:
            raise ValueError("volume name is unsafe")
        data = self._inspect_json([self.executable, "volume", "inspect", name])
        raw_options = data.get("Options")
        options = (
            {
                str(key): str(value)
                for key, value in raw_options.items()
                if isinstance(key, str) and value is not None
            }
            if isinstance(raw_options, dict)
            else {}
        )
        return VolumeInspection(
            name=str(data.get("Name") or name),
            driver=str(data.get("Driver")) if data.get("Driver") else None,
            options=options,
            mountpoint=(
                str(data.get("Mountpoint")) if data.get("Mountpoint") else None
            ),
            scope=str(data.get("Scope")) if data.get("Scope") else None,
        )

    def remove_volume(self, name: str, *, force: bool) -> EngineRunResult:
        if _VOLUME_NAME_RE.fullmatch(name) is None:
            raise ValueError("volume name is unsafe")
        argv = [self.executable, "volume", "rm"]
        if force:
            argv.append("-f")
        argv.append(name)
        return self._execute(
            argv,
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=256 * 1024,
            stderr_limit=256 * 1024,
        )

    def volume_exists(self, name: str) -> bool:
        if _VOLUME_NAME_RE.fullmatch(name) is None:
            raise ValueError("volume name is unsafe")
        return self._object_exists(
            [self.executable, "volume", "inspect", name],
            object_kind="volume",
            object_name=name,
            not_found_markers=_VOLUME_NOT_FOUND_MARKERS,
        )

    def copy_to_container(
        self,
        name: str,
        source: Path,
        destination: str,
    ) -> EngineRunResult:
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise ValueError("container name is unsafe")
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_dir():
            raise ValueError("container copy source must be a directory")
        destination_path = PurePosixPath(destination)
        if (
            not destination_path.is_absolute()
            or destination == "/"
            or ".." in destination_path.parts
            or any(
                character in destination for character in ("\x00", "\r", "\n", ":")
            )
        ):
            raise ValueError("container copy destination is unsafe")
        source_contents = str(source_path) + os.sep + "."
        return self._execute(
            [
                self.executable,
                "cp",
                source_contents,
                f"{name}:{destination}",
            ],
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=256 * 1024,
            stderr_limit=256 * 1024,
        )

    def exec_container(
        self,
        name: str,
        command: Sequence[str],
        *,
        user: str,
        working_directory: str,
        resources: OciResourcePolicy,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
    ) -> EngineRunResult:
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise ValueError("container name is unsafe")
        if user in {"", "0", "0:0", "root", "root:root"}:
            raise ValueError("container exec user must be non-root")
        workdir = PurePosixPath(working_directory)
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise ValueError("container exec working directory is unsafe")
        command_tuple = tuple(str(value) for value in command)
        if not command_tuple or any("\x00" in value for value in command_tuple):
            raise ValueError("container exec command must be a safe argv vector")
        argv = [self.executable, "exec"]
        if stdin_data is not None:
            argv.append("--interactive")
        argv.extend(
            [
                "--user",
                user,
                "--workdir",
                working_directory,
                name,
                *command_tuple,
            ]
        )
        return self._execute(
            argv,
            timeout_seconds=resources.wall_time_ms / 1_000,
            stdout_limit=min(resources.stdout_bytes, MAX_ENGINE_OUTPUT_BYTES),
            stderr_limit=min(resources.stderr_bytes, MAX_ENGINE_OUTPUT_BYTES),
            cancel_event=cancel_event,
            stdin_data=stdin_data,
        )

    def run_container(
        self,
        spec: ContainerSpec,
        *,
        cancel_event: threading.Event | None = None,
        stdin_data: bytes | None = None,
    ) -> EngineRunResult:
        if (stdin_data is not None) != spec.stdin_open:
            raise ValueError(
                "container stdin payload and stdin_open policy must be supplied together"
            )
        return self._execute(
            build_run_argv(self.executable, spec),
            timeout_seconds=spec.resources.wall_time_ms / 1_000,
            stdout_limit=min(spec.resources.stdout_bytes, MAX_ENGINE_OUTPUT_BYTES),
            stderr_limit=min(spec.resources.stderr_bytes, MAX_ENGINE_OUTPUT_BYTES),
            cancel_event=cancel_event,
            stdin_data=stdin_data,
        )

    def inspect_container(self, name: str) -> ContainerInspection:
        data = self._inspect_json([self.executable, "inspect", name])
        host = data.get("HostConfig") if isinstance(data.get("HostConfig"), dict) else {}
        config = data.get("Config") if isinstance(data.get("Config"), dict) else {}
        requested_volume_options: dict[
            tuple[str, str], tuple[str | None, bool]
        ] = {}
        for value in host.get("Mounts") or ():
            if not isinstance(value, dict) or value.get("Type") != "volume":
                continue
            source = str(value.get("Source") or "")
            destination = str(value.get("Target") or value.get("Destination") or "")
            raw_options = value.get("VolumeOptions")
            options = raw_options if isinstance(raw_options, dict) else {}
            subpath_value = options.get("Subpath")
            requested_volume_options[(source, destination)] = (
                str(subpath_value) if subpath_value else None,
                bool(options.get("NoCopy")),
            )
        mounts: list[InspectedMount] = []
        for value in data.get("Mounts") or ():
            if not isinstance(value, dict):
                continue
            mount_type = str(value.get("Type") or "bind")
            source = (
                str(value.get("Name") or "")
                if mount_type == "volume"
                else str(value.get("Source") or "")
            )
            volume_subpath, volume_nocopy = requested_volume_options.get(
                (source, str(value.get("Destination") or "")),
                (None, False),
            )
            mounts.append(
                InspectedMount(
                    source=source,
                    destination=str(value.get("Destination") or ""),
                    read_only=not bool(value.get("RW")),
                    mount_type=mount_type,
                    volume_subpath=volume_subpath,
                    volume_nocopy=volume_nocopy,
                )
            )
        env_names = tuple(
            sorted(
                {
                    value.split("=", 1)[0]
                    for value in (config.get("Env") or ())
                    if isinstance(value, str) and "=" in value
                }
            )
        )
        security_options = tuple(str(value) for value in (host.get("SecurityOpt") or ()))
        storage = host.get("StorageOpt")
        storage_size = (
            str(storage.get("size"))
            if isinstance(storage, dict) and storage.get("size") is not None
            else None
        )
        file_size_limit = None
        for limit in host.get("Ulimits") or ():
            if isinstance(limit, dict) and limit.get("Name") == "fsize":
                soft = limit.get("Soft")
                hard = limit.get("Hard")
                if soft == hard and soft is not None:
                    file_size_limit = int(soft)
        return ContainerInspection(
            name=str(data.get("Name") or name).lstrip("/"),
            user=str(config.get("User") or ""),
            read_only_rootfs=bool(host.get("ReadonlyRootfs")),
            cap_drop=tuple(str(value) for value in (host.get("CapDrop") or ())),
            no_new_privileges=any(
                value.lower().startswith("no-new-privileges")
                for value in security_options
            ),
            pids_limit=int(host["PidsLimit"]) if host.get("PidsLimit") is not None else None,
            memory_bytes=int(host["Memory"]) if host.get("Memory") is not None else None,
            nano_cpus=int(host["NanoCpus"]) if host.get("NanoCpus") is not None else None,
            file_size_limit=file_size_limit,
            storage_size=storage_size,
            network_mode=str(host.get("NetworkMode") or ""),
            ipc_mode=str(host.get("IpcMode") or ""),
            dns_servers=tuple(str(value) for value in (host.get("Dns") or ())),
            tmpfs={
                str(key): str(value)
                for key, value in (host.get("Tmpfs") or {}).items()
            },
            mounts=tuple(mounts),
            environment_names=env_names,
            stdin_open=bool(config.get("OpenStdin")),
        )

    def remove_container(self, name: str, *, force: bool) -> EngineRunResult:
        argv = [self.executable, "rm"]
        if force:
            argv.append("-f")
        argv.append(name)
        return self._execute(
            argv,
            timeout_seconds=DEFAULT_ENGINE_TIMEOUT_SECONDS,
            stdout_limit=256 * 1024,
            stderr_limit=256 * 1024,
        )

    def container_exists(self, name: str) -> bool:
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            raise ValueError("container name is unsafe")
        return self._object_exists(
            [self.executable, "inspect", name],
            object_kind="container",
            object_name=name,
            not_found_markers=_CONTAINER_NOT_FOUND_MARKERS,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class OciTrialRequest:
    attempt: TrialAttempt
    task: TaskPackage
    harness_image: DigestPinnedImage | str
    harness_command: tuple[str, ...]
    track: OciTrack = OciTrack.CONTROLLED
    network: OciNetworkPolicy = dataclasses.field(default_factory=OciNetworkPolicy.none)
    storage_mode: OciStorageMode = OciStorageMode.AUTO
    task_image: DigestPinnedImage | str | None = None
    grader_image: DigestPinnedImage | str | None = None
    grader_command: tuple[str, ...] | None = None
    harness_resources: OciResourcePolicy | None = None
    grader_resources: OciResourcePolicy | None = None
    gateway_handle: OpaqueTrialHandle | None = None
    credential_broker: CredentialBroker | None = None
    protocol_session: ProtocolSession | None = None
    protocol_parser: Any | None = None
    accepted_event: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, TrialAttempt):
            raise TypeError("attempt must be an eval.TrialAttempt")
        if not isinstance(self.task, TaskPackage):
            raise TypeError("task must be an eval.TaskPackage")
        if not isinstance(self.track, OciTrack):
            object.__setattr__(self, "track", OciTrack(self.track))
        if not isinstance(self.storage_mode, OciStorageMode):
            object.__setattr__(self, "storage_mode", OciStorageMode(self.storage_mode))
        if self.track.value not in self.task.manifest["track_compatibility"]:
            raise ValueError(
                f"task does not declare compatibility with {self.track.value!r}"
            )
        if self.attempt.spec.task != self.task.task_ref:
            raise ValueError("trial attempt task does not match TaskPackage")
        manifest = self.task.manifest
        task_image_value = self.task_image or manifest["environment"]["image"]
        grader_image_value = self.grader_image or manifest["grader"]["image"]
        object.__setattr__(
            self,
            "task_image",
            task_image_value
            if isinstance(task_image_value, DigestPinnedImage)
            else DigestPinnedImage.parse(str(task_image_value)),
        )
        object.__setattr__(
            self,
            "harness_image",
            self.harness_image
            if isinstance(self.harness_image, DigestPinnedImage)
            else DigestPinnedImage.parse(str(self.harness_image)),
        )
        object.__setattr__(
            self,
            "grader_image",
            grader_image_value
            if isinstance(grader_image_value, DigestPinnedImage)
            else DigestPinnedImage.parse(str(grader_image_value)),
        )
        object.__setattr__(self, "harness_command", tuple(self.harness_command))
        grader_command = self.grader_command or tuple(manifest["grader"]["command"])
        object.__setattr__(self, "grader_command", tuple(grader_command))
        if not self.harness_command or not self.grader_command:
            raise ValueError("harness and grader commands must be non-empty")
        if any(
            "\x00" in value
            for value in (*self.harness_command, *self.grader_command)
        ):
            raise ValueError("container commands cannot contain NUL")
        if self.harness_resources is None:
            object.__setattr__(
                self,
                "harness_resources",
                OciResourcePolicy.from_budget_limits(manifest["budget_limits"]),
            )
        if self.grader_resources is None:
            object.__setattr__(
                self,
                "grader_resources",
                OciResourcePolicy.from_budget_limits(
                    manifest["grader"]["budget_limits"]
                ),
            )
        has_handle = self.gateway_handle is not None
        has_broker = self.credential_broker is not None
        if has_handle != has_broker:
            raise ValueError("gateway_handle and credential_broker must be supplied together")
        if self.network.mode is OciNetworkMode.MODEL_GATEWAY_ONLY and not has_handle:
            raise NetworkPolicyError(
                "model-gateway-only execution requires an opaque gateway handle"
            )
        if self.network.mode is OciNetworkMode.NONE and has_handle:
            raise NetworkPolicyError("network-none execution cannot receive a gateway handle")
        assert isinstance(self.task_image, DigestPinnedImage)
        assert isinstance(self.harness_image, DigestPinnedImage)
        if (
            self.track is OciTrack.CONTROLLED
            and self.task_image.digest != self.harness_image.digest
        ):
            raise ImageRolePolicyError(
                "Controlled v1 requires the harness execution image to be the "
                "declared task/environment image digest; unbound harness images "
                "are ineligible"
            )
        if self.protocol_session is not None and (
            self.protocol_parser is not None or self.accepted_event is not None
        ):
            raise ValueError(
                "trusted ProtocolSession and legacy parser compatibility are "
                "mutually exclusive"
            )
        if self.protocol_session is not None:
            session_request = self.protocol_session.trial_request
            if (
                session_request["trial_id"] != self.attempt.spec.trial_id
                or session_request["attempt_id"] != self.attempt.attempt_id
                or session_request["track"] != self.track.value
            ):
                raise ValueError(
                    "ProtocolSession request identity/track does not match the OCI attempt"
                )
        if (self.protocol_parser is None) != (self.accepted_event is None):
            raise ValueError(
                "protocol_parser and controller-authored accepted_event are required together"
            )

    def image_role_policy(self) -> dict[str, Any]:
        assert isinstance(self.task_image, DigestPinnedImage)
        assert isinstance(self.harness_image, DigestPinnedImage)
        common_digest = self.task_image.digest == self.harness_image.digest
        return {
            "track": self.track.value,
            "task_environment_image": self.task_image.to_dict(),
            "harness_execution_image": self.harness_image.to_dict(),
            "controlled_environment_bound": (
                common_digest if self.track is OciTrack.CONTROLLED else None
            ),
            "binding_mode": (
                "common-image-digest"
                if common_digest
                else "systems-harness-selected-image"
            ),
            "systems_confound": (
                None
                if self.track is OciTrack.CONTROLLED or common_digest
                else "harness execution image differs from declared task environment"
            ),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class StorageObservation:
    limit_bytes: int
    peak_bytes: int
    exceeded: bool
    monitor_error: str | None
    completed: bool
    enforcement: str = "best-effort-controller-polling"
    hard_quota_verified: bool = False
    volume_names: tuple[str, ...] = ()
    component_limits: tuple[tuple[str, int], ...] = ()
    cleanup_succeeded: bool = False

    @property
    def monitor_succeeded(self) -> bool:
        if self.hard_storage_enforced:
            return self.completed and self.monitor_error is None
        return self.completed and not self.exceeded and self.monitor_error is None

    @property
    def hard_storage_enforced(self) -> bool:
        return (
            self.enforcement == _HARD_QUOTA_ENFORCEMENT
            and self.hard_quota_verified
        )

    @property
    def official_eligible(self) -> bool:
        return (
            self.hard_storage_enforced
            and self.monitor_succeeded
            and self.cleanup_succeeded
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enforcement": self.enforcement,
            "limit_bytes": self.limit_bytes,
            "peak_bytes": self.peak_bytes,
            "exceeded": self.exceeded,
            "monitor_error": self.monitor_error,
            "completed": self.completed,
            "monitor_succeeded": self.monitor_succeeded,
            "hard_quota_verified": self.hard_quota_verified,
            "hard_storage_enforced": self.hard_storage_enforced,
            "volume_names": list(self.volume_names),
            "component_limits": {
                name: value for name, value in self.component_limits
            },
            "cleanup_succeeded": self.cleanup_succeeded,
            "official_eligible": self.official_eligible,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class PhaseEvidence:
    phase: ContainerPhase
    policy: Mapping[str, Any]
    run: EngineRunResult
    inspection: ContainerInspection | None
    inspection_mismatches: tuple[str, ...]
    storage: StorageObservation
    cleanup: CleanupEvidence
    auxiliary_runs: tuple[EngineRunResult, ...] = ()

    @property
    def runtime_policy_verified(self) -> bool:
        return (
            self.inspection is not None
            and not self.inspection_mismatches
            and self.storage.monitor_succeeded
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "policy": dict(self.policy),
            "run": self.run.to_dict(),
            "inspection": self.inspection.to_dict() if self.inspection else None,
            "inspection_mismatches": list(self.inspection_mismatches),
            "storage": self.storage.to_dict(),
            "runtime_policy_verified": self.runtime_policy_verified,
            "cleanup": self.cleanup.to_dict(),
            "auxiliary_runs": [run.to_dict() for run in self.auxiliary_runs],
        }


@dataclasses.dataclass(frozen=True, slots=True)
class OciRunnerEvidence:
    runner_id: str
    attempt: Mapping[str, Any]
    track: OciTrack
    image_roles: Mapping[str, Any]
    engine: EngineIdentity
    images: tuple[ImageInspection, ...]
    network: Mapping[str, Any]
    storage: Mapping[str, Any]
    phase_order: tuple[str, ...]
    volume_keeper: PhaseEvidence | None
    seed: PhaseEvidence | None
    harness: PhaseEvidence | None
    output_capture: PhaseEvidence | None
    grader: PhaseEvidence | None
    handle_action: str
    protocol: Mapping[str, Any]
    workspace: Mapping[str, Any]
    runtime_verified: bool
    official_eligible: bool
    official_ineligibility_reasons: tuple[str, ...]
    errors: tuple[str, ...]
    started_at: str
    duration_ms: int

    @property
    def official_verified(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUNNER_SCHEMA,
            "runner_id": self.runner_id,
            "attempt": dict(self.attempt),
            "track": self.track.value,
            "image_roles": dict(self.image_roles),
            "engine": self.engine.to_dict(),
            "images": [image.to_dict() for image in self.images],
            "network": dict(self.network),
            "storage": dict(self.storage),
            "phase_order": list(self.phase_order),
            "volume_keeper": (
                self.volume_keeper.to_dict() if self.volume_keeper else None
            ),
            "seed": self.seed.to_dict() if self.seed else None,
            "harness": self.harness.to_dict() if self.harness else None,
            "output_capture": (
                self.output_capture.to_dict() if self.output_capture else None
            ),
            "grader": self.grader.to_dict() if self.grader else None,
            "handle_action": self.handle_action,
            "protocol": dict(self.protocol),
            "workspace": dict(self.workspace),
            "runtime_verified": self.runtime_verified,
            "official_eligible": self.official_eligible,
            "official_ineligibility_reasons": list(
                self.official_ineligibility_reasons
            ),
            "official_verified": False,
            "errors": list(self.errors),
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return sha256_bytes(self.canonical_bytes)


@dataclasses.dataclass(frozen=True, slots=True)
class OciRunnerLifecycleReceipt(RunnerLifecycleReceipt):
    evidence_digest: str
    _execution_complete: bool
    credential_finalized: bool
    hidden_inputs_mounted_after_harness_exit: bool
    runtime_verified: bool

    @property
    def execution_complete(self) -> bool:
        return self._execution_complete

    @property
    def trust_tier(self) -> GradingTrustTier:
        return GradingTrustTier.LOCAL_SELF_ATTESTED

    @property
    def official_verified(self) -> bool:
        return False

    @property
    def credentials_destroyed(self) -> bool:
        return self.credential_finalized

    @property
    def hidden_inputs_mounted_after_exit(self) -> bool:
        return self.hidden_inputs_mounted_after_harness_exit

    @property
    def receipt_digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "schema": "atv.oci-runner-lifecycle-receipt/v1",
                    "evidence_digest": self.evidence_digest,
                    "execution_complete": self.execution_complete,
                    "credential_finalized": self.credential_finalized,
                    "hidden_inputs_mounted_after_harness_exit": (
                        self.hidden_inputs_mounted_after_harness_exit
                    ),
                    "runtime_verified": self.runtime_verified,
                    "trust_tier": self.trust_tier.value,
                    "official_verified": False,
                }
            )
        )

    def validate_for_grading(self) -> None:
        if not self.execution_complete:
            raise OciRunnerError("harness execution is not complete")
        if not self.credential_finalized:
            raise OciRunnerError("gateway handle was not completed or revoked")
        if not self.hidden_inputs_mounted_after_harness_exit:
            raise OciRunnerError("hidden inputs were not mounted strictly after harness exit")


@dataclasses.dataclass(frozen=True, slots=True)
class SeedFile:
    path: str
    data: bytes
    size: int
    sha256: str

    def manifest_entry(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclasses.dataclass(frozen=True, slots=True)
class WorkspaceSeedEvidence:
    expected_digest: str
    source_digest: str
    seeded_digest: str
    file_count: int
    total_bytes: int
    verified: bool
    quota_filesystems: Mapping[str, Mapping[str, Any]] = dataclasses.field(
        default_factory=dict
    )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class OciTrialResult:
    status: OciTrialStatus
    evidence: OciRunnerEvidence
    lifecycle_receipt: OciRunnerLifecycleReceipt
    harness_stdout: bytes
    harness_stderr: bytes
    grader_stdout: bytes
    grader_stderr: bytes
    protocol_transcript: ProtocolTranscript | Any | None


def _safe_container_name(attempt: TrialAttempt, phase: ContainerPhase) -> str:
    return (
        f"atv-{phase.value}-{attempt.attempt_id[:18]}-"
        f"{secrets.token_hex(4)}"
    )


def _safe_volume_name(attempt: TrialAttempt, purpose: str) -> str:
    safe_purpose = re.sub(r"[^A-Za-z0-9_.-]+", "-", purpose).strip("-.")
    if not safe_purpose:
        raise ValueError("volume purpose does not produce a safe name")
    return (
        f"atv-{safe_purpose}-{attempt.workspace_id[:18]}-"
        f"{secrets.token_hex(4)}"
    )[:128]


def _make_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    try:
        path.chmod(0o777)
    except OSError:
        pass


def _path_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))),
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))),
        int(getattr(metadata, "st_nlink", 1)),
    )


def _reject_seed_path(metadata: os.stat_result, display: str, *, directory: bool) -> None:
    reparse = bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if stat.S_ISLNK(metadata.st_mode) or reparse:
        raise OciRunnerError(f"seed snapshot contains a link: {display}")
    if directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise OciRunnerError(f"seed path is not a directory: {display}")
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise OciRunnerError(f"seed snapshot contains a special file: {display}")
    if getattr(metadata, "st_nlink", 1) > 1:
        raise OciRunnerError(f"seed snapshot contains a hardlink: {display}")


def _seed_tree_digest(files: Sequence[SeedFile]) -> str:
    manifest = [item.manifest_entry() for item in sorted(files, key=lambda row: row.path)]
    return sha256_bytes(canonical_json_bytes({"files": manifest}))


def _snapshot_seed_tree(
    root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
) -> tuple[SeedFile, ...]:
    absolute = Path(os.path.abspath(root))
    root_before = os.lstat(absolute)
    _reject_seed_path(root_before, ".", directory=True)
    rows: list[SeedFile] = []
    total = 0

    def visit(directory: Path, parts: tuple[str, ...]) -> None:
        nonlocal total
        before = os.lstat(directory)
        _reject_seed_path(before, "/".join(parts) or ".", directory=True)
        with os.scandir(directory) as scanner:
            entries = sorted(scanner, key=lambda entry: entry.name)
        for entry in entries:
            relative_parts = parts + (entry.name,)
            relative = "/".join(relative_parts)
            if (
                entry.name in {"", ".", ".."}
                or "/" in entry.name
                or "\\" in entry.name
                or any(ord(character) < 0x20 for character in entry.name)
            ):
                raise OciRunnerError(f"seed snapshot has unsafe path: {relative!r}")
            metadata = os.lstat(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                _reject_seed_path(metadata, relative, directory=True)
                visit(Path(entry.path), relative_parts)
                continue
            _reject_seed_path(metadata, relative, directory=False)
            if len(rows) >= max_files:
                raise OciRunnerError(f"seed tree exceeds file limit {max_files}")
            try:
                data = read_confined_regular_file(
                    absolute,
                    relative,
                    max_bytes=max_total_bytes,
                )
            except CaptureRejected as exc:
                raise OciRunnerError(str(exc)) from exc
            after = os.lstat(entry.path)
            _reject_seed_path(after, relative, directory=False)
            if _path_identity(metadata) != _path_identity(after):
                raise OciRunnerError(f"seed file changed during snapshot: {relative}")
            total += len(data)
            if total > max_total_bytes:
                raise OciRunnerError(
                    f"seed tree exceeds total byte limit {max_total_bytes}"
                )
            rows.append(
                SeedFile(
                    path=relative,
                    data=data,
                    size=len(data),
                    sha256=sha256_bytes(data),
                )
            )
        directory_after = os.lstat(directory)
        _reject_seed_path(directory_after, "/".join(parts) or ".", directory=True)
        if _path_identity(before) != _path_identity(directory_after):
            raise OciRunnerError(
                f"seed directory changed during snapshot: {'/'.join(parts) or '.'}"
            )

    visit(absolute, ())
    root_after = os.lstat(absolute)
    if _path_identity(root_before) != _path_identity(root_after):
        raise OciRunnerError("seed root changed during snapshot")
    return tuple(sorted(rows, key=lambda item: item.path))


def _write_seed_snapshot(destination: Path, files: Sequence[SeedFile]) -> None:
    if any(destination.iterdir()):
        raise OciRunnerError("fresh workspace was not empty before seeding")
    for item in files:
        target = destination.joinpath(*PurePosixPath(item.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.chmod(0o777)
        except OSError:
            pass
        with target.open("xb") as stream:
            stream.write(item.data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            target.chmod(0o666)
        except OSError:
            pass


def _validated_seed_snapshot(
    task: TaskPackage,
    *,
    max_total_bytes: int,
) -> tuple[tuple[SeedFile, ...], str, str]:
    expected = str(task.manifest["source"]["tree_digest"]["value"])
    source = _snapshot_seed_tree(
        task.public_workspace,
        max_files=100_000,
        max_total_bytes=max_total_bytes,
    )
    source_digest = _seed_tree_digest(source)
    if source_digest != expected:
        raise OciRunnerError(
            "public task snapshot digest changed before workspace seed: "
            f"expected {expected}, observed {source_digest}"
        )
    return source, expected, source_digest


def _seed_workspace_from_snapshot(
    destination: Path,
    files: Sequence[SeedFile],
    *,
    expected: str,
    source_digest: str,
    max_total_bytes: int,
) -> WorkspaceSeedEvidence:
    _write_seed_snapshot(destination, files)
    seeded = _snapshot_seed_tree(
        destination,
        max_files=100_000,
        max_total_bytes=max_total_bytes,
    )
    seeded_digest = _seed_tree_digest(seeded)
    if seeded_digest != expected:
        raise OciRunnerError(
            "seeded workspace digest mismatch: "
            f"expected {expected}, observed {seeded_digest}"
        )
    if [item.manifest_entry() for item in files] != [
        item.manifest_entry() for item in seeded
    ]:
        raise OciRunnerError("seeded workspace bytes differ from public task snapshot")
    return WorkspaceSeedEvidence(
        expected_digest=expected,
        source_digest=source_digest,
        seeded_digest=seeded_digest,
        file_count=len(seeded),
        total_bytes=sum(item.size for item in seeded),
        verified=True,
    )


def _seed_workspace(
    task: TaskPackage,
    destination: Path,
    *,
    max_total_bytes: int,
) -> WorkspaceSeedEvidence:
    source, expected, source_digest = _validated_seed_snapshot(
        task,
        max_total_bytes=max_total_bytes,
    )
    return _seed_workspace_from_snapshot(
        destination,
        source,
        expected=expected,
        source_digest=source_digest,
        max_total_bytes=max_total_bytes,
    )


def _tree_transfer_bytes(
    files: Sequence[SeedFile],
    *,
    expected_digest: str,
) -> bytes:
    ordered = tuple(sorted(files, key=lambda item: item.path))
    header = canonical_json_bytes(
        {
            "schema": "atv.oci-confined-tree-transfer/v1",
            "expected_digest": expected_digest,
            "files": [item.manifest_entry() for item in ordered],
        }
    )
    return header + b"\n" + b"".join(item.data for item in ordered)


def _trusted_tree_transfer_command(
    *,
    destination: str,
    expected_digest: str,
    result_schema: str,
    require_empty: Sequence[str] = (),
    exec_command: Sequence[str] | None = None,
    exec_working_directory: str | None = None,
    quota_layouts: Sequence[tuple[str, str, int, Sequence[str]]] = (),
) -> tuple[str, ...]:
    normalized_quota_layouts = tuple(
        (label, root, limit, tuple(subpaths))
        for label, root, limit, subpaths in quota_layouts
    )
    script = f"""
import hashlib
import json
import os
import pathlib
import stat
import sys

DESTINATION = pathlib.Path({destination!r})
EXPECTED = {expected_digest!r}
RESULT_SCHEMA = {result_schema!r}
REQUIRE_EMPTY = tuple(pathlib.Path(value) for value in {tuple(require_empty)!r})
EXEC_COMMAND = {tuple(exec_command or ())!r}
EXEC_WORKING_DIRECTORY = {exec_working_directory!r}
QUOTA_LAYOUTS = {normalized_quota_layouts!r}
HEADER_LIMIT = 16 * 1024 * 1024

def mount_filesystem_type(path):
    target = str(path)
    for line in pathlib.Path("/proc/self/mountinfo").read_text(
        encoding="utf-8"
    ).splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if separator and len(fields) > 4 and fields[4] == target:
            return after.split()[0]
    raise RuntimeError("quota root is not a distinct container mount")

quota_filesystems = {{}}
for label, root_value, limit, subpaths in QUOTA_LAYOUTS:
    root = pathlib.Path(root_value)
    metadata = os.lstat(root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("quota root is not a confined directory")
    if any(root.iterdir()):
        raise RuntimeError("quota root was not fresh")
    filesystem_type = mount_filesystem_type(root)
    if filesystem_type != "tmpfs":
        raise RuntimeError("quota root is not backed by tmpfs")
    filesystem = os.statvfs(root)
    capacity = filesystem.f_frsize * filesystem.f_blocks
    if capacity <= 0 or capacity > limit:
        raise RuntimeError("quota root capacity exceeds the requested hard limit")
    device = metadata.st_dev
    for subpath in subpaths:
        child = root / subpath
        child.mkdir(mode=0o700)
        child_metadata = os.lstat(child)
        if (
            stat.S_ISLNK(child_metadata.st_mode)
            or not stat.S_ISDIR(child_metadata.st_mode)
            or child_metadata.st_dev != device
        ):
            raise RuntimeError("quota subpath escaped its aggregate filesystem")
    quota_filesystems[label] = {{
        "root": root_value,
        "filesystem_type": filesystem_type,
        "capacity_bytes": capacity,
        "device": str(device),
        "subpaths": list(subpaths),
    }}

if any(DESTINATION.iterdir()) or any(any(path.iterdir()) for path in REQUIRE_EMPTY):
    raise RuntimeError("trusted transfer destination was not fresh")

stream = sys.stdin.buffer
header_line = stream.readline(HEADER_LIMIT + 1)
if (
    not header_line.endswith(b"\\n")
    or len(header_line) > HEADER_LIMIT
):
    raise RuntimeError("trusted transfer header is missing or too large")
header = json.loads(header_line[:-1].decode("utf-8"))
if (
    not isinstance(header, dict)
    or header.get("schema") != "atv.oci-confined-tree-transfer/v1"
    or header.get("expected_digest") != EXPECTED
    or not isinstance(header.get("files"), list)
):
    raise RuntimeError("trusted transfer header is invalid")

manifest = []
seen = set()
for row in header["files"]:
    if not isinstance(row, dict):
        raise RuntimeError("trusted transfer manifest row is invalid")
    relative = row.get("path")
    size = row.get("size")
    digest = row.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or relative in seen
        or "\\\\" in relative
        or any(ord(character) < 0x20 for character in relative)
    ):
        raise RuntimeError("trusted transfer path is unsafe")
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise RuntimeError("trusted transfer path escapes destination")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RuntimeError("trusted transfer manifest metadata is invalid")
    seen.add(relative)
    target = DESTINATION.joinpath(*pure.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    remaining = size
    observed = hashlib.sha256()
    with target.open("xb") as output:
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeError("trusted transfer payload ended early")
            output.write(chunk)
            observed.update(chunk)
            remaining -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    if observed.hexdigest() != digest:
        raise RuntimeError("trusted transfer file digest mismatch")
    manifest.append({{"path": relative, "size": size, "sha256": digest}})

if stream.read(1):
    raise RuntimeError("trusted transfer payload has trailing bytes")
manifest.sort(key=lambda row: row["path"])
canonical = json.dumps(
    {{"files": manifest}},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
digest = hashlib.sha256(canonical).hexdigest()
payload = {{
    "schema": RESULT_SCHEMA,
    "expected_digest": EXPECTED,
    "seeded_digest": digest,
    "file_count": len(manifest),
    "total_bytes": sum(row["size"] for row in manifest),
    "quota_filesystems": quota_filesystems,
}}
if digest != EXPECTED:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    sys.exit(74)
if EXEC_COMMAND:
    if EXEC_WORKING_DIRECTORY is not None:
        os.chdir(EXEC_WORKING_DIRECTORY)
    os.execvp(EXEC_COMMAND[0], EXEC_COMMAND)
print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
""".strip()
    return ("python", "-c", script)


def _trusted_seed_command(
    expected_digest: str,
    *,
    harness_limit: int | None = None,
    grader_limit: int | None = None,
) -> tuple[str, ...]:
    quota_layouts: tuple[tuple[str, str, int, Sequence[str]], ...] = ()
    destination = "/workspace"
    require_empty = ("/artifacts",)
    if harness_limit is not None or grader_limit is not None:
        if harness_limit is None or grader_limit is None:
            raise ValueError("both hard-quota filesystem limits are required")
        quota_layouts = (
            (
                "harness",
                _HARD_QUOTA_HARNESS_ROOT,
                harness_limit,
                _HARD_QUOTA_HARNESS_SUBPATHS,
            ),
            (
                "grader",
                _HARD_QUOTA_GRADER_ROOT,
                grader_limit,
                _HARD_QUOTA_GRADER_SUBPATHS,
            ),
        )
        destination = f"{_HARD_QUOTA_HARNESS_ROOT}/workspace"
        require_empty = (
            f"{_HARD_QUOTA_HARNESS_ROOT}/artifacts",
            f"{_HARD_QUOTA_HARNESS_ROOT}/tmp",
            f"{_HARD_QUOTA_GRADER_ROOT}/output",
            f"{_HARD_QUOTA_GRADER_ROOT}/tmp",
        )
    return _trusted_tree_transfer_command(
        destination=destination,
        expected_digest=expected_digest,
        result_schema="atv.oci-workspace-seed/v1",
        require_empty=require_empty,
        quota_layouts=quota_layouts,
    )


def _trusted_grader_command(
    expected_digest: str,
    grader_command: Sequence[str],
    *,
    quota_limit: int | None = None,
) -> tuple[str, ...]:
    execution_command = tuple(grader_command)
    execution_working_directory: str | None = "/output"
    if quota_limit is not None:
        execution_command = _trusted_chdir_exec_command(
            "/output",
            grader_command,
            quota_paths=("/grader-output", "/tmp"),
            quota_limit=quota_limit,
        )
        execution_working_directory = None
    return _trusted_tree_transfer_command(
        destination="/trusted",
        expected_digest=expected_digest,
        result_schema="atv.oci-hidden-transfer/v1",
        exec_command=execution_command,
        exec_working_directory=execution_working_directory,
    )


def _trusted_chdir_exec_command(
    working_directory: str,
    command: Sequence[str],
    *,
    quota_paths: Sequence[str] = (),
    quota_limit: int | None = None,
) -> tuple[str, ...]:
    command_tuple = tuple(command)
    quota_path_tuple = tuple(quota_paths)
    if bool(quota_path_tuple) != (quota_limit is not None):
        raise ValueError("quota paths and quota limit must be supplied together")
    script = (
        "import os\n"
        "import pathlib\n"
        "import stat\n"
        f"WORKDIR = {working_directory!r}\n"
        f"COMMAND = {command_tuple!r}\n"
        f"QUOTA_PATHS = {quota_path_tuple!r}\n"
        f"QUOTA_LIMIT = {quota_limit!r}\n"
        "def mount_filesystem_type(path):\n"
        "    target = str(path)\n"
        "    for line in pathlib.Path('/proc/self/mountinfo').read_text("
        "encoding='utf-8').splitlines():\n"
        "        before, separator, after = line.partition(' - ')\n"
        "        fields = before.split()\n"
        "        if separator and len(fields) > 4 and fields[4] == target:\n"
        "            return after.split()[0]\n"
        "    raise RuntimeError('quota path is not a distinct container mount')\n"
        "devices = set()\n"
        "capacities = set()\n"
        "for value in QUOTA_PATHS:\n"
        "    path = pathlib.Path(value)\n"
        "    metadata = os.lstat(path)\n"
        "    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):\n"
        "        raise RuntimeError('quota path is not a confined directory')\n"
        "    if mount_filesystem_type(path) != 'tmpfs':\n"
        "        raise RuntimeError('quota path is not backed by tmpfs')\n"
        "    filesystem = os.statvfs(path)\n"
        "    capacity = filesystem.f_frsize * filesystem.f_blocks\n"
        "    if capacity <= 0 or capacity > QUOTA_LIMIT:\n"
        "        raise RuntimeError('quota capacity exceeds the requested hard limit')\n"
        "    devices.add(metadata.st_dev)\n"
        "    capacities.add(capacity)\n"
        "if len(devices) > 1 or len(capacities) > 1:\n"
        "    raise RuntimeError('writable quota paths do not share one filesystem')\n"
        "os.chdir(WORKDIR)\n"
        "os.execvp(COMMAND[0], COMMAND)\n"
    )
    return ("python", "-c", script)


def _parse_hidden_transfer(
    run: EngineRunResult,
    *,
    expected_digest: str,
) -> None:
    if run.stdout_truncated:
        raise OciRunnerError("trusted hidden-input transfer stdout was truncated")
    if run.exit_code != 0 or run.timed_out or run.cancelled:
        detail = run.stderr.decode("utf-8", errors="replace").strip()
        raise OciRunnerError(
            detail or f"trusted hidden-input transfer failed with exit {run.exit_code}"
        )
    try:
        payload = json.loads(run.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRunnerError(
            "trusted hidden-input transfer returned invalid UTF-8 JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "atv.oci-hidden-transfer/v1"
        or payload.get("expected_digest") != expected_digest
        or payload.get("seeded_digest") != expected_digest
    ):
        raise OciRunnerError("trusted hidden-input transfer digest mismatch")


def _parse_seed_container_output(
    run: EngineRunResult,
    *,
    expected: str,
    source_digest: str,
    quota_limits: Mapping[str, int] | None = None,
) -> WorkspaceSeedEvidence:
    if run.stdout_truncated:
        raise OciRunnerError("trusted seed container stdout was truncated")
    if run.exit_code != 0 or run.timed_out or run.cancelled:
        detail = run.stderr.decode("utf-8", errors="replace").strip()
        raise OciRunnerError(
            detail or f"trusted seed container failed with exit {run.exit_code}"
        )
    try:
        payload = json.loads(run.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRunnerError(
            "trusted seed container returned invalid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != (
        "atv.oci-workspace-seed/v1"
    ):
        raise OciRunnerError("trusted seed container returned an invalid evidence shape")
    seeded_digest = str(payload.get("seeded_digest") or "")
    if (
        payload.get("expected_digest") != expected
        or seeded_digest != expected
        or source_digest != expected
    ):
        raise OciRunnerError(
            "seeded workspace digest mismatch: "
            f"expected {expected}, observed {seeded_digest or '<missing>'}"
        )
    file_count = payload.get("file_count")
    total_bytes = payload.get("total_bytes")
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count < 0
        or not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes < 0
    ):
        raise OciRunnerError("trusted seed container returned invalid file totals")
    quota_filesystems = payload.get("quota_filesystems")
    if quota_limits is None:
        if quota_filesystems not in ({}, None):
            raise OciRunnerError(
                "bind-mode seed unexpectedly reported hard-quota filesystems"
            )
        verified_quota_filesystems: Mapping[str, Mapping[str, Any]] = {}
    else:
        if (
            not isinstance(quota_filesystems, dict)
            or set(quota_filesystems) != set(quota_limits)
        ):
            raise OciRunnerError(
                "trusted seed container returned incomplete quota evidence"
            )
        normalized: dict[str, Mapping[str, Any]] = {}
        for label, limit in quota_limits.items():
            observed = quota_filesystems.get(label)
            if not isinstance(observed, dict):
                raise OciRunnerError(
                    "trusted seed container returned invalid quota evidence"
                )
            capacity = observed.get("capacity_bytes")
            device = observed.get("device")
            subpaths = observed.get("subpaths")
            expected_subpaths = (
                _HARD_QUOTA_HARNESS_SUBPATHS
                if label == "harness"
                else _HARD_QUOTA_GRADER_SUBPATHS
            )
            if (
                observed.get("filesystem_type") != "tmpfs"
                or not isinstance(capacity, int)
                or isinstance(capacity, bool)
                or capacity <= 0
                or capacity > limit
                or not isinstance(device, str)
                or not device
                or tuple(subpaths or ()) != expected_subpaths
            ):
                raise OciRunnerError(
                    "trusted seed container quota evidence did not match policy"
                )
            normalized[label] = dict(observed)
        verified_quota_filesystems = normalized
    return WorkspaceSeedEvidence(
        expected_digest=expected,
        source_digest=source_digest,
        seeded_digest=seeded_digest,
        file_count=file_count,
        total_bytes=total_bytes,
        verified=True,
        quota_filesystems=verified_quota_filesystems,
    )


def _trusted_output_capture_command(
    *,
    workspace_limit: int,
    artifact_limit: int,
) -> tuple[str, ...]:
    script = f"""
import hashlib
import json
import os
import pathlib
import stat

def scan(root, limit):
    rows = []
    total = 0
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        names.sort()
        filenames.sort()
        for name in list(names):
            path = pathlib.Path(directory) / name
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("output contains an unsafe directory")
        for name in filenames:
            path = pathlib.Path(directory) / name
            metadata = os.lstat(path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink > 1
            ):
                raise RuntimeError("output contains a link, hardlink, or special file")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            total += size
            if total > limit:
                raise RuntimeError("output exceeds its verified quota")
            rows.append({{
                "path": path.relative_to(root).as_posix(),
                "size": size,
                "sha256": digest.hexdigest(),
            }})
    rows.sort(key=lambda row: row["path"])
    canonical = json.dumps(
        {{"files": rows}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {{
        "file_count": len(rows),
        "total_bytes": total,
        "tree_digest": hashlib.sha256(canonical).hexdigest(),
    }}

payload = {{
    "schema": "atv.oci-output-capture/v1",
    "workspace": scan(pathlib.Path("/output"), {workspace_limit}),
    "artifacts": scan(pathlib.Path("/harness-artifacts"), {artifact_limit}),
}}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
""".strip()
    return ("python", "-c", script)


def _parse_output_capture(run: EngineRunResult) -> dict[str, Any]:
    if run.stdout_truncated:
        raise OciRunnerError("trusted output capture stdout was truncated")
    if run.exit_code != 0 or run.timed_out or run.cancelled:
        detail = run.stderr.decode("utf-8", errors="replace").strip()
        raise OciRunnerError(
            detail or f"trusted output capture failed with exit {run.exit_code}"
        )
    try:
        payload = json.loads(run.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciRunnerError(
            "trusted output capture returned invalid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != (
        "atv.oci-output-capture/v1"
    ):
        raise OciRunnerError("trusted output capture returned an invalid evidence shape")
    for key in ("workspace", "artifacts"):
        value = payload.get(key)
        if not isinstance(value, dict):
            raise OciRunnerError("trusted output capture is missing tree evidence")
        for integer_key in ("file_count", "total_bytes"):
            item = value.get(integer_key)
            if (
                not isinstance(item, int)
                or isinstance(item, bool)
                or item < 0
            ):
                raise OciRunnerError("trusted output capture returned invalid totals")
        digest = value.get("tree_digest")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise OciRunnerError("trusted output capture returned an invalid digest")
    return payload


def _write_handle_env(path: Path, handle: OpaqueTrialHandle) -> tuple[str, ...]:
    values = handle.harness_env()
    data = "".join(f"{key}={value}\n" for key, value in values.items())
    path.write_text(data, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return tuple(values)


def _tree_usage(root: Path, *, maximum: int | None = None) -> tuple[int, int]:
    total = 0
    files = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or bool(
                    getattr(metadata, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    raise OciRunnerError(f"output contains a link: {entry.path}")
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise OciRunnerError(f"output contains a special file: {entry.path}")
                if getattr(metadata, "st_nlink", 1) > 1:
                    raise OciRunnerError(f"output contains a hardlink: {entry.path}")
                files += 1
                total += metadata.st_size
                if maximum is not None and total > maximum:
                    raise OciRunnerError(
                        f"output storage exceeds enforced limit of {maximum} bytes"
                    )
    return files, total


def _legacy_normalize_protocol_output(
    stdout: bytes,
    accepted_event: Mapping[str, Any],
) -> bytes:
    """Legacy integrity-only merge; never grants controller authority."""
    newline = stdout.find(b"\n")
    if newline < 0:
        first = stdout
        remainder = b""
    else:
        first = stdout[:newline]
        remainder = stdout[newline + 1 :]
    if not first:
        raise OciRunnerError("protocol output is missing the hello event")
    accepted = canonical_json_bytes(dict(accepted_event))
    normalized = first + b"\n" + accepted + b"\n"
    if remainder:
        normalized += remainder
        if not normalized.endswith(b"\n"):
            normalized += b"\n"
    return normalized


class OciTrialRunner:
    def __init__(
        self,
        engine: OciEngine,
        *,
        work_root: Path | str | None = None,
        runner_id: str = RUNNER_ID,
        interactive_transport: Any | None = None,
    ) -> None:
        self.engine = engine
        self.work_root = Path(work_root).resolve() if work_root else None
        self.runner_id = runner_id
        self._interactive_transport_override = interactive_transport

    def _interactive_transport(self) -> Any:
        if self._interactive_transport_override is not None:
            return self._interactive_transport_override
        if not isinstance(self.engine, CliOciEngine):
            raise EngineUnavailableError(
                "trusted protocol execution requires an injected interactive "
                "transport for non-CLI OCI engines"
            )
        from .interactive import CliInteractiveOciBackend, InteractiveOciTransport

        return InteractiveOciTransport(
            CliInteractiveOciBackend(self.engine.executable)
        )

    def _hard_quota_capability(self) -> tuple[bool, str]:
        method = getattr(self.engine, "hard_quota_volume_capability", None)
        required = (
            "create_volume",
            "inspect_volume",
            "remove_volume",
            "volume_exists",
            "exec_container",
        )
        if method is None or any(
            not callable(getattr(self.engine, name, None)) for name in required
        ):
            return (
                False,
                "engine does not implement the size-limited tmpfs named-volume capability",
            )
        try:
            supported, detail = method()
        except Exception as exc:
            return False, f"hard-quota capability probe failed: {exc}"
        return bool(supported), str(detail)

    def _create_quota_volume(self, spec: VolumeSpec) -> VolumeLifecycleEvidence:
        create = self.engine.create_volume(spec)
        inspection: VolumeInspection | None = None
        mismatches: tuple[str, ...] = ()
        if (
            create.exit_code == 0
            and not create.timed_out
            and not create.cancelled
        ):
            try:
                inspection = self.engine.inspect_volume(spec.name)
                mismatches = inspection.mismatches(spec)
            except Exception as exc:
                mismatches = (f"volume_inspection_failed:{exc}",)
        else:
            detail = create.stderr.decode("utf-8", errors="replace").strip()
            mismatches = (
                f"volume_create_failed:{detail or f'exit {create.exit_code}'}",
            )
        return VolumeLifecycleEvidence(
            spec=spec,
            create=create,
            inspection=inspection,
            inspection_mismatches=mismatches,
            cleanup=VolumeCleanupEvidence(
                volume_name=spec.name,
                attempted=False,
                status=CleanupStatus.NOT_ATTEMPTED,
                remove_argv=(),
                remove_exit_code=None,
                confirmed_absent=False,
            ),
        )

    def _cleanup_volume(self, name: str) -> VolumeCleanupEvidence:
        try:
            remove = self.engine.remove_volume(name, force=True)
        except Exception as exc:
            return VolumeCleanupEvidence(
                volume_name=name,
                attempted=True,
                status=CleanupStatus.FAILED,
                remove_argv=(self.engine.executable, "volume", "rm", "-f", name),
                remove_exit_code=None,
                confirmed_absent=False,
                error=f"volume force-remove failed: {exc}",
            )
        try:
            exists = self.engine.volume_exists(name)
        except Exception as exc:
            return VolumeCleanupEvidence(
                volume_name=name,
                attempted=True,
                status=CleanupStatus.FAILED,
                remove_argv=remove.argv,
                remove_exit_code=remove.exit_code,
                confirmed_absent=False,
                error=f"volume absence could not be verified: {exc}",
            )
        succeeded = not exists
        detail = None
        if not succeeded:
            detail = "volume still exists after force-remove"
        elif remove.exit_code not in (0, None):
            detail = "volume force-remove returned nonzero but absence was verified"
        return VolumeCleanupEvidence(
            volume_name=name,
            attempted=True,
            status=CleanupStatus.SUCCEEDED if succeeded else CleanupStatus.FAILED,
            remove_argv=remove.argv,
            remove_exit_code=remove.exit_code,
            confirmed_absent=not exists,
            error=detail,
        )

    @staticmethod
    def _hard_storage_observation(
        volume_evidence: Sequence[VolumeLifecycleEvidence],
        *,
        run: EngineRunResult | None = None,
        cleanup_succeeded: bool = False,
    ) -> StorageObservation:
        exceeded = False
        if run is not None:
            combined = (run.stdout + b"\n" + run.stderr).lower()
            exceeded = any(
                marker in combined
                for marker in (
                    b"no space left on device",
                    b"enospc",
                    b"errno 28",
                )
            )
        limits = tuple(
            (item.spec.purpose, item.spec.size_bytes) for item in volume_evidence
        )
        verified = bool(volume_evidence) and all(
            item.quota_verified for item in volume_evidence
        )
        return StorageObservation(
            limit_bytes=sum(value for _, value in limits),
            peak_bytes=0,
            exceeded=exceeded,
            monitor_error=None if verified else "hard quota volume verification failed",
            completed=True,
            enforcement=_HARD_QUOTA_ENFORCEMENT,
            hard_quota_verified=verified,
            volume_names=tuple(item.spec.name for item in volume_evidence),
            component_limits=limits,
            cleanup_succeeded=cleanup_succeeded,
        )

    def _run_phase(
        self,
        spec: ContainerSpec,
        *,
        hard_quota_volumes: Sequence[VolumeLifecycleEvidence],
        writable_roots: Sequence[Path],
        cancel_event: threading.Event | None,
    ) -> tuple[EngineRunResult, StorageObservation]:
        if hard_quota_volumes:
            run = self.engine.run_container(spec, cancel_event=cancel_event)
            return run, self._hard_storage_observation(
                hard_quota_volumes,
                run=run,
            )
        return self._run_with_storage_monitor(
            spec,
            writable_roots=writable_roots,
            cancel_event=cancel_event,
        )

    def _run_with_storage_monitor(
        self,
        spec: ContainerSpec,
        *,
        writable_roots: Sequence[Path],
        cancel_event: threading.Event | None,
    ) -> tuple[EngineRunResult, StorageObservation]:
        run, observation = self._run_with_storage_monitor_call(
            spec,
            writable_roots=writable_roots,
            cancel_event=cancel_event,
            invoke=lambda phase_cancel: self.engine.run_container(
                spec,
                cancel_event=phase_cancel,
            ),
        )
        if not isinstance(run, EngineRunResult):
            raise OciRunnerError("OCI engine returned an invalid phase result")
        return run, observation

    def _run_with_storage_monitor_call(
        self,
        spec: ContainerSpec,
        *,
        writable_roots: Sequence[Path],
        cancel_event: threading.Event | None,
        invoke: Callable[[threading.Event], Any],
    ) -> tuple[Any, StorageObservation]:
        stop = threading.Event()
        phase_cancel = threading.Event()
        state: dict[str, Any] = {
            "peak": 0,
            "exceeded": False,
            "error": None,
        }

        def observe() -> None:
            try:
                total = sum(_tree_usage(root)[1] for root in writable_roots)
                state["peak"] = max(int(state["peak"]), total)
                if total > spec.resources.storage_bytes:
                    state["exceeded"] = True
                    phase_cancel.set()
            except Exception as exc:
                state["error"] = str(exc)
                phase_cancel.set()

        def monitor() -> None:
            while not stop.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    phase_cancel.set()
                observe()
                stop.wait(0.025)

        observe()
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            result = invoke(phase_cancel)
        finally:
            stop.set()
            watcher.join(timeout=2)
            observe()
        return result, StorageObservation(
            limit_bytes=spec.resources.storage_bytes,
            peak_bytes=int(state["peak"]),
            exceeded=bool(state["exceeded"]),
            monitor_error=(
                str(state["error"]) if state["error"] is not None else None
            ),
            completed=not watcher.is_alive(),
        )

    @staticmethod
    def _interactive_phase_status(status: Any) -> OciTrialStatus:
        value = getattr(status, "value", str(status))
        return {
            "completed": OciTrialStatus.COMPLETED,
            "timed_out": OciTrialStatus.TIMED_OUT,
            "cancelled": OciTrialStatus.CANCELLED,
            "nonzero_exit": OciTrialStatus.NONZERO_EXIT,
            "protocol_error": OciTrialStatus.PROTOCOL_ERROR,
            "limit_error": OciTrialStatus.PROTOCOL_ERROR,
            "pipe_leak": OciTrialStatus.PROTOCOL_ERROR,
            "cleanup_error": OciTrialStatus.CLEANUP_FAILED,
            "transport_error": OciTrialStatus.ENGINE_ERROR,
        }.get(value, OciTrialStatus.ENGINE_ERROR)

    def _interactive_engine_run(
        self,
        spec: ContainerSpec,
        result: Any,
    ) -> EngineRunResult:
        from .interactive import build_interactive_run_argv

        stdout = bytes(result.stdout)
        stderr = bytes(result.stderr)
        evidence = result.evidence
        return EngineRunResult(
            argv=build_interactive_run_argv(self.engine.executable, spec),
            exit_code=evidence.process_exit_code,
            timed_out=getattr(result.status, "value", "") == "timed_out",
            cancelled=getattr(result.status, "value", "") == "cancelled",
            duration_ms=evidence.duration_ms,
            stdout=stdout,
            stderr=stderr,
            stdout_total_bytes=evidence.stdout_total_bytes,
            stderr_total_bytes=evidence.stderr_total_bytes,
            stdout_truncated=evidence.stdout_total_bytes > len(stdout),
            stderr_truncated=evidence.stderr_total_bytes > len(stderr),
        )

    def _interactive_cleanup_evidence(self, result: Any) -> CleanupEvidence:
        observed = result.evidence.cleanup
        succeeded = bool(observed.confirmed_absent)
        detail = None
        if not succeeded:
            detail = "interactive transport did not verify container absence"
        elif observed.remove_exit_code not in (0, None):
            detail = (
                "interactive force-remove returned nonzero but container absence "
                "was verified"
            )
        return CleanupEvidence(
            container_name=observed.container_name,
            attempted=observed.remove_attempted,
            status=(
                CleanupStatus.SUCCEEDED
                if succeeded
                else CleanupStatus.FAILED
            ),
            remove_argv=(
                self.engine.executable,
                "rm",
                "-f",
                observed.container_name,
            ),
            remove_exit_code=observed.remove_exit_code,
            confirmed_absent=succeeded,
            error=detail,
        )

    def _cleanup_container(self, name: str) -> CleanupEvidence:
        try:
            remove = self.engine.remove_container(name, force=True)
        except Exception as exc:
            return CleanupEvidence(
                container_name=name,
                attempted=True,
                status=CleanupStatus.FAILED,
                remove_argv=(self.engine.executable, "rm", "-f", name),
                remove_exit_code=None,
                confirmed_absent=False,
                error=f"force-remove failed: {exc}",
            )
        try:
            exists = self.engine.container_exists(name)
        except Exception as exc:
            return CleanupEvidence(
                container_name=name,
                attempted=True,
                status=CleanupStatus.FAILED,
                remove_argv=remove.argv,
                remove_exit_code=remove.exit_code,
                confirmed_absent=False,
                error=f"container absence could not be verified: {exc}",
            )
        succeeded = not exists
        detail = None
        if not succeeded:
            detail = "container still exists after force-remove"
        elif remove.exit_code not in (0, None):
            detail = "force-remove returned nonzero but container is confirmed absent"
        return CleanupEvidence(
            container_name=name,
            attempted=True,
            status=CleanupStatus.SUCCEEDED if succeeded else CleanupStatus.FAILED,
            remove_argv=remove.argv,
            remove_exit_code=remove.exit_code,
            confirmed_absent=not exists,
            error=detail,
        )

    @staticmethod
    def _phase_status(run: EngineRunResult) -> OciTrialStatus:
        if run.cancelled:
            return OciTrialStatus.CANCELLED
        if run.timed_out:
            return OciTrialStatus.TIMED_OUT
        if run.exit_code != 0:
            return OciTrialStatus.NONZERO_EXIT
        return OciTrialStatus.COMPLETED

    def run(
        self,
        request: OciTrialRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> OciTrialResult:
        started_mono = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        phase_order: list[str] = []
        errors: list[str] = []
        images: list[ImageInspection] = []
        volume_evidence: list[VolumeLifecycleEvidence] = []
        volume_keeper_phase: PhaseEvidence | None = None
        seed_phase: PhaseEvidence | None = None
        harness_phase: PhaseEvidence | None = None
        output_capture_phase: PhaseEvidence | None = None
        grader_phase: PhaseEvidence | None = None
        protocol_transcript: ProtocolTranscript | Any | None = None
        interactive_result: Any | None = None
        protocol_evidence: dict[str, Any] = {
            "enabled": (
                request.protocol_session is not None
                or request.protocol_parser is not None
            ),
            "mode": (
                "trusted-protocol-session"
                if request.protocol_session is not None
                else "legacy-accepted-splice"
                if request.protocol_parser is not None
                else "disabled"
            ),
            "parsed": False,
            "authority_verified": False,
            "integrity_only": request.protocol_parser is not None,
            "official_eligible": False,
            "transcript_sha256": None,
            "error": None,
        }
        handle_action = "not_applicable"
        handle_finalized = request.gateway_handle is None
        hidden_late = False
        result_status = OciTrialStatus.ENGINE_ERROR
        harness_stdout = b""
        harness_stderr = b""
        grader_stdout = b""
        grader_stderr = b""
        engine_identity = self.engine.identity()

        prefix = f"atv-{request.attempt.workspace_id[:16]}-"
        attempt_root = Path(
            tempfile.mkdtemp(
                prefix=prefix,
                dir=str(self.work_root) if self.work_root else None,
            )
        ).resolve()
        workspace = attempt_root / "workspace"
        artifacts = attempt_root / "artifacts"
        harness_temp = attempt_root / "harness-temp"
        grader_output = attempt_root / "grader-output"
        grader_temp = attempt_root / "grader-temp"
        phase_order.append("ephemeral_root_created")
        env_file: Path | None = None
        keeper_name = _safe_container_name(
            request.attempt, ContainerPhase.VOLUME_KEEPER
        )
        seed_name = _safe_container_name(request.attempt, ContainerPhase.SEED)
        harness_name = _safe_container_name(request.attempt, ContainerPhase.HARNESS)
        capture_name = _safe_container_name(
            request.attempt, ContainerPhase.OUTPUT_CAPTURE
        )
        grader_name = _safe_container_name(request.attempt, ContainerPhase.GRADER)
        active_names: set[str] = set()
        workspace_files = 0
        workspace_bytes = 0
        artifact_bytes = 0
        workspace_cleaned = False
        seed_evidence: WorkspaceSeedEvidence | None = None
        output_capture: dict[str, Any] | None = None
        network_before: NetworkInspection | None = None
        network_after: NetworkInspection | None = None
        hard_quota_active = False
        hard_quota_capable, hard_quota_capability_detail = (
            self._hard_quota_capability()
        )
        storage_selected_mode = OciStorageMode.BIND_MONITOR.value
        storage_fallback_reason: str | None = None
        harness_volume: VolumeLifecycleEvidence | None = None
        grader_volume: VolumeLifecycleEvidence | None = None
        trusted_root = (request.task.root / "trusted").resolve(strict=True)
        hidden_files: tuple[SeedFile, ...] = ()
        hidden_tree_digest: str | None = None

        try:
            seed_files, expected_seed_digest, source_seed_digest = (
                _validated_seed_snapshot(
                    request.task,
                    max_total_bytes=request.harness_resources.storage_bytes,
                )
            )
            phase_order.append("seed_snapshot_validated")
            hidden_files = _snapshot_seed_tree(
                trusted_root,
                max_files=100_000,
                max_total_bytes=request.grader_resources.tmpfs_bytes,
            )
            hidden_tree_digest = _seed_tree_digest(hidden_files)
            phase_order.append("hidden_inputs_snapshotted")
            for image in (
                request.task_image,
                request.harness_image,
                request.grader_image,
            ):
                assert isinstance(image, DigestPinnedImage)
                inspection = self.engine.inspect_image(image)
                images.append(inspection)
                if not inspection.verified:
                    raise OciRunnerError(
                        f"engine did not resolve expected image digest: {image.reference}"
                    )
                if inspection.declared_volumes:
                    raise OciRunnerError(
                        "image declares implicit writable volumes, which would bypass "
                        "the verified mount policy: "
                        + ", ".join(inspection.declared_volumes)
                    )
            phase_order.append("images_inspected")

            hard_quota_requested = (
                request.storage_mode is not OciStorageMode.BIND_MONITOR
            )
            if hard_quota_requested and not hard_quota_capable:
                if request.storage_mode is OciStorageMode.HARD_QUOTA:
                    raise EngineUnavailableError(hard_quota_capability_detail)
                storage_fallback_reason = hard_quota_capability_detail
            elif hard_quota_requested:
                volume_specs = (
                    VolumeSpec(
                        purpose="harness-aggregate",
                        name=_safe_volume_name(request.attempt, "harness-aggregate"),
                        size_bytes=request.harness_resources.storage_bytes,
                        subpaths=_HARD_QUOTA_HARNESS_SUBPATHS,
                    ),
                    VolumeSpec(
                        purpose="grader-aggregate",
                        name=_safe_volume_name(request.attempt, "grader-aggregate"),
                        size_bytes=request.grader_resources.storage_bytes,
                        subpaths=_HARD_QUOTA_GRADER_SUBPATHS,
                    ),
                )
                setup_error: str | None = None
                for volume_spec in volume_specs:
                    lifecycle = self._create_quota_volume(volume_spec)
                    volume_evidence.append(lifecycle)
                    phase_order.append(
                        f"{volume_spec.purpose}_volume_"
                        + ("created" if lifecycle.create.exit_code == 0 else "create_failed")
                    )
                    if lifecycle.inspection is not None:
                        phase_order.append(f"{volume_spec.purpose}_volume_inspected")
                    if not lifecycle.quota_verified:
                        setup_error = "; ".join(lifecycle.inspection_mismatches)
                        break
                if setup_error is not None:
                    detail = f"hard-quota volume setup failed: {setup_error}"
                    cleanup_failed = False
                    for index, lifecycle in enumerate(tuple(volume_evidence)):
                        if lifecycle.cleanup.status is not CleanupStatus.NOT_ATTEMPTED:
                            continue
                        cleanup = self._cleanup_volume(lifecycle.spec.name)
                        volume_evidence[index] = dataclasses.replace(
                            lifecycle,
                            cleanup=cleanup,
                        )
                        phase_order.append(
                            f"{lifecycle.spec.purpose}_volume_"
                            + (
                                "removed_after_setup_failure"
                                if cleanup.status is CleanupStatus.SUCCEEDED
                                else "cleanup_failed_after_setup_failure"
                            )
                        )
                        cleanup_failed = (
                            cleanup_failed
                            or cleanup.status is CleanupStatus.FAILED
                        )
                    if cleanup_failed:
                        raise OciRunnerError(
                            f"{detail}; failed to clean partial quota volumes"
                        )
                    # Once an engine advertises hard-quota support, any create or
                    # verification mismatch is a security failure. Falling back
                    # after a partial hard-quota setup would silently weaken the
                    # requested containment.
                    raise OciRunnerError(detail)
                else:
                    hard_quota_active = True
                    storage_selected_mode = OciStorageMode.HARD_QUOTA.value
                    harness_volume, grader_volume = volume_evidence

            if hard_quota_active:
                assert harness_volume is not None
                assert grader_volume is not None
                keeper_spec = ContainerSpec(
                    phase=ContainerPhase.VOLUME_KEEPER,
                    name=keeper_name,
                    image=request.task_image,
                    command=("python", "-c", "import time; time.sleep(86400)"),
                    mounts=(
                        MountSpec.volume(
                            harness_volume.spec.name,
                            _HARD_QUOTA_HARNESS_ROOT,
                            True,
                            no_copy=True,
                        ),
                        MountSpec.volume(
                            grader_volume.spec.name,
                            _HARD_QUOTA_GRADER_ROOT,
                            True,
                            no_copy=True,
                        ),
                    ),
                    resources=dataclasses.replace(
                        request.harness_resources,
                        wall_time_ms=max(
                            request.harness_resources.wall_time_ms,
                            30_000,
                        ),
                    ),
                    network=OciNetworkPolicy.none(),
                    working_directory="/tmp",
                    env_file=None,
                    environment_names=(),
                    detached=True,
                )
                active_names.add(keeper_name)
                keeper_run = self.engine.run_container(keeper_spec)
                if (
                    keeper_run.exit_code != 0
                    or keeper_run.timed_out
                    or keeper_run.cancelled
                ):
                    detail = keeper_run.stderr.decode(
                        "utf-8", errors="replace"
                    ).strip()
                    raise OciRunnerError(
                        detail or "hard-quota volume keeper failed to start"
                    )
                phase_order.append("volume_keeper_started")
                try:
                    keeper_inspection = self.engine.inspect_container(keeper_name)
                    keeper_mismatches = keeper_inspection.mismatches(keeper_spec)
                except Exception as exc:
                    keeper_inspection = None
                    keeper_mismatches = (
                        f"container_inspection_failed:{exc}",
                    )
                keeper_storage = self._hard_storage_observation(
                    volume_evidence,
                    run=keeper_run,
                )
                volume_keeper_phase = PhaseEvidence(
                    phase=ContainerPhase.VOLUME_KEEPER,
                    policy=keeper_spec.policy_dict(),
                    run=keeper_run,
                    inspection=keeper_inspection,
                    inspection_mismatches=keeper_mismatches,
                    storage=keeper_storage,
                    cleanup=_cleanup_not_attempted(keeper_name),
                )
                phase_order.append("volume_keeper_inspected")
                if keeper_mismatches:
                    raise OciRunnerError(
                        "volume keeper runtime policy mismatch: "
                        + ", ".join(keeper_mismatches)
                    )

                seed_spec = ContainerSpec(
                    phase=ContainerPhase.SEED,
                    name=seed_name,
                    image=request.task_image,
                    command=_trusted_seed_command(
                        expected_seed_digest,
                        harness_limit=request.harness_resources.storage_bytes,
                        grader_limit=request.grader_resources.storage_bytes,
                    ),
                    mounts=(
                        MountSpec.volume(
                            harness_volume.spec.name,
                            _HARD_QUOTA_HARNESS_ROOT,
                            False,
                            no_copy=True,
                        ),
                        MountSpec.volume(
                            grader_volume.spec.name,
                            _HARD_QUOTA_GRADER_ROOT,
                            False,
                            no_copy=True,
                        ),
                    ),
                    resources=dataclasses.replace(
                        request.harness_resources,
                        wall_time_ms=max(
                            request.harness_resources.wall_time_ms,
                            30_000,
                        ),
                    ),
                    network=OciNetworkPolicy.none(),
                    working_directory="/tmp",
                    env_file=None,
                    environment_names=(),
                    stdin_open=True,
                )
                phase_order.append("seed_started")
                active_names.add(seed_name)
                seed_transfer = _tree_transfer_bytes(
                    seed_files,
                    expected_digest=expected_seed_digest,
                )
                seed_run = self.engine.run_container(
                    seed_spec,
                    stdin_data=seed_transfer,
                )
                seed_storage = self._hard_storage_observation(
                    (harness_volume, grader_volume),
                    run=seed_run,
                )
                phase_order.append("seed_exited")
                try:
                    seed_inspection = self.engine.inspect_container(seed_name)
                    seed_mismatches = seed_inspection.mismatches(seed_spec)
                except Exception as exc:
                    seed_inspection = None
                    seed_mismatches = (f"container_inspection_failed:{exc}",)
                seed_cleanup = self._cleanup_container(seed_name)
                active_names.discard(seed_name)
                phase_order.append("seed_container_removed")
                seed_policy = seed_spec.policy_dict()
                seed_policy["payload_command"] = list(
                    _trusted_seed_command(
                        expected_seed_digest,
                        harness_limit=request.harness_resources.storage_bytes,
                        grader_limit=request.grader_resources.storage_bytes,
                    )
                )
                seed_policy["snapshot_transfer"] = {
                    "mode": "controller-stdin-to-trusted-seed",
                    "payload_bytes": len(seed_transfer),
                    "payload_sha256": sha256_bytes(seed_transfer),
                }
                seed_phase = PhaseEvidence(
                    phase=ContainerPhase.SEED,
                    policy=seed_policy,
                    run=seed_run,
                    inspection=seed_inspection,
                    inspection_mismatches=seed_mismatches,
                    storage=seed_storage,
                    cleanup=seed_cleanup,
                )
                if seed_cleanup.status is CleanupStatus.FAILED:
                    raise OciRunnerError(
                        seed_cleanup.error or "trusted seed container cleanup failed"
                    )
                if seed_mismatches:
                    raise OciRunnerError(
                        "trusted seed runtime policy mismatch: "
                        + ", ".join(seed_mismatches)
                    )
                seed_evidence = _parse_seed_container_output(
                    seed_run,
                    expected=expected_seed_digest,
                    source_digest=source_seed_digest,
                    quota_limits={
                        "harness": request.harness_resources.storage_bytes,
                        "grader": request.grader_resources.storage_bytes,
                    },
                )
                phase_order.append("workspace_seeded")
            else:
                _make_writable_directory(workspace)
                _make_writable_directory(artifacts)
                _make_writable_directory(harness_temp)
                _make_writable_directory(grader_output)
                _make_writable_directory(grader_temp)
                phase_order.append("bind_workspace_created")
                seed_evidence = _seed_workspace_from_snapshot(
                    workspace,
                    seed_files,
                    expected=expected_seed_digest,
                    source_digest=source_seed_digest,
                    max_total_bytes=request.harness_resources.storage_bytes,
                )
                phase_order.append("workspace_seeded")

            if request.network.mode is OciNetworkMode.MODEL_GATEWAY_ONLY:
                assert request.network.network_name is not None
                network_before = self.engine.inspect_network(
                    request.network.network_name
                )
                if (
                    network_before.name != request.network.network_name
                    or not network_before.internal
                    or network_before.endpoint_identities
                    != tuple(sorted(request.network.allowed_gateway_identities))
                ):
                    raise NetworkPolicyError(
                        "model gateway network must be internal and contain exactly "
                        "the declared gateway identities"
                    )
                phase_order.append("gateway_network_inspected")

            environment_names: tuple[str, ...] = ()
            if request.gateway_handle is not None:
                env_file = attempt_root / "harness.env"
                environment_names = _write_handle_env(
                    env_file, request.gateway_handle
                )

            if hard_quota_active:
                assert harness_volume is not None
                harness_mounts = (
                    MountSpec.volume(
                        harness_volume.spec.name,
                        "/workspace",
                        False,
                        subpath="workspace",
                        no_copy=True,
                    ),
                    MountSpec.volume(
                        harness_volume.spec.name,
                        "/artifacts",
                        False,
                        subpath="artifacts",
                        no_copy=True,
                    ),
                    MountSpec.volume(
                        harness_volume.spec.name,
                        "/tmp",
                        False,
                        subpath="tmp",
                        no_copy=True,
                    ),
                )
                harness_quota_volumes = (harness_volume,)
                harness_writable_roots: tuple[Path, ...] = ()
                harness_runtime_command = _trusted_chdir_exec_command(
                    "/workspace",
                    request.harness_command,
                    quota_paths=("/workspace", "/artifacts", "/tmp"),
                    quota_limit=request.harness_resources.storage_bytes,
                )
                harness_working_directory = "/tmp"
            else:
                harness_mounts = (
                    MountSpec(request.task.public_workspace, "/task", True),
                    MountSpec(request.task.prompt_path, "/prompt/task.md", True),
                    MountSpec(workspace, "/workspace", False),
                    MountSpec(artifacts, "/artifacts", False),
                    MountSpec(harness_temp, "/tmp", False),
                )
                harness_quota_volumes = ()
                harness_writable_roots = (workspace, artifacts, harness_temp)
                harness_runtime_command = request.harness_command
                harness_working_directory = "/workspace"

            if any(
                mount.host_path is not None
                and (
                    mount.host_path == trusted_root
                    or trusted_root in mount.host_path.parents
                )
                for mount in harness_mounts
            ):
                raise OciRunnerError("trusted task data entered the harness mount set")
            harness_spec = ContainerSpec(
                phase=ContainerPhase.HARNESS,
                name=harness_name,
                image=request.harness_image,
                command=harness_runtime_command,
                mounts=harness_mounts,
                resources=request.harness_resources,
                network=request.network,
                working_directory=harness_working_directory,
                env_file=env_file,
                environment_names=environment_names,
                stdin_open=request.protocol_session is not None,
            )
            phase_order.append("harness_started")
            active_names.add(harness_name)
            if request.protocol_session is not None:
                inspection_state: dict[str, Any] = {
                    "observed": False,
                    "inspection": None,
                    "mismatches": (),
                }

                def inspect_before_cleanup(spec: ContainerSpec) -> None:
                    try:
                        inspection = self.engine.inspect_container(spec.name)
                        inspection_state.update(
                            {
                                "observed": True,
                                "inspection": inspection,
                                "mismatches": inspection.mismatches(spec),
                            }
                        )
                    except Exception as exc:
                        inspection_state.update(
                            {
                                "observed": True,
                                "inspection": None,
                                "mismatches": (
                                    f"container_inspection_failed:{exc}",
                                ),
                            }
                        )
                        raise

                transport = self._interactive_transport()
                if harness_quota_volumes:
                    interactive_result = transport.run(
                        harness_spec,
                        request.protocol_session,
                        cancel_event=cancel_event,
                        before_cleanup=inspect_before_cleanup,
                    )
                    harness_run = self._interactive_engine_run(
                        harness_spec,
                        interactive_result,
                    )
                    harness_storage = self._hard_storage_observation(
                        harness_quota_volumes,
                        run=harness_run,
                    )
                else:
                    interactive_result, harness_storage = (
                        self._run_with_storage_monitor_call(
                            harness_spec,
                            writable_roots=harness_writable_roots,
                            cancel_event=cancel_event,
                            invoke=lambda phase_cancel: transport.run(
                                harness_spec,
                                request.protocol_session,
                                cancel_event=phase_cancel,
                                before_cleanup=inspect_before_cleanup,
                            ),
                        )
                    )
                    harness_run = self._interactive_engine_run(
                        harness_spec,
                        interactive_result,
                    )
                harness_inspection = inspection_state["inspection"]
                harness_mismatches = tuple(inspection_state["mismatches"])
                if not inspection_state["observed"]:
                    harness_mismatches = (
                        "container_inspection_not_observed_before_cleanup",
                    )
                harness_cleanup = self._interactive_cleanup_evidence(
                    interactive_result
                )
                protocol_transcript = interactive_result.transcript
                protocol_evidence.update(interactive_result.evidence.to_dict())
                protocol_evidence.update(
                    {
                        "enabled": True,
                        "parsed": protocol_transcript is not None,
                        "authority_verified": bool(
                            interactive_result.authority_verified
                        ),
                        "integrity_only": False,
                        "official_eligible": bool(
                            interactive_result.authority_verified
                            and harness_cleanup.status
                            is CleanupStatus.SUCCEEDED
                        ),
                        "transport_evidence_digest": (
                            interactive_result.evidence.digest
                        ),
                        "error": interactive_result.error,
                    }
                )
                if protocol_transcript is not None:
                    phase_order.append("protocol_session_finished")
                if interactive_result.error:
                    errors.append(
                        f"interactive_protocol:{interactive_result.error}"
                    )
            else:
                harness_run, harness_storage = self._run_phase(
                    harness_spec,
                    hard_quota_volumes=harness_quota_volumes,
                    writable_roots=harness_writable_roots,
                    cancel_event=cancel_event,
                )
                try:
                    harness_inspection = self.engine.inspect_container(
                        harness_name
                    )
                    harness_mismatches = harness_inspection.mismatches(
                        harness_spec
                    )
                except Exception as exc:
                    harness_inspection = None
                    harness_mismatches = (
                        f"container_inspection_failed:{exc}",
                    )
                harness_cleanup = self._cleanup_container(harness_name)
            harness_stdout = harness_run.stdout
            harness_stderr = harness_run.stderr
            phase_order.append("harness_exited")
            active_names.discard(harness_name)
            phase_order.append("harness_container_removed")
            harness_phase = PhaseEvidence(
                phase=ContainerPhase.HARNESS,
                policy={
                    **harness_spec.policy_dict(),
                    "payload_command": list(request.harness_command),
                    "payload_working_directory": "/workspace",
                },
                run=harness_run,
                inspection=harness_inspection,
                inspection_mismatches=harness_mismatches,
                storage=harness_storage,
                cleanup=harness_cleanup,
            )
            result_status = (
                self._interactive_phase_status(interactive_result.status)
                if interactive_result is not None
                else self._phase_status(harness_run)
            )
            if harness_storage.exceeded and harness_storage.hard_storage_enforced:
                result_status = OciTrialStatus.STORAGE_FAILED
                errors.append("harness hit the enforced aggregate storage quota (ENOSPC)")
            elif not harness_storage.monitor_succeeded:
                result_status = OciTrialStatus.INVALID_OUTPUT
                errors.append(
                    harness_storage.monitor_error
                    or (
                        "harness storage limit exceeded"
                        if harness_storage.exceeded
                        else "harness storage monitor did not complete"
                    )
                )
            elif harness_cleanup.status is CleanupStatus.FAILED:
                result_status = OciTrialStatus.CLEANUP_FAILED
                errors.append(harness_cleanup.error or "harness cleanup failed")
            elif harness_mismatches:
                result_status = OciTrialStatus.POLICY_MISMATCH
                errors.extend(harness_mismatches)

            if request.network.mode is OciNetworkMode.MODEL_GATEWAY_ONLY:
                assert request.network.network_name is not None
                network_after = self.engine.inspect_network(
                    request.network.network_name
                )
                expected_peers = tuple(
                    sorted(request.network.allowed_gateway_identities)
                )
                if (
                    network_after.name != request.network.network_name
                    or not network_after.internal
                    or network_after.endpoint_identities != expected_peers
                ):
                    result_status = OciTrialStatus.POLICY_MISMATCH
                    errors.append(
                        "model gateway network peer set changed during harness phase"
                    )
                phase_order.append("gateway_network_reinspected")

            if request.protocol_parser is not None:
                if harness_run.stdout_truncated:
                    protocol_evidence.update(
                        {
                            "mode": "legacy-accepted-splice",
                            "authority_verified": False,
                            "integrity_only": True,
                            "official_eligible": False,
                            "error": "protocol stdout was truncated",
                        }
                    )
                    result_status = OciTrialStatus.PROTOCOL_ERROR
                else:
                    try:
                        normalized = _legacy_normalize_protocol_output(
                            harness_run.stdout,
                            request.accepted_event or {},
                        )
                        protocol_transcript = request.protocol_parser.parse_bytes(
                            normalized
                        )
                        protocol_evidence.update(
                            {
                                "mode": "legacy-accepted-splice",
                                "parsed": True,
                                "authority_verified": False,
                                "integrity_only": True,
                                "official_eligible": False,
                                "transcript_sha256": sha256_bytes(normalized),
                            }
                        )
                        phase_order.append("legacy_protocol_integrity_checked")
                    except (ProtocolError, OciRunnerError, ValueError) as exc:
                        protocol_evidence.update(
                            {
                                "mode": "legacy-accepted-splice",
                                "authority_verified": False,
                                "integrity_only": True,
                                "official_eligible": False,
                                "error": str(exc),
                            }
                        )
                        result_status = OciTrialStatus.PROTOCOL_ERROR
                        errors.append(f"protocol:{exc}")

            if request.gateway_handle is not None:
                assert request.credential_broker is not None
                try:
                    if result_status is OciTrialStatus.COMPLETED:
                        request.credential_broker.complete(request.gateway_handle)
                        handle_action = "completed"
                    else:
                        request.credential_broker.revoke(request.gateway_handle)
                        handle_action = "revoked"
                    handle_finalized = True
                    phase_order.append(f"gateway_handle_{handle_action}")
                except Exception as exc:
                    handle_action = "finalization_failed"
                    handle_finalized = False
                    result_status = OciTrialStatus.ENGINE_ERROR
                    errors.append(f"gateway_handle:{exc}")
            if env_file is not None:
                env_file.unlink(missing_ok=True)
                phase_order.append("gateway_handle_file_destroyed")

            can_grade = (
                harness_cleanup.status is CleanupStatus.SUCCEEDED
                and not harness_mismatches
                and harness_storage.monitor_succeeded
                and handle_finalized
                and result_status is not OciTrialStatus.CANCELLED
            )

            if can_grade and hard_quota_active:
                assert harness_volume is not None
                capture_spec = ContainerSpec(
                    phase=ContainerPhase.OUTPUT_CAPTURE,
                    name=capture_name,
                    image=request.task_image,
                    command=_trusted_output_capture_command(
                        workspace_limit=request.harness_resources.storage_bytes,
                        artifact_limit=request.harness_resources.artifact_bytes,
                    ),
                    mounts=(
                        MountSpec.volume(
                            harness_volume.spec.name,
                            "/output",
                            True,
                            subpath="workspace",
                            no_copy=True,
                        ),
                        MountSpec.volume(
                            harness_volume.spec.name,
                            "/harness-artifacts",
                            True,
                            subpath="artifacts",
                            no_copy=True,
                        ),
                    ),
                    resources=request.grader_resources,
                    network=OciNetworkPolicy.none(),
                    working_directory="/tmp",
                    env_file=None,
                    environment_names=(),
                )
                phase_order.append("output_capture_started")
                active_names.add(capture_name)
                capture_run, capture_storage = self._run_phase(
                    capture_spec,
                    hard_quota_volumes=(
                        harness_volume,
                    ),
                    writable_roots=(),
                    cancel_event=None,
                )
                phase_order.append("output_capture_exited")
                try:
                    capture_inspection = self.engine.inspect_container(capture_name)
                    capture_mismatches = capture_inspection.mismatches(capture_spec)
                except Exception as exc:
                    capture_inspection = None
                    capture_mismatches = (
                        f"container_inspection_failed:{exc}",
                    )
                capture_cleanup = self._cleanup_container(capture_name)
                active_names.discard(capture_name)
                phase_order.append("output_capture_container_removed")
                output_capture_phase = PhaseEvidence(
                    phase=ContainerPhase.OUTPUT_CAPTURE,
                    policy=capture_spec.policy_dict(),
                    run=capture_run,
                    inspection=capture_inspection,
                    inspection_mismatches=capture_mismatches,
                    storage=capture_storage,
                    cleanup=capture_cleanup,
                )
                if capture_cleanup.status is CleanupStatus.FAILED:
                    result_status = OciTrialStatus.CLEANUP_FAILED
                    errors.append(
                        capture_cleanup.error or "output capture cleanup failed"
                    )
                    can_grade = False
                elif capture_mismatches:
                    result_status = OciTrialStatus.POLICY_MISMATCH
                    errors.extend(capture_mismatches)
                    can_grade = False
                else:
                    try:
                        output_capture = _parse_output_capture(capture_run)
                        workspace_files = int(
                            output_capture["workspace"]["file_count"]
                        )
                        workspace_bytes = int(
                            output_capture["workspace"]["total_bytes"]
                        )
                        artifact_bytes = int(
                            output_capture["artifacts"]["total_bytes"]
                        )
                        phase_order.append("output_capture_verified")
                    except OciRunnerError as exc:
                        result_status = OciTrialStatus.INVALID_OUTPUT
                        errors.append(str(exc))
                        can_grade = False

            if can_grade:
                if hard_quota_active:
                    assert harness_volume is not None
                    assert grader_volume is not None
                    assert hidden_tree_digest is not None
                    grader_mounts = (
                        MountSpec.volume(
                            harness_volume.spec.name,
                            "/output",
                            True,
                            subpath="workspace",
                            no_copy=True,
                        ),
                        MountSpec.volume(
                            harness_volume.spec.name,
                            "/harness-artifacts",
                            True,
                            subpath="artifacts",
                            no_copy=True,
                        ),
                        MountSpec.volume(
                            grader_volume.spec.name,
                            "/grader-output",
                            False,
                            subpath="output",
                            no_copy=True,
                        ),
                        MountSpec.volume(
                            grader_volume.spec.name,
                            "/tmp",
                            False,
                            subpath="tmp",
                            no_copy=True,
                        ),
                    )
                    grader_spec = ContainerSpec(
                        phase=ContainerPhase.GRADER,
                        name=grader_name,
                        image=request.grader_image,
                        command=_trusted_grader_command(
                            hidden_tree_digest,
                            request.grader_command,
                            quota_limit=request.grader_resources.storage_bytes,
                        ),
                        mounts=grader_mounts,
                        resources=request.grader_resources,
                        network=OciNetworkPolicy.none(),
                        working_directory="/tmp",
                        env_file=None,
                        environment_names=(),
                        stdin_open=True,
                        tmpfs_mounts=(
                            (
                                "/trusted",
                                (
                                    "rw,noexec,nosuid,nodev,"
                                    f"size={request.grader_resources.tmpfs_bytes},"
                                    "mode=0700,uid=65534,gid=65534"
                                ),
                            ),
                        ),
                    )
                    phase_order.append("grader_started")
                    active_names.add(grader_name)
                    hidden_transfer = _tree_transfer_bytes(
                        hidden_files,
                        expected_digest=hidden_tree_digest,
                    )
                    hidden_late = (
                        "harness_container_removed" in phase_order
                        and phase_order.index("harness_container_removed")
                        < len(phase_order)
                    )
                    grader_run = self.engine.run_container(
                        grader_spec,
                        stdin_data=hidden_transfer,
                    )
                    grader_storage = self._hard_storage_observation(
                        (grader_volume,),
                        run=grader_run,
                    )
                    grader_stdout = grader_run.stdout
                    grader_stderr = grader_run.stderr
                    phase_order.append("grader_exited")
                    try:
                        grader_inspection = self.engine.inspect_container(grader_name)
                        grader_mismatches = grader_inspection.mismatches(grader_spec)
                    except Exception as exc:
                        grader_inspection = None
                        grader_mismatches = (
                            f"container_inspection_failed:{exc}",
                        )
                    grader_cleanup = self._cleanup_container(grader_name)
                    active_names.discard(grader_name)
                    phase_order.append("grader_container_removed")
                    grader_policy = grader_spec.policy_dict()
                    grader_policy["payload_command"] = list(
                        request.grader_command
                    )
                    grader_policy["hidden_input_transfer"] = {
                        "mode": "controller-stdin-to-grader-tmpfs",
                        "destination": "/trusted",
                        "after_harness_removal": hidden_late,
                        "payload_bytes": len(hidden_transfer),
                        "payload_sha256": sha256_bytes(hidden_transfer),
                    }
                    grader_phase = PhaseEvidence(
                        phase=ContainerPhase.GRADER,
                        policy=grader_policy,
                        run=grader_run,
                        inspection=grader_inspection,
                        inspection_mismatches=grader_mismatches,
                        storage=grader_storage,
                        cleanup=grader_cleanup,
                    )
                else:
                    grader_mounts = (
                        MountSpec(workspace, "/output", True),
                        MountSpec(artifacts, "/harness-artifacts", True),
                        MountSpec(trusted_root, "/trusted", True),
                        MountSpec(grader_output, "/grader-output", False),
                        MountSpec(grader_temp, "/tmp", False),
                    )
                    grader_spec = ContainerSpec(
                        phase=ContainerPhase.GRADER,
                        name=grader_name,
                        image=request.grader_image,
                        command=request.grader_command,
                        mounts=grader_mounts,
                        resources=request.grader_resources,
                        network=OciNetworkPolicy.none(),
                        working_directory="/output",
                        env_file=None,
                        environment_names=(),
                    )
                    hidden_late = (
                        "harness_container_removed" in phase_order
                        and phase_order.index("harness_container_removed")
                        < len(phase_order)
                    )
                    phase_order.append("grader_started")
                    active_names.add(grader_name)
                    grader_run, grader_storage = self._run_phase(
                        grader_spec,
                        hard_quota_volumes=(),
                        writable_roots=(grader_output, grader_temp),
                        cancel_event=None,
                    )
                    grader_stdout = grader_run.stdout
                    grader_stderr = grader_run.stderr
                    phase_order.append("grader_exited")
                    try:
                        grader_inspection = self.engine.inspect_container(grader_name)
                        grader_mismatches = grader_inspection.mismatches(grader_spec)
                    except Exception as exc:
                        grader_inspection = None
                        grader_mismatches = (
                            f"container_inspection_failed:{exc}",
                        )
                    grader_cleanup = self._cleanup_container(grader_name)
                    active_names.discard(grader_name)
                    phase_order.append("grader_container_removed")
                    grader_phase = PhaseEvidence(
                        phase=ContainerPhase.GRADER,
                        policy=grader_spec.policy_dict(),
                        run=grader_run,
                        inspection=grader_inspection,
                        inspection_mismatches=grader_mismatches,
                        storage=grader_storage,
                        cleanup=grader_cleanup,
                    )
                if grader_storage.exceeded and grader_storage.hard_storage_enforced:
                    result_status = OciTrialStatus.GRADER_FAILED
                    errors.append(
                        "grader hit the enforced aggregate storage quota (ENOSPC)"
                    )
                elif not grader_storage.monitor_succeeded:
                    result_status = OciTrialStatus.GRADER_FAILED
                    errors.append(
                        grader_storage.monitor_error
                        or (
                            "grader storage limit exceeded"
                            if grader_storage.exceeded
                            else "grader storage monitor did not complete"
                        )
                    )
                elif grader_cleanup.status is CleanupStatus.FAILED:
                    result_status = OciTrialStatus.CLEANUP_FAILED
                    errors.append(grader_cleanup.error or "grader cleanup failed")
                elif grader_mismatches:
                    result_status = OciTrialStatus.POLICY_MISMATCH
                    errors.extend(grader_mismatches)
                elif (
                    grader_run.timed_out
                    or grader_run.cancelled
                    or grader_run.exit_code != 0
                ):
                    if result_status is OciTrialStatus.COMPLETED:
                        result_status = OciTrialStatus.GRADER_FAILED

            if not hard_quota_active:
                try:
                    workspace_files, workspace_bytes = _tree_usage(workspace)
                    _, artifact_bytes = _tree_usage(
                        artifacts,
                        maximum=request.harness_resources.artifact_bytes,
                    )
                    _, temp_bytes = _tree_usage(harness_temp)
                    if (
                        workspace_bytes + artifact_bytes + temp_bytes
                        > request.harness_resources.storage_bytes
                    ):
                        raise OciRunnerError(
                            "output storage exceeds enforced aggregate limit of "
                            f"{request.harness_resources.storage_bytes} bytes"
                        )
                except OciRunnerError as exc:
                    result_status = OciTrialStatus.INVALID_OUTPUT
                    errors.append(str(exc))
        except Exception as exc:
            result_status = OciTrialStatus.ENGINE_ERROR
            errors.append(str(exc))
        finally:
            ordered_active = sorted(
                name for name in active_names if name != keeper_name
            )
            if keeper_name in active_names:
                ordered_active.append(keeper_name)
            for name in ordered_active:
                cleanup = self._cleanup_container(name)
                active_names.discard(name)
                if name == keeper_name and volume_keeper_phase is not None:
                    volume_keeper_phase = dataclasses.replace(
                        volume_keeper_phase,
                        cleanup=cleanup,
                    )
                    phase_order.append(
                        "volume_keeper_removed"
                        if cleanup.status is CleanupStatus.SUCCEEDED
                        else "volume_keeper_cleanup_failed"
                    )
                if cleanup.status is CleanupStatus.FAILED:
                    result_status = OciTrialStatus.CLEANUP_FAILED
                    errors.append(cleanup.error or f"cleanup failed for {name}")
            if request.gateway_handle is not None and not handle_finalized:
                try:
                    assert request.credential_broker is not None
                    request.credential_broker.revoke(request.gateway_handle)
                    handle_action = "revoked"
                    handle_finalized = True
                    phase_order.append("gateway_handle_revoked")
                except Exception as exc:
                    errors.append(f"gateway_handle_revoke_failed:{exc}")
            if env_file is not None:
                env_file.unlink(missing_ok=True)

            for index in range(len(volume_evidence) - 1, -1, -1):
                lifecycle = volume_evidence[index]
                if lifecycle.cleanup.status is not CleanupStatus.NOT_ATTEMPTED:
                    continue
                cleanup = self._cleanup_volume(lifecycle.spec.name)
                volume_evidence[index] = dataclasses.replace(
                    lifecycle,
                    cleanup=cleanup,
                )
                phase_order.append(
                    f"{lifecycle.spec.purpose}_volume_"
                    + (
                        "removed"
                        if cleanup.status is CleanupStatus.SUCCEEDED
                        else "cleanup_failed"
                    )
                )
                if cleanup.status is CleanupStatus.FAILED:
                    result_status = OciTrialStatus.CLEANUP_FAILED
                    errors.append(
                        cleanup.error
                        or f"volume cleanup failed for {lifecycle.spec.name}"
                    )

            volume_by_name = {
                item.spec.name: item for item in volume_evidence
            }

            def storage_cleanup_state(
                phase: PhaseEvidence | None,
            ) -> PhaseEvidence | None:
                if phase is None or not phase.storage.hard_storage_enforced:
                    return phase
                succeeded = all(
                    volume_by_name.get(name) is not None
                    and volume_by_name[name].cleanup.status
                    is CleanupStatus.SUCCEEDED
                    and volume_by_name[name].cleanup.confirmed_absent
                    for name in phase.storage.volume_names
                )
                return dataclasses.replace(
                    phase,
                    storage=dataclasses.replace(
                        phase.storage,
                        cleanup_succeeded=succeeded,
                    ),
                )

            volume_keeper_phase = storage_cleanup_state(volume_keeper_phase)
            seed_phase = storage_cleanup_state(seed_phase)
            harness_phase = storage_cleanup_state(harness_phase)
            output_capture_phase = storage_cleanup_state(output_capture_phase)
            grader_phase = storage_cleanup_state(grader_phase)

            try:
                shutil.rmtree(attempt_root)
                workspace_cleaned = not attempt_root.exists()
            except OSError as exc:
                workspace_cleaned = False
                result_status = OciTrialStatus.CLEANUP_FAILED
                errors.append(f"workspace_cleanup:{exc}")
            phase_order.append(
                "ephemeral_root_removed"
                if workspace_cleaned
                else "ephemeral_root_cleanup_failed"
            )

        image_verified = len(images) == 3 and all(image.verified for image in images)
        expected_gateway_peers = tuple(
            sorted(request.network.allowed_gateway_identities)
        )
        network_verified = (
            request.network.mode is OciNetworkMode.NONE
            or (
                network_before is not None
                and network_after is not None
                and network_before.internal
                and network_after.internal
                and network_before.name == request.network.network_name
                and network_after.name == request.network.network_name
                and network_before.endpoint_identities == expected_gateway_peers
                and network_after.endpoint_identities == expected_gateway_peers
            )
        )
        image_roles = request.image_role_policy()
        environment_bound = (
            request.track is OciTrack.SYSTEMS
            or bool(image_roles["controlled_environment_bound"])
        )
        protocol_authority_ok = (
            not protocol_evidence["enabled"]
            or bool(protocol_evidence["authority_verified"])
        )
        hard_storage_enforced = bool(
            hard_quota_active
            and volume_evidence
            and all(item.quota_verified for item in volume_evidence)
        )
        hard_storage_cleanup_succeeded = bool(
            hard_storage_enforced
            and all(item.lifecycle_verified for item in volume_evidence)
        )
        core_phases = [harness_phase, grader_phase]
        if hard_quota_active:
            core_phases.extend(
                [
                    volume_keeper_phase,
                    seed_phase,
                    output_capture_phase,
                ]
            )
        phases_verified = all(
            phase is not None
            and phase.runtime_policy_verified
            and phase.cleanup.status is CleanupStatus.SUCCEEDED
            for phase in core_phases
        )
        runtime_verified = bool(
            image_verified
            and network_verified
            and environment_bound
            and seed_evidence is not None
            and seed_evidence.verified
            and phases_verified
            and handle_finalized
            and hidden_late
            and workspace_cleaned
            and protocol_authority_ok
            and (
                not hard_quota_active
                or hard_storage_cleanup_succeeded
            )
        )
        official_ineligibility_reasons: list[str] = []
        if not runtime_verified:
            official_ineligibility_reasons.append("runtime_policy_not_fully_verified")
        if request.track is OciTrack.SYSTEMS and image_roles["systems_confound"]:
            official_ineligibility_reasons.append("systems_image_confound")
        if not protocol_evidence["official_eligible"]:
            official_ineligibility_reasons.append(
                "trusted_interactive_protocol_session_not_proven"
            )
        if not hard_storage_enforced:
            official_ineligibility_reasons.append(
                "hard_aggregate_storage_quota_unavailable"
            )
        elif not hard_storage_cleanup_succeeded:
            official_ineligibility_reasons.append(
                "hard_storage_volume_cleanup_unverified"
            )
        official_eligible = not official_ineligibility_reasons
        storage_evidence = {
            "requested_mode": request.storage_mode.value,
            "selected_mode": storage_selected_mode,
            "capability": {
                "supported": hard_quota_capable,
                "detail": hard_quota_capability_detail,
            },
            "fallback_reason": storage_fallback_reason,
            "hard_storage_enforced": hard_storage_enforced,
            "hard_storage_cleanup_succeeded": hard_storage_cleanup_succeeded,
            "official_eligible": (
                hard_storage_enforced and hard_storage_cleanup_succeeded
            ),
            "host_bind_writable_workspace_or_artifacts": not hard_quota_active,
            "volumes": [item.to_dict() for item in volume_evidence],
        }
        evidence = OciRunnerEvidence(
            runner_id=self.runner_id,
            attempt=request.attempt.to_dict(),
            track=request.track,
            image_roles=image_roles,
            engine=engine_identity,
            images=tuple(images),
            network={
                "requested": request.network.to_dict(),
                "before": network_before.to_dict() if network_before else None,
                "after": network_after.to_dict() if network_after else None,
                "peer_set_verified": network_verified,
            },
            storage=storage_evidence,
            phase_order=tuple(phase_order),
            volume_keeper=volume_keeper_phase,
            seed=seed_phase,
            harness=harness_phase,
            output_capture=output_capture_phase,
            grader=grader_phase,
            handle_action=handle_action,
            protocol=protocol_evidence,
            workspace={
                "workspace_id": request.attempt.workspace_id,
                "ephemeral_root": str(attempt_root),
                "fresh": True,
                "removed": workspace_cleaned,
                "storage_mode": storage_selected_mode,
                "seed": seed_evidence.to_dict() if seed_evidence else None,
                "output_capture": output_capture,
                "files": workspace_files,
                "bytes": workspace_bytes + artifact_bytes,
                "workspace_bytes": workspace_bytes,
                "artifact_bytes": artifact_bytes,
            },
            runtime_verified=runtime_verified,
            official_eligible=official_eligible,
            official_ineligibility_reasons=tuple(
                official_ineligibility_reasons
            ),
            errors=tuple(errors),
            started_at=started_at,
            duration_ms=max(0, int((time.monotonic() - started_mono) * 1_000)),
        )
        receipt = OciRunnerLifecycleReceipt(
            evidence_digest=evidence.digest,
            _execution_complete=(
                harness_phase is not None
                and harness_phase.cleanup.status is CleanupStatus.SUCCEEDED
            ),
            credential_finalized=handle_finalized,
            hidden_inputs_mounted_after_harness_exit=hidden_late,
            runtime_verified=runtime_verified,
        )
        return OciTrialResult(
            status=result_status,
            evidence=evidence,
            lifecycle_receipt=receipt,
            harness_stdout=harness_stdout,
            harness_stderr=harness_stderr,
            grader_stdout=grader_stdout,
            grader_stderr=grader_stderr,
            protocol_transcript=protocol_transcript,
        )
