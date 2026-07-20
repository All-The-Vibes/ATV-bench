from __future__ import annotations

import base64
import json
import secrets
import subprocess
import threading
import time
import zlib
from copy import deepcopy
from pathlib import Path

import pytest

from atv_bench.adapters import (
    AdapterRequest,
    AdapterStatus,
    Budget as AdapterBudget,
)
from atv_bench.harness_manifest import (
    create_oci_adapter_plan,
    create_process_adapter_plan,
    load_harness_manifest,
)
from atv_bench.protocol import sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "harnesses" / "generic-command"
EXAMPLE_MANIFEST = EXAMPLE / "harness.json"
SMOKE_TASK = ROOT / "tasks" / "smoke" / "repair_config"


def _digest(character: str) -> dict[str, str]:
    return {"algorithm": "sha256", "value": character * 64}


def _budget_limits():
    return {
        "wall_time_ms": 60_000,
        "cpu_time_ms": 60_000,
        "model_input_tokens": 100_000,
        "model_output_tokens": 100_000,
        "model_total_tokens": 200_000,
        "model_calls": 100,
        "cost_microusd": 5_000_000,
        "tool_calls": 1_000,
        "memory_bytes": 536_870_912,
        "storage_bytes": 1_073_741_824,
        "pids": 128,
        "stdout_bytes": 8_388_608,
        "stderr_bytes": 8_388_608,
        "artifact_bytes": 16_777_216,
    }


def _trial_request(loaded, mode: str, *, trial_id="trial-conformance", attempt_id="attempt-conformance"):
    prompt = f"mode={mode}"
    document = loaded.as_dict()
    return {
        "schema": "atv.trial-request/v1",
        "protocol_version": 1,
        "benchmark_release": "ATV-2026.09",
        "track": "controlled",
        "run_id": "run-conformance",
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "schedule_id": "schedule-conformance",
        "task_set": {
            "id": "adapter-conformance",
            "version": "1.0.0",
            "manifest_digest": _digest("1"),
        },
        "issued_at": "2026-07-19T12:00:00Z",
        "expires_at": "2026-07-19T13:00:00Z",
        "nonce": "abcdefghijklmnopqrstuvwxyzABCDEF0123456789_-",
        "task": {
            "id": "adapter-conformance-task",
            "version": "1.0.0",
            "manifest_digest": _digest("2"),
        },
        "harness": {
            "id": loaded.id,
            "version": loaded.version,
            "manifest_digest": loaded.digest_descriptor,
        },
        "model_policy": {
            "id": "adapter-conformance-models",
            "version": "1.0.0",
            "policy_digest": _digest("3"),
            "allowed_models": [
                "example/model-snapshot",
                "example/model-a",
                "example/model-b",
            ],
            "parameters_digest": _digest("4"),
            "retry_policy_digest": _digest("5"),
            "subagent_policy_digest": None,
            "gateway": "model-gateway.internal:443",
        },
        "workspace": {
            "path": "/workspace",
            "artifacts_path": "/artifacts",
            "clean": True,
            "base_tree_digest": _digest("6"),
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
            "network": {"mode": "none", "allowed_destinations": []},
            "writable_paths": list(document["security"]["writable_paths"]),
            "credentials": [],
        },
        "seed": 42,
        "order_assignment": {
            "block": 0,
            "repetition": 0,
            "position": 0,
            "side": "none",
            "worker_class": "windows-amd64",
        },
        "output": {
            "mode": "workspace-tree",
            "allow_any_relative_path": True,
            "required_paths": [],
            "allowed_paths": [],
            "allowed_media_types": [
                "text/x-python",
                "text/plain",
                "application/json",
            ],
            "max_files": 16,
            "max_total_bytes": 1_048_576,
        },
        "required_capabilities": deepcopy(document["capabilities"]),
        "forbidden_capabilities": ["browser"],
    }


def _seed_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "main.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=ATV Tests",
            "-c",
            "user.email=atv-tests@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )


def _run_mode(
    tmp_path: Path,
    mode: str,
    *,
    max_seconds: int = 30,
    cancel_event: threading.Event | None = None,
):
    loaded = load_harness_manifest(EXAMPLE_MANIFEST)
    plan = create_process_adapter_plan(loaded, _trial_request(loaded, mode))
    repo = tmp_path / ("repo-" + mode)
    _seed_repo(repo)
    result = plan.run(
        AdapterRequest(
            repo_path=str(repo),
            goal=f"mode={mode}",
            model="example/model-snapshot",
            budget=AdapterBudget(
                max_turns=10,
                max_seconds=max_seconds,
                max_tokens=200_000,
            ),
            bot_file="main.py",
        ),
        cancel_event=cancel_event,
    )
    return plan, repo, result


