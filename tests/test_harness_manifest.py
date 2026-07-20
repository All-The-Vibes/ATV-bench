from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

import atv_bench.harness_manifest as harness_manifest_module
from atv_bench.adapters import CommandHarnessAdapter
from atv_bench.harness_manifest import (
    HarnessManifestError,
    HarnessManifestRegistry,
    LoadedHarnessManifest,
    StaticCompatibility,
    create_oci_adapter_plan,
    create_process_adapter_plan,
    load_harness_manifest,
    render_argv_template,
)
from atv_bench.protocol import canonical_sha256, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "harnesses" / "generic-command"
EXAMPLE_MANIFEST = EXAMPLE / "harness.json"
SMOKE_TASK = ROOT / "tasks" / "smoke" / "repair_config"


def _digest(character: str) -> dict[str, str]:
    return {"algorithm": "sha256", "value": character * 64}


def _capabilities(**overrides):
    value = {
        "workspace_edit": True,
        "subagents": False,
        "resumable": False,
        "browser": False,
        "model_events": False,
        "tool_events": False,
        "usage_events": False,
        "checkpoint_events": False,
        "model_selection": "single",
        "token_usage_reporting": "reported",
        "call_usage_reporting": "reported",
        "cost_usage_reporting": "reported",
    }
    value.update(overrides)
    return value


def _budget_limits():
    return {
        "wall_time_ms": 60_000,
        "cpu_time_ms": 60_000,
        "model_input_tokens": 10_000,
        "model_output_tokens": 10_000,
        "model_total_tokens": 20_000,
        "model_calls": 20,
        "cost_microusd": 500_000,
        "tool_calls": 200,
        "memory_bytes": 536_870_912,
        "storage_bytes": 1_073_741_824,
        "pids": 128,
        "stdout_bytes": 8_388_608,
        "stderr_bytes": 8_388_608,
        "artifact_bytes": 16_777_216,
    }


