from __future__ import annotations

from copy import deepcopy

import pytest

from atv_bench.protocol import (
    build_accepted_event,
    canonical_digest,
    canonical_jsonl,
    negotiate_capabilities,
    sha256_bytes,
)

TIMESTAMP = "2026-07-19T12:00:00Z"


def digest(character: str) -> dict[str, str]:
    return {"algorithm": "sha256", "value": character * 64}


def document(
    schema: str,
    path: str,
    character: str,
    *,
    media_type: str = "application/json",
) -> dict[str, object]:
    return {
        "schema": schema,
        "path": path,
        "media_type": media_type,
        "size_bytes": 1,
        "digest": digest(character),
    }


def budget_limits(**overrides: int) -> dict[str, int]:
    value = {
        "wall_time_ms": 300_000,
        "cpu_time_ms": 240_000,
        "model_input_tokens": 120_000,
        "model_output_tokens": 80_000,
        "model_total_tokens": 200_000,
        "model_calls": 100,
        "cost_microusd": 5_000_000,
        "tool_calls": 1_000,
        "memory_bytes": 2_147_483_648,
        "storage_bytes": 4_294_967_296,
        "pids": 256,
        "stdout_bytes": 16_777_216,
        "stderr_bytes": 8_388_608,
        "artifact_bytes": 16_777_216,
    }
    value.update(overrides)
    return value


def usage(**overrides: int | None) -> dict[str, int | None]:
    value: dict[str, int | None] = {
        "wall_time_ms": 10_000,
        "cpu_time_ms": 5_000,
        "model_input_tokens": 1_200,
        "model_output_tokens": 800,
        "model_total_tokens": 2_000,
        "model_calls": 2,
        "cost_microusd": 25_000,
        "tool_calls": 8,
        "memory_bytes": 128_000_000,
        "storage_bytes": 1_024,
        "pids": 4,
        "stdout_bytes": 512,
        "stderr_bytes": 64,
        "artifact_bytes": 12,
    }
    value.update(overrides)
    return value


def capabilities(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "workspace_edit": True,
        "subagents": True,
        "resumable": False,
        "browser": False,
        "model_events": True,
        "tool_events": True,
        "usage_events": True,
        "checkpoint_events": False,
        "model_selection": "multiple",
        "token_usage_reporting": "reported",
        "call_usage_reporting": "reported",
        "cost_usage_reporting": "reported",
    }
    value.update(overrides)
    return value


