"""Local-only benchmark CLI contract and Docker smoke tests."""
from __future__ import annotations

import base64
import json
import shutil
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import atv_bench.benchmark_cli as cli
import atv_bench.control_plane.trial_controller as controller_module
from atv_bench.benchmark_cli import CliProblem, ExitCode, benchmark_app
from atv_bench.cli import app as main_app
from atv_bench.eval import HarnessStatus, InfrastructureStatus
from atv_bench.protocol import canonical_digest, canonical_json_bytes, sha256_bytes
from atv_bench.sandbox import (
    CliOciEngine,
    DigestPinnedImage,
    EngineUnavailableError,
    OciRunnerLifecycleReceipt,
    OciTrialStatus,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
TASK = ROOT / "tasks" / "smoke" / "repair_config"
PROCESS_HARNESS = (
    ROOT / "examples" / "harnesses" / "generic-command" / "harness.json"
)
IMAGE = (
    "docker.io/library/python@sha256:"
    "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
)
runner = CliRunner()


class PortableFakeOciRunner:
    def run(self, request):
        oracle = next(
            path
            for gate, _, path, expected in request.task.validation_cases()
            if gate.value == "oracle" and expected
        )
        snapshot = controller_module._encode_output_snapshot(oracle)
        evidence_bytes = canonical_json_bytes(
            {
                "schema": "portable-cli-fake-evidence/v1",
                "attempt_id": request.attempt.attempt_id,
            }
        )
        evidence_digest = sha256_bytes(evidence_bytes)
        run = SimpleNamespace(
            exit_code=0,
            timed_out=False,
            cancelled=False,
            duration_ms=1,
            stdout_total_bytes=0,
            stderr_total_bytes=0,
        )
        evidence = SimpleNamespace(
            digest=evidence_digest,
            canonical_bytes=evidence_bytes,
            harness=SimpleNamespace(
                run=run,
                storage=SimpleNamespace(peak_bytes=64),
                policy={
                    "resources": {
                        "memory_bytes": 128 * 1024 * 1024,
                        "pids_limit": 16,
                    }
                },
            ),
            started_at="2026-07-20T12:00:00Z",
            duration_ms=1,
            workspace={"bytes": 64},
        )
        return SimpleNamespace(
            status=OciTrialStatus.COMPLETED,
            evidence=evidence,
            lifecycle_receipt=OciRunnerLifecycleReceipt(
                evidence_digest=evidence_digest,
                _execution_complete=True,
                credential_finalized=True,
                hidden_inputs_mounted_after_harness_exit=True,
                runtime_verified=True,
            ),
            grader_stdout=snapshot,
            grader_stderr=b"",
            harness_stdout=b"",
            harness_stderr=b"",
            protocol_transcript=None,
        )


def _protocol_command(payload: str) -> str:
    capabilities = {
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
    script = textwrap.dedent(
        f"""
        import datetime
        import hashlib
        import json
        import sys

        def now():
            return datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z")

        def emit(value):
            sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\\n")
            sys.stdout.flush()

        request = json.loads(sys.stdin.readline())
        emit({{
            "schema": "atv.harness-event/v1",
            "type": "hello",
            "protocol_version": 1,
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "harness_sequence": 0,
            "emitted_at": now(),
            "supported_protocol_versions": [1],
            "capabilities": {capabilities!r},
            "harness": request["harness"],
        }})
        accepted = json.loads(sys.stdin.readline())
        if accepted.get("type") != "accepted":
            raise RuntimeError("controller did not accept the protocol session")
        exec(compile({payload!r}, "<cli-test-payload>", "exec"))
        usage = {{
            "wall_time_ms": 1,
            "cpu_time_ms": 1,
            "model_input_tokens": 0,
            "model_output_tokens": 0,
            "model_total_tokens": 0,
            "model_calls": 0,
            "cost_microusd": 0,
            "tool_calls": 0,
            "memory_bytes": 1,
            "storage_bytes": 1,
            "pids": 1,
            "stdout_bytes": 1,
            "stderr_bytes": 0,
            "artifact_bytes": 1,
        }}
        emit({{
            "schema": "atv.harness-event/v1",
            "type": "result",
            "protocol_version": 1,
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "harness_sequence": 1,
            "emitted_at": now(),
            "harness_result": {{
                "schema": "atv.harness-result/v1",
                "status": "completed",
                "exit": {{
                    "code": 0,
                    "signal": None,
                    "timed_out": False,
                    "cancelled": False,
                }},
                "output_tree_digest": {{
                    "algorithm": "sha256",
                    "value": hashlib.sha256(b"cli-test-output").hexdigest(),
                }},
                "artifacts": [],
                "reported_usage": usage,
                "failure": None,
            }},
        }})
        """
    )
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        "import base64;"
        f"exec(compile(base64.b64decode({encoded!r}),"
        "'<atv-cli-protocol>','exec'))"
    )


def _manifest(
    tmp_path: Path,
    *,
    harness_id: str = "cli-harness",
    command: str = "print('ok')",
    protocol_v1: bool = False,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "atv.harness/v1",
        "id": harness_id,
        "version": "1.0.0",
        "display_name": harness_id,
        "runtime": {
            "kind": "oci",
            "image": IMAGE,
            "entrypoint": [
                "python",
                "-c",
                _protocol_command(command) if protocol_v1 else command,
            ],
            "working_directory": "/workspace",
        },
        "protocol": {
            "minimum_version": 1,
            "maximum_version": 1,
            "input": "stdin-json",
            "output": "stdout-jsonl",
        },
        "capabilities": {
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
        },
        "security": {
            "env_allowlist": [],
            "network_requirement": "none",
            "writable_paths": ["/workspace", "/artifacts"],
            "requires_tty": False,
        },
        "metadata": {
            "source": {
                "repository": "https://example.invalid/local-cli",
                "revision": "local-cli-v1",
                "tree_digest": {
                    "algorithm": "sha256",
                    "value": "a" * 64,
                },
            },
            "license": "MIT",
        },
    }
    path = tmp_path / f"{harness_id}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _json(result):
    return json.loads(result.stdout)


def _portable_trial_export(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_task = tmp_path / "source-task"
    shutil.copytree(TASK, source_task)
    harness_path = _manifest(tmp_path / "source-harness")
    plan_path = tmp_path / "plan.json"
    cli._write_canonical(
        plan_path,
        cli._plan_document(
            tasks=(source_task,),
            harnesses=(harness_path,),
            repetitions=1,
            seed=0,
            worker="linux-amd64",
            benchmark_release="ATV-2026.07",
        ),
    )
    plan, packages, harnesses, schedule = cli._load_plan(plan_path)
    scheduled = schedule[0]
    policy = cli._model_free_policy()
    controller = cli.TrialController(
        oci_runner=PortableFakeOciRunner(),
        ledger=cli.ControllerLedger(tmp_path / "controller-ledger.jsonl"),
        store=cli.ContentAddressedStore(tmp_path / "cas"),
    )
    result = controller.run(
        cli.ControllerRunRequest(
            scheduled=scheduled,
            task=packages[0],
            harness=harnesses[0],
            model_policy=policy,
            task_set=cli.ControllerTaskSet(
                "portable-test",
                "1.0.0",
                plan["plan_digest"],
            ),
            run_id="portable-cli-test",
            network=cli.OciNetworkPolicy.none(),
        )
    )
    assert result.problem is None
    trial = tmp_path / "trial"
    cli._write_export(
        trial,
        result,
        task_path=source_task,
        harness_path=harness_path,
    )
    return trial, source_task, harness_path


def test_subapps_and_help_are_exposed_from_main_cli():
    result = runner.invoke(benchmark_app, ["--help"])
    assert result.exit_code == 0
    for name in ("schema", "harness", "task", "trial", "eval"):
        assert name in result.stdout
    for command in ("plan", "run", "verify", "analyze", "reproduce"):
        result = runner.invoke(benchmark_app, ["eval", command, "--help"])
        assert result.exit_code == 0
    main = runner.invoke(main_app, ["benchmark", "--help"])
    assert main.exit_code == 0
    assert "Local-only harness benchmark tooling" in main.stdout


def test_schema_harness_and_task_validation_happy_paths(tmp_path):
    manifest = _manifest(tmp_path)
    schema = runner.invoke(benchmark_app, ["schema", "check", str(SCHEMAS), "--json"])
    harness = runner.invoke(
        benchmark_app, ["harness", "validate", str(manifest), "--json"]
    )
    task = runner.invoke(benchmark_app, ["task", "validate", str(TASK), "--json"])
    assert schema.exit_code == harness.exit_code == task.exit_code == 0
    assert _json(schema)["data"]["schema_count"] == 6
    assert _json(harness)["data"]["digest"]
    assert _json(task)["data"]["eligible"] is True
    assert _json(harness)["rankable"] is False


def test_validation_errors_have_stable_codes_and_actionable_human_output(tmp_path):
    missing_schemas = tmp_path / "schemas"
    missing_schemas.mkdir()
    bad_task = tmp_path / "task"
    bad_task.mkdir()
    bad_manifest = tmp_path / "bad.json"
    bad_manifest.write_text('{"schema":"atv.harness/v1"}', encoding="utf-8")

    schema = runner.invoke(benchmark_app, ["schema", "check", str(missing_schemas)])
    harness = runner.invoke(
        benchmark_app, ["harness", "validate", str(bad_manifest)]
    )
    task = runner.invoke(benchmark_app, ["task", "validate", str(bad_task)])
    assert schema.exit_code == harness.exit_code == task.exit_code == ExitCode.VALIDATION
    for result in (schema, harness, task):
        assert all(
            label in result.output
            for label in ("Problem:", "Cause:", "Fix:", "Evidence:")
        )
        result.output.encode("cp1252", errors="strict")


def test_plan_is_deterministic_and_uses_paired_scheduler(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    harness_a = _manifest(tmp_path, harness_id="harness-a")
    harness_b = _manifest(tmp_path, harness_id="harness-b")
    args = [
        "eval",
        "plan",
        "--task",
        str(TASK),
        "--harness",
        str(harness_a),
        "--harness",
        str(harness_b),
        "--repetitions",
        "2",
        "--seed",
        "17",
        "--out",
    ]
    left = runner.invoke(benchmark_app, [*args, str(first), "--json"])
    right = runner.invoke(benchmark_app, [*args, str(second), "--json"])
    assert left.exit_code == right.exit_code == 0
    assert first.read_bytes() == second.read_bytes()
    plan = json.loads(first.read_text())
    assert len(plan["schedule"]) == 4
    assert plan["trust_tier"] == "local-self-attested"
    assert plan["rankable"] is False


def test_eval_plan_rejects_process_harness_before_scheduling(tmp_path):
    result = runner.invoke(
        benchmark_app,
        [
            "eval",
            "plan",
            "--task",
            str(TASK),
            "--harness",
            str(PROCESS_HARNESS),
            "--out",
            str(tmp_path / "plan.json"),
            "--json",
        ],
    )
    payload = _json(result)

    assert result.exit_code == ExitCode.VALIDATION
    assert payload["error"]["code"] == "eval_requires_oci_harness"
    assert "protocol-v1 OCI" in payload["error"]["problem"]
    assert not (tmp_path / "plan.json").exists()


def test_analysis_preserves_grader_failure_infrastructure_class(monkeypatch):
    request = {
        "budget_limits": {"wall_time_ms": 1},
        "order_assignment": {"repetition": 0},
    }
    policy_digest = "2" * 64
    budget_digest = canonical_digest(request["budget_limits"])["value"]
    public_result = {
        "trial_id": "1" * 64,
        "task": {"id": "task-1"},
        "harness": {"id": "harness-1"},
        "model_policy": {
            "id": "model-policy-1",
            "version": "1.2.3",
            "policy_digest": {
                "algorithm": "sha256",
                "value": policy_digest,
            },
        },
        "budget": {
            "profile_id": "budget-profile-1",
            "limits_digest": {
                "algorithm": "sha256",
                "value": budget_digest,
            },
        },
        "failure": {"infrastructure": True},
        "status": "grader_failed",
        "evaluation": {"score": None},
    }
    monkeypatch.setattr(
        cli,
        "verify_public_protocol_export",
        lambda bundle, documents: public_result,
    )
    item = SimpleNamespace(
        bundle={
            "contents": {
                "trial_request": {"path": "trial/request.json"},
            }
        },
        documents={"trial/request.json": json.dumps(request).encode("utf-8")},
    )

    observation = cli._observation(item)

    assert observation.infrastructure_status is InfrastructureStatus.GRADER_FAILED
    assert observation.harness_status is HarnessStatus.COMPLETED
    assert observation.score is None
    assert observation.model_policy_id == (
        f"model-policy-1@1.2.3#sha256:{policy_digest}"
    )
    assert observation.budget_profile_id == (
        f"budget-profile-1#sha256:{budget_digest}"
    )


def test_duplicate_harness_and_task_ids_are_rejected(tmp_path):
    first = _manifest(tmp_path / "one", harness_id="duplicate")
    second = _manifest(tmp_path / "two", harness_id="duplicate")
    duplicate_harness = runner.invoke(
        benchmark_app,
        [
            "eval",
            "plan",
            "--task",
            str(TASK),
            "--harness",
            str(first),
            "--harness",
            str(second),
            "--out",
            str(tmp_path / "harness-plan.json"),
            "--json",
        ],
    )
    manifest = _manifest(tmp_path / "three", harness_id="unique")
    duplicate_task = runner.invoke(
        benchmark_app,
        [
            "eval",
            "plan",
            "--task",
            str(TASK),
            "--task",
            str(TASK),
            "--harness",
            str(manifest),
            "--out",
            str(tmp_path / "task-plan.json"),
            "--json",
        ],
    )
    assert duplicate_harness.exit_code == duplicate_task.exit_code == ExitCode.VALIDATION


def test_malformed_and_traversing_plans_fail_before_docker(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")
    malformed_result = runner.invoke(
        benchmark_app,
        ["eval", "run", str(malformed), "--out", str(tmp_path / "out"), "--json"],
    )
    assert malformed_result.exit_code == ExitCode.VALIDATION

    harness = _manifest(tmp_path)
    plan_path = tmp_path / "plan.json"
    planned = runner.invoke(
        benchmark_app,
        [
            "eval",
            "plan",
            "--task",
            str(TASK),
            "--harness",
            str(harness),
            "--out",
            str(plan_path),
        ],
    )
    assert planned.exit_code == 0
    plan = json.loads(plan_path.read_text())
    plan["tasks"][0]["path"] = "../escape"
    payload = {key: value for key, value in plan.items() if key != "plan_digest"}
    plan["plan_digest"] = canonical_digest(payload)["value"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    traversal = runner.invoke(
        benchmark_app,
        ["eval", "run", str(plan_path), "--out", str(tmp_path / "out2"), "--json"],
    )
    assert traversal.exit_code == ExitCode.VERIFICATION
    assert _json(traversal)["error"]["code"] == "path_traversal"


def test_no_docker_is_typed_unavailable(monkeypatch, tmp_path):
    harness = _manifest(tmp_path)

    def unavailable():
        raise CliProblem(
            "oci_engine_unavailable",
            "Docker is unavailable.",
            "daemon stopped",
            "Start the daemon.",
            "docker",
            ExitCode.UNAVAILABLE,
        )

    monkeypatch.setattr(cli, "_engine", unavailable)
    result = runner.invoke(
        benchmark_app,
        [
            "trial",
            "smoke",
            "--harness",
            str(harness),
            "--task",
            str(TASK),
            "--out",
            str(tmp_path / "smoke"),
            "--json",
        ],
    )
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert _json(result)["error"]["code"] == "oci_engine_unavailable"


def test_json_envelope_is_ascii_and_deterministic(tmp_path):
    manifest = _manifest(tmp_path)
    first = runner.invoke(
        benchmark_app, ["harness", "validate", str(manifest), "--json"]
    )
    second = runner.invoke(
        benchmark_app, ["harness", "validate", str(manifest), "--json"]
    )
    assert first.stdout == second.stdout
    first.stdout.encode("ascii", errors="strict")
    first.stdout.encode("cp1252", errors="strict")
    envelope = _json(first)
    assert set(envelope) == {
        "ok",
        "command",
        "trust_tier",
        "rankable",
        "data",
        "error",
    }


def test_source_has_no_remote_publication_or_upload_path():
    source = Path(cli.__file__).read_text(encoding="utf-8").lower()
    for forbidden in (
        "github actions",
        "api.github",
        "gh pr",
        "score upload",
        "requests.post",
        "urllib.request",
    ):
        assert forbidden not in source


def test_reproduce_uses_canonical_bundle_after_move_and_ignores_local_metadata(
    tmp_path,
):
    trial, source_task, harness_path = _portable_trial_export(
        tmp_path / "build"
    )
    moved = tmp_path / "moved-trial"
    shutil.move(str(trial), moved)
    shutil.rmtree(source_task)
    harness_path.unlink()
    shutil.rmtree(moved / "immutable-output")
    (moved / "local-run.json").write_text(
        '{"task_path":"C:/mutable/original","grade":{"score":0}}',
        encoding="utf-8",
    )

    result = runner.invoke(
        benchmark_app,
        ["eval", "reproduce", str(moved), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert _json(result)["data"]["match"] is True
    assert _json(result)["data"]["recorded_score"] == 1.0


def _docker_or_skip():
    try:
        engine = CliOciEngine.auto()
    except EngineUnavailableError as exc:
        pytest.skip(f"benchmark CLI Docker unavailable: {exc}")
    ok, detail = engine.daemon_status()
    if not ok:
        pytest.skip(f"benchmark CLI Docker daemon unavailable: {detail}")
    image = DigestPinnedImage.parse(IMAGE)
    last_error = None
    inspected = None
    for _ in range(3):
        try:
            inspected = engine.inspect_image(image)
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    if inspected is None:
        pytest.skip(f"benchmark CLI digest image unavailable after retries: {last_error}")
    if not inspected.verified:
        pytest.skip(f"benchmark CLI image digest mismatch: {image.digest}")


@pytest.mark.integration
def test_real_docker_local_plan_run_verify_analyze_reproduce_and_smoke(tmp_path):
    _docker_or_skip()
    command = (
        "import json,pathlib;"
        "p=pathlib.Path('/workspace/config.json');"
        "d=json.loads(p.read_text());"
        "d['status']='ready';"
        "p.write_text(json.dumps(d,sort_keys=True)+'\\n')"
    )
    harness_a = _manifest(
        tmp_path,
        harness_id="cli-real-a",
        command=command,
        protocol_v1=True,
    )
    harness_b = _manifest(
        tmp_path,
        harness_id="cli-real-b",
        command=command,
        protocol_v1=True,
    )
    plan = tmp_path / "plan.json"
    planned = runner.invoke(
        benchmark_app,
        [
            "eval",
            "plan",
            "--task",
            str(TASK),
            "--harness",
            str(harness_a),
            "--harness",
            str(harness_b),
            "--out",
            str(plan),
            "--json",
        ],
    )
    assert planned.exit_code == 0, planned.output

    output = tmp_path / "results"
    executed = runner.invoke(
        benchmark_app,
        ["eval", "run", str(plan), "--out", str(output), "--json"],
    )
    assert executed.exit_code == 0, executed.output
    assert _json(executed)["data"]["trial_count"] == 2

    verified = runner.invoke(
        benchmark_app, ["eval", "verify", str(output), "--json"]
    )
    assert verified.exit_code == 0, verified.output
    assert _json(verified)["data"]["verified_count"] == 2

    trial_dirs = sorted(
        path for path in output.iterdir() if (path / "bundle.json").is_file()
    )
    reproduced = runner.invoke(
        benchmark_app, ["eval", "reproduce", str(trial_dirs[0]), "--json"]
    )
    assert reproduced.exit_code == 0, reproduced.output
    assert _json(reproduced)["data"]["match"] is True
    assert _json(reproduced)["data"]["official_score_created"] is False

    analysis = runner.invoke(
        benchmark_app,
        [
            "eval",
            "analyze",
            str(output),
            "--harness-a",
            "cli-real-a",
            "--harness-b",
            "cli-real-b",
            "--out",
            str(tmp_path / "analysis"),
            "--json",
        ],
    )
    assert analysis.exit_code == 0, analysis.output
    assert _json(analysis)["data"]["publication_eligible"] is False
    assert _json(analysis)["data"]["quality_gate_failures"]

    smoke = runner.invoke(
        benchmark_app,
        [
            "trial",
            "smoke",
            "--harness",
            str(harness_a),
            "--task",
            str(TASK),
            "--out",
            str(tmp_path / "smoke"),
            "--json",
        ],
    )
    assert smoke.exit_code == 0, smoke.output
    assert _json(smoke)["data"]["rankable"] is False

    bundle = json.loads((trial_dirs[0] / "bundle.json").read_text())
    bundle["rounds"] = []
    (trial_dirs[0] / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    nested = runner.invoke(
        benchmark_app,
        [
            "eval",
            "analyze",
            str(output),
            "--harness-a",
            "cli-real-a",
            "--harness-b",
            "cli-real-b",
            "--out",
            str(tmp_path / "nested"),
            "--json",
        ],
    )
    assert nested.exit_code == ExitCode.ANALYSIS
    assert _json(nested)["error"]["code"] == "nested_observation_forbidden"


@pytest.mark.integration
def test_offline_verify_detects_tampering(tmp_path):
    _docker_or_skip()
    command = (
        "import json,pathlib;"
        "p=pathlib.Path('/workspace/config.json');"
        "d=json.loads(p.read_text());d['status']='ready';"
        "p.write_text(json.dumps(d,sort_keys=True)+'\\n')"
    )
    harness = _manifest(tmp_path, harness_id="cli-tamper", command=command)
    smoke_dir = tmp_path / "smoke"
    smoke = runner.invoke(
        benchmark_app,
        [
            "trial",
            "smoke",
            "--harness",
            str(harness),
            "--task",
            str(TASK),
            "--out",
            str(smoke_dir),
        ],
    )
    assert smoke.exit_code == 0, smoke.output
    trial = next(path for path in smoke_dir.iterdir() if (path / "bundle.json").is_file())
    document = trial / "documents" / "trial" / "result.json"
    document.write_bytes(document.read_bytes() + b" ")
    result = runner.invoke(
        benchmark_app, ["eval", "verify", str(trial), "--json"]
    )
    assert result.exit_code == ExitCode.VERIFICATION
    assert _json(result)["error"]["code"] == "bundle_verification_failed"