def _trial_request(
    loaded: LoadedHarnessManifest,
    *,
    trial_id: str = "trial-manifest-1",
    attempt_id: str = "attempt-manifest-1",
    track: str = "controlled",
    task=None,
) -> dict:
    prompt = (
        task.prompt_path.read_text(encoding="utf-8")
        if task is not None
        else "Implement the requested change."
    )
    task_manifest = task.manifest if task is not None else None
    output = (
        deepcopy(task_manifest["output"])
        if task_manifest is not None
        else {
            "mode": "named-artifacts",
            "allow_any_relative_path": False,
            "required_paths": ["main.py"],
            "allowed_paths": ["main.py", "helper.py", "untracked.txt", "observed-env.json"],
            "allowed_media_types": ["text/x-python", "text/plain", "application/json"],
            "max_files": 16,
            "max_total_bytes": 1_048_576,
        }
    )
    source_digest = (
        task_manifest["source"]["tree_digest"]
        if task_manifest is not None
        else _digest("1")
    )
    task_ref = (
        {
            "id": task.id,
            "version": task.version,
            "manifest_digest": {
                "algorithm": "sha256",
                "value": task.digest,
            },
        }
        if task is not None
        else {
            "id": "manifest-smoke-task",
            "version": "1.0.0",
            "manifest_digest": _digest("2"),
        }
    )
    document = loaded.as_dict()
    network_requirement = document["security"]["network_requirement"]
    network = (
        {
            "mode": "model-gateway-only",
            "allowed_destinations": ["model-gateway.internal:443"],
        }
        if network_requirement == "model-gateway-only"
        else {"mode": "none", "allowed_destinations": []}
    )
    credentials = [
        {
            "name": name,
            "handle": f"atv-credential://{attempt_id}/{name.lower()}",
        }
        for name in document["security"]["env_allowlist"]
    ]
    return {
        "schema": "atv.trial-request/v1",
        "protocol_version": 1,
        "benchmark_release": "ATV-2026.09",
        "track": track,
        "run_id": "run-manifest-1",
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "schedule_id": "schedule-manifest-1",
        "task_set": {
            "id": "manifest-smoke-suite",
            "version": "1.0.0",
            "manifest_digest": _digest("3"),
        },
        "issued_at": "2026-07-19T12:00:00Z",
        "expires_at": "2026-07-19T13:00:00Z",
        "nonce": "abcdefghijklmnopqrstuvwxyzABCDEF0123456789_-",
        "task": task_ref,
        "harness": {
            "id": loaded.id,
            "version": loaded.version,
            "manifest_digest": loaded.digest_descriptor,
        },
        "model_policy": {
            "id": "manifest-model-policy",
            "version": "1.0.0",
            "policy_digest": _digest("4"),
            "allowed_models": [
                "example/model-snapshot",
                "example/model-a",
                "example/model-b",
            ],
            "parameters_digest": _digest("5"),
            "retry_policy_digest": _digest("6"),
            "subagent_policy_digest": None,
            "gateway": "model-gateway.internal:443",
        },
        "workspace": {
            "path": "/workspace",
            "artifacts_path": "/artifacts",
            "clean": True,
            "base_tree_digest": source_digest,
        },
        "prompt": {
            "text": prompt,
            "encoding": "utf-8",
            "digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(prompt.encode("utf-8")),
            },
        },
        "budget_limits": _budget_limits(),
        "protocol_limits": {
            "max_line_bytes": 262_144,
            "max_total_bytes": 33_554_432,
            "max_events": 20_000,
            "max_depth": 32,
            "max_nodes": 100_000,
            "max_object_properties": 256,
        },
        "cancellation": {
            "soft_signal": "sigterm",
            "grace_period_ms": 5_000,
            "hard_kill": True,
            "destroy_execution_cell": True,
        },
        "policy": {
            "tools": {"allowed": ["shell", "editor"], "denied": ["browser"]},
            "network": network,
            "writable_paths": list(document["security"]["writable_paths"]),
            "credentials": credentials,
        },
        "seed": 42,
        "order_assignment": {
            "block": 0,
            "repetition": 0,
            "position": 0,
            "side": "none",
            "worker_class": "linux-amd64",
        },
        "output": output,
        "required_capabilities": deepcopy(document["capabilities"]),
        "forbidden_capabilities": ["browser"],
    }


def _copy_example(tmp_path: Path) -> Path:
    destination = tmp_path / "generic-command"
    shutil.copytree(EXAMPLE, destination)
    return destination / "harness.json"


def _write_document(tmp_path: Path, document: dict, *, suffix: str = ".json") -> Path:
    directory = tmp_path / ("manifest-" + hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:8])
    directory.mkdir(parents=True)
    shutil.copy2(EXAMPLE / "generic_harness.py", directory / "generic_harness.py")
    path = directory / ("harness" + suffix)
    if suffix == ".json":
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    else:
        yaml = pytest.importorskip("yaml")
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _oci_imports():
    pytest.importorskip("cryptography")
    from atv_bench.eval import (
        Budget,
        BudgetProfile,
        HarnessRef,
        ModelPolicyRef,
        TaskPackage,
        TrialAttempt,
        TrialSpec,
    )
    from atv_bench.sandbox import OciNetworkPolicy, OciTrialRequest

    return {
        "Budget": Budget,
        "BudgetProfile": BudgetProfile,
        "HarnessRef": HarnessRef,
        "ModelPolicyRef": ModelPolicyRef,
        "TaskPackage": TaskPackage,
        "TrialAttempt": TrialAttempt,
        "TrialSpec": TrialSpec,
        "OciNetworkPolicy": OciNetworkPolicy,
        "OciTrialRequest": OciTrialRequest,
    }


def _oci_manifest(tmp_path: Path, task, *, image: str | None = None) -> LoadedHarnessManifest:
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    document["id"] = "generic-oci-example"
    document["runtime"] = {
        "kind": "oci",
        "image": image or task.manifest["environment"]["image"],
        "entrypoint": ["python", "-c", "print('oci harness')"],
        "working_directory": "/workspace",
    }
    path = _write_document(tmp_path, document)
    return load_harness_manifest(path)