def _oci_plan(tmp_path: Path):
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
    from atv_bench.sandbox import OciNetworkPolicy

    task = TaskPackage.load(SMOKE_TASK)
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    document["id"] = "adapter-conformance-oci"
    document["runtime"] = {
        "kind": "oci",
        "image": task.manifest["environment"]["image"],
        "entrypoint": ["python", "-c", "print('oci')"],
        "working_directory": "/workspace",
    }
    manifest_dir = tmp_path / "oci-manifest"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "harness.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_harness_manifest(manifest_path)
    spec = TrialSpec(
        benchmark_release="ATV-2026.09",
        protocol_version="1",
        schedule_id="0" * 64,
        task=task.task_ref,
        harness=HarnessRef(loaded.id, loaded.version, loaded.digest),
        model_policy=ModelPolicyRef(
            "adapter-conformance-models", "1.0.0", "3" * 64
        ),
        budget_profile=BudgetProfile(
            "adapter-conformance",
            Budget(60, 200_000, 100, 5_000_000),
        ),
        repetition=0,
        schedule_seed=42,
    )
    attempt = TrialAttempt(spec, 1, "7" * 64)
    request = _trial_request(
        loaded,
        "no_edit",
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
    )
    request["task"] = {
        "id": task.id,
        "version": task.version,
        "manifest_digest": {"algorithm": "sha256", "value": task.digest},
    }
    request["workspace"]["base_tree_digest"] = task.manifest["source"]["tree_digest"]
    request["output"] = deepcopy(task.manifest["output"])
    return create_oci_adapter_plan(
        loaded,
        request,
        attempt=attempt,
        task=task,
        network=OciNetworkPolicy.none(),
    )


def test_actual_hello_accept_handshake_produces_authority_verified_transcript(tmp_path):
    plan, _, result = _run_mode(tmp_path, "no_edit")
    assert plan.compatibility.protocol_version == 1
    assert result.transcript is not None
    assert result.transcript.authority_verified is True
    assert [event["type"] for event in result.transcript.events[:2]] == [
        "hello",
        "accepted",
    ]
    assert result.conformance_error is None
    assert result.adapter_result.status is AdapterStatus.NO_EDIT


@pytest.mark.parametrize(
    ("mode", "expected_files"),
    [
        ("single_file", {"main.py"}),
        ("multi_file", {"main.py", "helper.py"}),
        ("commit", {"main.py"}),
        ("staged", {"main.py"}),
        ("untracked", {"untracked.txt"}),
    ],
)
def test_edit_modes_capture_single_multi_commit_and_untracked(
    tmp_path, mode, expected_files
):
    _, _, result = _run_mode(tmp_path, mode)
    assert result.adapter_result.status is AdapterStatus.OK
    assert result.transcript is not None
    for name in expected_files:
        assert name in result.adapter_result.diff


@pytest.mark.parametrize(
    "mode",
    ["binary", "tracked_binary", "oversized", "aggregate_oversized"],
)
def test_binary_and_oversized_outputs_are_rejected(tmp_path, mode):
    _, _, result = _run_mode(tmp_path, mode)
    assert result.adapter_result.status is AdapterStatus.ERROR
    assert result.transcript is not None
    assert result.transcript.result["status"] == "invalid_artifact"
    if mode in {"binary", "aggregate_oversized"}:
        assert "capture rejected" in result.adapter_result.log


def test_nonzero_timeout_cancel_and_child_cleanup(tmp_path):
    _, _, nonzero = _run_mode(tmp_path, "nonzero")
    assert nonzero.adapter_result.status is AdapterStatus.ERROR
    assert nonzero.transcript is not None
    assert nonzero.transcript.result["status"] == "harness_crash"

    _, _, timeout = _run_mode(tmp_path, "timeout", max_seconds=1)
    assert timeout.adapter_result.status is AdapterStatus.TIMEOUT
    assert timeout.transcript is None

    cancelled = threading.Event()
    cancelled.set()
    _, _, cancel = _run_mode(
        tmp_path / "cancel",
        "timeout",
        max_seconds=10,
        cancel_event=cancelled,
    )
    assert cancel.adapter_result.status is AdapterStatus.CANCELLED
    assert cancel.transcript is None

    _, repo, child = _run_mode(tmp_path, "child_leak")
    assert child.adapter_result.status is AdapterStatus.NO_EDIT
    assert child.transcript is not None
    time.sleep(2)
    assert not (repo / "child-survived.txt").exists()


def test_huge_stream_is_bounded_and_protocol_rejected(tmp_path):
    _, _, result = _run_mode(tmp_path, "huge_stream")
    assert result.adapter_result.status is AdapterStatus.ERROR
    assert result.transcript is None
    assert result.adapter_result.runtime.stdout_truncated is False
    assert len(result.adapter_result.log.encode("utf-8")) <= 128 * 1024

    _, _, stderr = _run_mode(tmp_path / "stderr", "huge_stderr")
    assert stderr.adapter_result.status is AdapterStatus.NO_EDIT
    assert stderr.transcript is not None
    assert len(stderr.adapter_result.log.encode("utf-8")) <= 128 * 1024


