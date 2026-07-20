"""Hermetic end-to-end trial controller state, evidence, and retry tests."""
from __future__ import annotations

import json
import multiprocessing
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import atv_bench.control_plane.trial_controller as controller_module
from atv_bench.control_plane import (
    ControllerLedger,
    ControllerModelPolicy,
    ControllerProblem,
    ControllerRunRequest,
    ControllerState,
    ControllerTaskSet,
    TrialController,
)
from atv_bench.eval import (
    Budget,
    BudgetProfile,
    HarnessRef,
    InfrastructureStatus,
    ModelPolicyRef,
    ScheduledTrial,
    TaskPackage,
    TaskRef,
    TrialAttempt,
    TrialSpec,
)
from atv_bench.eval.bundle import ContentAddressedStore
from atv_bench.harness_manifest import load_harness_manifest
from atv_bench.protocol import canonical_digest, canonical_json_bytes, sha256_bytes
from atv_bench.sandbox import (
    OciNetworkPolicy,
    OciRunnerLifecycleReceipt,
    OciTrialStatus,
)
from atv_bench.security import CredentialBroker, TrialBudget

ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = ROOT / "tasks" / "smoke" / "repair_config"
IMAGE = (
    "docker.io/library/python@sha256:"
    "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
)
SECRET_CANARY = "CONTROLLER_PROVIDER_SECRET_CANARY_DO_NOT_LEAK"


class SequenceClock:
    def __init__(self):
        self.index = 0

    def __call__(self) -> str:
        second = self.index
        self.index += 1
        return f"2026-07-19T12:00:{second:02d}Z"


def _manifest(tmp_path: Path, *, model_required: bool = False):
    document = {
        "schema": "atv.harness/v1",
        "id": "controller-test-harness",
        "version": "1.0.0",
        "display_name": "Controller Test Harness",
        "runtime": {
            "kind": "oci",
            "image": IMAGE,
            "entrypoint": ["python", "-c", "print('controller-test')"],
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
            "env_allowlist": (
                ["ATV_MODEL_GATEWAY_HANDLE"] if model_required else []
            ),
            "network_requirement": (
                "model-gateway-only" if model_required else "none"
            ),
            "writable_paths": ["/workspace", "/artifacts"],
            "requires_tty": False,
        },
        "metadata": {
            "source": {
                "repository": "https://example.invalid/controller-test",
                "revision": "controller-test-v1",
                "tree_digest": {
                    "algorithm": "sha256",
                    "value": "a" * 64,
                },
            },
            "license": "MIT",
        },
    }
    path = tmp_path / ("model-harness.json" if model_required else "harness.json")
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_harness_manifest(path)


def _model_policy(*, required: bool = False) -> ControllerModelPolicy:
    if not required:
        return ControllerModelPolicy.model_free()
    return ControllerModelPolicy(
        id="controlled-model",
        version="1.0.0",
        model_required=True,
        allowed_models=("provider/model-v1",),
        allowed_route_ids=("route-primary",),
        gateway="model-gateway.internal:443",
        budget=TrialBudget(2, 100, 100, 200, 10_000),
    )


def _scheduled(
    task: TaskPackage,
    harness,
    model_policy: ControllerModelPolicy,
    *,
    task_digest: str | None = None,
    fresh_nonce: str = "4" * 64,
) -> ScheduledTrial:
    spec = TrialSpec(
        benchmark_release="ATV-2026.09",
        protocol_version="atv.trial/v1",
        schedule_id="1" * 64,
        task=TaskRef(
            task.id,
            task.version,
            task_digest or canonical_digest(task.manifest)["value"],
        ),
        harness=HarnessRef(harness.id, harness.version, harness.digest),
        model_policy=ModelPolicyRef(
            model_policy.id,
            model_policy.version,
            model_policy.digest,
        ),
        budget_profile=BudgetProfile(
            "smoke",
            Budget(60, 20_000, 20, 500_000),
        ),
        repetition=0,
        schedule_seed=7,
    )
    attempt = TrialAttempt(spec, 1, fresh_nonce)
    return ScheduledTrial(
        attempt=attempt,
        block_id="5" * 64,
        order_index=0,
        sequence_index=0,
        worker_id="linux-amd64",
    )


