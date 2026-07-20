"""Real Docker model-free control-plane smoke across both validated tasks."""
from __future__ import annotations

import base64
import json
import textwrap
from pathlib import Path

import pytest

from atv_bench.control_plane import (
    ControllerLedger,
    ControllerModelPolicy,
    ControllerRunRequest,
    ControllerState,
    ControllerTaskSet,
    TrialController,
)
from atv_bench.eval import (
    Budget,
    BudgetProfile,
    HarnessRef,
    ModelPolicyRef,
    ScheduledTrial,
    TaskPackage,
    TaskRef,
    TrialAttempt,
    TrialSpec,
)
from atv_bench.eval.bundle import ContentAddressedStore
from atv_bench.harness_manifest import load_harness_manifest
from atv_bench.protocol import canonical_digest
from atv_bench.sandbox import (
    CliOciEngine,
    DigestPinnedImage,
    EngineUnavailableError,
    OciNetworkPolicy,
    OciTrialRunner,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
TASKS = (
    ROOT / "tasks" / "smoke" / "repair_config",
    ROOT / "tasks" / "smoke" / "cross_file_total",
)
TRIAL_CASES = (
    (TASKS[0], False),
    (TASKS[1], False),
    (TASKS[0], True),
)


def _engine_or_skip():
    try:
        engine = CliOciEngine.auto()
    except EngineUnavailableError as exc:
        pytest.skip(f"trial-controller Docker unavailable: {exc}")
    reachable, detail = engine.daemon_status()
    if not reachable:
        pytest.skip(f"trial-controller Docker daemon unavailable: {detail}")
    return engine


def _command(task_id: str, *, max_output: bool = False) -> str:
    if task_id == "smoke.repair-config" and max_output:
        edit = """
p = pathlib.Path("/workspace/config.json")
payload = json.dumps(
    {"label": "ATV Bench", "status": "ready"},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
maximum = 1048576
p.write_bytes(payload + b" " * (maximum - len(payload)))
"""
    elif task_id == "smoke.repair-config":
        edit = """
p = pathlib.Path("/workspace/config.json")
d = json.loads(p.read_text())
d["status"] = "ready"
p.write_text(json.dumps(d, sort_keys=True) + "\\n")
"""
    else:
        edit = """
root = pathlib.Path("/workspace")
a = int((root / "data/a.txt").read_text())
b = int((root / "data/b.txt").read_text())
(root / "result.json").write_text(
    json.dumps(dict(total=a + b), sort_keys=True) + "\\n"
)
"""
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
    prefix = textwrap.dedent(
        f"""
import datetime
import hashlib
import json
import pathlib
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
"""
    )
    suffix = textwrap.dedent(
        """
usage = {
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
}
emit({
    "schema": "atv.harness-event/v1",
    "type": "result",
    "protocol_version": 1,
    "trial_id": request["trial_id"],
    "attempt_id": request["attempt_id"],
    "harness_sequence": 1,
    "emitted_at": now(),
    "harness_result": {
        "schema": "atv.harness-result/v1",
        "status": "completed",
        "exit": {
            "code": 0,
            "signal": None,
            "timed_out": False,
            "cancelled": False,
        },
        "output_tree_digest": {
            "algorithm": "sha256",
            "value": hashlib.sha256(
                b"controller-smoke-output"
            ).hexdigest(),
        },
        "artifacts": [],
        "reported_usage": usage,
        "failure": None,
    },
})
"""
    )
    script = prefix + textwrap.dedent(edit) + suffix
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        "import base64;"
        f"exec(compile(base64.b64decode({encoded!r}),"
        "'<atv-controller-smoke>','exec'))"
    )


def _manifest(
    tmp_path: Path,
    task: TaskPackage,
    *,
    max_output: bool = False,
):
    document = {
        "schema": "atv.harness/v1",
        "id": f"controller-{task.id}",
        "version": "1.0.0",
        "display_name": f"Controller {task.id}",
        "runtime": {
            "kind": "oci",
            "image": task.manifest["environment"]["image"],
            "entrypoint": [
                "python",
                "-c",
                _command(task.id, max_output=max_output),
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
                "repository": "https://example.invalid/controller-smoke",
                "revision": "controller-smoke-v1",
                "tree_digest": {
                    "algorithm": "sha256",
                    "value": "a" * 64,
                },
            },
            "license": "MIT",
        },
    }
    path = tmp_path / f"{task.id}.harness.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_harness_manifest(path)