def test_unknown_multiple_models_missing_usage_and_retries(tmp_path):
    _, _, unknown = _run_mode(tmp_path, "unknown_model")
    assert unknown.adapter_result.model == "unknown"
    assert unknown.transcript is not None
    assert not any(event["type"] == "model_call" for event in unknown.transcript.events)

    _, _, multiple = _run_mode(tmp_path, "multiple_models")
    assert multiple.adapter_result.model == "unknown"
    assert multiple.transcript is not None
    models = [
        event["resolved_model"]
        for event in multiple.transcript.events
        if event["type"] == "model_call"
    ]
    assert models == ["example/model-a", "example/model-b"]

    _, _, missing = _run_mode(tmp_path, "missing_usage")
    assert missing.transcript is not None
    assert missing.transcript.result["reported_usage"]["model_total_tokens"] is None
    assert missing.adapter_result.usage.tokens == 0

    _, _, retries = _run_mode(tmp_path, "retries")
    assert retries.transcript is not None
    retry_indexes = [
        event["retry_index"]
        for event in retries.transcript.events
        if event["type"] == "model_call"
    ]
    assert retry_indexes == [2]
    assert '"retries": 2' in retries.adapter_result.log

    _, _, disallowed = _run_mode(tmp_path, "disallowed_model")
    assert disallowed.transcript is None
    assert "disallowed model" in str(disallowed.conformance_error)


def test_secret_isolation_and_network_tier_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("ATV_SECRET_CANARY", "must-not-leak")
    plan, repo, result = _run_mode(tmp_path, "secret_probe")
    assert result.adapter_result.status is AdapterStatus.OK
    observed = json.loads((repo / "observed-env.json").read_text(encoding="utf-8"))
    assert "ATV_SECRET_CANARY" not in observed
    assert "must-not-leak" not in json.dumps(observed)
    assert plan.trust_tier == "local-self-attested"
    assert plan.official_eligible is False
    assert plan.network_enforced is False


def test_windows_paths_newlines_and_actual_child_argv_are_preserved(tmp_path):
    _, repo, result = _run_mode(tmp_path, "windows_newlines")
    assert result.adapter_result.status is AdapterStatus.OK
    assert (repo / "main.py").read_bytes() == b"VALUE = 4\r\n"

    manifest_dir = tmp_path / "argv manifest"
    manifest_dir.mkdir()
    shutil_script = EXAMPLE / "generic_harness.py"
    target_script = manifest_dir / "generic_harness.py"
    target_script.write_bytes(shutil_script.read_bytes())
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    document["id"] = "argv-probe-example"
    document["runtime"]["command"] = [
        "python",
        "{manifest_dir}/generic_harness.py",
        "--goal",
        "{goal}",
        "--repo",
        "{repo}",
        "--request",
        "{request_path}",
    ]
    manifest_path = manifest_dir / "harness.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_harness_manifest(manifest_path)
    plan = create_process_adapter_plan(loaded, _trial_request(loaded, "argv_probe"))
    argv_repo = tmp_path / "workspace with spaces"
    _seed_repo(argv_repo)
    probe = plan.run(
        AdapterRequest(
            repo_path=str(argv_repo),
            goal="a; echo not-a-shell && touch pwned",
            model="example/model-snapshot",
            bot_file="main.py",
        ),
    )
    assert probe.adapter_result.status is AdapterStatus.OK
    argv = json.loads((argv_repo / "argv.json").read_text(encoding="utf-8"))
    assert argv[0:2] == ["--goal", "a; echo not-a-shell && touch pwned"]
    assert argv[2:4] == ["--repo", str(argv_repo.resolve())]
    assert argv[4] == "--request"
    assert len(argv) == 6
    assert not (argv_repo / "pwned").exists()


def test_process_and_oci_share_canonical_contract_parity_where_supported(tmp_path):
    loaded = load_harness_manifest(EXAMPLE_MANIFEST)
    process_plan = create_process_adapter_plan(
        loaded,
        _trial_request(loaded, "no_edit"),
    )
    oci_plan = _oci_plan(tmp_path)
    assert dict(process_plan.canonical_result_contract) == dict(
        oci_plan.canonical_result_contract
    )
    assert oci_plan.executable is True
    assert oci_plan.official_eligible is None
    oci_plan.require_executable()