def _request(
    tmp_path: Path,
    *,
    model_required: bool = False,
    task_digest: str | None = None,
    fresh_nonce: str = "4" * 64,
):
    task = TaskPackage.load(TASK_ROOT)
    harness = _manifest(tmp_path, model_required=model_required)
    policy = _model_policy(required=model_required)
    scheduled = _scheduled(
        task,
        harness,
        policy,
        task_digest=task_digest,
        fresh_nonce=fresh_nonce,
    )
    network = (
        OciNetworkPolicy.model_gateway_only(
            "private-gateway-network",
            allowed_gateway_identities=("gateway",),
        )
        if model_required
        else OciNetworkPolicy.none()
    )
    return ControllerRunRequest(
        scheduled=scheduled,
        task=task,
        harness=harness,
        model_policy=policy,
        task_set=ControllerTaskSet("smoke", "1.0.0", "6" * 64),
        run_id="run-controller-test",
        network=network,
    )


def _snapshot_bytes(root: Path) -> bytes:
    return controller_module._encode_output_snapshot(root)


def _ledger_process_worker(path, start, index):
    ledger = ControllerLedger(Path(path))
    start.wait()
    attempt_id = f"{index + 1:064x}"
    trial_id = f"{index + 101:064x}"
    for state in (
        ControllerState.CREATED,
        ControllerState.VALIDATING,
        ControllerState.VALIDATED,
    ):
        ledger.append(
            trial_id=trial_id,
            attempt_id=attempt_id,
            state=state,
        )


