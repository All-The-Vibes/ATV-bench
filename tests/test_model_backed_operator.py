"""Hermetic end-to-end tests for the local model-backed operator."""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import atv_bench.control_plane.trial_controller as controller_module
from atv_bench.operator import (
    ModelBackedEvalPlan,
    ModelBackedEvalPolicy,
    ModelBackedOperator,
    ModelBackedOperatorError,
    ProviderBindings,
)
from atv_bench.protocol import canonical_digest, canonical_json_bytes, sha256_bytes
from atv_bench.sandbox import OciRunnerLifecycleReceipt
from atv_bench.security import (
    AttestationSigner,
    ProviderUsage,
    ResponsesBackendRequest,
    ResponsesBackendResponse,
)


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "smoke" / "repair_config"
PROVIDER_ID = "provider-a"
PUBLIC_MODEL = "controlled-model"
PROVIDER_MODEL = "provider-model-snapshot"
ROUTE_ID = "controlled-route"
PROVIDER_SECRET = "MODEL_BACKED_OPERATOR_PROVIDER_SECRET_CANARY"


class FakeResponsesBackend:
    def __init__(self, *, resolved_model: str = PROVIDER_MODEL):
        self.resolved_model = resolved_model
        self.requests: list[ResponsesBackendRequest] = []
        self.credentials: list[str | bytes] = []
        self.cancelled: list[str] = []

    def create(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> ResponsesBackendResponse:
        self.credentials.append(credential)
        self.requests.append(request)
        return ResponsesBackendResponse(
            provider_id=PROVIDER_ID,
            model=self.resolved_model,
            request_id=request.provider_request_id,
            response={
                "id": f"response-{len(self.requests)}",
                "object": "response",
                "created_at": 1_721_500_000,
                "status": "completed",
                "model": self.resolved_model,
                "output": [
                    {
                        "id": f"call-{len(self.requests)}",
                        "type": "function_call",
                        "status": "completed",
                        "arguments": '{"path":"README.md"}',
                        "call_id": f"call-{len(self.requests)}",
                        "name": "read_file",
                    }
                ],
                "usage": {
                    "input_tokens": 256,
                    "output_tokens": 16,
                    "total_tokens": 272,
                },
            },
            usage=ProviderUsage(
                input_tokens=256,
                output_tokens=16,
                cost_microusd=0,
            ),
        )

    def stream(self, credential, request):
        raise AssertionError("streaming was not requested")

    def cancel(self, provider_request_id: str) -> None:
        self.cancelled.append(provider_request_id)


class FakeOciRunner:
    def __init__(
        self,
        server,
        *,
        forbidden_then_allowed: bool = False,
    ):
        self.server = server
        self.port = server.port
        self.forbidden_then_allowed = forbidden_then_allowed
        self.requests: list[Any] = []
        self.http_statuses: list[int] = []
        self.response_headers: list[dict[str, str]] = []
        self.handle_values: list[str] = []

    def _call(self, request, model: str) -> int:
        assert request.gateway_handle is not None
        handle = request.gateway_handle.value
        payload = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": "Inspect the repository.",
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read a repository file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            ],
            "tool_choice": "auto",
            "max_output_tokens": 64,
            "stream": False,
            "store": False,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPConnection(
            self.server.host,
            self.server.port,
            timeout=5,
        )
        connection.request(
            "POST",
            "/v1/responses",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {handle}",
            },
        )
        response = connection.getresponse()
        response.read()
        self.response_headers.append(
            {name.lower(): value for name, value in response.getheaders()}
        )
        status = response.status
        connection.close()
        self.http_statuses.append(status)
        return status

    def run(self, request):
        self.requests.append(request)
        assert request.gateway_handle is not None
        self.handle_values.append(request.gateway_handle.value)
        if self.forbidden_then_allowed:
            assert self._call(request, "forbidden-subagent-model") == 403
        expected = 200
        observed = self._call(request, PUBLIC_MODEL)
        if observed not in {expected, 502}:
            raise AssertionError(f"unexpected gateway status: {observed}")

        oracle = next(
            path
            for gate, _, path, expected_pass in request.task.validation_cases()
            if gate.value == "oracle" and expected_pass
        )
        snapshot = controller_module._encode_output_snapshot(oracle)
        evidence_document = {
            "schema": "fake-model-backed-oci-evidence/v1",
            "attempt_id": request.attempt.attempt_id,
            "network": request.network.to_dict(),
            "status": "completed",
        }
        evidence_bytes = canonical_json_bytes(evidence_document)
        evidence_digest = sha256_bytes(evidence_bytes)
        run = SimpleNamespace(
            exit_code=0,
            timed_out=False,
            cancelled=False,
            duration_ms=10,
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
                        "pids_limit": 32,
                    }
                },
            ),
            started_at="2026-07-20T12:00:00Z",
            duration_ms=10,
            workspace={"bytes": 64},
        )
        assert request.credential_broker is not None
        request.credential_broker.complete(request.gateway_handle)
        return SimpleNamespace(
            status=controller_module.OciTrialStatus.COMPLETED,
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


def _task_image() -> str:
    return json.loads((TASK / "task.json").read_text(encoding="utf-8"))[
        "environment"
    ]["image"]


def _write_harness(path: Path, *, harness_id: str, subagents: bool) -> Path:
    document = {
        "schema": "atv.harness/v1",
        "id": harness_id,
        "version": "1.0.0",
        "display_name": harness_id,
        "runtime": {
            "kind": "oci",
            "image": _task_image(),
            "entrypoint": [
                "/opt/atv/bin/protocol-wrapper",
                "--harness",
                "hve-core" if subagents else "phoenix",
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
            "subagents": subagents,
            "resumable": False,
            "browser": False,
            "model_events": True,
            "tool_events": True,
            "usage_events": True,
            "checkpoint_events": False,
            "model_selection": "multiple" if subagents else "single",
            "token_usage_reporting": "reported",
            "call_usage_reporting": "reported",
            "cost_usage_reporting": "reported",
        },
        "security": {
            "env_allowlist": ["ATV_MODEL_GATEWAY_HANDLE"],
            "network_requirement": "model-gateway-only",
            "writable_paths": ["/workspace", "/artifacts", "/tmp"],
            "requires_tty": False,
        },
        "metadata": {
            "source": {
                "repository": f"https://example.invalid/{harness_id}",
                "revision": f"{harness_id}-v1",
                "tree_digest": {
                    "algorithm": "sha256",
                    "value": hashlib_sha256(harness_id.encode("utf-8")),
                },
            },
            "license": "MIT",
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def hashlib_sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _policy_document(*, seed: int = 7) -> dict[str, Any]:
    document = {
        "schema": "atv.model-backed-eval-policy/v1",
        "id": "controlled-policy",
        "version": "1.0.0",
        "benchmark_release": "ATV-2026.07",
        "protocol_version": "atv.trial/v1",
        "track": "controlled",
        "task_set": {"id": "smoke-task-set", "version": "1.0.0"},
        "default_model": PUBLIC_MODEL,
        "routes": [
            {
                "route_id": ROUTE_ID,
                "public_model": PUBLIC_MODEL,
                "provider_id": PROVIDER_ID,
                "provider_model": PROVIDER_MODEL,
                "input_microusd_per_million": 0,
                "output_microusd_per_million": 0,
            }
        ],
        "budget_profile": {
            "id": "controlled-budget",
            "wall_time_seconds": 60,
            "max_model_calls": 4,
            "max_input_tokens": 1_000,
            "max_output_tokens": 1_000,
            "max_total_tokens": 2_000,
            "max_cost_microusd": 10_000,
        },
        "repetitions": 1,
        "seed": seed,
        "workers": ["linux-amd64"],
        "network": {
            "name": "atv-model-private",
            "gateway_identity": "atv-model-gateway",
        },
        "max_retries": 0,
        "underreport_policy": "reject",
        "handle_ttl_seconds": 600,
    }
    document["policy_digest"] = canonical_digest(document)["value"]
    return document


def _write_policy(path: Path, *, seed: int = 7) -> Path:
    path.write_text(json.dumps(_policy_document(seed=seed)), encoding="utf-8")
    return path


def _plan(tmp_path: Path, *, seed: int = 7) -> ModelBackedEvalPlan:
    tmp_path.mkdir(parents=True, exist_ok=True)
    phoenix = _write_harness(
        tmp_path / "phoenix.json",
        harness_id="atv-phoenix",
        subagents=False,
    )
    hve = _write_harness(
        tmp_path / "hve-core.json",
        harness_id="hve-core",
        subagents=True,
    )
    return ModelBackedEvalPlan.load(
        policy_path=_write_policy(tmp_path / "policy.json", seed=seed),
        task_paths=(TASK,),
        harness_paths=(phoenix, hve),
    )


def _operator(
    backend: FakeResponsesBackend,
    runners: list[FakeOciRunner],
    *,
    forbidden_then_allowed: bool = False,
) -> ModelBackedOperator:
    def factory(_work_root, server):
        runner = FakeOciRunner(
            server,
            forbidden_then_allowed=forbidden_then_allowed,
        )
        runners.append(runner)
        return runner

    return ModelBackedOperator(
        providers=ProviderBindings(
            backends={PROVIDER_ID: backend},
            credentials={PROVIDER_ID: PROVIDER_SECRET},
        ),
        oci_runner_factory=factory,
        signer=AttestationSigner.create(
            key_id="operator-test-key",
            secret_factory=lambda: b"O" * 32,
        ),
        clock=lambda: "2026-07-20T12:00:00Z",
    )


def _all_output_bytes(root: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_runs_paired_schedule_through_controller_and_writes_immutable_evidence(
    tmp_path,
):
    plan = _plan(tmp_path)
    blocks: dict[str, set[str]] = {}
    for item in plan.schedule:
        blocks.setdefault(item.block_id, set()).add(item.spec.harness.id)
    assert list(blocks.values()) == [{"atv-phoenix", "hve-core"}]

    backend = FakeResponsesBackend()
    runners: list[FakeOciRunner] = []
    output = tmp_path / "output"
    result = _operator(backend, runners).run(plan, output)

    assert result.succeeded is True
    assert result.rankable is False
    assert result.official_verified is False
    assert result.trust_tier == "local-self-attested"
    result.verify()
    assert len(result.attempts) == 2
    assert len(runners) == 1
    runner = runners[0]
    assert len(runner.requests) == 2
    expected_network = plan.policy.network_policy()
    assert all(request.network == expected_network for request in runner.requests)
    assert all(
        request.protocol_session.trial_request["model_policy"]["gateway"].endswith(
            f":{runner.port}"
        )
        for request in runner.requests
    )
    assert runner.http_statuses == [200, 200]
    assert len(set(runner.handle_values)) == 2
    assert all("x-atv-usage-receipt" in item for item in runner.response_headers)

    assert backend.credentials == [PROVIDER_SECRET, PROVIDER_SECRET]
    assert len(backend.requests) == 2
    for request in backend.requests:
        serialized = canonical_json_bytes(request.payload)
        assert PROVIDER_SECRET.encode() not in serialized
        assert all(handle.encode() not in serialized for handle in runner.handle_values)
        assert request.provider_model == PROVIDER_MODEL

    fingerprints = {
        item["opaque_handle_sha256"] for item in result.attempts
    }
    assert len(fingerprints) == 2
    assert all(item["gateway"]["receipt_count"] == 1 for item in result.attempts)
    assert all(item["gateway"]["usage"]["model_calls"] == 1 for item in result.attempts)
    assert all(
        item["controller"]["expected_export_handoff"] is True
        for item in result.attempts
    )
    assert PROVIDER_SECRET.encode() not in _all_output_bytes(output)
    assert all(
        handle.encode() not in _all_output_bytes(output)
        for handle in runner.handle_values
    )
    with pytest.raises(OSError):
        socket.create_connection(
            (runner.server.host, runner.port),
            timeout=0.2,
        )


def test_forbidden_subagent_model_is_fatal_even_if_allowed_call_succeeds(tmp_path):
    plan = _plan(tmp_path)
    backend = FakeResponsesBackend()
    runners: list[FakeOciRunner] = []
    result = _operator(
        backend,
        runners,
        forbidden_then_allowed=True,
    ).run(plan, tmp_path / "forbidden-output")

    assert result.succeeded is False
    assert result.rankable is False
    assert len(result.attempts) == 1
    codes = {item["code"] for item in result.attempts[0]["violations"]}
    assert "model_policy_violation" in codes
    assert "gateway_request_failed" in codes
    assert runners[0].http_statuses == [403, 200]
    assert result.verify()["status"] == "failed"


def test_backend_resolving_wrong_provider_model_fails_closed(tmp_path):
    plan = _plan(tmp_path)
    backend = FakeResponsesBackend(resolved_model="unregistered-provider-model")
    runners: list[FakeOciRunner] = []
    result = _operator(backend, runners).run(
        plan,
        tmp_path / "wrong-model-output",
    )

    assert result.succeeded is False
    assert runners[0].http_statuses == [502]
    codes = {item["code"] for item in result.attempts[0]["violations"]}
    assert "gateway_terminal_failure" in codes
    assert "gateway_request_failed" in codes
    assert result.rankable is False


def test_policy_digest_and_plan_identity_bind_seed_and_routes(tmp_path):
    first = _plan(tmp_path / "first", seed=7)
    second = _plan(tmp_path / "second", seed=8)
    assert first.digest != second.digest
    assert [item.to_dict() for item in first.schedule] != [
        item.to_dict() for item in second.schedule
    ]

    bad = _policy_document()
    bad["routes"][0]["provider_model"] = "mutated-after-preregistration"
    bad_path = tmp_path / "bad-policy.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ModelBackedOperatorError) as caught:
        ModelBackedEvalPolicy.load(bad_path)
    assert caught.value.code == "policy_digest_mismatch"


def test_output_is_never_overwritten_and_tampering_is_detected(tmp_path):
    plan = _plan(tmp_path)
    backend = FakeResponsesBackend()
    runners: list[FakeOciRunner] = []
    operator = _operator(backend, runners)
    output = tmp_path / "immutable-output"
    result = operator.run(plan, output)

    with pytest.raises(ModelBackedOperatorError) as caught:
        operator.run(plan, output)
    assert caught.value.code == "output_exists"

    target = next((output / "attempts").rglob("summary.json"))
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ModelBackedOperatorError) as tampered:
        result.verify()
    assert tampered.value.code == "output_file_tampered"


def test_provider_bindings_must_match_preregistered_providers(tmp_path):
    plan = _plan(tmp_path)
    backend = FakeResponsesBackend()
    operator = ModelBackedOperator(
        providers=ProviderBindings(
            backends={"wrong-provider": backend},
            credentials={"wrong-provider": PROVIDER_SECRET},
        ),
        oci_runner_factory=lambda _work, _server: pytest.fail(
            "runner must not start"
        ),
    )
    with pytest.raises(ModelBackedOperatorError) as caught:
        operator.run(plan, tmp_path / "provider-mismatch")
    assert caught.value.code == "provider_policy_mismatch"