def _attempt(task, loaded: LoadedHarnessManifest):
    api = _oci_imports()
    spec = api["TrialSpec"](
        benchmark_release="ATV-2026.09",
        protocol_version="1",
        schedule_id="0" * 64,
        task=task.task_ref,
        harness=api["HarnessRef"](
            id=loaded.id,
            version=loaded.version,
            digest=loaded.digest,
        ),
        model_policy=api["ModelPolicyRef"](
            id="manifest-model-policy",
            version="1.0.0",
            digest="4" * 64,
        ),
        budget_profile=api["BudgetProfile"](
            id="manifest-budget",
            budget=api["Budget"](
                wall_time_seconds=60,
                max_model_tokens=20_000,
                max_model_calls=20,
                max_cost_microusd=500_000,
            ),
        ),
        repetition=0,
        schedule_seed=42,
    )
    return api["TrialAttempt"](spec=spec, attempt_number=1, fresh_nonce="7" * 64)


def test_example_manifest_loads_without_registry_code_change():
    loaded = load_harness_manifest(EXAMPLE_MANIFEST)
    assert loaded.id == "generic-command-example"
    assert loaded.runtime_kind == "process"
    assert len(loaded.digest) == 64
    assert loaded.digest == canonical_sha256(loaded.as_dict())

    mutable_copy = loaded.as_dict()
    mutable_copy["id"] = "tampered"
    assert loaded.id == "generic-command-example"
    with pytest.raises(TypeError):
        loaded.document["id"] = "tampered"


def test_json_unknown_fields_and_duplicate_keys_fail_actionably(tmp_path):
    manifest_path = _copy_example(tmp_path)
    document = json.loads(manifest_path.read_text())
    document["verified"] = True
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(HarnessManifestError) as captured:
        load_harness_manifest(manifest_path)
    message = str(captured.value)
    assert all(label in message for label in ("Problem:", "Cause:", "Fix:", "Evidence:"))
    assert "unknown fields" in message

    manifest_path.write_text('{"schema":"atv.harness/v1","schema":"duplicate"}')
    with pytest.raises(HarnessManifestError, match="duplicate"):
        load_harness_manifest(manifest_path)


def test_yaml_uses_safe_loader_and_missing_dependency_is_actionable(tmp_path, monkeypatch):
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    yaml_path = _write_document(tmp_path, document, suffix=".yaml")
    assert load_harness_manifest(yaml_path).digest == canonical_sha256(document)

    monkeypatch.setattr(
        harness_manifest_module,
        "_import_yaml",
        lambda: (_ for _ in ()).throw(
            HarnessManifestError(
                "yaml_dependency_missing",
                problem="YAML harness manifests require PyYAML.",
                cause="missing",
                fix="Install PyYAML or use JSON.",
                evidence="import yaml",
            )
        ),
    )
    with pytest.raises(HarnessManifestError, match="Install PyYAML"):
        load_harness_manifest(yaml_path)


@pytest.mark.parametrize(
    ("mutator", "needle"),
    [
        (lambda value: value["security"].update(requires_tty=True), "TTY-only"),
        (lambda value: value["security"].update(env_allowlist=["*"]), "environment"),
        (
            lambda value: value["security"].update(env_allowlist=["API_KEY=secret"]),
            "environment",
        ),
        (
            lambda value: value["security"].update(writable_paths=["/workspace", "/host"]),
            "writable",
        ),
        (
            lambda value: value["runtime"].update(command=["bash", "-c", "echo unsafe"]),
            "Shell-based",
        ),
        (
            lambda value: value["runtime"].update(
                command=["python", "{unknown}/harness.py"]
            ),
            "placeholder",
        ),
    ],
)
def test_security_and_argv_policy_fail_early(tmp_path, mutator, needle):
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    mutator(document)
    with pytest.raises(HarnessManifestError, match=needle):
        load_harness_manifest(_write_document(tmp_path, document))