def _cached_oci_engine_or_skip():
    from atv_bench.sandbox import CliOciEngine, DigestPinnedImage
    from atv_bench.sandbox.oci import EngineUnavailableError

    try:
        engine = CliOciEngine.auto()
    except EngineUnavailableError as exc:
        pytest.skip(f"shared adapter conformance unavailable: {exc}")
    reachable, detail = engine.daemon_status()
    if not reachable:
        pytest.skip(f"shared adapter conformance unavailable: {detail}")
    image = DigestPinnedImage.parse(
        "docker.io/library/python@sha256:"
        "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    try:
        inspection = engine.inspect_image(image)
    except Exception as exc:
        pytest.skip(f"shared adapter conformance image is unavailable: {exc}")
    if not inspection.verified:
        pytest.skip("shared adapter conformance image digest did not verify")
    return engine, image


def _run_real_oci_mode(tmp_path: Path, mode: str):
    from atv_bench.protocol import ProtocolSession, validate_conformance
    from atv_bench.sandbox import (
        ContainerPhase,
        ContainerSpec,
        MountSpec,
        OciNetworkPolicy,
        OciResourcePolicy,
    )
    from atv_bench.sandbox.interactive import (
        CliInteractiveOciBackend,
        InteractiveOciTransport,
        InteractiveTransportLimits,
        InteractiveTransportStatus,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    engine, image = _cached_oci_engine_or_skip()
    document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    document["id"] = f"adapter-conformance-oci-{mode.replace('_', '-')}"
    harness_source = (EXAMPLE / "generic_harness.py").read_bytes()
    encoded_source = base64.b64encode(zlib.compress(harness_source, level=9)).decode(
        "ascii"
    )
    loader = (
        "import base64,zlib;"
        f"exec(zlib.decompress(base64.b64decode({encoded_source!r})))"
    )
    document["runtime"] = {
        "kind": "oci",
        "image": image.reference,
        "entrypoint": ["python", "-c", loader],
        "working_directory": "/workspace",
    }
    manifest_dir = tmp_path / f"oci-{mode}-manifest"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "harness.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_harness_manifest(manifest_path)
    request = _trial_request(loaded, mode)
    workspace = tmp_path / f"oci-{mode}-workspace"
    artifacts = tmp_path / f"oci-{mode}-artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    try:
        workspace.chmod(0o777)
        artifacts.chmod(0o777)
        (workspace / "main.py").chmod(0o666)
    except OSError:
        pass
    spec = ContainerSpec(
        phase=ContainerPhase.HARNESS,
        name=f"atv-shared-conformance-{secrets.token_hex(5)}",
        image=image,
        command=tuple(document["runtime"]["entrypoint"]),
        mounts=(
            MountSpec(workspace, "/workspace", False),
            MountSpec(artifacts, "/artifacts", False),
        ),
        resources=OciResourcePolicy(
            wall_time_ms=15_000,
            memory_bytes=256 * 1024 * 1024,
            cpu_millis=1_000,
            pids_limit=64,
            storage_bytes=8 * 1024 * 1024,
            stdout_bytes=2 * 1024 * 1024,
            stderr_bytes=256 * 1024,
            artifact_bytes=2 * 1024 * 1024,
            tmpfs_bytes=2 * 1024 * 1024,
        ),
        network=OciNetworkPolicy.none(),
        working_directory="/workspace",
    )
    session = ProtocolSession(loaded.as_dict(), request)
    result = InteractiveOciTransport(
        CliInteractiveOciBackend(engine.executable),
        limits=InteractiveTransportLimits(
            result_eof_timeout_ms=2_000,
            exited_pipe_timeout_ms=2_000,
            hard_kill_wait_ms=2_000,
        ),
    ).run(spec, session)
    assert result.status is InteractiveTransportStatus.COMPLETED, result.error
    assert result.transcript is not None
    validate_conformance(result.transcript, loaded.as_dict(), request)
    assert result.evidence.cleanup.confirmed_absent is True
    assert engine.container_exists(spec.name) is False
    return workspace, result.transcript


def _behavior_summary(workspace: Path, transcript) -> dict:
    result = transcript.result
    files = {}
    for name in ("main.py", "helper.py"):
        candidate = workspace / name
        if candidate.is_file():
            files[name] = candidate.read_text(encoding="utf-8")
    return {
        "authority_verified": transcript.authority_verified,
        "event_types": [event["type"] for event in transcript.events],
        "status": result["status"],
        "artifacts": result["artifacts"],
        "reported_usage": result["reported_usage"],
        "output_tree_digest": result["output_tree_digest"],
        "files": files,
    }


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["no_edit", "single_file", "multi_file"])
def test_process_and_oci_pass_the_same_behavioral_conformance_cases(tmp_path, mode):
    _, process_workspace, process_result = _run_mode(
        tmp_path / "process",
        mode,
    )
    oci_workspace, oci_transcript = _run_real_oci_mode(tmp_path / "oci", mode)

    assert process_result.transcript is not None
    assert _behavior_summary(
        process_workspace,
        process_result.transcript,
    ) == _behavior_summary(oci_workspace, oci_transcript)
