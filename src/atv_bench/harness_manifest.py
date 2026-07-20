"""Manifest-driven third-party harness integration.

This module is intentionally vendor-neutral. A new harness integrates by adding an
``atv.harness/v1`` manifest and executable artifact; no core registry edit is required.
"""
from __future__ import annotations

import dataclasses
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from atv_bench.adapters import (
    DEFAULT_ENV_ALLOWLIST,
    AdapterRequest,
    AdapterResult,
    CommandHarnessAdapter,
)
from atv_bench.protocol import (
    CANONICAL_EXTERNAL_RESULT_SCHEMA,
    ProtocolSession,
    ProtocolTranscript,
    SchemaKind,
    SchemaStore,
    canonical_digest,
    canonical_json_bytes,
    canonical_sha256,
    default_schema_store,
    strict_json_loads,
    validate_conformance,
)

MAX_MANIFEST_BYTES = 1024 * 1024
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_DYNAMIC_PLACEHOLDERS = frozenset(
    {"{goal}", "{repo}", "{bot_file}", "{model}", "{request_path}"}
)
_STATIC_PLACEHOLDERS = frozenset({"{manifest_dir}"})
_ALL_PLACEHOLDERS = _DYNAMIC_PLACEHOLDERS | _STATIC_PLACEHOLDERS
_SHELL_NAMES = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "wsl",
        "wsl.exe",
    }
)
_SHELL_SUFFIXES = (".bat", ".cmd", ".ps1", ".sh")
_SAFE_WRITABLE_PATHS = frozenset({"/workspace", "/artifacts", "/tmp"})
_BOOLEAN_CAPABILITIES = (
    "workspace_edit",
    "subagents",
    "resumable",
    "browser",
    "model_events",
    "tool_events",
    "usage_events",
    "checkpoint_events",
)
_REPORTING_CAPABILITIES = (
    "token_usage_reporting",
    "call_usage_reporting",
    "cost_usage_reporting",
)
_REPORTING_RANK = {"unsupported": 0, "reported": 1}
_MODEL_SELECTION_RANK = {"none": 0, "single": 1, "multiple": 2}
_BRIDGE_CONFIG_ENV = "ATV_HARNESS_BRIDGE_CONFIG_B64"


class HarnessManifestError(ValueError):
    """Actionable manifest, registry, or factory failure."""

    def __init__(
        self,
        code: str,
        *,
        problem: str,
        cause: str,
        fix: str,
        evidence: str,
    ) -> None:
        self.code = code
        self.problem = problem
        self.cause = cause
        self.fix = fix
        self.evidence = evidence
        super().__init__(
            f"Problem: {problem}\n"
            f"Cause: {cause}\n"
            f"Fix: {fix}\n"
            f"Evidence: {evidence}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "problem": self.problem,
            "cause": self.cause,
            "fix": self.fix,
            "evidence": self.evidence,
        }