def test_mutable_or_tag_qualified_oci_images_fail_early(tmp_path):
    api = _oci_imports()
    task = api["TaskPackage"].load(SMOKE_TASK)
    for image in ("python:latest", "python:tag@sha256:" + "1" * 64):
        with pytest.raises(HarnessManifestError, match="digest-pinned"):
            _oci_manifest(tmp_path / hashlib.sha256(image.encode()).hexdigest()[:8], task, image=image)


def test_registry_rejects_duplicate_digest_and_identity_conflict(tmp_path):
    registry = HarnessManifestRegistry()
    first = registry.load(EXAMPLE_MANIFEST)
    assert registry.by_digest(first.digest) is first
    assert registry.by_identity(first.id, first.version) is first
    with pytest.raises(HarnessManifestError, match="already registered"):
        registry.register(first)

    document = first.as_dict()
    document["display_name"] = "Conflicting content"
    conflict = load_harness_manifest(_write_document(tmp_path, document))
    with pytest.raises(HarnessManifestError, match="id/version"):
        registry.register(conflict)


def test_process_factory_returns_generic_command_adapter_after_static_compatibility():
    loaded = load_harness_manifest(EXAMPLE_MANIFEST)
    request = _trial_request(loaded)
    plan = create_process_adapter_plan(loaded, request)
    assert isinstance(plan.adapter, CommandHarnessAdapter)
    assert isinstance(plan.compatibility, StaticCompatibility)
    assert plan.compatibility.protocol_version == 1
    assert plan.executable_path == (EXAMPLE / "generic_harness.py").resolve()
    assert "{manifest_dir}" not in " ".join(plan.harness_command_template)
    assert plan.canonical_contract["result_schema"] == "atv.trial-result/v1"
    assert plan.trust_tier == "local-self-attested"
    assert plan.official_eligible is False
    assert set(plan.manifest.document["security"]["env_allowlist"]).issubset(
        plan.effective_environment_names
    )
    assert tuple(plan.adapter.env_allowlist) == tuple(
        plan.manifest.document["security"]["env_allowlist"]
    )
    assert "hello_event" not in inspect.signature(
        create_process_adapter_plan
    ).parameters


def test_process_factory_detects_artifact_drift_and_capability_mismatch(tmp_path):
    manifest_path = _copy_example(tmp_path)
    loaded = load_harness_manifest(manifest_path)
    (manifest_path.parent / "generic_harness.py").write_text("print('tampered')\n")
    with pytest.raises(HarnessManifestError, match="does not match"):
        create_process_adapter_plan(loaded, _trial_request(loaded))

    loaded = load_harness_manifest(EXAMPLE_MANIFEST)
    request = _trial_request(loaded)
    request["required_capabilities"]["resumable"] = True
    with pytest.raises(HarnessManifestError, match="compatibility failed"):
        create_process_adapter_plan(loaded, request)


def test_safe_placeholder_rendering_never_invokes_a_shell():
    rendered = render_argv_template(
        ("python", "tool.py", "--goal", "{goal}", "--repo={repo}"),
        goal="value; echo should-not-run && touch pwned",
        repo=r"C:\workspace with spaces",
        bot_file="main.py",
        model="example/model",
        request_path=r"C:\Temp\request.json",
    )
    assert rendered[3] == "value; echo should-not-run && touch pwned"
    assert rendered[4] == r"--repo=C:\workspace with spaces"
    assert len(rendered) == 5


def test_manifest_relative_traversal_and_final_symlink_are_rejected(tmp_path):
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    document["runtime"]["command"] = [
        "python",
        "{manifest_dir}/../outside.py",
    ]
    with pytest.raises(HarnessManifestError, match="escapes"):
        load_harness_manifest(_write_document(tmp_path, document))

    target = _copy_example(tmp_path / "symlink")
    link = tmp_path / "linked-harness.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(HarnessManifestError, match="regular file"):
        load_harness_manifest(link)