class FakeOciRunner:
    def __init__(
        self,
        *,
        status: OciTrialStatus = OciTrialStatus.COMPLETED,
        malformed_snapshot: bool = False,
        lifecycle_complete: bool = True,
        max_output_snapshot: bool = False,
        enforce_stdout_limit: bool = False,
    ):
        self.status = status
        self.malformed_snapshot = malformed_snapshot
        self.lifecycle_complete = lifecycle_complete
        self.max_output_snapshot = max_output_snapshot
        self.enforce_stdout_limit = enforce_stdout_limit
        self.requests = []
        self.snapshot_size = 0

    def run(self, request):
        self.requests.append(request)
        oracle = next(
            path
            for gate, _, path, expected in request.task.validation_cases()
            if gate.value == "oracle" and expected
        )
        if self.malformed_snapshot:
            snapshot = b"not-json"
        elif self.max_output_snapshot:
            maximum = int(request.task.manifest["output"]["max_total_bytes"])
            document = json.dumps(
                {"label": "ATV Bench", "status": "ready"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            assert len(document) <= maximum
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                (output / "config.json").write_bytes(
                    document + b" " * (maximum - len(document))
                )
                snapshot = _snapshot_bytes(output)
        else:
            snapshot = _snapshot_bytes(oracle)
        self.snapshot_size = len(snapshot)
        if self.enforce_stdout_limit:
            snapshot = snapshot[: request.grader_resources.stdout_bytes]
        timed_out = self.status is OciTrialStatus.TIMED_OUT
        cancelled = self.status is OciTrialStatus.CANCELLED
        exit_code = (
            17
            if self.status is OciTrialStatus.NONZERO_EXIT
            else None
            if timed_out or cancelled
            else 0
        )
        evidence_payload = {
            "schema": "fake-oci-evidence/v1",
            "attempt_id": request.attempt.attempt_id,
            "status": self.status.value,
        }
        evidence_bytes = canonical_json_bytes(evidence_payload)
        evidence_digest = sha256_bytes(evidence_bytes)
        run = SimpleNamespace(
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            duration_ms=10,
            stdout_total_bytes=0,
            stderr_total_bytes=0,
        )
        harness = SimpleNamespace(
            run=run,
            storage=SimpleNamespace(peak_bytes=64),
            policy={
                "resources": {
                    "memory_bytes": 128 * 1024 * 1024,
                    "pids_limit": 32,
                }
            },
        )
        evidence = SimpleNamespace(
            digest=evidence_digest,
            canonical_bytes=evidence_bytes,
            harness=harness,
            started_at="2026-07-19T12:00:00Z",
            duration_ms=10,
            workspace={"bytes": 64},
        )
        receipt = OciRunnerLifecycleReceipt(
            evidence_digest=evidence_digest,
            _execution_complete=self.lifecycle_complete,
            credential_finalized=self.lifecycle_complete,
            hidden_inputs_mounted_after_harness_exit=self.lifecycle_complete,
            runtime_verified=self.lifecycle_complete,
        )
        return SimpleNamespace(
            status=self.status,
            evidence=evidence,
            lifecycle_receipt=receipt,
            grader_stdout=snapshot,
            grader_stderr=b"",
            harness_stdout=b"",
            harness_stderr=b"",
            protocol_transcript=None,
        )


def _controller(
    tmp_path: Path,
    runner,
    *,
    broker: CredentialBroker | None = None,
    healthcheck=None,
) -> TrialController:
    clock = SequenceClock()
    return TrialController(
        oci_runner=runner,
        ledger=ControllerLedger(tmp_path / "controller-ledger.jsonl", clock=clock),
        store=ContentAddressedStore(tmp_path / "cas"),
        broker=broker,
        gateway_healthcheck=healthcheck,
        clock=clock,
    )


def test_model_free_full_lifecycle_builds_cas_and_canonical_local_export(tmp_path):
    request = _request(tmp_path)
    runner = FakeOciRunner()
    controller = _controller(tmp_path, runner)

    result = controller.run(request)

    assert result.state is ControllerState.COMPLETED
    assert result.problem is None
    assert result.grade is not None and result.grade.score == 1.0
    assert result.outcome.score == 1.0
    assert result.internal_bundle is not None
    result.internal_bundle.verify()
    assert result.protocol_export is not None
    assert result.trust_tier == "local-self-attested"
    assert result.rankable is False
    assert result.official_verified is False
    trial_result = result.protocol_export.verify()
    assert trial_result["trust_tier"] == "local-self-attested"
    assert trial_result["rankable"] is False
    request_document = json.loads(
        result.protocol_export.documents["trial/request.json"]
    )
    analysis_descriptor = trial_result["analysis"]["document"]
    analysis_document = json.loads(
        result.protocol_export.documents[analysis_descriptor["path"]]
    )
    policy = request_document["model_policy"]
    policy_digest = policy["policy_digest"]["value"]
    budget_digest = canonical_digest(
        request_document["budget_limits"]
    )["value"]
    assert analysis_document["model_policy_id"] == (
        f"{policy['id']}@{policy['version']}#sha256:{policy_digest}"
    )
    assert analysis_document["budget_profile_id"] == (
        f"{result.request.scheduled.spec.budget_profile.id}"
        f"#sha256:{budget_digest}"
    )
    assert result.oci_result.lifecycle_receipt.official_verified is False
    assert not isinstance(
        result.oci_result.lifecycle_receipt,
        controller_module.TrustedRunnerLifecycleReceipt,
    )
    assert runner.requests[0].protocol_session is not None
    assert runner.requests[0].grader_command == controller_module.SNAPSHOT_EXPORT_COMMAND
    log_schemas = {
        descriptor["schema"]
        for descriptor in result.protocol_export.bundle["contents"]["logs"]
    }
    assert {
        "atv.reproduction-evidence/v1",
        "atv.grader.file-assertions/v1",
        "atv.output-snapshot/v2",
    } <= log_schemas
    assert [notice.code for notice in result.limitations] == [
        "local_self_attested_runner",
        "protocol_transport_unverified",
    ]
    states = [entry.state for entry in result.ledger_entries]
    assert states[0] is ControllerState.CREATED
    assert states[-1] is ControllerState.COMPLETED
    assert states == sorted(states, key=lambda state: list(ControllerState).index(state))


def test_controller_run_and_ledger_are_idempotent(tmp_path):
    request = _request(tmp_path)
    controller = _controller(tmp_path, FakeOciRunner())
    first = controller.run(request)
    count = len(controller.ledger.entries)
    second = controller.run(request)
    assert second is first
    assert len(controller.ledger.entries) == count
    duplicate = controller.ledger.append(
        trial_id=request.scheduled.spec.trial_id,
        attempt_id=request.scheduled.attempt.attempt_id,
        state=ControllerState.COMPLETED,
        evidence_digest=first.ledger_entries[-1].evidence_digest,
    )
    assert duplicate == first.ledger_entries[-1]
    assert len(controller.ledger.entries) == count


def test_interprocess_ledger_appends_preserve_one_contiguous_digest_chain(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    path = tmp_path / "shared-ledger.jsonl"
    ledger = ControllerLedger(path)
    processes = [
        context.Process(
            target=_ledger_process_worker,
            args=(str(path), start, index),
        )
        for index in range(8)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert len(ledger.entries) == 24
    assert [entry.sequence for entry in ledger.entries] == list(range(24))
    for index, entry in enumerate(ledger.entries):
        expected_previous = (
            ledger.entries[index - 1].entry_digest if index else None
        )
        assert entry.previous_digest == expected_previous
    for worker in range(8):
        attempt_id = f"{worker + 1:064x}"
        assert [entry.state for entry in ledger.entries_for(attempt_id)] == [
            ControllerState.CREATED,
            ControllerState.VALIDATING,
            ControllerState.VALIDATED,
        ]


def test_internal_bundle_embeds_only_current_attempt_ledger_slice(tmp_path):
    controller = _controller(tmp_path, FakeOciRunner())
    first = controller.run(_request(tmp_path, fresh_nonce="4" * 64))
    second = controller.run(_request(tmp_path, fresh_nonce="7" * 64))

    assert first.state is second.state is ControllerState.COMPLETED
    assert len(controller.ledger.entries) > len(second.ledger_entries)
    for result in (first, second):
        record = next(
            item
            for item in result.internal_bundle.manifest["artifacts"]
            if item["path"] == "controller/ledger.jsonl"
        )
        embedded = result.internal_bundle.store.read_bytes(record["sha256"])
        rows = [
            json.loads(line)
            for line in embedded.decode("utf-8").splitlines()
        ]
        assert rows
        assert {
            row["attempt_id"] for row in rows
        } == {result.request.scheduled.attempt.attempt_id}
        assert len(rows) < len(controller.ledger.entries)


def test_valid_max_output_fits_controller_snapshot_stdout_budget(tmp_path):
    runner = FakeOciRunner(
        max_output_snapshot=True,
        enforce_stdout_limit=True,
    )
    result = _controller(tmp_path, runner).run(_request(tmp_path))

    declared = int(
        result.request.task.manifest["grader"]["budget_limits"][
            "stdout_bytes"
        ]
    )
    requested = runner.requests[0].grader_resources.stdout_bytes
    assert runner.snapshot_size > declared
    assert requested >= runner.snapshot_size
    assert result.state is ControllerState.COMPLETED
    assert result.grade is not None and result.grade.passed is True


def test_identity_mismatch_fails_before_oci_with_actionable_problem(tmp_path):
    request = _request(tmp_path, task_digest="f" * 64)
    runner = FakeOciRunner()
    result = _controller(tmp_path, runner).run(request)
    assert result.state is ControllerState.FAILED
    assert result.problem.code == "task_identity_mismatch"
    assert all(
        label in str(result.problem)
        for label in ("Problem:", "Cause:", "Fix:", "Evidence:")
    )
    assert runner.requests == []


@pytest.mark.parametrize(
    ("status", "expected_harness"),
    [
        (OciTrialStatus.TIMED_OUT, "timed_out"),
        (OciTrialStatus.NONZERO_EXIT, "crashed"),
        (OciTrialStatus.PROTOCOL_ERROR, "protocol_error"),
    ],
)
def test_harness_failures_score_zero_and_are_not_retryable(
    tmp_path, status, expected_harness
):
    result = _controller(tmp_path, FakeOciRunner(status=status)).run(
        _request(tmp_path)
    )
    assert result.state is ControllerState.COMPLETED
    assert result.outcome.infrastructure_status is InfrastructureStatus.OK
    assert result.outcome.harness_status.value == expected_harness
    assert result.outcome.score == 0.0
    assert result.retryable is False
    with pytest.raises(ControllerProblem, match="Only typed infrastructure"):
        _controller(tmp_path / "retry", FakeOciRunner()).retry(
            result, fresh_nonce="8" * 64
        )


def test_cleanup_failure_is_infrastructure_and_retryable(tmp_path):
    result = _controller(
        tmp_path,
        FakeOciRunner(
            status=OciTrialStatus.CLEANUP_FAILED,
            lifecycle_complete=False,
        ),
    ).run(_request(tmp_path))
    assert result.state is ControllerState.FAILED
    assert result.outcome.infrastructure_status is InfrastructureStatus.RUNNER_FAILED
    assert result.outcome.score is None
    assert result.retryable is True
    assert result.problem.code == "lifecycle_export_invalid"


def test_malformed_post_run_snapshot_is_typed_grader_infrastructure_failure(
    tmp_path,
):
    result = _controller(
        tmp_path,
        FakeOciRunner(malformed_snapshot=True),
    ).run(_request(tmp_path))
    assert result.state is ControllerState.FAILED
    assert result.problem.code == "grader_snapshot_invalid"
    assert result.outcome.infrastructure_status is InfrastructureStatus.GRADER_FAILED
    assert result.retryable is True


def test_gateway_healthcheck_failure_retries_with_fresh_attempt_and_lineage(
    tmp_path,
):
    broker = CredentialBroker()
    broker.register_provider("provider", SECRET_CANARY)
    calls = 0

    def healthcheck(_handle, _request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(SECRET_CANARY)

    runner = FakeOciRunner(status=OciTrialStatus.ENGINE_ERROR)
    controller = _controller(
        tmp_path,
        runner,
        broker=broker,
        healthcheck=healthcheck,
    )
    first = controller.run(_request(tmp_path, model_required=True))
    assert first.outcome.infrastructure_status is InfrastructureStatus.MODEL_GATEWAY_FAILED
    assert first.retryable is True
    assert runner.requests == []
    second = controller.retry(first, fresh_nonce="9" * 64)
    assert second.request.scheduled.block_id == first.request.scheduled.block_id
    assert second.request.scheduled.order_index == first.request.scheduled.order_index
    assert second.request.scheduled.worker_id == first.request.scheduled.worker_id
    assert second.request.scheduled.attempt.attempt_number == 2
    assert second.request.scheduled.attempt.workspace_id != (
        first.request.scheduled.attempt.workspace_id
    )
    assert second.capability_lineage["issuance"]["parent_attempt_id"] == (
        first.request.scheduled.attempt.attempt_id
    )

    visible = (
        controller.ledger.path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "first_problem": first.problem.to_dict(),
                "second_problem": second.problem.to_dict() if second.problem else None,
                "lineage": second.capability_lineage,
            },
            sort_keys=True,
        )
    )
    assert SECRET_CANARY not in visible


def test_export_mismatch_fails_closed_with_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(
        controller_module,
        "export_protocol_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("mismatch")),
    )
    result = _controller(tmp_path, FakeOciRunner()).run(_request(tmp_path))
    assert result.state is ControllerState.FAILED
    assert result.problem.code == "controller_unexpected_failure"
    assert result.protocol_export is None
