#!/usr/bin/env python3
"""ATV protocol-v1 wrapper for GitHub Copilot CLI harnesses.

The wrapper is intentionally standalone. The OCI image does not need the
``atv_bench`` Python package at runtime.

Protocol stdout contains only ``atv.harness-event/v1`` JSONL. Human-readable
diagnostics go to stderr. Copilot is always launched with an argv vector and
``shell=False``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
REQUEST_SCHEMA = "atv.trial-request/v1"
HARNESS_EVENT_SCHEMA = "atv.harness-event/v1"
CONTROLLER_EVENT_SCHEMA = "atv.event/v1"
RESULT_SCHEMA = "atv.harness-result/v1"
MODEL_GATEWAY_ENV = "ATV_MODEL_GATEWAY_HANDLE"
DEFAULT_COPILOT = "/opt/atv/bin/copilot"
DEFAULT_PHOENIX_HOME = "/opt/atv/copilot-home/phoenix"
DEFAULT_HVE_PLUGIN = "/opt/atv/hve-plugin"
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_ARTIFACTS = "/artifacts"
HARD_INPUT_LINE_BYTES = 1_048_576
HARD_CHILD_LINE_BYTES = 1_048_576
STDERR_TAIL_BYTES = 65_536
SAFE_INTEGER_MAX = 9_007_199_254_740_991
IDENTIFIER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")

COMMON_CAPABILITIES: dict[str, Any] = {
    "workspace_edit": True,
    "resumable": False,
    "browser": False,
    "model_events": True,
    "tool_events": True,
    "usage_events": True,
    "checkpoint_events": False,
    "token_usage_reporting": "reported",
    "call_usage_reporting": "reported",
    # Copilot reports premium-request credits, not USD. Do not relabel them.
    "cost_usage_reporting": "unsupported",
}
CAPABILITIES = {
    "phoenix": {
        **COMMON_CAPABILITIES,
        "subagents": False,
        "model_selection": "single",
    },
    "hve-core": {
        **COMMON_CAPABILITIES,
        "subagents": True,
        "model_selection": "multiple",
    },
}

MEDIA_TYPES = {
    ".c": "text/x-c",
    ".cc": "text/x-c++",
    ".cpp": "text/x-c++",
    ".css": "text/css",
    ".go": "text/x-go",
    ".h": "text/x-c",
    ".hpp": "text/x-c++",
    ".html": "text/html",
    ".java": "text/x-java",
    ".js": "text/javascript",
    ".json": "application/json",
    ".jsx": "text/javascript",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".rs": "text/x-rust",
    ".sh": "text/x-shellscript",
    ".toml": "application/toml",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class WrapperError(RuntimeError):
    """Expected fail-closed wrapper error."""


class StreamLimitError(WrapperError):
    """A bounded Copilot stream exceeded its negotiated limit."""


def _reject_float(value: str) -> None:
    raise WrapperError(f"floating-point JSON is forbidden by protocol v1: {value}")


def _reject_constant(value: str) -> None:
    raise WrapperError(f"non-standard JSON constant is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WrapperError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def strict_json_loads(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WrapperError(f"protocol input is not strict UTF-8: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise WrapperError(f"malformed protocol JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WrapperError("protocol line must contain one JSON object")
    return value


def canonical_json_bytes(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def canonical_digest(value: Mapping[str, Any] | Sequence[Any]) -> dict[str, str]:
    return {
        "algorithm": "sha256",
        "value": hashlib.sha256(canonical_json_bytes(value)).hexdigest(),
    }


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _positive_int(value: Any, *, field_name: str, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WrapperError(f"{field_name} must be an integer")
    lower = 0 if allow_zero else 1
    if value < lower or value > SAFE_INTEGER_MAX:
        raise WrapperError(f"{field_name} is outside the supported range")
    return value


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= SAFE_INTEGER_MAX:
        return value
    if isinstance(value, float) and value.is_integer():
        integer = int(value)
        if 0 <= integer <= SAFE_INTEGER_MAX:
            return integer
    return None


def _read_line(stream: BinaryIO, limit: int, *, label: str) -> bytes:
    raw = stream.readline(limit + 2)
    if not raw:
        raise WrapperError(f"EOF while waiting for {label}")
    content = raw[:-1] if raw.endswith(b"\n") else raw
    if content.endswith(b"\r"):
        content = content[:-1]
    if len(content) > limit or (not raw.endswith(b"\n") and len(raw) > limit):
        raise WrapperError(f"{label} exceeds {limit} bytes")
    if not content:
        raise WrapperError(f"{label} cannot be blank")
    return content


@dataclass
class ProtocolInput:
    """Unbuffered stdin reader shared across handshake and cancellation."""

    file_descriptor: int
    buffered: bytearray = field(default_factory=bytearray)

    def _pop_line(self, limit: int, *, label: str) -> bytes | None:
        newline = self.buffered.find(b"\n")
        if newline < 0:
            if len(self.buffered) > limit:
                raise WrapperError(f"{label} exceeds {limit} bytes")
            return None
        raw = bytes(self.buffered[:newline])
        del self.buffered[: newline + 1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if len(raw) > limit:
            raise WrapperError(f"{label} exceeds {limit} bytes")
        if not raw:
            raise WrapperError(f"{label} cannot be blank")
        return raw

    def read_line(self, limit: int, *, label: str) -> bytes:
        while True:
            line = self._pop_line(limit, label=label)
            if line is not None:
                return line
            chunk = os.read(self.file_descriptor, min(65_536, limit + 1))
            if not chunk:
                raise WrapperError(f"EOF while waiting for {label}")
            self.buffered.extend(chunk)


def _safe_identifier(value: Any, *, prefix: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("._-")
    if not text:
        text = prefix
    if not text[0].isalnum():
        text = f"{prefix}-{text}"
    if len(text) > 128 or not IDENTIFIER_RE.fullmatch(text):
        suffix = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
        text = f"{prefix}-{suffix}"
    return text[:128].rstrip("._-")


def _failure(
    code: str,
    scope: str,
    *,
    retryable: bool = False,
    protocol_event: bool = False,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "code": _safe_identifier(code, prefix="failure"),
        "scope": scope,
        "retryable": retryable,
    }
    if protocol_event:
        value["infrastructure"] = False
    return value


@dataclass
class EventEmitter:
    trial_id: str
    attempt_id: str
    sequence: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {
            "schema": HARNESS_EVENT_SCHEMA,
            "type": event_type,
            "protocol_version": PROTOCOL_VERSION,
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "harness_sequence": self.sequence,
            "emitted_at": utc_timestamp(),
            **payload,
        }
        encoded = canonical_json_bytes(event)
        with self._lock:
            sys.stdout.buffer.write(encoded + b"\n")
            sys.stdout.buffer.flush()
            self.sequence += 1


def _validate_request(request: Mapping[str, Any], harness: str) -> None:
    if request.get("schema") != REQUEST_SCHEMA:
        raise WrapperError(f"request schema must be {REQUEST_SCHEMA!r}")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise WrapperError("only protocol version 1 is supported")
    for field_name in ("trial_id", "attempt_id"):
        value = request.get(field_name)
        if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
            raise WrapperError(f"request {field_name} is not a valid identifier")
    harness_ref = request.get("harness")
    if not isinstance(harness_ref, dict):
        raise WrapperError("request harness identity is missing")
    expected_fragment = "phoenix" if harness == "phoenix" else "hve-core"
    if expected_fragment not in str(harness_ref.get("id", "")):
        raise WrapperError(
            f"request harness id does not identify the selected {harness!r} wrapper"
        )
    prompt = request.get("prompt")
    if (
        not isinstance(prompt, dict)
        or not isinstance(prompt.get("text"), str)
        or not prompt["text"]
    ):
        raise WrapperError("request prompt.text must be a non-empty string")
    model_policy = request.get("model_policy")
    if not isinstance(model_policy, dict):
        raise WrapperError("request model_policy is missing")
    models = model_policy.get("allowed_models")
    if not isinstance(models, list) or not models or not all(
        isinstance(item, str) and item for item in models
    ):
        raise WrapperError("model_policy.allowed_models must be non-empty")
    gateway = model_policy.get("gateway")
    if not isinstance(gateway, str) or not gateway:
        raise WrapperError("model_policy.gateway is missing")
    workspace = request.get("workspace")
    if (
        not isinstance(workspace, dict)
        or workspace.get("path") != DEFAULT_WORKSPACE
        or workspace.get("artifacts_path") != DEFAULT_ARTIFACTS
    ):
        raise WrapperError("protocol v1 requires /workspace and /artifacts")
    limits = request.get("protocol_limits")
    if not isinstance(limits, dict):
        raise WrapperError("request protocol_limits is missing")
    for field_name in ("max_line_bytes", "max_total_bytes", "max_events"):
        _positive_int(limits.get(field_name), field_name=field_name, allow_zero=False)
    budgets = request.get("budget_limits")
    if not isinstance(budgets, dict):
        raise WrapperError("request budget_limits is missing")
    for field_name in (
        "wall_time_ms",
        "stdout_bytes",
        "stderr_bytes",
        "artifact_bytes",
    ):
        _positive_int(budgets.get(field_name), field_name=field_name)
    output = request.get("output")
    if not isinstance(output, dict):
        raise WrapperError("request output contract is missing")


def _validate_accepted(
    accepted: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    if accepted.get("schema") != CONTROLLER_EVENT_SCHEMA:
        raise WrapperError("accepted event has the wrong schema")
    expected = {
        "type": "accepted",
        "source": "controller",
        "protocol_version": PROTOCOL_VERSION,
        "trial_id": request["trial_id"],
        "attempt_id": request["attempt_id"],
        "selected_protocol_version": PROTOCOL_VERSION,
        "request_digest": canonical_digest(request),
        "policy_digest": canonical_digest(request["policy"]),
    }
    for field_name, value in expected.items():
        if accepted.get(field_name) != value:
            raise WrapperError(f"accepted event {field_name} does not match request")
    if accepted.get("capabilities") != request["required_capabilities"] and not isinstance(
        accepted.get("capabilities"), dict
    ):
        raise WrapperError("accepted capabilities are malformed")
    if accepted.get("effective_budget_limits") != request["budget_limits"]:
        raise WrapperError("accepted budget limits do not match the request")
    if accepted.get("effective_protocol_limits") != request["protocol_limits"]:
        raise WrapperError("accepted protocol limits do not match the request")


def _gateway_url(gateway: str) -> str:
    raw = gateway.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WrapperError("model gateway must be an HTTP(S) host or URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WrapperError("model gateway URL cannot contain credentials/query/fragment")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif path != "/v1" and not path.endswith("/v1"):
        path += "/v1"
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{path}"


def _copy_phoenix_home(template: pathlib.Path, destination: pathlib.Path) -> None:
    if not template.is_dir():
        raise WrapperError(f"Phoenix home template is missing: {template}")
    shutil.copytree(template, destination, dirs_exist_ok=True)
    agent = destination / "agents" / "phoenix.agent.md"
    mcp = destination / "mcp-config.json"
    if not agent.is_file() or not mcp.is_file():
        raise WrapperError("Phoenix template lacks agent or MCP registration")


def _clean_child_environment(
    *,
    runtime_root: pathlib.Path,
    copilot_home: pathlib.Path,
    request: Mapping[str, Any],
    model: str,
    harness: str,
) -> dict[str, str]:
    gateway_handle = os.environ.get(MODEL_GATEWAY_ENV)
    if not gateway_handle:
        raise WrapperError(f"{MODEL_GATEWAY_ENV} is required")
    credentials = request.get("policy", {}).get("credentials", [])
    credential_names = {
        item.get("name")
        for item in credentials
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if MODEL_GATEWAY_ENV not in credential_names:
        raise WrapperError(
            f"trial request does not authorize credential {MODEL_GATEWAY_ENV}"
        )

    path = os.environ.get("PATH", "/opt/atv/bin:/usr/local/bin:/usr/bin:/bin")
    env = {
        "PATH": path,
        "HOME": str(runtime_root / "home"),
        "COPILOT_HOME": str(copilot_home),
        "XDG_CONFIG_HOME": str(runtime_root / "xdg-config"),
        "XDG_CACHE_HOME": str(runtime_root / "xdg-cache"),
        "XDG_DATA_HOME": str(runtime_root / "xdg-data"),
        "TMPDIR": str(runtime_root / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "CI": "true",
        "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_OFFLINE": "true",
        "COPILOT_PROVIDER_BASE_URL": _gateway_url(
            str(request["model_policy"]["gateway"])
        ),
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BEARER_TOKEN": gateway_handle,
        "COPILOT_PROVIDER_WIRE_API": "responses",
        "COPILOT_MODEL": model,
        "COPILOT_OTEL_ENABLED": "false",
    }
    output_tokens = _optional_nonnegative_int(
        request["budget_limits"].get("model_output_tokens")
    )
    input_tokens = _optional_nonnegative_int(
        request["budget_limits"].get("model_input_tokens")
    )
    if output_tokens:
        env["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"] = str(output_tokens)
    if input_tokens:
        env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] = str(input_tokens)
    if harness == "phoenix":
        env["PHOENIX_WORKSPACE"] = DEFAULT_WORKSPACE
    else:
        env.update(
            {
                "HVE_TELEMETRY": "0",
                "HVE_HOME": str(runtime_root / "hve"),
                "HVE_TELEMETRY_DIR": str(runtime_root / "hve-telemetry"),
            }
        )
    # A private CA can be injected as a read-only image/mount path. Never inherit
    # provider/GitHub tokens or arbitrary proxy variables.
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def build_copilot_argv(
    *,
    executable: str,
    prefix_args: Sequence[str],
    harness: str,
    prompt: str,
    model: str,
    workspace: pathlib.Path,
    hve_plugin: pathlib.Path,
) -> list[str]:
    argv = [executable, *prefix_args]
    if harness == "phoenix":
        argv.extend(["--agent", "phoenix"])
    else:
        if not hve_plugin.is_dir():
            raise WrapperError(f"hve-core plugin is missing: {hve_plugin}")
        plugin_manifest = hve_plugin / ".github" / "plugin" / "plugin.json"
        if not plugin_manifest.is_file():
            raise WrapperError("hve-core plugin manifest is missing")
        argv.extend(
            [
                "--plugin-dir",
                str(hve_plugin),
                "--agent",
                "hve-core:rpi-agent",
            ]
        )
    argv.extend(
        [
            "-C",
            str(workspace),
            "--model",
            model,
            "--prompt",
            prompt,
            "--allow-all-tools",
            "--no-ask-user",
            "--disable-builtin-mcps",
            "--no-auto-update",
            "--no-remote",
            "--no-remote-export",
            "--no-color",
            "--plain-diff",
            "--output-format",
            "json",
            "--stream",
            "off",
            "--log-level",
            "none",
            "--secret-env-vars",
            f"{MODEL_GATEWAY_ENV},COPILOT_PROVIDER_BEARER_TOKEN",
        ]
    )
    return argv


def _run_git(workspace: pathlib.Path, args: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
        shell=False,
    )
    if completed.returncode != 0:
        raise WrapperError(f"git {' '.join(args[:2])} failed")
    return completed.stdout


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise WrapperError(f"unsafe artifact path: {value!r}")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise WrapperError(f"unsafe artifact path: {value!r}")
    normalized = path.as_posix()
    if normalized.startswith(".git/") or normalized == ".git":
        raise WrapperError("Git internals cannot be emitted as task artifacts")
    return normalized


@dataclass
class WorkspaceBaseline:
    workspace: pathlib.Path
    git_head: str | None
    candidate_hashes: dict[str, str | None]

    @classmethod
    def capture(
        cls, workspace: pathlib.Path, output_contract: Mapping[str, Any]
    ) -> "WorkspaceBaseline":
        git_head: str | None = None
        try:
            git_head = _run_git(workspace, ["rev-parse", "HEAD"]).decode(
                "ascii", errors="strict"
            ).strip()
        except (WrapperError, UnicodeError):
            pass
        candidates = set()
        for key in ("required_paths", "allowed_paths"):
            values = output_contract.get(key, [])
            if isinstance(values, list):
                candidates.update(
                    _safe_relative_path(item)
                    for item in values
                    if isinstance(item, str)
                )
        hashes: dict[str, str | None] = {}
        for relative in sorted(candidates):
            path = workspace.joinpath(*pathlib.PurePosixPath(relative).parts)
            hashes[relative] = sha256_file(path) if path.is_file() else None
        return cls(workspace=workspace, git_head=git_head, candidate_hashes=hashes)

    def changed_paths(self) -> set[str]:
        changed: set[str] = set()
        if self.git_head:
            commands = (
                ["diff", "--name-only", "-z", self.git_head, "HEAD", "--"],
                ["diff", "--name-only", "-z", "--"],
                ["diff", "--name-only", "-z", "--cached", "--"],
                ["ls-files", "--others", "--exclude-standard", "-z"],
            )
            for command in commands:
                try:
                    raw = _run_git(self.workspace, command)
                except WrapperError:
                    continue
                for item in raw.split(b"\0"):
                    if not item:
                        continue
                    try:
                        changed.add(
                            _safe_relative_path(item.decode("utf-8", errors="strict"))
                        )
                    except (UnicodeDecodeError, WrapperError):
                        continue
        for relative, before in self.candidate_hashes.items():
            path = self.workspace.joinpath(*pathlib.PurePosixPath(relative).parts)
            after = sha256_file(path) if path.is_file() else None
            if after != before:
                changed.add(relative)
        return changed


def _path_is_confined(root: pathlib.Path, path: pathlib.Path) -> bool:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        return os.path.commonpath([resolved_root, resolved_path]) == str(resolved_root)
    except (FileNotFoundError, OSError, ValueError):
        return False


def _media_type(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MEDIA_TYPES:
        return MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def collect_artifacts(
    *,
    workspace: pathlib.Path,
    baseline: WorkspaceBaseline,
    output_contract: Mapping[str, Any],
    artifact_budget: int,
) -> tuple[list[dict[str, Any]], str | None]:
    required = {
        _safe_relative_path(item)
        for item in output_contract.get("required_paths", [])
    }
    allowed = {
        _safe_relative_path(item)
        for item in output_contract.get("allowed_paths", [])
    }
    allow_any = output_contract.get("allow_any_relative_path") is True
    max_files = _positive_int(
        output_contract.get("max_files"), field_name="output.max_files"
    )
    max_total = min(
        _positive_int(
            output_contract.get("max_total_bytes"),
            field_name="output.max_total_bytes",
        ),
        artifact_budget,
    )
    allowed_media = set(output_contract.get("allowed_media_types", []))

    for relative in required:
        path = workspace.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not path.is_file():
            return [], "required-artifact-missing"

    changed = baseline.changed_paths()
    selected = sorted(
        relative
        for relative in changed
        if allow_any or relative in allowed or relative in required
    )
    if len(selected) > max_files:
        return [], "artifact-file-limit"

    artifacts: list[dict[str, Any]] = []
    total = 0
    for relative in selected:
        path = workspace.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not path.exists():
            if relative in required:
                return [], "required-artifact-deleted"
            continue
        if path.is_symlink() or not path.is_file() or not _path_is_confined(
            workspace, path
        ):
            return [], "unsafe-artifact"
        stat = path.stat()
        total += stat.st_size
        if total > max_total:
            return [], "artifact-byte-limit"
        media_type = _media_type(path)
        if allowed_media and media_type not in allowed_media:
            return [], "artifact-media-type"
        artifacts.append(
            {
                "path": relative,
                "media_type": media_type,
                "size_bytes": stat.st_size,
                "digest": {
                    "algorithm": "sha256",
                    "value": sha256_file(path),
                },
                "role": "primary" if relative in required else "supplemental",
            }
        )
    return artifacts, None


@dataclass
class Usage:
    started: float
    model_input_tokens: int | None = 0
    model_output_tokens: int | None = 0
    model_calls: int | None = 0
    tool_calls: int = 0
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def add_model_usage(self, data: Mapping[str, Any]) -> dict[str, int | None]:
        input_tokens = _optional_nonnegative_int(
            data.get("inputTokens", data.get("input_tokens"))
        )
        output_tokens = _optional_nonnegative_int(
            data.get("outputTokens", data.get("output_tokens"))
        )
        if input_tokens is None:
            self.model_input_tokens = None
        elif self.model_input_tokens is not None:
            self.model_input_tokens += input_tokens
        if output_tokens is None:
            self.model_output_tokens = None
        elif self.model_output_tokens is not None:
            self.model_output_tokens += output_tokens
        if self.model_calls is not None:
            self.model_calls += 1
        total = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total,
            "calls": 1,
            "cost_microusd": None,
        }

    def measurement(self, artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        total_tokens = (
            self.model_input_tokens + self.model_output_tokens
            if self.model_input_tokens is not None
            and self.model_output_tokens is not None
            else None
        )
        artifact_bytes = sum(int(item["size_bytes"]) for item in artifacts)
        return {
            "wall_time_ms": max(0, int((time.monotonic() - self.started) * 1000)),
            "cpu_time_ms": None,
            "model_input_tokens": self.model_input_tokens,
            "model_output_tokens": self.model_output_tokens,
            "model_total_tokens": total_tokens,
            "model_calls": self.model_calls,
            "cost_microusd": None,
            "tool_calls": self.tool_calls,
            "memory_bytes": None,
            "storage_bytes": artifact_bytes,
            "pids": None,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "artifact_bytes": artifact_bytes,
        }


@dataclass
class TranslationState:
    requested_model: str
    usage: Usage
    active_models: dict[str, tuple[str, float]] = field(default_factory=dict)
    active_tools: dict[str, tuple[str, float, str | None]] = field(default_factory=dict)
    model_failures: int = 0
    terminal_error: tuple[str, str, bool] | None = None
    child_result: dict[str, Any] | None = None


def _copilot_data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _copilot_timestamp(event: Mapping[str, Any]) -> str:
    value = event.get("timestamp")
    if isinstance(value, str) and value.endswith("Z"):
        return value
    return utc_timestamp()


def _classify_message(message: str) -> tuple[str, str, bool]:
    lowered = message.lower()
    if any(word in lowered for word in ("unauthorized", "authentication", "401", "403")):
        return "auth-failed", "model", False
    if any(word in lowered for word in ("policy", "permission denied", "access denied")):
        return "policy-denied", "policy", False
    if any(word in lowered for word in ("budget", "credit", "quota", "token limit")):
        return "budget-exhausted", "model", False
    if any(
        word in lowered
        for word in (
            "connect",
            "connection",
            "econnrefused",
            "provider",
            "network",
            "timed out",
        )
    ):
        return "model-unreachable", "model", True
    return "copilot-error", "harness", False


def _emit_model_failure(
    emitter: EventEmitter,
    state: TranslationState,
    event: Mapping[str, Any],
) -> None:
    data = _copilot_data(event)
    message = str(data.get("errorMessage") or data.get("message") or "model call failed")
    code, scope, retryable = _classify_message(message)
    model = str(data.get("model") or state.requested_model)
    call_id = _safe_identifier(event.get("id"), prefix="model-failure")
    elapsed = _optional_nonnegative_int(
        data.get("durationMs", data.get("duration"))
    )
    emitter.emit(
        "model_call",
        call_id=call_id,
        parent_call_id=None,
        phase="failed",
        requested_model=model,
        resolved_model=model,
        provider="model-gateway",
        provider_request_id=None,
        usage_delta={
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "calls": 1,
            "cost_microusd": None,
        },
        retry_index=state.model_failures,
        elapsed_ms=elapsed or 0,
        finish_reason=None,
        failure=_failure(code, scope, retryable=retryable, protocol_event=True),
    )
    state.model_failures += 1
    state.terminal_error = (code, scope, retryable)


def translate_copilot_event(
    event: Mapping[str, Any],
    *,
    emitter: EventEmitter,
    state: TranslationState,
) -> None:
    event_type = str(event.get("type") or "")
    data = _copilot_data(event)
    emitted_at = _copilot_timestamp(event)

    if event_type == "assistant.turn_start":
        call_id = _safe_identifier(
            data.get("turnId") or event.get("id"), prefix="model-call"
        )
        model = str(data.get("model") or state.requested_model)
        state.active_models[call_id] = (model, time.monotonic())
        emitter.emit(
            "model_call",
            call_id=call_id,
            parent_call_id=None,
            phase="started",
            requested_model=model,
            resolved_model=None,
            provider=None,
            provider_request_id=None,
            usage_delta={
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "calls": None,
                "cost_microusd": None,
            },
            retry_index=0,
            elapsed_ms=None,
            finish_reason=None,
            failure=None,
            emitted_at=emitted_at,
        )
        return

    if event_type == "assistant.usage":
        call_id = _safe_identifier(
            data.get("turnId") or data.get("apiCallId") or event.get("id"),
            prefix="model-call",
        )
        model = str(data.get("model") or state.requested_model)
        matching = state.active_models.pop(call_id, None)
        elapsed = _optional_nonnegative_int(data.get("duration"))
        if elapsed is None and matching is not None:
            elapsed = int((time.monotonic() - matching[1]) * 1000)
        provider_request_id = data.get("providerCallId") or data.get("apiCallId")
        if not isinstance(provider_request_id, str) or not provider_request_id:
            provider_request_id = str(event.get("id") or call_id)
        emitter.emit(
            "model_call",
            call_id=call_id,
            parent_call_id=None,
            phase="completed",
            requested_model=model,
            resolved_model=model,
            provider="model-gateway",
            provider_request_id=provider_request_id[:255],
            usage_delta=state.usage.add_model_usage(data),
            retry_index=0,
            elapsed_ms=elapsed or 0,
            finish_reason="completed",
            failure=None,
            emitted_at=emitted_at,
        )
        return

    if event_type in {"model.call_failure", "model.call_failed"}:
        _emit_model_failure(emitter, state, event)
        return

    if event_type in {"tool.execution_start", "tool.user_requested"}:
        call_id = _safe_identifier(
            data.get("toolCallId") or event.get("id"), prefix="tool-call"
        )
        tool_name = _safe_identifier(
            data.get("mcpToolName") or data.get("toolName"), prefix="tool"
        )
        parent = data.get("parentToolCallId")
        parent_id = (
            _safe_identifier(parent, prefix="tool-parent")
            if isinstance(parent, str) and parent
            else None
        )
        state.active_tools[call_id] = (tool_name, time.monotonic(), parent_id)
        state.usage.tool_calls += 1
        emitter.emit(
            "tool_call",
            call_id=call_id,
            parent_call_id=parent_id,
            tool=tool_name,
            phase="started",
            policy_decision="allowed",
            elapsed_ms=None,
            outcome="pending",
            failure=None,
            emitted_at=emitted_at,
        )
        return

    if event_type == "tool.execution_complete":
        call_id = _safe_identifier(
            data.get("toolCallId") or event.get("id"), prefix="tool-call"
        )
        prior = state.active_tools.pop(call_id, None)
        tool_name = prior[0] if prior else "tool"
        parent_id = prior[2] if prior else None
        elapsed = int((time.monotonic() - prior[1]) * 1000) if prior else 0
        success = data.get("success") is True
        emitter.emit(
            "tool_call",
            call_id=call_id,
            parent_call_id=parent_id,
            tool=tool_name,
            phase="completed" if success else "failed",
            policy_decision="allowed",
            elapsed_ms=elapsed,
            outcome="success" if success else "error",
            failure=(
                None
                if success
                else _failure(
                    "tool-error", "tool", retryable=False, protocol_event=True
                )
            ),
            emitted_at=emitted_at,
        )
        return

    if event_type == "subagent.started":
        call_id = _safe_identifier(
            data.get("toolCallId") or event.get("id"), prefix="subagent"
        )
        tool_name = _safe_identifier(
            f"subagent-{data.get('agentName', 'agent')}", prefix="subagent"
        )
        state.active_tools[call_id] = (tool_name, time.monotonic(), None)
        state.usage.tool_calls += 1
        emitter.emit(
            "tool_call",
            call_id=call_id,
            parent_call_id=None,
            tool=tool_name,
            phase="started",
            policy_decision="allowed",
            elapsed_ms=None,
            outcome="pending",
            failure=None,
            emitted_at=emitted_at,
        )
        return

    if event_type in {"subagent.completed", "subagent.failed"}:
        call_id = _safe_identifier(
            data.get("toolCallId") or event.get("id"), prefix="subagent"
        )
        prior = state.active_tools.pop(call_id, None)
        tool_name = (
            prior[0]
            if prior
            else _safe_identifier(
                f"subagent-{data.get('agentName', 'agent')}", prefix="subagent"
            )
        )
        elapsed = int((time.monotonic() - prior[1]) * 1000) if prior else 0
        success = event_type == "subagent.completed"
        emitter.emit(
            "tool_call",
            call_id=call_id,
            parent_call_id=None,
            tool=tool_name,
            phase="completed" if success else "failed",
            policy_decision="allowed",
            elapsed_ms=elapsed,
            outcome="success" if success else "error",
            failure=(
                None
                if success
                else _failure(
                    "subagent-failed",
                    "tool",
                    retryable=False,
                    protocol_event=True,
                )
            ),
            emitted_at=emitted_at,
        )
        return

    if event_type == "session.error":
        message = str(data.get("message") or "Copilot session error")
        code, scope, retryable = _classify_message(message)
        state.terminal_error = (code, scope, retryable)
        emitter.emit(
            "error",
            failure=_failure(
                code, scope, retryable=retryable, protocol_event=True
            ),
            recovered=False,
            related_call_id=None,
            emitted_at=emitted_at,
        )
        return

    if event_type == "result":
        state.child_result = dict(event)


def _stdout_reader(
    stream: BinaryIO,
    events: queue.Queue[tuple[str, Any]],
    *,
    total_limit: int,
    line_limit: int,
) -> None:
    total = 0
    buffered = bytearray()
    try:
        while True:
            read = getattr(stream, "read1", stream.read)
            chunk = read(65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > total_limit:
                events.put(("stream_error", StreamLimitError("stdout-limit-exceeded")))
                return
            buffered.extend(chunk)
            while True:
                newline = buffered.find(b"\n")
                if newline < 0:
                    break
                content = bytes(buffered[:newline])
                del buffered[: newline + 1]
                if content.endswith(b"\r"):
                    content = content[:-1]
                if len(content) > line_limit:
                    events.put(
                        ("stream_error", StreamLimitError("stdout-line-limit"))
                    )
                    return
                events.put(("stdout", (content, len(content) + 1)))
            if len(buffered) > line_limit:
                events.put(("stream_error", StreamLimitError("stdout-line-limit")))
                return
        if buffered:
            events.put(("stdout", (bytes(buffered), len(buffered))))
    except Exception as exc:  # pragma: no cover - OS pipe failure
        events.put(("stream_error", WrapperError(f"stdout-read-error: {exc}")))
    finally:
        events.put(("stdout_eof", total))


def _stderr_reader(
    stream: BinaryIO,
    events: queue.Queue[tuple[str, Any]],
    *,
    total_limit: int,
) -> None:
    total = 0
    tail = bytearray()
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                break
            total += len(chunk)
            tail.extend(chunk)
            if len(tail) > STDERR_TAIL_BYTES:
                del tail[:-STDERR_TAIL_BYTES]
            if total > total_limit:
                events.put(("stream_error", StreamLimitError("stderr-limit-exceeded")))
                return
    except Exception as exc:  # pragma: no cover - OS pipe failure
        events.put(("stream_error", WrapperError(f"stderr-read-error: {exc}")))
    finally:
        events.put(("stderr_eof", (total, bytes(tail))))


def _controller_reader(
    protocol_input: ProtocolInput,
    events: queue.Queue[tuple[str, Any]],
    *,
    line_limit: int,
    stop_event: threading.Event,
) -> None:
    try:
        os.set_blocking(protocol_input.file_descriptor, False)
        while not stop_event.is_set():
            try:
                content = protocol_input._pop_line(
                    line_limit, label="controller event"
                )
            except WrapperError as exc:
                events.put(("controller_error", str(exc)))
                return
            if content is None:
                try:
                    chunk = os.read(protocol_input.file_descriptor, 65_536)
                except BlockingIOError:
                    stop_event.wait(0.02)
                    continue
                if not chunk:
                    return
                protocol_input.buffered.extend(chunk)
                continue
            try:
                value = strict_json_loads(content)
            except WrapperError as exc:
                events.put(("controller_error", str(exc)))
                return
            events.put(("controller", value))
    except Exception as exc:  # pragma: no cover - OS pipe failure
        events.put(("controller_error", f"controller-read-error: {exc}"))


def _creation_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    grace_ms: int,
    hard: bool,
) -> str | None:
    if process.poll() is not None:
        return None
    if os.name == "nt":
        # Windows has no killpg equivalent. ``taskkill /T`` must run while the
        # root PID still exists or descendants can be orphaned, so the fallback
        # is deliberately hard and immediate.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
        return "SIGKILL"

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    deadline = time.monotonic() + (grace_ms / 1000)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None and hard:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return "SIGKILL"
    return "SIGTERM"


def _classify_terminal(
    *,
    cancelled: bool,
    timed_out: bool,
    stream_error: str | None,
    protocol_error: str | None,
    child_returncode: int | None,
    state: TranslationState,
    artifacts: Sequence[Mapping[str, Any]],
    artifact_error: str | None,
) -> tuple[str, dict[str, Any] | None]:
    if cancelled:
        return "cancelled", _failure("controller-cancelled", "harness")
    if timed_out:
        return "task_timeout", _failure("wall-time-exceeded", "harness")
    if stream_error:
        return "harness_crash", _failure(stream_error, "protocol")
    if protocol_error:
        return "harness_crash", _failure("copilot-protocol-error", "protocol")
    if artifact_error:
        return "invalid_artifact", _failure(artifact_error, "artifact")
    if child_returncode not in (0, None):
        if state.terminal_error:
            code, scope, retryable = state.terminal_error
            status = {
                "auth-failed": "auth_failed",
                "policy-denied": "policy_denied",
                "budget-exhausted": "budget_exhausted",
                "model-unreachable": "model_unreachable",
            }.get(code, "harness_crash")
            return status, _failure(code, scope, retryable=retryable)
        return "harness_crash", _failure("copilot-nonzero-exit", "harness")
    if not artifacts:
        return "no_edit", _failure("no-output-artifact", "harness")
    return "completed", None


def _controller_cancel(
    event: Mapping[str, Any], request: Mapping[str, Any]
) -> tuple[bool, int]:
    if event.get("schema") != CONTROLLER_EVENT_SCHEMA:
        raise WrapperError("controller event has the wrong schema")
    if event.get("source") != "controller":
        raise WrapperError("post-accept input must be controller-authored")
    if event.get("trial_id") != request["trial_id"] or event.get(
        "attempt_id"
    ) != request["attempt_id"]:
        raise WrapperError("controller event identity does not match request")
    if event.get("type") == "cancel":
        grace = _positive_int(
            event.get("grace_period_ms"), field_name="cancel.grace_period_ms"
        )
        configured = _positive_int(
            request["cancellation"]["grace_period_ms"],
            field_name="request.cancellation.grace_period_ms",
        )
        return True, min(grace, configured)
    if event.get("type") == "controller_error":
        return True, 0
    raise WrapperError(f"unexpected controller event: {event.get('type')!r}")


def run_wrapper(args: argparse.Namespace) -> int:
    protocol_input = ProtocolInput(sys.stdin.fileno())
    request_raw = protocol_input.read_line(
        HARD_INPUT_LINE_BYTES, label="trial request"
    )
    request = strict_json_loads(request_raw)
    _validate_request(request, args.harness)
    emitter = EventEmitter(request["trial_id"], request["attempt_id"])
    emitter.emit(
        "hello",
        supported_protocol_versions=[PROTOCOL_VERSION],
        capabilities=CAPABILITIES[args.harness],
        harness=request["harness"],
    )

    accepted_limit = min(
        int(request["protocol_limits"]["max_line_bytes"]), HARD_INPUT_LINE_BYTES
    )
    accepted_raw = protocol_input.read_line(
        accepted_limit, label="controller accepted event"
    )
    accepted = strict_json_loads(accepted_raw)
    _validate_accepted(accepted, request)

    workspace = pathlib.Path(args.workspace_root).resolve(strict=True)
    artifacts_root = pathlib.Path(args.artifacts_root)
    artifacts_root.mkdir(parents=True, exist_ok=True)
    if not artifacts_root.is_dir():
        raise WrapperError("artifacts root is not a directory")
    runtime_parent = pathlib.Path(args.runtime_root)
    runtime_parent.mkdir(parents=True, exist_ok=True)
    runtime_root = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f"atv-copilot-{args.harness}-", dir=str(runtime_parent)
        )
    )
    for name in (
        "home",
        "copilot-home",
        "xdg-config",
        "xdg-cache",
        "xdg-data",
        "tmp",
        "hve",
        "hve-telemetry",
    ):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    usage = Usage(started=started)
    state = TranslationState(
        requested_model=str(request["model_policy"]["allowed_models"][0]),
        usage=usage,
    )
    baseline = WorkspaceBaseline.capture(workspace, request["output"])
    process: subprocess.Popen[bytes] | None = None
    stderr_tail = b""
    signal_name: str | None = None
    cancelled = False
    timed_out = False
    stream_error: str | None = None
    protocol_error: str | None = None

    try:
        copilot_home = runtime_root / "copilot-home"
        if args.harness == "phoenix":
            _copy_phoenix_home(
                pathlib.Path(args.phoenix_home_template), copilot_home
            )
        child_env = _clean_child_environment(
            runtime_root=runtime_root,
            copilot_home=copilot_home,
            request=request,
            model=state.requested_model,
            harness=args.harness,
        )
        argv = build_copilot_argv(
            executable=args.copilot_executable,
            prefix_args=args.copilot_prefix_arg,
            harness=args.harness,
            prompt=request["prompt"]["text"],
            model=state.requested_model,
            workspace=workspace,
            hve_plugin=pathlib.Path(args.hve_plugin_dir),
        )
        emitter.emit("status", status="initializing", detail_code="copilot-start")
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **_creation_kwargs(),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        emitter.emit("status", status="running", detail_code="copilot-running")

        events: queue.Queue[tuple[str, Any]] = queue.Queue()
        controller_stop = threading.Event()
        line_limit = min(
            int(request["protocol_limits"]["max_line_bytes"]),
            HARD_CHILD_LINE_BYTES,
        )
        stdout_thread = threading.Thread(
            target=_stdout_reader,
            args=(process.stdout, events),
            kwargs={
                "total_limit": int(accepted["effective_budget_limits"]["stdout_bytes"]),
                "line_limit": line_limit,
            },
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_stderr_reader,
            args=(process.stderr, events),
            kwargs={
                "total_limit": int(accepted["effective_budget_limits"]["stderr_bytes"])
            },
            daemon=True,
        )
        controller_thread = threading.Thread(
            target=_controller_reader,
            args=(protocol_input, events),
            kwargs={
                "line_limit": accepted_limit,
                "stop_event": controller_stop,
            },
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        controller_thread.start()

        stdout_eof = False
        stderr_eof = False
        wall_limit = int(accepted["effective_budget_limits"]["wall_time_ms"])
        while process.poll() is None or not (stdout_eof and stderr_eof):
            try:
                kind, payload = events.get(timeout=0.02)
            except queue.Empty:
                kind, payload = "", None
            if kind == "stdout":
                content, byte_count = payload
                usage.stdout_bytes += byte_count
                if not content:
                    protocol_error = "blank-copilot-json-line"
                else:
                    try:
                        child_event = json.loads(content.decode("utf-8", errors="strict"))
                        if not isinstance(child_event, dict):
                            raise ValueError("Copilot JSONL row is not an object")
                        translate_copilot_event(
                            child_event, emitter=emitter, state=state
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                        protocol_error = f"invalid-copilot-json: {exc}"
            elif kind == "stderr_eof":
                usage.stderr_bytes, stderr_tail = payload
                stderr_eof = True
            elif kind == "stdout_eof":
                # The reader includes all bytes, including rows already counted.
                usage.stdout_bytes = max(usage.stdout_bytes, int(payload))
                stdout_eof = True
            elif kind == "stream_error":
                stream_error = str(payload)
            elif kind == "controller_error":
                protocol_error = str(payload)
            elif kind == "controller":
                try:
                    should_cancel, grace_ms = _controller_cancel(payload, request)
                except WrapperError as exc:
                    protocol_error = str(exc)
                else:
                    if should_cancel and not cancelled:
                        cancelled = True
                        emitter.emit(
                            "status",
                            status="cancelling",
                            detail_code="controller-cancel",
                        )
                        signal_name = _terminate_process_tree(
                            process,
                            grace_ms=grace_ms,
                            hard=request["cancellation"]["hard_kill"] is True,
                        )

            if (
                not cancelled
                and not timed_out
                and (time.monotonic() - started) * 1000 >= wall_limit
            ):
                timed_out = True
                emitter.emit(
                    "status", status="cancelling", detail_code="wall-time-limit"
                )
                signal_name = _terminate_process_tree(
                    process,
                    grace_ms=int(request["cancellation"]["grace_period_ms"]),
                    hard=request["cancellation"]["hard_kill"] is True,
                )
            if (stream_error or protocol_error) and process.poll() is None:
                signal_name = _terminate_process_tree(
                    process, grace_ms=0, hard=True
                )

        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        controller_stop.set()
        controller_thread.join(timeout=2)
        if process.poll() is None:
            signal_name = _terminate_process_tree(process, grace_ms=0, hard=True)
        child_returncode = process.wait(timeout=5)

        emitter.emit("status", status="verifying", detail_code="artifact-hash")
        artifacts, artifact_error = collect_artifacts(
            workspace=workspace,
            baseline=baseline,
            output_contract=request["output"],
            artifact_budget=int(
                accepted["effective_budget_limits"]["artifact_bytes"]
            ),
        )
        for artifact in artifacts:
            emitter.emit("artifact", artifact=artifact)
        reported_usage = usage.measurement(artifacts)
        emitter.emit("usage", cumulative_reported=reported_usage)
        status, failure = _classify_terminal(
            cancelled=cancelled,
            timed_out=timed_out,
            stream_error=stream_error,
            protocol_error=protocol_error,
            child_returncode=child_returncode,
            state=state,
            artifacts=artifacts,
            artifact_error=artifact_error,
        )
        output_tree_digest = (
            canonical_digest({"artifacts": artifacts})
            if status == "completed"
            else None
        )
        emitter.emit(
            "result",
            harness_result={
                "schema": RESULT_SCHEMA,
                "status": status,
                "exit": {
                    "code": child_returncode,
                    "signal": signal_name,
                    "timed_out": timed_out,
                    "cancelled": cancelled,
                },
                "output_tree_digest": output_tree_digest,
                "artifacts": artifacts,
                "reported_usage": reported_usage,
                "failure": failure,
            },
        )
        if stderr_tail:
            redacted = stderr_tail.decode("utf-8", errors="replace")
            redacted = redacted.replace(
                os.environ.get(MODEL_GATEWAY_ENV, ""), "[REDACTED]"
            )
            print(
                f"Copilot stderr tail ({len(stderr_tail)} bytes): {redacted}",
                file=sys.stderr,
            )
        return 0
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process, grace_ms=0, hard=True)
        shutil.rmtree(runtime_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Phoenix or hve-core behind ATV protocol v1"
    )
    parser.add_argument(
        "--harness", required=True, choices=("phoenix", "hve-core")
    )
    parser.add_argument(
        "--copilot-executable", default=DEFAULT_COPILOT
    )
    parser.add_argument(
        "--copilot-prefix-arg",
        action="append",
        default=[],
        help="argv inserted after the executable; intended for model-free test fakes",
    )
    parser.add_argument(
        "--phoenix-home-template", default=DEFAULT_PHOENIX_HOME
    )
    parser.add_argument("--hve-plugin-dir", default=DEFAULT_HVE_PLUGIN)
    parser.add_argument("--workspace-root", default=DEFAULT_WORKSPACE)
    parser.add_argument("--artifacts-root", default=DEFAULT_ARTIFACTS)
    parser.add_argument("--runtime-root", default="/tmp")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_wrapper(args)
    except (WrapperError, OSError, subprocess.SubprocessError) as exc:
        print(f"protocol-wrapper: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