def test_request_cannot_expand_manifest_environment_allowlist(tmp_path):
    loaded = load_harness_manifest(EXAMPLE_MANIFEST)
    plan = create_process_adapter_plan(loaded, _trial_request(loaded))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "main.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=ATV",
            "-c",
            "user.email=atv@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )
    from atv_bench.adapters import AdapterRequest

    with pytest.raises(HarnessManifestError, match="expands"):
        plan.run(
            AdapterRequest(
                repo_path=str(repo),
                goal="mode=no_edit",
                env_allowlist=("UNDECLARED_SECRET",),
            )
        )


def test_manifest_factory_has_no_vendor_specific_conditionals():
    source = (ROOT / "src" / "atv_bench" / "harness_manifest.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "claude" not in source
    assert "copilot" not in source


def test_oci_factory_builds_valid_request_and_enforces_track_policy(tmp_path):
    api = _oci_imports()
    task = api["TaskPackage"].load(SMOKE_TASK)
    loaded = _oci_manifest(tmp_path, task)
    attempt = _attempt(task, loaded)
    request = _trial_request(
        loaded,
        trial_id=attempt.spec.trial_id,
        attempt_id=attempt.attempt_id,
        task=task,
    )
    plan = create_oci_adapter_plan(
        loaded,
        request,
        attempt=attempt,
        task=task,
        network=api["OciNetworkPolicy"].none(),
    )
    assert isinstance(plan.request, api["OciTrialRequest"])
    assert plan.request.protocol_session is plan.protocol_session
    assert plan.request.harness_image.digest == plan.request.task_image.digest
    assert plan.canonical_contract["result_schema"] == "atv.trial-result/v1"
    assert plan.executable is True
    assert plan.official_eligible is None
    plan.require_executable()

    systems_loaded = _oci_manifest(
        tmp_path / "systems",
        task,
        image="ghcr.io/example/harness@sha256:" + "a" * 64,
    )
    systems_attempt = _attempt(task, systems_loaded)
    systems_request = _trial_request(
        systems_loaded,
        trial_id=systems_attempt.spec.trial_id,
        attempt_id=systems_attempt.attempt_id,
        track="systems",
        task=task,
    )
    systems_plan = create_oci_adapter_plan(
        systems_loaded,
        systems_request,
        attempt=systems_attempt,
        task=task,
        network=api["OciNetworkPolicy"].none(),
    )
    assert systems_plan.request.image_role_policy()["systems_confound"]


def test_oci_factory_rejects_unsupported_track_and_network(tmp_path):
    api = _oci_imports()
    task = api["TaskPackage"].load(SMOKE_TASK)
    loaded = _oci_manifest(tmp_path, task)
    attempt = _attempt(task, loaded)
    request = _trial_request(
        loaded,
        trial_id=attempt.spec.trial_id,
        attempt_id=attempt.attempt_id,
        track="resilience",
        task=task,
    )
    with pytest.raises(HarnessManifestError, match="controlled or systems"):
        create_oci_adapter_plan(
            loaded,
            request,
            attempt=attempt,
            task=task,
            network=api["OciNetworkPolicy"].none(),
        )

    gateway_document = loaded.as_dict()
    gateway_document["id"] = "generic-oci-gateway"
    gateway_document["security"]["network_requirement"] = "model-gateway-only"
    gateway_loaded = load_harness_manifest(_write_document(tmp_path / "gateway", gateway_document))
    gateway_attempt = _attempt(task, gateway_loaded)
    gateway_request = _trial_request(
        gateway_loaded,
        trial_id=gateway_attempt.spec.trial_id,
        attempt_id=gateway_attempt.attempt_id,
        task=task,
    )
    with pytest.raises(HarnessManifestError, match="private network plan"):
        create_oci_adapter_plan(
            gateway_loaded,
            gateway_request,
            attempt=gateway_attempt,
            task=task,
        )