@pytest.fixture
def protocol_documents() -> dict[str, object]:
    harness = {
        "schema": "atv.harness/v1",
        "id": "example-harness",
        "version": "1.2.3",
        "display_name": "Example Harness",
        "runtime": {
            "kind": "process",
            "command": ["example-harness", "--atv-run"],
            "working_directory": "/workspace",
            "executable_digest": digest("a"),
        },
        "protocol": {
            "minimum_version": 1,
            "maximum_version": 1,
            "input": "stdin-json",
            "output": "stdout-jsonl",
        },
        "capabilities": capabilities(),
        "security": {
            "env_allowlist": ["MODEL_BROKER_TOKEN"],
            "network_requirement": "model-gateway-only",
            "writable_paths": ["/workspace", "/artifacts"],
            "requires_tty": False,
        },
        "metadata": {
            "source": {
                "repository": "https://github.com/example/harness",
                "revision": "0123456789abcdef0123456789abcdef01234567",
                "tree_digest": digest("b"),
            },
            "license": "MIT",
        },
    }
    evidence = {
        "oracle": document("atv.task-validation/v1", "evidence/oracle.json", "c"),
        "noop": document("atv.task-validation/v1", "evidence/noop.json", "d"),
        "alternative_solutions": [
            document("atv.task-validation/v1", "evidence/alternative-1.json", "e")
        ],
        "exploit_cases": [
            document("atv.task-validation/v1", "evidence/exploit-1.json", "f")
        ],
        "mutation_cases": [
            document("atv.task-validation/v1", "evidence/mutation-1.json", "0")
        ],
        "independent_review": document(
            "atv.task-review/v1", "evidence/review.json", "1"
        ),
    }
    task = {
        "schema": "atv.task/v1",
        "id": "repair-smoke",
        "version": "1.0.0",
        "title": "Repair a small deterministic defect",
        "category": "repair",
        "capability_tags": ["multi-file", "verification"],
        "track_compatibility": ["controlled", "systems"],
        "difficulty": "smoke",
        "visibility": "public",
        "source": {
            "repository": "https://github.com/example/repository",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "tree_digest": digest("2"),
        },
        "environment": {
            "image": "ghcr.io/example/task@sha256:" + "3" * 64,
            "platform": {"os": "linux", "architecture": "amd64"},
        },
        "prompt": {
            "path": "prompt.md",
            "encoding": "utf-8",
            "media_type": "text/markdown",
            "digest": digest("4"),
        },
        "policy": {
            "tools": {"allowed": ["shell", "editor"], "denied": ["browser"]},
            "network": {
                "mode": "model-gateway-only",
                "allowed_destinations": ["model-gateway.internal:443"],
            },
            "writable_paths": ["/workspace", "/artifacts"],
            "credential_names": ["MODEL_BROKER_TOKEN"],
        },
        "budget_limits": budget_limits(),
        "output": {
            "mode": "named-artifacts",
            "allow_any_relative_path": False,
            "required_paths": ["main.py"],
            "allowed_paths": ["main.py", "notes.txt"],
            "allowed_media_types": ["text/x-python", "text/plain"],
            "max_files": 16,
            "max_total_bytes": 1_048_576,
        },
        "grader": {
            "image": "ghcr.io/example/grader@sha256:" + "5" * 64,
            "command": ["python", "/grader/run.py"],
            "network": {"mode": "none", "allowed_destinations": []},
            "budget_limits": budget_limits(
                wall_time_ms=60_000,
                cpu_time_ms=60_000,
                model_input_tokens=0,
                model_output_tokens=0,
                model_total_tokens=0,
                model_calls=0,
                cost_microusd=0,
                tool_calls=0,
            ),
            "hidden_inputs_digest": digest("6"),
            "result_schema_digest": digest("7"),
            "score_scale": {"possible": 100, "unit": "points"},
            "replay_runs": 3,
        },
        "validation_evidence": evidence,
        "protocol_range": {"minimum": 1, "maximum": 1},
        "license": {"spdx": "MIT", "redistribution": "allowed"},
    }
    prompt_text = "Repair main.py and verify the result."
    request = {
        "schema": "atv.trial-request/v1",
        "protocol_version": 1,
        "benchmark_release": "ATV-2026.09",
        "track": "controlled",
        "run_id": "run-0001",
        "trial_id": "trial-0001",
        "attempt_id": "attempt-0001",
        "schedule_id": "schedule-0001",
        "task_set": {
            "id": "pilot-tasks",
            "version": "1.0.0",
            "manifest_digest": digest("8"),
        },
        "issued_at": TIMESTAMP,
        "expires_at": "2026-07-19T13:00:00Z",
        "nonce": "abcdefghijklmnopqrstuvwxyzABCDEF0123456789_-",
        "task": {
            "id": task["id"],
            "version": task["version"],
            "manifest_digest": canonical_digest(task),
        },
        "harness": {
            "id": harness["id"],
            "version": harness["version"],
            "manifest_digest": canonical_digest(harness),
        },
        "model_policy": {
            "id": "controlled-gpt",
            "version": "1.0.0",
            "policy_digest": digest("9"),
            "allowed_models": ["provider/model-snapshot"],
            "parameters_digest": digest("a"),
            "retry_policy_digest": digest("b"),
            "subagent_policy_digest": None,
            "gateway": "model-gateway.internal:443",
        },
        "workspace": {
            "path": "/workspace",
            "artifacts_path": "/artifacts",
            "clean": True,
            "base_tree_digest": task["source"]["tree_digest"],
        },
        "prompt": {
            "text": prompt_text,
            "encoding": "utf-8",
            "digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(prompt_text.encode("utf-8")),
            },
        },
        "budget_limits": budget_limits(),
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
            "network": {
                "mode": "model-gateway-only",
                "allowed_destinations": ["model-gateway.internal:443"],
            },
            "writable_paths": ["/workspace", "/artifacts"],
            "credentials": [
                {
                    "name": "MODEL_BROKER_TOKEN",
                    "handle": "atv-credential://trial-0001/model",
                }
            ],
        },
        "seed": 42,
        "order_assignment": {
            "block": 0,
            "repetition": 0,
            "position": 1,
            "side": "none",
            "worker_class": "linux-amd64",
        },
        "output": deepcopy(task["output"]),
        "required_capabilities": capabilities(
            subagents=False,
            model_events=False,
            tool_events=False,
            model_selection="single",
        ),
        "forbidden_capabilities": ["browser"],
    }
    hello = {
        "schema": "atv.event/v1",
        "type": "hello",
        "source": "harness",
        "protocol_version": 1,
        "trial_id": request["trial_id"],
        "attempt_id": request["attempt_id"],
        "sequence": 0,
        "emitted_at": TIMESTAMP,
        "recorded_at": TIMESTAMP,
        "supported_protocol_versions": [1],
        "capabilities": deepcopy(harness["capabilities"]),
        "harness": deepcopy(request["harness"]),
    }
    negotiation = negotiate_capabilities(harness, request, hello)
    accepted = build_accepted_event(
        request,
        negotiation,
        emitted_at="2026-07-19T12:00:01Z",
    )
    artifact_bytes = b"print('ok')\n"
    artifact = {
        "path": "main.py",
        "media_type": "text/x-python",
        "size_bytes": len(artifact_bytes),
        "digest": {
            "algorithm": "sha256",
            "value": sha256_bytes(artifact_bytes),
        },
        "role": "primary",
    }
    reported_usage = usage()
    harness_result = {
        "schema": "atv.harness-result/v1",
        "status": "completed",
        "exit": {
            "code": 0,
            "signal": None,
            "timed_out": False,
            "cancelled": False,
        },
        "output_tree_digest": digest("c"),
        "artifacts": [deepcopy(artifact)],
        "reported_usage": deepcopy(reported_usage),
        "failure": None,
    }
    events = [
        hello,
        accepted,
        {
            "schema": "atv.event/v1",
            "type": "status",
            "source": "harness",
            "protocol_version": 1,
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "sequence": 2,
            "emitted_at": "2026-07-19T12:00:02Z",
            "recorded_at": "2026-07-19T12:00:02Z",
            "status": "running",
            "detail_code": "editing",
        },
        {
            "schema": "atv.event/v1",
            "type": "usage",
            "source": "harness",
            "protocol_version": 1,
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "sequence": 3,
            "emitted_at": "2026-07-19T12:00:08Z",
            "recorded_at": "2026-07-19T12:00:08Z",
            "cumulative_reported": deepcopy(reported_usage),
        },
        {
            "schema": "atv.event/v1",
            "type": "artifact",
            "source": "harness",
            "protocol_version": 1,
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "sequence": 4,
            "emitted_at": "2026-07-19T12:00:09Z",
            "recorded_at": "2026-07-19T12:00:09Z",
            "artifact": deepcopy(artifact),
        },
        {
            "schema": "atv.event/v1",
            "type": "result",
            "source": "harness",
            "protocol_version": 1,
            "trial_id": request["trial_id"],
            "attempt_id": request["attempt_id"],
            "sequence": 5,
            "emitted_at": "2026-07-19T12:00:10Z",
            "recorded_at": "2026-07-19T12:00:10Z",
            "harness_result": harness_result,
        },
    ]
    harness_events = []
    for harness_sequence, canonical_event in enumerate(
        event for event in events if event["source"] == "harness"
    ):
        raw_event = deepcopy(canonical_event)
        raw_event["schema"] = "atv.harness-event/v1"
        raw_event["harness_sequence"] = harness_sequence
        raw_event.pop("source")
        raw_event.pop("sequence")
        raw_event.pop("recorded_at")
        harness_events.append(raw_event)
    request_descriptor = document(
        "atv.trial-request/v1", "trial/request.json", "d"
    )
    event_descriptor = {
        "schema": "atv.event/v1",
        "path": "trial/events.jsonl",
        "media_type": "application/x-ndjson",
        "size_bytes": len(canonical_jsonl(events)),
        "digest": {
            "algorithm": "sha256",
            "value": sha256_bytes(canonical_jsonl(events)),
        },
    }
    harness_result_descriptor = document(
        "atv.harness-result/v1", "trial/harness-result.json", "e"
    )
    receipt = document("atv.model-receipt/v1", "receipts/model-1.json", "f")
    grader_identity = {
        "id": "reference-grader",
        "version": "1.0.0",
        "manifest_digest": digest("0"),
    }
    trial_result = {
        "schema": "atv.trial-result/v1",
        "protocol_version": 1,
        "benchmark_release": request["benchmark_release"],
        "track": request["track"],
        "run_id": request["run_id"],
        "trial_id": request["trial_id"],
        "attempt_id": request["attempt_id"],
        "task_set": deepcopy(request["task_set"]),
        "task": deepcopy(request["task"]),
        "harness": deepcopy(request["harness"]),
        "model_policy": deepcopy(request["model_policy"]),
        "trust_tier": "local-self-attested",
        "rankable": False,
        "status": "success",
        "failure": None,
        "protocol": {
            "request": request_descriptor,
            "event_stream": event_descriptor,
            "harness_result": harness_result_descriptor,
        },
        "execution": {
            "runner": {
                "id": "atv-local-runner",
                "version": "1.0.0",
                "manifest_digest": digest("1"),
            },
            "platform": {"os": "linux", "architecture": "amd64"},
            "runtime_digest": digest("2"),
            "started_at": TIMESTAMP,
            "ended_at": "2026-07-19T12:00:10Z",
            "duration_ms": 10_000,
            "exit": deepcopy(harness_result["exit"]),
        },
        "output_tree_digest": deepcopy(harness_result["output_tree_digest"]),
        "artifacts": [deepcopy(artifact)],
        "usage": {
            "reported": deepcopy(reported_usage),
            "observed": usage(cost_microusd=None),
            "authoritative": usage(),
        },
        "models": [
            {
                "requested": "provider/model-snapshot",
                "gateway_resolved": "provider/model-snapshot",
                "provider_reported": "provider/model-snapshot",
                "provider": "provider",
                "request_ids": ["request-0001", "request-0002"],
                "receipt": receipt,
            }
        ],
        "evaluation": {
            "state": "completed",
            "task_outcome": "pass",
            "task_success": True,
            "score": {"earned": 100, "possible": 100, "unit": "points"},
            "metrics": [
                {
                    "name": "tests-passed",
                    "numerator": 10,
                    "denominator": 10,
                    "unit": "tests",
                }
            ],
            "grader": {
                "identity": grader_identity,
                "image_digest": digest("3"),
            },
            "raw_result_digest": digest("4"),
        },
        "retry": {
            "attempt_index": 0,
            "prior_attempt_ids": [],
            "reason": None,
        },
        "attestations": [],
    }
    contents = {
        "harness_manifest": {
            **document("atv.harness/v1", "manifests/harness.json", "5"),
            "digest": canonical_digest(harness),
        },
        "task_manifest": {
            **document("atv.task/v1", "manifests/task.json", "6"),
            "digest": canonical_digest(task),
        },
        "trial_request": {
            **request_descriptor,
            "digest": canonical_digest(request),
        },
        "event_stream": event_descriptor,
        "harness_result": harness_result_descriptor,
        "trial_result": {
            **document("atv.trial-result/v1", "trial/result.json", "7"),
            "digest": canonical_digest(trial_result),
        },
        "output_tree": {
            "path": "artifacts/output-tree.tar.zst",
            "media_type": "application/zstd",
            "size_bytes": 1,
            "digest": deepcopy(harness_result["output_tree_digest"]),
            "role": "output-tree",
        },
        "grader_result": document(
            "atv.grader-result/v1", "grader/result.json", "8"
        ),
        "artifacts": [deepcopy(artifact)],
        "logs": [],
        "model_receipts": [receipt],
        "attestations": [],
    }
    bundle = {
        "schema": "atv.bundle/v1",
        "bundle_id": "bundle-0001",
        "created_at": "2026-07-19T12:01:00Z",
        "trust_tier": "local-self-attested",
        "canonicalization": "atv.canonical-json/v1",
        "hash_algorithm": "sha256",
        "run_id": request["run_id"],
        "trial_id": request["trial_id"],
        "attempt_id": request["attempt_id"],
        "contents": contents,
        "contents_digest": canonical_digest(contents),
        "runner": deepcopy(trial_result["execution"]["runner"]),
        "platform": {"os": "linux", "architecture": "amd64"},
    }
    return {
        "harness": harness,
        "task": task,
        "request": request,
        "hello": hello,
        "accepted": accepted,
        "artifact": artifact,
        "artifact_bytes": artifact_bytes,
        "harness_result": harness_result,
        "trial_result": trial_result,
        "events": events,
        "harness_events": harness_events,
        "bundle": bundle,
    }