def _scheduled(task: TaskPackage, harness, policy) -> ScheduledTrial:
    spec = TrialSpec(
        benchmark_release="ATV-2026.09",
        protocol_version="atv.trial/v1",
        schedule_id="1" * 64,
        task=TaskRef(
            task.id,
            task.version,
            canonical_digest(task.manifest)["value"],
        ),
        harness=HarnessRef(harness.id, harness.version, harness.digest),
        model_policy=ModelPolicyRef(policy.id, policy.version, policy.digest),
        budget_profile=BudgetProfile("smoke", Budget(60, 1, 1, 1)),
        repetition=0,
        schedule_seed=11,
    )
    return ScheduledTrial(
        attempt=TrialAttempt(spec, 1, "4" * 64),
        block_id="5" * 64,
        order_index=0,
        sequence_index=0,
        worker_id="linux-amd64",
    )


@pytest.mark.parametrize(
    ("task_root", "max_output"),
    TRIAL_CASES,
    ids=("repair-config", "cross-file-total", "repair-config-max-output"),
)
def test_real_docker_model_free_full_control_plane(
    task_root,
    max_output,
    tmp_path,
):
    engine = _engine_or_skip()
    task = TaskPackage.load(task_root)
    image = DigestPinnedImage.parse(task.manifest["environment"]["image"])
    try:
        inspection = engine.inspect_image(image)
    except Exception as exc:
        pytest.skip(f"required digest-pinned task image is not cached: {exc}")
    if not inspection.verified:
        pytest.skip(f"cached image does not verify digest {image.digest}")

    harness = _manifest(tmp_path, task, max_output=max_output)
    policy = ControllerModelPolicy.model_free()
    scheduled = _scheduled(task, harness, policy)
    oci_root = tmp_path / "oci"
    oci_root.mkdir()
    controller = TrialController(
        oci_runner=OciTrialRunner(engine, work_root=oci_root),
        ledger=ControllerLedger(tmp_path / "ledger.jsonl"),
        store=ContentAddressedStore(tmp_path / "cas"),
    )
    result = controller.run(
        ControllerRunRequest(
            scheduled=scheduled,
            task=task,
            harness=harness,
            model_policy=policy,
            task_set=ControllerTaskSet("smoke-two", "1.0.0", "6" * 64),
            run_id=f"run-{task.id}",
            network=OciNetworkPolicy.none(),
        )
    )

    assert result.state is ControllerState.COMPLETED
    assert result.trust_tier == "local-self-attested"
    assert result.rankable is False
    assert result.official_verified is False
    assert result.problem is None
    assert result.grade is not None and result.grade.passed is True
    assert result.grade.score == 1.0
    assert result.internal_bundle is not None
    result.internal_bundle.verify()
    assert result.protocol_export is not None
    trial_result = result.protocol_export.verify()
    exported_harness_result = json.loads(
        result.protocol_export.documents["trial/harness-result.json"]
    )
    assert trial_result["trust_tier"] == "local-self-attested"
    assert trial_result["rankable"] is False
    assert trial_result["status"] == "success"
    assert exported_harness_result["output_tree_digest"]["value"] == (
        result.grade.output_tree_digest
    )
    assert exported_harness_result["output_tree_digest"] != (
        result.oci_result.protocol_transcript.result["output_tree_digest"]
    )
    assert result.oci_result.evidence.workspace["seed"]["verified"] is True
    assert result.oci_result.evidence.workspace["removed"] is True
    assert [notice.code for notice in result.limitations] == [
        "local_self_attested_runner"
    ]
    assert result.oci_result.protocol_transcript is not None
    assert result.oci_result.protocol_transcript.authority_verified is True
    assert result.oci_result.evidence.protocol["mode"] == (
        "interactive-attached-roundtrip"
    )
    if max_output:
        declared_stdout = int(
            task.manifest["grader"]["budget_limits"]["stdout_bytes"]
        )
        assert len(result.oci_result.grader_stdout) > declared_stdout
        assert result.oci_result.evidence.grader.run.stdout_truncated is False