def _error(
    code: str,
    *,
    problem: str,
    cause: str,
    fix: str,
    evidence: str,
) -> HarnessManifestError:
    return HarnessManifestError(
        code,
        problem=problem,
        cause=cause,
        fix=fix,
        evidence=evidence,
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _safe_manifest_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _error(
            "manifest_unreadable",
            problem="The harness manifest could not be read.",
            cause=str(exc),
            fix="Provide a readable regular .json, .yaml, or .yml manifest file.",
            evidence=str(path),
        ) from exc
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if path.is_symlink() or is_reparse or not stat.S_ISREG(metadata.st_mode):
        raise _error(
            "manifest_not_regular",
            problem="The harness manifest is not a confined regular file.",
            cause="Links, junctions, directories, and special files are not accepted.",
            fix="Copy the manifest into a normal regular file and retry.",
            evidence=str(path),
        )
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise _error(
            "manifest_too_large",
            problem="The harness manifest exceeds the size limit.",
            cause=f"Observed {metadata.st_size} bytes; limit is {MAX_MANIFEST_BYTES}.",
            fix="Remove embedded logs, binaries, and unrelated metadata.",
            evidence=str(path),
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _error(
            "manifest_unreadable",
            problem="The harness manifest could not be read.",
            cause=str(exc),
            fix="Correct filesystem permissions and retry.",
            evidence=str(path),
        ) from exc
    if len(data) != metadata.st_size:
        raise _error(
            "manifest_changed",
            problem="The harness manifest changed while it was being loaded.",
            cause="The pre-read and post-read sizes differ.",
            fix="Stop the concurrent writer and load an immutable manifest.",
            evidence=str(path),
        )
    return data


def _import_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise _error(
            "yaml_dependency_missing",
            problem="YAML harness manifests require PyYAML.",
            cause="The optional 'yaml' module is not installed.",
            fix="Install the project dev dependencies or use canonical JSON.",
            evidence="import yaml",
        ) from exc
    return yaml


def _load_yaml(text: str, *, evidence: str) -> Any:
    yaml = _import_yaml()

    class UniqueSafeLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key: {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueSafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    try:
        return yaml.load(text, Loader=UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise _error(
            "manifest_yaml_invalid",
            problem="The YAML harness manifest is invalid.",
            cause=str(exc),
            fix="Correct the YAML syntax and duplicate keys, or use canonical JSON.",
            evidence=evidence,
        ) from exc


def _parse_manifest(path: Path, data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error(
            "manifest_encoding",
            problem="The harness manifest is not valid UTF-8.",
            cause=str(exc),
            fix="Encode the manifest as UTF-8 without a byte-order mark.",
            evidence=str(path),
        ) from exc
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            value = strict_json_loads(text)
        except ValueError as exc:
            raise _error(
                "manifest_json_invalid",
                problem="The JSON harness manifest is invalid.",
                cause=str(exc),
                fix="Correct malformed JSON, duplicate keys, floats, or nonportable values.",
                evidence=str(path),
            ) from exc
    elif suffix in {".yaml", ".yml"}:
        value = _load_yaml(text, evidence=str(path))
    else:
        raise _error(
            "manifest_extension",
            problem="The harness manifest extension is unsupported.",
            cause=f"Received {suffix or '<none>'!r}.",
            fix="Use .json, .yaml, or .yml.",
            evidence=str(path),
        )
    if not isinstance(value, dict):
        raise _error(
            "manifest_shape",
            problem="The harness manifest must contain one object.",
            cause=f"Decoded {type(value).__name__}.",
            fix="Make the document root a mapping/object.",
            evidence=str(path),
        )
    return value


def _validate_env_allowlist(values: Sequence[str], *, evidence: str) -> tuple[str, ...]:
    names = tuple(values)
    for name in names:
        if (
            not isinstance(name, str)
            or not _ENV_NAME.fullmatch(name)
            or any(character in name for character in "*?[]=:\r\n\x00")
        ):
            raise _error(
                "unsafe_environment_allowlist",
                problem="The harness environment allowlist is not a list of exact names.",
                cause=f"Unsafe entry: {name!r}. Wildcards and raw values are forbidden.",
                fix="List only exact environment variable names such as MODEL_BROKER_TOKEN.",
                evidence=evidence,
            )
    if len(set(names)) != len(names):
        raise _error(
            "duplicate_environment_name",
            problem="The harness environment allowlist contains duplicates.",
            cause=repr(names),
            fix="List each environment variable name exactly once.",
            evidence=evidence,
        )
    return names


def _validate_argv_template(
    values: Sequence[str],
    *,
    evidence: str,
    allow_manifest_dir: bool,
) -> tuple[str, ...]:
    argv = tuple(values)
    if not argv:
        raise _error(
            "empty_argv",
            problem="The harness command is empty.",
            cause="At least one argv element is required.",
            fix="Declare an executable and arguments as a JSON/YAML array.",
            evidence=evidence,
        )
    allowed = _DYNAMIC_PLACEHOLDERS | (
        _STATIC_PLACEHOLDERS if allow_manifest_dir else frozenset()
    )
    for index, part in enumerate(argv):
        if (
            not isinstance(part, str)
            or not part
            or any(character in part for character in "\x00\r\n")
        ):
            raise _error(
                "unsafe_argv",
                problem="The harness argv contains an unsafe element.",
                cause=f"argv[{index}]={part!r}",
                fix="Use non-empty, newline-free argv strings; no shell command string.",
                evidence=evidence,
            )
        tokens = set(_PLACEHOLDER.findall(part))
        unknown = tokens - allowed
        remainder = part
        for token in tokens:
            remainder = remainder.replace(token, "")
        if unknown or "{" in remainder or "}" in remainder:
            raise _error(
                "unknown_placeholder",
                problem="The harness command contains an unsupported placeholder.",
                cause=f"argv[{index}] tokens={sorted(tokens)} unknown={sorted(unknown)}",
                fix="Use only {goal}, {repo}, {bot_file}, {model}, {request_path}, "
                "and process-only {manifest_dir}.",
                evidence=evidence,
            )
        if "{manifest_dir}" in part:
            if not part.startswith(("{manifest_dir}/", "{manifest_dir}\\")):
                raise _error(
                    "unsafe_manifest_relative_path",
                    problem="{manifest_dir} must prefix one confined relative path.",
                    cause=f"argv[{index}]={part!r}",
                    fix="Use {manifest_dir}/relative/path without traversal.",
                    evidence=evidence,
                )
            relative = part[len("{manifest_dir}") :].lstrip("/\\")
            if (
                not relative
                or ".." in Path(relative).parts
                or Path(relative).is_absolute()
            ):
                raise _error(
                    "unsafe_manifest_relative_path",
                    problem="The manifest-relative executable path escapes its directory.",
                    cause=f"argv[{index}]={part!r}",
                    fix="Use a child path below {manifest_dir}; '..' is forbidden.",
                    evidence=evidence,
                )
    if any(token in argv[0] for token in _DYNAMIC_PLACEHOLDERS):
        raise _error(
            "dynamic_executable",
            problem="The executable path cannot depend on trial input.",
            cause=f"argv[0]={argv[0]!r}",
            fix="Use a fixed executable or process-only {manifest_dir} path.",
            evidence=evidence,
        )
    executable_name = Path(argv[0].replace("{manifest_dir}", "manifest")).name.lower()
    if executable_name in _SHELL_NAMES or executable_name.endswith(_SHELL_SUFFIXES):
        raise _error(
            "shell_forbidden",
            problem="Shell-based harness launch is forbidden.",
            cause=f"Executable {argv[0]!r} is a shell or shell script.",
            fix="Expose a direct executable/argv interface without sh, cmd, or PowerShell.",
            evidence=evidence,
        )
    return argv


def _semantic_validate(document: Mapping[str, Any], *, evidence: str) -> None:
    security = document["security"]
    _validate_env_allowlist(security["env_allowlist"], evidence=evidence)
    if security["requires_tty"]:
        raise _error(
            "tty_unsupported",
            problem="TTY-only harnesses cannot run in the benchmark.",
            cause="security.requires_tty is true.",
            fix="Provide a deterministic headless stdin/stdout mode.",
            evidence=evidence,
        )
    writable = set(security["writable_paths"])
    unsafe = writable - _SAFE_WRITABLE_PATHS
    if unsafe or "/workspace" not in writable:
        raise _error(
            "unsafe_writable_paths",
            problem="The harness requests unsupported writable guest paths.",
            cause=f"Requested={sorted(writable)} unsupported={sorted(unsafe)}",
            fix="Restrict writes to /workspace, /artifacts, and optional /tmp.",
            evidence=evidence,
        )
    runtime = document["runtime"]
    command_key = "command" if runtime["kind"] == "process" else "entrypoint"
    _validate_argv_template(
        runtime[command_key],
        evidence=evidence,
        allow_manifest_dir=runtime["kind"] == "process",
    )
    if runtime["kind"] == "oci":
        if any("{manifest_dir}" in part for part in runtime["entrypoint"]):
            raise _error(
                "oci_manifest_path",
                problem="OCI commands cannot reference the host manifest directory.",
                cause="The OCI filesystem does not mount {manifest_dir}.",
                fix="Bake the executable into the digest-pinned image.",
                evidence=evidence,
            )
        try:
            from atv_bench.sandbox import DigestPinnedImage

            DigestPinnedImage.parse(runtime["image"])
        except Exception as exc:
            raise _error(
                "mutable_oci_image",
                problem="The OCI harness image is not immutably digest-pinned.",
                cause=str(exc),
                fix="Use name@sha256:<64 lowercase hex> without a mutable tag.",
                evidence=evidence,
            ) from exc


@dataclasses.dataclass(frozen=True, slots=True)
class LoadedHarnessManifest:
    source_path: Path
    canonical_bytes: bytes
    digest: str
    _document: Mapping[str, Any] = dataclasses.field(repr=False)

    @property
    def id(self) -> str:
        return str(self._document["id"])

    @property
    def version(self) -> str:
        return str(self._document["version"])

    @property
    def identity(self) -> tuple[str, str]:
        return self.id, self.version

    @property
    def runtime_kind(self) -> str:
        return str(self._document["runtime"]["kind"])

    @property
    def manifest_dir(self) -> Path:
        return self.source_path.parent

    @property
    def digest_descriptor(self) -> dict[str, str]:
        return {"algorithm": "sha256", "value": self.digest}

    @property
    def document(self) -> Mapping[str, Any]:
        return self._document

    def as_dict(self) -> dict[str, Any]:
        value = strict_json_loads(self.canonical_bytes.decode("utf-8"))
        assert isinstance(value, dict)
        return value


def load_harness_manifest(
    path: str | os.PathLike[str],
    *,
    store: SchemaStore | None = None,
) -> LoadedHarnessManifest:
    source = Path(os.path.abspath(os.fspath(path)))
    document = _parse_manifest(source, _safe_manifest_bytes(source))
    try:
        _semantic_validate(document, evidence=str(source))
    except HarnessManifestError:
        raise
    except (KeyError, TypeError, AttributeError):
        # Let the canonical JSON Schema produce the missing/type diagnostic.
        pass
    active_store = store or default_schema_store()
    try:
        active_store.validate(document, SchemaKind.HARNESS)
    except Exception as exc:
        raise _error(
            "manifest_schema_invalid",
            problem="The harness manifest does not conform to atv.harness/v1.",
            cause=str(exc),
            fix="Remove unknown fields and satisfy every required versioned field.",
            evidence=str(source),
        ) from exc
    _semantic_validate(document, evidence=str(source))
    try:
        canonical = canonical_json_bytes(document)
    except ValueError as exc:
        raise _error(
            "manifest_not_canonical",
            problem="The harness manifest cannot be canonically serialized.",
            cause=str(exc),
            fix="Use strings, booleans, null, arrays, objects, and safe integers only.",
            evidence=str(source),
        ) from exc
    return LoadedHarnessManifest(
        source_path=source,
        canonical_bytes=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
        _document=_freeze(document),
    )


class HarnessManifestRegistry:
    """Explicit registry with no vendor-specific core table."""

    def __init__(self) -> None:
        self._by_digest: dict[str, LoadedHarnessManifest] = {}
        self._by_identity: dict[tuple[str, str], LoadedHarnessManifest] = {}
        self._lock = threading.RLock()

    def __len__(self) -> int:
        return len(self._by_digest)

    def register(self, manifest: LoadedHarnessManifest) -> LoadedHarnessManifest:
        if not isinstance(manifest, LoadedHarnessManifest):
            raise TypeError("registry accepts LoadedHarnessManifest objects")
        with self._lock:
            if manifest.digest in self._by_digest:
                raise _error(
                    "duplicate_manifest_digest",
                    problem="The harness manifest digest is already registered.",
                    cause=manifest.digest,
                    fix="Register each immutable manifest exactly once.",
                    evidence=str(manifest.source_path),
                )
            existing = self._by_identity.get(manifest.identity)
            if existing is not None:
                raise _error(
                    "manifest_identity_conflict",
                    problem="A different manifest already uses this id/version.",
                    cause=f"{manifest.id}@{manifest.version}: "
                    f"{existing.digest} != {manifest.digest}",
                    fix="Bump the harness version whenever manifest content changes.",
                    evidence=str(manifest.source_path),
                )
            self._by_digest[manifest.digest] = manifest
            self._by_identity[manifest.identity] = manifest
        return manifest

    def load(
        self,
        path: str | os.PathLike[str],
        *,
        store: SchemaStore | None = None,
    ) -> LoadedHarnessManifest:
        return self.register(load_harness_manifest(path, store=store))

    def by_digest(self, digest: str) -> LoadedHarnessManifest:
        try:
            return self._by_digest[digest]
        except KeyError as exc:
            raise KeyError(f"unknown harness manifest digest: {digest}") from exc

    def by_identity(self, harness_id: str, version: str) -> LoadedHarnessManifest:
        try:
            return self._by_identity[(harness_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown harness manifest: {harness_id}@{version}") from exc

    def manifests(self) -> tuple[LoadedHarnessManifest, ...]:
        return tuple(
            self._by_identity[key]
            for key in sorted(self._by_identity)
        )


@dataclasses.dataclass(frozen=True, slots=True)
class StaticCompatibility:
    protocol_version: int
    manifest_digest: str
    request_digest: Mapping[str, str]
    declared_capabilities: Mapping[str, Any]


def preflight_manifest_compatibility(
    manifest: LoadedHarnessManifest,
    trial_request: Mapping[str, Any],
    *,
    store: SchemaStore | None = None,
) -> StaticCompatibility:
    active_store = store or default_schema_store()
    try:
        active_store.validate(trial_request, SchemaKind.TRIAL_REQUEST)
        document = manifest.as_dict()
        harness_ref = trial_request["harness"]
        if (
            harness_ref["id"] != manifest.id
            or harness_ref["version"] != manifest.version
            or harness_ref["manifest_digest"] != manifest.digest_descriptor
        ):
            raise ValueError("trial request harness identity/digest does not match manifest")
        protocol = document["protocol"]
        requested_version = int(trial_request["protocol_version"])
        if not (
            int(protocol["minimum_version"])
            <= requested_version
            <= int(protocol["maximum_version"])
        ):
            raise ValueError("requested protocol version is outside the manifest range")
        declared = document["capabilities"]
        required = trial_request["required_capabilities"]
        missing: list[str] = []
        for field in _BOOLEAN_CAPABILITIES:
            if required[field] and not declared[field]:
                missing.append(field)
        if (
            _MODEL_SELECTION_RANK[declared["model_selection"]]
            < _MODEL_SELECTION_RANK[required["model_selection"]]
        ):
            missing.append(f"model_selection>={required['model_selection']}")
        for field in _REPORTING_CAPABILITIES:
            if _REPORTING_RANK[declared[field]] < _REPORTING_RANK[required[field]]:
                missing.append(f"{field}>={required[field]}")
        forbidden = [
            field
            for field in trial_request["forbidden_capabilities"]
            if declared[field]
        ]
        if missing:
            raise ValueError("required capabilities are missing: " + ", ".join(missing))
        if forbidden:
            raise ValueError(
                "forbidden capabilities are declared: " + ", ".join(forbidden)
            )
        security = document["security"]
        credential_names = {
            item["name"] for item in trial_request["policy"]["credentials"]
        }
        if not credential_names.issubset(security["env_allowlist"]):
            raise ValueError("trial credentials exceed the manifest environment allowlist")
        if not set(trial_request["policy"]["writable_paths"]).issubset(
            security["writable_paths"]
        ):
            raise ValueError("trial writable paths exceed the manifest declaration")
        network = trial_request["policy"]["network"]
        if security["network_requirement"] == "model-gateway-only":
            gateway = trial_request["model_policy"]["gateway"]
            if (
                network["mode"] == "none"
                or gateway not in network["allowed_destinations"]
            ):
                raise ValueError("manifest requires model-gateway-only network access")
        if network["mode"] == "model-gateway-only" and network[
            "allowed_destinations"
        ] != [trial_request["model_policy"]["gateway"]]:
            raise ValueError("model gateway destination does not match model policy")
        return StaticCompatibility(
            protocol_version=requested_version,
            manifest_digest=manifest.digest,
            request_digest=MappingProxyType(canonical_digest(trial_request)),
            declared_capabilities=_freeze(declared),
        )
    except Exception as exc:
        raise _error(
            "manifest_compatibility_failed",
            problem="Static harness compatibility failed before execution.",
            cause=str(exc),
            fix="Align protocol versions, identity digest, capabilities, credentials, "
            "writable paths, and network policy.",
            evidence=f"{manifest.id}@{manifest.version} digest={manifest.digest}",
        ) from exc


def _render_static_process_argv(manifest: LoadedHarnessManifest) -> tuple[str, ...]:
    template = tuple(manifest.document["runtime"]["command"])
    root = str(manifest.manifest_dir)
    return tuple(part.replace("{manifest_dir}", root) for part in template)


def render_argv_template(
    template: Sequence[str],
    *,
    goal: str,
    repo: str,
    bot_file: str,
    model: str,
    request_path: str,
) -> tuple[str, ...]:
    replacements = {
        "{goal}": goal,
        "{repo}": repo,
        "{bot_file}": bot_file,
        "{model}": model,
        "{request_path}": request_path,
    }
    rendered: list[str] = []
    for part in template:
        value = str(part)
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        if any(token in value for token in _ALL_PLACEHOLDERS) or "{" in value or "}" in value:
            raise _error(
                "unresolved_placeholder",
                problem="The harness argv still contains an unresolved placeholder.",
                cause=value,
                fix="Provide every required rendering value before execution.",
                evidence=repr(tuple(template)),
            )
        rendered.append(value)
    return tuple(rendered)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_process_artifact(
    manifest: LoadedHarnessManifest,
    rendered_command: Sequence[str],
) -> Path:
    template = tuple(manifest.document["runtime"]["command"])
    candidate: Path | None = None
    for original, rendered in zip(template, rendered_command, strict=True):
        if "{manifest_dir}" in original:
            possible = Path(rendered)
            if possible.is_file():
                candidate = possible.resolve()
                manifest_root = manifest.manifest_dir.resolve()
                if candidate != manifest_root and manifest_root not in candidate.parents:
                    raise _error(
                        "manifest_artifact_escape",
                        problem="The digest-verified harness artifact escapes manifest_dir.",
                        cause=str(candidate),
                        fix="Keep the executable artifact below the manifest directory.",
                        evidence=str(manifest.source_path),
                    )
                break
    if candidate is None:
        executable = rendered_command[0]
        possible = (
            Path(executable)
            if os.path.isabs(executable) or Path(executable).parent != Path(".")
            else Path(shutil.which(executable) or executable)
        )
        if possible.is_file():
            candidate = possible.resolve()
    if candidate is None:
        raise _error(
            "harness_executable_missing",
            problem="The manifest harness executable could not be resolved.",
            cause=repr(tuple(rendered_command)),
            fix="Install the executable or reference a file under {manifest_dir}.",
            evidence=str(manifest.source_path),
        )
    expected = manifest.document["runtime"]["executable_digest"]["value"]
    observed = _sha256_file(candidate)
    if observed != expected:
        raise _error(
            "harness_executable_digest_mismatch",
            problem="The harness executable does not match the manifest.",
            cause=f"expected={expected} observed={observed}",
            fix="Rebuild the artifact and update the manifest version and digest.",
            evidence=str(candidate),
        )
    return candidate


def _canonical_plan_contract(
    manifest: LoadedHarnessManifest,
    compatibility: StaticCompatibility,
) -> Mapping[str, Any]:
    document = manifest.as_dict()
    return MappingProxyType(
        {
            "schema": "atv.harness-adapter-plan/v1",
            "harness": {
                "id": manifest.id,
                "version": manifest.version,
                "manifest_digest": manifest.digest_descriptor,
            },
            "protocol_version": compatibility.protocol_version,
            "capabilities": document["capabilities"],
            "security": document["security"],
            "result_schema": CANONICAL_EXTERNAL_RESULT_SCHEMA,
        }
    )


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestProcessResult:
    adapter_result: AdapterResult
    transcript: ProtocolTranscript | None
    conformance_error: str | None


def _last_json_line(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _replay_bridge_evidence(
    manifest: LoadedHarnessManifest,
    trial_request: Mapping[str, Any],
    evidence_path: Path,
) -> ProtocolTranscript:
    normalized_request = _thaw(trial_request)
    assert isinstance(normalized_request, dict)
    temp_root = Path(tempfile.gettempdir()).resolve()
    supplied = Path(os.path.abspath(os.fspath(evidence_path)))
    try:
        metadata = supplied.lstat()
    except OSError as exc:
        raise _error(
            "bridge_evidence_missing",
            problem="The protocol bridge evidence file is unavailable.",
            cause=str(exc),
            fix="Discard the run and rerun with the controller-owned bridge.",
            evidence=str(supplied),
        ) from exc
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if supplied.is_symlink() or is_reparse or not stat.S_ISREG(metadata.st_mode):
        raise _error(
            "bridge_evidence_file",
            problem="The protocol bridge evidence is not a regular file.",
            cause=str(supplied),
            fix="Discard the run and rerun with the controller-owned bridge.",
            evidence=str(manifest.source_path),
        )
    resolved = supplied.resolve(strict=True)
    if temp_root not in resolved.parents or not resolved.name.startswith(
        "atv-bridge-evidence-"
    ):
        raise _error(
            "bridge_evidence_path",
            problem="The protocol bridge reported an unsafe evidence path.",
            cause=str(resolved),
            fix="Use the controller-owned process bridge unchanged.",
            evidence=str(manifest.source_path),
        )
    if metadata.st_size > 33_554_432:
        raise _error(
            "bridge_evidence_file",
            problem="The protocol bridge evidence is not a bounded regular file.",
            cause=f"path={resolved} size={metadata.st_size}",
            fix="Discard the run and rerun with the controller-owned bridge.",
            evidence=str(manifest.source_path),
        )
    try:
        evidence = strict_json_loads(resolved.read_text(encoding="utf-8"))
    finally:
        resolved.unlink(missing_ok=True)
    if not isinstance(evidence, dict) or evidence.get("schema") != (
        "atv.process-bridge-evidence/v1"
    ):
        raise _error(
            "bridge_evidence_schema",
            problem="The protocol bridge evidence is malformed.",
            cause=repr(evidence)[:500],
            fix="Discard the run and rerun with the controller-owned bridge.",
            evidence=str(manifest.source_path),
        )
    if evidence.get("request_digest") != canonical_digest(normalized_request):
        raise _error(
            "bridge_request_mismatch",
            problem="The protocol bridge evidence is bound to another request.",
            cause=repr(evidence.get("request_digest")),
            fix="Discard the run; never reuse bridge evidence across attempts.",
            evidence=str(manifest.source_path),
        )
    session = ProtocolSession(manifest.as_dict(), normalized_request)
    for item in evidence.get("observations", []):
        if not isinstance(item, dict):
            raise _error(
                "bridge_observation_shape",
                problem="The bridge observation stream is malformed.",
                cause=repr(item),
                fix="Discard the run.",
                evidence=str(manifest.source_path),
            )
        kind = item.get("kind")
        if kind == "harness":
            session.receive_harness_event(
                item["event"],
                recorded_at=item["recorded_at"],
            )
        elif kind == "controller_accept":
            session.record_controller_accept(recorded_at=item["recorded_at"])
        elif kind == "controller_cancel":
            session.record_controller_cancel(
                recorded_at=item["recorded_at"],
                reason_code=item["reason_code"],
                grace_period_ms=item["grace_period_ms"],
            )
        elif kind == "controller_error":
            session.record_controller_error(
                recorded_at=item["recorded_at"],
                failure=item["failure"],
            )
        else:
            raise _error(
                "bridge_observation_kind",
                problem="The bridge observation kind is unsupported.",
                cause=repr(kind),
                fix="Discard the run.",
                evidence=str(manifest.source_path),
            )
    transcript = session.finish()
    validate_conformance(transcript, manifest.as_dict(), normalized_request)
    if canonical_sha256(list(transcript.events)) != evidence.get("transcript_sha256"):
        raise _error(
            "bridge_transcript_mismatch",
            problem="The replayed protocol transcript digest does not match bridge evidence.",
            cause=f"observed={canonical_sha256(list(transcript.events))} "
            f"claimed={evidence.get('transcript_sha256')}",
            fix="Discard the run.",
            evidence=str(manifest.source_path),
        )
    return transcript


@dataclasses.dataclass(frozen=True, slots=True)
class ProcessAdapterPlan:
    manifest: LoadedHarnessManifest
    adapter: CommandHarnessAdapter
    compatibility: StaticCompatibility
    executable_path: Path
    harness_command_template: tuple[str, ...]
    trial_request: Mapping[str, Any]
    canonical_contract: Mapping[str, Any]
    effective_environment_names: tuple[str, ...]
    official_eligible: bool = False
    trust_tier: str = "local-self-attested"
    network_enforced: bool = False

    @property
    def canonical_result_contract(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema": "atv.adapter-result-contract/v1",
                "protocol_version": self.compatibility.protocol_version,
                "trial_result_schema": CANONICAL_EXTERNAL_RESULT_SCHEMA,
            }
        )

    def render_argv(self, request: AdapterRequest, *, request_path: str) -> tuple[str, ...]:
        return render_argv_template(
            self.harness_command_template,
            goal=request.goal,
            repo=str(Path(request.repo_path).resolve()),
            bot_file=request.bot_file,
            model=request.model,
            request_path=request_path,
        )

    def run(
        self,
        request: AdapterRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ManifestProcessResult:
        manifest_allowlist = set(self.manifest.document["security"]["env_allowlist"])
        unexpected = set(request.env_allowlist) - manifest_allowlist
        if unexpected:
            raise _error(
                "request_environment_conflict",
                problem="The adapter request expands the manifest environment allowlist.",
                cause=", ".join(sorted(unexpected)),
                fix="Remove request-specific environment additions or version the manifest.",
                evidence=f"{self.manifest.id}@{self.manifest.version}",
            )
        result = self.adapter.run(request, cancel_event=cancel_event)
        summary = _last_json_line(result.log)
        evidence_value = summary.get("bridge_evidence_path")
        if not isinstance(evidence_value, str) or not evidence_value:
            return ManifestProcessResult(
                adapter_result=result,
                transcript=None,
                conformance_error="protocol bridge evidence was not produced",
            )
        try:
            transcript = _replay_bridge_evidence(
                self.manifest,
                self.trial_request,
                Path(evidence_value),
            )
        except Exception as exc:
            return ManifestProcessResult(
                adapter_result=result,
                transcript=None,
                conformance_error=str(exc),
            )
        return ManifestProcessResult(
            adapter_result=result,
            transcript=transcript,
            conformance_error=None,
        )


def create_process_adapter_plan(
    manifest: LoadedHarnessManifest | str | os.PathLike[str],
    trial_request: Mapping[str, Any],
    *,
    store: SchemaStore | None = None,
) -> ProcessAdapterPlan:
    loaded = (
        manifest
        if isinstance(manifest, LoadedHarnessManifest)
        else load_harness_manifest(manifest, store=store)
    )
    if loaded.runtime_kind != "process":
        raise _error(
            "wrong_runtime_kind",
            problem="A process adapter was requested for a non-process manifest.",
            cause=f"runtime.kind={loaded.runtime_kind!r}",
            fix="Use create_oci_adapter_plan for OCI manifests.",
            evidence=str(loaded.source_path),
        )
    compatibility = preflight_manifest_compatibility(
        loaded, trial_request, store=store
    )
    command = _render_static_process_argv(loaded)
    executable_path = _verify_process_artifact(loaded, command)
    bridge_config = {
        "schema": "atv.process-bridge-config/v1",
        "manifest": loaded.as_dict(),
        "trial_request": dict(trial_request),
        "command": list(command),
        "env_allowlist": list(loaded.document["security"]["env_allowlist"]),
    }
    bridge_config_b64 = base64.b64encode(
        canonical_json_bytes(bridge_config)
    ).decode("ascii")
    adapter = CommandHarnessAdapter(
        (
            sys.executable,
            "-m",
            "atv_bench.harness_runtime.process_bridge",
            "{request_path}",
        ),
        pass_request_on_stdin=True,
        extra_env={
            _BRIDGE_CONFIG_ENV: bridge_config_b64,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
        env_allowlist=tuple(loaded.document["security"]["env_allowlist"]),
    )
    if not adapter.available():
        raise _error(
            "harness_command_unavailable",
            problem="The manifest command is not available on this host.",
            cause=repr(command),
            fix="Install the executable or use the OCI runtime.",
            evidence=str(loaded.source_path),
        )
    return ProcessAdapterPlan(
        manifest=loaded,
        adapter=adapter,
        compatibility=compatibility,
        executable_path=executable_path,
        harness_command_template=command,
        trial_request=_freeze(dict(trial_request)),
        canonical_contract=_canonical_plan_contract(loaded, compatibility),
        effective_environment_names=tuple(
            dict.fromkeys(
                (
                    *DEFAULT_ENV_ALLOWLIST,
                    *tuple(loaded.document["security"]["env_allowlist"]),
                )
            )
        ),
    )


def _render_oci_command(
    manifest: LoadedHarnessManifest,
    trial_request: Mapping[str, Any],
    *,
    model: str | None,
    bot_file: str | None,
) -> tuple[str, ...]:
    template = tuple(manifest.document["runtime"]["entrypoint"])
    if any("{manifest_dir}" in part for part in template):
        raise _error(
            "oci_host_placeholder",
            problem="The OCI command references a host-only placeholder.",
            cause="{manifest_dir} is not mounted into the container.",
            fix="Bake the executable into the image.",
            evidence=str(manifest.source_path),
        )
    if any("{request_path}" in part for part in template):
        raise _error(
            "oci_request_path_unsupported",
            problem="The current OCI runner does not mount a request file.",
            cause="{request_path} cannot be resolved inside the container.",
            fix="Read /prompt/task.md and the broker handle, or use stdin in process mode.",
            evidence=str(manifest.source_path),
        )
    allowed_models = list(trial_request["model_policy"]["allowed_models"])
    selected_model = model
    if selected_model is None:
        if len(allowed_models) != 1 and any("{model}" in part for part in template):
            raise _error(
                "ambiguous_model",
                problem="The OCI command requires a model but policy allows multiple models.",
                cause=repr(allowed_models),
                fix="Pass model= with one allowed model.",
                evidence=str(manifest.source_path),
            )
        selected_model = allowed_models[0]
    if selected_model not in allowed_models:
        raise _error(
            "model_not_allowed",
            problem="The selected OCI model is outside the trial model policy.",
            cause=selected_model,
            fix="Select one of: " + ", ".join(allowed_models),
            evidence=str(manifest.source_path),
        )
    output = trial_request["output"]
    selected_bot = bot_file or (
        output["required_paths"][0] if output["required_paths"] else "bot.py"
    )
    return render_argv_template(
        template,
        goal=trial_request["prompt"]["text"],
        repo="/workspace",
        bot_file=selected_bot,
        model=selected_model,
        request_path="/unsupported/request.json",
    )


@dataclasses.dataclass(frozen=True, slots=True)
class OciAdapterPlan:
    manifest: LoadedHarnessManifest
    request: Any
    compatibility: StaticCompatibility
    protocol_session: ProtocolSession
    canonical_contract: Mapping[str, Any]
    executable: bool = True
    official_eligible: bool | None = None
    execution_blocker: str | None = None

    @property
    def canonical_result_contract(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema": "atv.adapter-result-contract/v1",
                "protocol_version": self.compatibility.protocol_version,
                "trial_result_schema": CANONICAL_EXTERNAL_RESULT_SCHEMA,
            }
        )

    def require_executable(self) -> None:
        if not self.executable:
            raise _error(
                "oci_protocol_transport_unavailable",
                problem="Protocol-v1 OCI execution is not available through this runner.",
                cause=self.execution_blocker or "The OCI adapter was marked non-executable.",
                fix="Add an interactive canonical request/accept transport to OciTrialRunner.",
                evidence=str(self.manifest.source_path),
            )


def create_oci_adapter_plan(
    manifest: LoadedHarnessManifest | str | os.PathLike[str],
    trial_request: Mapping[str, Any],
    *,
    attempt: Any,
    task: Any,
    network: Any | None = None,
    gateway_handle: Any | None = None,
    credential_broker: Any | None = None,
    model: str | None = None,
    bot_file: str | None = None,
    store: SchemaStore | None = None,
) -> OciAdapterPlan:
    loaded = (
        manifest
        if isinstance(manifest, LoadedHarnessManifest)
        else load_harness_manifest(manifest, store=store)
    )
    if loaded.runtime_kind != "oci":
        raise _error(
            "wrong_runtime_kind",
            problem="An OCI adapter was requested for a non-OCI manifest.",
            cause=f"runtime.kind={loaded.runtime_kind!r}",
            fix="Use create_process_adapter_plan for process manifests.",
            evidence=str(loaded.source_path),
        )
    compatibility = preflight_manifest_compatibility(
        loaded, trial_request, store=store
    )
    try:
        from atv_bench.sandbox import (
            OciNetworkMode,
            OciNetworkPolicy,
            OciTrack,
            OciTrialRequest,
        )
    except ImportError as exc:
        raise _error(
            "oci_runtime_unavailable",
            problem="The OCI runner is not available.",
            cause=str(exc),
            fix="Install the benchmark runtime dependencies.",
            evidence="import atv_bench.sandbox",
        ) from exc

    try:
        track = OciTrack(trial_request["track"])
    except (KeyError, ValueError) as exc:
        raise _error(
            "oci_track_unsupported",
            problem="The OCI runner does not support this benchmark track.",
            cause=repr(trial_request.get("track")),
            fix="Use the controlled or systems track.",
            evidence=str(loaded.source_path),
        ) from exc
    network_mode = trial_request["policy"]["network"]["mode"]
    if network_mode == "allowlist":
        raise _error(
            "oci_network_unsupported",
            problem="The OCI runner does not support arbitrary network allowlists.",
            cause="Only none and model-gateway-only are accepted.",
            fix="Use a brokered model-gateway-only policy or network none.",
            evidence=str(loaded.source_path),
        )
    if network is None:
        if network_mode == "none":
            network = OciNetworkPolicy.none()
        else:
            raise _error(
                "oci_gateway_network_missing",
                problem="Model-gateway-only OCI execution requires a private network plan.",
                cause="No OciNetworkPolicy was supplied.",
                fix="Provide an internal pre-created network with exact gateway identities.",
                evidence=str(loaded.source_path),
            )
    if network_mode == "none" and network.mode is not OciNetworkMode.NONE:
        raise _error(
            "oci_network_conflict",
            problem="The OCI network plan contradicts the trial policy.",
            cause=f"trial=none plan={network.mode.value}",
            fix="Use OciNetworkPolicy.none().",
            evidence=str(loaded.source_path),
        )
    if (
        network_mode == "model-gateway-only"
        and network.mode is not OciNetworkMode.MODEL_GATEWAY_ONLY
    ):
        raise _error(
            "oci_network_conflict",
            problem="The OCI network plan contradicts the trial policy.",
            cause=f"trial=model-gateway-only plan={network.mode.value}",
            fix="Provide OciNetworkPolicy.model_gateway_only(...).",
            evidence=str(loaded.source_path),
        )

    command = _render_oci_command(
        loaded,
        trial_request,
        model=model,
        bot_file=bot_file,
    )
    protocol_session = ProtocolSession(loaded.as_dict(), trial_request, store=store)
    try:
        request = OciTrialRequest(
            attempt=attempt,
            task=task,
            harness_image=loaded.document["runtime"]["image"],
            harness_command=command,
            track=track,
            network=network,
            gateway_handle=gateway_handle,
            credential_broker=credential_broker,
            protocol_session=protocol_session,
        )
    except Exception as exc:
        raise _error(
            "oci_plan_invalid",
            problem="The OCI harness plan violates image, track, resource, or broker policy.",
            cause=str(exc),
            fix="Align the digest-pinned image with the selected track and network policy.",
            evidence=str(loaded.source_path),
        ) from exc
    return OciAdapterPlan(
        manifest=loaded,
        request=request,
        compatibility=compatibility,
        protocol_session=protocol_session,
        canonical_contract=_canonical_plan_contract(loaded, compatibility),
    )
