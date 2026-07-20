"""Canonical protocol export, trust gating, status mapping, and tamper tests."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from atv_bench.eval._canonical import (
    canonical_json_bytes as relaxed_json_bytes,
    sha256_json,
    snapshot_regular_tree,
)
from atv_bench.eval.grader import (
    ControllerAssertedLifecycleReceipt,
    FileAssertionsGrader,
    GradingStateError,
    OfficialGradingContext,
    TrustedRunnerLifecycleReceipt,
    VerifiedRunnerLifecycleReceipt,
)
from atv_bench.eval.protocol_export import (
    HARNESS_STATUS_MAP,
    INFRASTRUCTURE_STATUS_MAP,
    AttestationEvidence,
    EvidenceArtifact,
    EvidenceDocument,
    GraderEvidence,
    ModelEvidence,
    ProtocolExportError,
    ProtocolExportEvidence,
    RunnerEvidence,
    budget_analysis_id,
    export_protocol_bundle as _export_protocol_bundle,
    model_policy_analysis_id,
    official_result_subject_digest,
    output_tree_evidence,
    protocol_trial_status,
    verify_public_protocol_export,
)
from atv_bench.security.signing import (
    AttestationRole,
    Ed25519StatementSigner,
    OfficialBindings,
    OfficialTrustPolicy,
    SignedDsseEnvelope,
    TrustedEd25519Key,
    build_official_statement,
)
from atv_bench.eval.stats import (
    Decision,
    EvaluationQualityEvidence,
    PublicationPolicy,
    TrialObservation,
    analyze_paired,
)
from atv_bench.eval.tasks import TaskGate, TaskPackage
from atv_bench.eval.trial import (
    Budget,
    BudgetProfile,
    HarnessRef,
    HarnessStatus,
    InfrastructureStatus,
    ModelPolicyRef,
    TaskRef,
    TrialAttempt,
    TrialOutcome,
    TrialSpec,
)
from atv_bench.protocol import (
    IntegrityError,
    SchemaKind,
    TrialStatus,
    canonical_digest,
    canonical_json_bytes,
    default_schema_store,
    sha256_bytes,
    verify_bundle_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
SMOKE_TASK = ROOT / "tasks" / "smoke" / "repair_config"
TIMESTAMP = "2026-07-19T12:00:00Z"
_POLICIES: dict[tuple[str, str], OfficialTrustPolicy] = {}


def export_protocol_bundle(**kwargs):
    evidence = kwargs["evidence"]
    if (
        evidence.trust_tier == "official-attested"
        and kwargs.get("official_trust_policy") is None
    ):
        key = (kwargs["spec"].trial_id, kwargs["attempt"].attempt_id)
        kwargs["official_trust_policy"] = _POLICIES.get(key)
    return _export_protocol_bundle(**kwargs)


def _digest(character: str) -> dict[str, str]:
    return {"algorithm": "sha256", "value": character * 64}


def _versioned_ref(id_: str, version: str, character: str) -> dict[str, object]:
    return {
        "id": id_,
        "version": version,
        "manifest_digest": _digest(character),
    }


def _capabilities() -> dict[str, object]:
    return {
        "workspace_edit": True,
        "subagents": False,
        "resumable": False,
        "browser": False,
        "model_events": True,
        "tool_events": True,
        "usage_events": True,
        "checkpoint_events": False,
        "model_selection": "single",
        "token_usage_reporting": "reported",
        "call_usage_reporting": "reported",
        "cost_usage_reporting": "reported",
    }


def _usage(*, model_free: bool = False) -> dict[str, int]:
    return {
        "wall_time_ms": 10_000,
        "cpu_time_ms": 5_000,
        "model_input_tokens": 0 if model_free else 1_200,
        "model_output_tokens": 0 if model_free else 800,
        "model_total_tokens": 0 if model_free else 2_000,
        "model_calls": 0 if model_free else 2,
        "cost_microusd": 0 if model_free else 25_000,
        "tool_calls": 8,
        "memory_bytes": 128_000_000,
        "storage_bytes": 1_024,
        "pids": 4,
        "stdout_bytes": 512,
        "stderr_bytes": 64,
        "artifact_bytes": 64,
    }


def _harness_manifest() -> dict[str, object]:
    return {
        "schema": "atv.harness/v1",
        "id": "harness-a",
        "version": "1.0.0",
        "display_name": "Synthetic Harness A",
        "runtime": {
            "kind": "process",
            "command": ["synthetic-harness", "--atv-run"],
            "working_directory": "/workspace",
            "executable_digest": _digest("a"),
        },
        "protocol": {
            "minimum_version": 1,
            "maximum_version": 1,
            "input": "stdin-json",
            "output": "stdout-jsonl",
        },
        "capabilities": _capabilities(),
        "security": {
            "env_allowlist": ["MODEL_BROKER_TOKEN"],
            "network_requirement": "model-gateway-only",
            "writable_paths": ["/workspace", "/artifacts"],
            "requires_tty": False,
        },
        "metadata": {
            "source": {
                "repository": "https://github.com/example/harness-a",
                "revision": "0123456789abcdef",
                "tree_digest": _digest("b"),
            },
            "license": "MIT",
        },
    }


@dataclass(frozen=True, slots=True)
class _Fixture:
    package: TaskPackage
    spec: TrialSpec
    attempt: TrialAttempt
    outcome: TrialOutcome
    grade: object
    analysis: object
    evidence: ProtocolExportEvidence
    trust_policy: OfficialTrustPolicy | None
    signers: dict[AttestationRole, Ed25519StatementSigner] | None


def _task_ids(package_id: str, count: int) -> list[str]:
    return [package_id, *[f"synthetic-task-{index:03d}" for index in range(1, count)]]


def _analysis(
    *,
    package_id: str,
    task_count: int,
    repetitions: int,
    official: bool,
    model_policy_id: str = "controlled-model",
    budget_profile_id: str = "equal-cost",
    direction: str = "a_better",
):
    if direction == "a_better":
        scores = (("harness-a", 0.8), ("harness-b", 0.5))
    elif direction == "b_better":
        scores = (("harness-a", 0.5), ("harness-b", 0.8))
    elif direction == "equivalent":
        scores = (("harness-a", 0.65), ("harness-b", 0.65))
    else:
        raise ValueError(f"unsupported synthetic direction: {direction}")
    rows: list[TrialObservation] = []
    task_ids = _task_ids(package_id, task_count)
    for task_id in task_ids:
        for repetition in range(repetitions):
            for harness_id, score in scores:
                rows.append(
                    TrialObservation(
                        trial_id=hashlib.sha256(
                            f"{task_id}|{harness_id}|{repetition}".encode()
                        ).hexdigest(),
                        task_id=task_id,
                        harness_id=harness_id,
                        model_policy_id=model_policy_id,
                        budget_profile_id=budget_profile_id,
                        repetition=repetition,
                        infrastructure_status=InfrastructureStatus.OK,
                        harness_status=HarnessStatus.COMPLETED,
                        score=score,
                    )
                )
    if official:
        quality = EvaluationQualityEvidence(
            task_eligibility=tuple((task_id, True) for task_id in task_ids),
            schedule_balanced=True,
            grader_replay_count=10_000,
            grader_nondeterministic_count=0,
        )
        policy = PublicationPolicy.official()
    else:
        quality = None
        policy = PublicationPolicy.simulation()
    return analyze_paired(
        rows,
        harness_a="harness-a",
        harness_b="harness-b",
        equivalence_margin=0.05,
        bootstrap_samples=1_000,
        seed=19,
        publication_policy=policy,
        quality_evidence=quality,
    )


def _trust_material():
    signers = {role: Ed25519StatementSigner.generate() for role in AttestationRole}
    keys = tuple(
        TrustedEd25519Key.from_signer(
            signer,
            roles=(role,),
            valid_from="2026-07-01T00:00:00Z",
        )
        for role, signer in signers.items()
    )
    policy = OfficialTrustPolicy(
        keys=keys,
        role_key_ids={role: (signers[role].key_id,) for role in AttestationRole},
        verification_time="2026-07-19T12:30:00Z",
    )
    return signers, policy


def _attestations(
    *,
    bindings: OfficialBindings,
    signers: dict[AttestationRole, Ed25519StatementSigner],
    execution_envelope,
    evaluation_envelope,
    model_free: bool,
) -> tuple[AttestationEvidence, ...]:
    roles = [
        AttestationRole.ADMISSION,
        AttestationRole.HARNESS_BUILD,
        AttestationRole.EXECUTION,
        AttestationRole.EVALUATION,
    ]
    if not model_free:
        roles.append(AttestationRole.MODEL)
    rows = []
    for role in roles:
        if role is AttestationRole.EXECUTION:
            envelope = execution_envelope
        elif role is AttestationRole.EVALUATION:
            envelope = evaluation_envelope
        else:
            claims = (
                {
                    "internal_operator_evidence": {
                        "algorithm": "HMAC-SHA256",
                        "integrity_only": True,
                        "signature": "synthetic-internal-hmac",
                    }
                }
                if role is AttestationRole.MODEL
                else {}
            )
            envelope = signers[role].sign_statement(
                build_official_statement(
                    role=role,
                    bindings=bindings,
                    issued_at=TIMESTAMP,
                    claims=claims,
                )
            )
        rows.append(
            AttestationEvidence(
                role=role.value,
                document=EvidenceDocument.from_protocol_json(
                    schema="dsse.envelope/v1",
                    path=f"attestations/{role.value}.json",
                    value=envelope.to_dict(),
                ),
            )
        )
    return tuple(rows)


def _fixture(
    *,
    official: bool,
    task_count: int,
    repetitions: int = 5,
    model_free: bool = False,
    model_policy_id: str = "controlled-model",
    model_policy_version: str = "1.0.0",
    model_policy_digest_character: str = "9",
    model_policy_digest: str | None = None,
    budget_profile_id: str = "equal-cost",
    budget_max_cost_microusd: int = 500_000,
    direction: str = "a_better",
    track: str = "controlled",
) -> _Fixture:
    package = TaskPackage.load(SMOKE_TASK)
    task_manifest = package.manifest
    harness_manifest = _harness_manifest()
    task_digest = canonical_digest(task_manifest)["value"]
    harness_digest = canonical_digest(harness_manifest)["value"]
    resolved_model_policy_digest = (
        model_policy_digest
        if model_policy_digest is not None
        else model_policy_digest_character * 64
    )
    model_policy = {
        "id": model_policy_id,
        "version": model_policy_version,
        "policy_digest": {
            "algorithm": "sha256",
            "value": resolved_model_policy_digest,
        },
        "allowed_models": ["provider/model-snapshot"],
        "parameters_digest": _digest("a"),
        "retry_policy_digest": _digest("b"),
        "subagent_policy_digest": None,
        "gateway": "model-gateway.internal:443",
    }
    spec = TrialSpec(
        benchmark_release="ATV-2026.07.1",
        protocol_version="atv.trial/v1",
        schedule_id="1" * 64,
        task=TaskRef(package.id, package.version, task_digest),
        harness=HarnessRef("harness-a", "1.0.0", harness_digest),
        model_policy=ModelPolicyRef(
            model_policy_id,
            model_policy_version,
            model_policy["policy_digest"]["value"],
        ),
        budget_profile=BudgetProfile(
            budget_profile_id,
            Budget(
                wall_time_seconds=60,
                max_model_tokens=20_000,
                max_model_calls=20,
                max_cost_microusd=budget_max_cost_microusd,
            ),
        ),
        repetition=0,
        schedule_seed=19,
    )
    attempt = TrialAttempt(spec=spec, attempt_number=1, fresh_nonce="2" * 64)
    oracle = next(
        path
        for gate, _, path, _ in package.validation_cases()
        if gate is TaskGate.ORACLE
    )
    grader = FileAssertionsGrader.from_task(package)
    local_lifecycle = ControllerAssertedLifecycleReceipt.completed(
        controller_id="local-export-test"
    )
    local_grade = grader.grade(
        package,
        oracle,
        lifecycle_receipt=local_lifecycle,
    )
    snapshots = snapshot_regular_tree(oracle)
    tree_artifact = output_tree_evidence(snapshots)
    primary_snapshot = next(item for item in snapshots if item.path == "config.json")
    artifact = EvidenceArtifact(
        path="config.json",
        media_type="application/json",
        data=primary_snapshot.data,
        role="primary",
    )
    prompt_text = package.prompt_path.read_text(encoding="utf-8")
    usage = _usage(model_free=model_free)
    runner_exit = {
        "code": 0,
        "signal": None,
        "timed_out": False,
        "cancelled": False,
    }
    task_set = _versioned_ref("synthetic-task-set", "1.0.0", "c")
    request = {
        "schema": "atv.trial-request/v1",
        "protocol_version": 1,
        "benchmark_release": spec.benchmark_release,
        "track": track,
        "run_id": "run-protocol-export",
        "trial_id": spec.trial_id,
        "attempt_id": attempt.attempt_id,
        "schedule_id": spec.schedule_id,
        "task_set": task_set,
        "issued_at": TIMESTAMP,
        "expires_at": "2026-07-19T13:00:00Z",
        "nonce": "abcdefghijklmnopqrstuvwxyzABCDEF0123456789_-",
        "task": {
            "id": spec.task.id,
            "version": spec.task.version,
            "manifest_digest": _digest(spec.task.digest[0]) | {
                "value": spec.task.digest
            },
        },
        "harness": {
            "id": spec.harness.id,
            "version": spec.harness.version,
            "manifest_digest": _digest(spec.harness.digest[0]) | {
                "value": spec.harness.digest
            },
        },
        "model_policy": model_policy,
        "workspace": {
            "path": "/workspace",
            "artifacts_path": "/artifacts",
            "clean": True,
            "base_tree_digest": deepcopy(task_manifest["source"]["tree_digest"]),
        },
        "prompt": {
            "text": prompt_text,
            "encoding": "utf-8",
            "digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(prompt_text.encode("utf-8")),
            },
        },
        "budget_limits": deepcopy(task_manifest["budget_limits"]),
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
            "tools": {"allowed": ["editor", "shell"], "denied": ["browser"]},
            "network": {
                "mode": "model-gateway-only",
                "allowed_destinations": ["model-gateway.internal:443"],
            },
            "writable_paths": ["/workspace", "/artifacts"],
            "credentials": [
                {
                    "name": "MODEL_BROKER_TOKEN",
                    "handle": f"atv-credential://{spec.trial_id}/model",
                }
            ],
        },
        "seed": spec.schedule_seed,
        "order_assignment": {
            "block": 0,
            "repetition": spec.repetition,
            "position": 0,
            "side": "none",
            "worker_class": "linux-amd64",
        },
        "output": deepcopy(task_manifest["output"]),
        "required_capabilities": _capabilities(),
        "forbidden_capabilities": ["browser"],
    }
    request["budget_limits"]["cost_microusd"] = budget_max_cost_microusd
    analysis = _analysis(
        package_id=package.id,
        task_count=task_count,
        repetitions=repetitions,
        official=official,
        model_policy_id=model_policy_analysis_id(model_policy),
        budget_profile_id=budget_analysis_id(
            budget_profile_id,
            request["budget_limits"],
        ),
        direction=direction,
    )
    harness_result = {
        "schema": "atv.harness-result/v1",
        "status": "completed",
        "exit": runner_exit,
        "output_tree_digest": _digest(local_grade.output_tree_digest[0]) | {
            "value": local_grade.output_tree_digest
        },
        "artifacts": [artifact.descriptor],
        "reported_usage": usage,
        "failure": None,
    }
    grader_evidence = GraderEvidence(
        identity=_versioned_ref("reference-grader", "1.0.0", "f"),
        image_digest=task_manifest["grader"]["image"].split("@sha256:", 1)[1],
    )
    signers = None
    trust_policy = None
    bindings = None
    if official:
        signers, trust_policy = _trust_material()
        bindings = OfficialBindings(
            benchmark_release=spec.benchmark_release,
            trial_id=spec.trial_id,
            attempt_id=attempt.attempt_id,
            task_digest=spec.task.digest,
            harness_digest=spec.harness.digest,
            model_digest=spec.model_policy.digest,
            budget_digest=canonical_digest(request["budget_limits"])["value"],
            runner_digest="e" * 64,
            grader_digest=local_grade.grader_digest,
            grader_image_digest=grader_evidence.image_digest,
            output_digest=local_grade.output_tree_digest,
            result_digest=official_result_subject_digest(
                request=request,
                harness_result=harness_result,
            ),
        )
        execution_envelope = signers[AttestationRole.EXECUTION].sign_statement(
            build_official_statement(
                role=AttestationRole.EXECUTION,
                bindings=bindings,
                issued_at=TIMESTAMP,
                claims={
                    "execution_complete": True,
                    "credentials_destroyed": True,
                    "hidden_inputs_mounted_after_exit": True,
                },
            )
        )
        lifecycle = VerifiedRunnerLifecycleReceipt.from_execution_envelope(
            envelope=execution_envelope,
            trust_policy=trust_policy,
            bindings=bindings,
        )
        grade = grader.grade(
            package,
            oracle,
            lifecycle_receipt=lifecycle,
            official_context=OfficialGradingContext(
                signer=signers[AttestationRole.EVALUATION],
                trust_policy=trust_policy,
                bindings=bindings,
                issued_at=TIMESTAMP,
            ),
        )
        lifecycle_payload = lifecycle.lifecycle_payload()
        _POLICIES[(spec.trial_id, attempt.attempt_id)] = trust_policy
    else:
        lifecycle = local_lifecycle
        grade = local_grade
        lifecycle_payload = {
            "schema": "atv.controller-lifecycle-assertion/v1",
            "controller_id": lifecycle.controller_id,
            "execution_complete": lifecycle.execution_complete,
            "trust_tier": lifecycle.trust_tier.value,
            "official_verified": False,
        }
    lifecycle_document = EvidenceDocument.from_protocol_json(
        schema="atv.runner-lifecycle/v1",
        path="runner/lifecycle.json",
        value=lifecycle_payload,
    )
    runner = RunnerEvidence(
        run_id=request["run_id"],
        track=request["track"],
        task_set=task_set,
        identity=_versioned_ref("atv-runner", "1.0.0", "d"),
        platform={"os": "linux", "architecture": "amd64"},
        runtime_digest="e" * 64,
        started_at=TIMESTAMP,
        ended_at="2026-07-19T12:00:10Z",
        duration_ms=10_000,
        exit=runner_exit,
        reported_usage=usage,
        observed_usage=usage,
        authoritative_usage=usage,
        lifecycle_receipt=lifecycle,
        lifecycle_document=lifecycle_document,
    )
    receipt = EvidenceDocument.from_protocol_json(
        schema="atv.model-receipt/v1",
        path="receipts/model-1.json",
        value={
            "schema": "atv.model-receipt/v1",
            "trial_id": spec.trial_id,
            "attempt_id": attempt.attempt_id,
            "requested_model": "provider/model-snapshot",
            "resolved_model": "provider/model-snapshot",
            "provider_reported": "provider/model-snapshot",
            "provider": "provider",
            "request_ids": ["request-0001", "request-0002"],
        },
    )
    models = (
        ()
        if model_free
        else (
            ModelEvidence(
                requested="provider/model-snapshot",
                gateway_resolved="provider/model-snapshot",
                provider_reported="provider/model-snapshot",
                provider="provider",
                request_ids=("request-0001", "request-0002"),
                receipt=receipt,
            ),
        )
    )
    outcome = TrialOutcome(
        trial_id=spec.trial_id,
        attempt_id=attempt.attempt_id,
        infrastructure_status=InfrastructureStatus.OK,
        harness_status=HarnessStatus.COMPLETED,
        score=grade.score,
    )
    evidence = ProtocolExportEvidence(
        trust_tier="official-attested" if official else "local-self-attested",
        created_at="2026-07-19T12:01:00Z",
        harness_manifest=harness_manifest,
        task_manifest=task_manifest,
        trial_request=request,
        event_stream=EvidenceDocument(
            schema="atv.event/v1",
            path="trial/events.jsonl",
            media_type="application/x-ndjson",
            data=b'{"type":"synthetic"}\n',
        ),
        harness_result=harness_result,
        runner=runner,
        models=models,
        grader=grader_evidence,
        attestations=(
            _attestations(
                bindings=bindings,
                signers=signers,
                execution_envelope=lifecycle.execution_envelope,
                evaluation_envelope=grade.evaluation_envelope,
                model_free=model_free,
            )
            if official
            else ()
        ),
        output_tree=tree_artifact,
        artifacts=(artifact,),
        model_free=model_free,
    )
    return _Fixture(
        package,
        spec,
        attempt,
        outcome,
        grade,
        analysis,
        evidence,
        trust_policy,
        signers,
    )


def _rewrite_document(
    bundle,
    documents,
    descriptor,
    value,
    *,
    relaxed: bool = False,
):
    data = (
        relaxed_json_bytes(value)
        if relaxed
        else canonical_json_bytes(value)
    )
    documents[descriptor["path"]] = data
    descriptor["size_bytes"] = len(data)
    descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(data),
    }


def _finish_bundle_rewrite(bundle):
    bundle["contents_digest"] = canonical_digest(bundle["contents"])
    bundle["bundle_id"] = (
        "bundle-" + bundle["contents_digest"]["value"][:32]
    )


def test_status_maps_are_total_and_match_every_protocol_branch():
    assert set(INFRASTRUCTURE_STATUS_MAP) == set(InfrastructureStatus)
    assert set(HARNESS_STATUS_MAP) == set(HarnessStatus)
    mapped = {
        status
        for status in (
            *INFRASTRUCTURE_STATUS_MAP.values(),
            *HARNESS_STATUS_MAP.values(),
        )
        if status is not None
    } | {TrialStatus.SUCCESS, TrialStatus.PARTIAL, TrialStatus.TASK_FAILED}
    assert mapped == set(TrialStatus)
    expected_infrastructure = {
        InfrastructureStatus.SETUP_FAILED: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.RUNNER_FAILED: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.MODEL_GATEWAY_FAILED: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.GRADER_FAILED: TrialStatus.GRADER_FAILED,
        InfrastructureStatus.ARTIFACT_CORRUPT: TrialStatus.INFRASTRUCTURE_ERROR,
        InfrastructureStatus.CANCELLED: TrialStatus.CANCELLED,
    }
    fixture = _fixture(official=False, task_count=2)
    for infrastructure, expected in expected_infrastructure.items():
        outcome = TrialOutcome(
            trial_id=fixture.spec.trial_id,
            attempt_id=fixture.attempt.attempt_id,
            infrastructure_status=infrastructure,
            harness_status=HarnessStatus.NOT_RUN,
            score=None,
            reason_code=infrastructure.value,
        )
        assert protocol_trial_status(outcome, None) is expected

    for harness, expected in HARNESS_STATUS_MAP.items():
        if harness in {HarnessStatus.NOT_RUN, HarnessStatus.COMPLETED}:
            continue
        outcome = TrialOutcome(
            trial_id=fixture.spec.trial_id,
            attempt_id=fixture.attempt.attempt_id,
            infrastructure_status=InfrastructureStatus.OK,
            harness_status=harness,
            score=0.0,
            reason_code=harness.value,
        )
        assert protocol_trial_status(outcome, None) is expected

    assert protocol_trial_status(fixture.outcome, fixture.grade) is TrialStatus.SUCCESS
    partial_grade = replace(
        fixture.grade,
        passed=False,
        score=0.5,
        result_digest=sha256_json(
            {
                **fixture.grade.payload_dict(),
                "passed": False,
                "score": 0.5,
            }
        ),
    )
    partial_outcome = replace(fixture.outcome, score=0.5)
    assert (
        protocol_trial_status(partial_outcome, partial_grade)
        is TrialStatus.PARTIAL
    )
    failed_grade = replace(
        fixture.grade,
        passed=False,
        score=0.0,
        result_digest=sha256_json(
            {
                **fixture.grade.payload_dict(),
                "passed": False,
                "score": 0.0,
            }
        ),
    )
    failed_outcome = replace(fixture.outcome, score=0.0)
    assert (
        protocol_trial_status(failed_outcome, failed_grade)
        is TrialStatus.TASK_FAILED
    )


def test_local_export_round_trips_as_unrankable_protocol_source_of_truth():
    fixture = _fixture(official=False, task_count=2)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    result = exported.trial_result
    assert result["trust_tier"] == "local-self-attested"
    assert result["rankable"] is False
    assert result["status"] == "success"
    assert exported.bundle["contents"]["attestations"] == []
    default_schema_store().validate(result, SchemaKind.TRIAL_RESULT)
    verify_bundle_manifest(exported.bundle)
    round_tripped_bundle = json.loads(json.dumps(exported.bundle))
    verified = verify_public_protocol_export(
        round_tripped_bundle,
        dict(exported.documents),
    )
    assert verified == result


def test_local_export_cannot_smuggle_official_attestation_roles():
    fixture = _fixture(official=False, task_count=2)
    official = _fixture(official=True, task_count=50)
    evidence = replace(
        fixture.evidence,
        attestations=official.evidence.attestations,
    )
    with pytest.raises(ProtocolExportError, match="local exports"):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=evidence,
        )


def test_protocol_bundle_and_document_tampering_fail_closed():
    fixture = _fixture(official=False, task_count=2)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    tampered_bundle = deepcopy(exported.bundle)
    tampered_bundle["contents"]["artifacts"][0]["size_bytes"] += 1
    with pytest.raises(IntegrityError):
        verify_public_protocol_export(tampered_bundle, exported.documents)

    tampered_documents = dict(exported.documents)
    tampered_documents["trial/result.json"] += b" "
    with pytest.raises(ProtocolExportError, match="digest mismatch|size mismatch"):
        verify_public_protocol_export(exported.bundle, tampered_documents)

    extra_documents = dict(exported.documents)
    extra_documents["unreferenced.json"] = b"{}"
    with pytest.raises(ProtocolExportError, match="document set mismatch"):
        verify_public_protocol_export(exported.bundle, extra_documents)


def test_public_verifier_rejects_tampered_official_status_and_score_with_old_dsse():
    fixture = _fixture(official=True, task_count=50)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    bundle = deepcopy(exported.bundle)
    documents = dict(exported.documents)
    original_attestations = {
        path: data
        for path, data in documents.items()
        if path.startswith("attestations/")
    }
    descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[descriptor["path"]])
    result["status"] = "task_failed"
    result["evaluation"]["task_outcome"] = "fail"
    result["evaluation"]["task_success"] = False
    result["evaluation"]["score"]["earned"] = 0
    _rewrite_document(bundle, documents, descriptor, result)
    _finish_bundle_rewrite(bundle)

    assert {
        path: data
        for path, data in documents.items()
        if path.startswith("attestations/")
    } == original_attestations
    with pytest.raises(
        ProtocolExportError,
        match="authoritative grader result",
    ):
        verify_public_protocol_export(
            bundle,
            documents,
            official_trust_policy=fixture.trust_policy,
        )


def test_public_verifier_recomputes_grade_core_digest():
    fixture = _fixture(official=True, task_count=50)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    bundle = deepcopy(exported.bundle)
    documents = dict(exported.documents)
    grader_descriptor = bundle["contents"]["grader_result"]
    grader_result = json.loads(documents[grader_descriptor["path"]])
    signed_grade_core_digest = grader_result["grade_core_digest"]
    grader_result["passed"] = False
    grader_result["score"] = 0.0
    grader_result["result_digest"] = sha256_json(
        {
            key: value
            for key, value in grader_result.items()
            if key != "result_digest"
        }
    )
    _rewrite_document(
        bundle,
        documents,
        grader_descriptor,
        grader_result,
        relaxed=True,
    )
    result_descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[result_descriptor["path"]])
    result["evaluation"]["raw_result_digest"] = deepcopy(
        grader_descriptor["digest"]
    )
    _rewrite_document(bundle, documents, result_descriptor, result)
    _finish_bundle_rewrite(bundle)

    assert grader_result["grade_core_digest"] == signed_grade_core_digest
    with pytest.raises(ProtocolExportError, match="grade_core_digest"):
        verify_public_protocol_export(
            bundle,
            documents,
            official_trust_policy=fixture.trust_policy,
        )


def test_public_verifier_rejects_fully_rehashed_score_forgery_with_old_signature():
    fixture = _fixture(official=True, task_count=50)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    bundle = deepcopy(exported.bundle)
    documents = dict(exported.documents)
    original_attestations = {
        path: data
        for path, data in documents.items()
        if path.startswith("attestations/")
    }

    grader_descriptor = bundle["contents"]["grader_result"]
    grader_result = json.loads(documents[grader_descriptor["path"]])
    grader_result["passed"] = False
    grader_result["score"] = 0.0
    for assertion in grader_result["assertions"]:
        assertion["passed"] = False
        assertion["detail"] = "forged-but-internally-consistent"
    core_fields = (
        "schema",
        "passed",
        "score",
        "pass_score",
        "assertions",
        "grader_digest",
        "output_tree_digest",
        "lifecycle_receipt_digest",
        "trust_tier",
        "official_verified",
    )
    grader_result["grade_core_digest"] = sha256_json(
        {field: grader_result[field] for field in core_fields}
    )
    grader_result["result_digest"] = sha256_json(
        {
            field: value
            for field, value in grader_result.items()
            if field != "result_digest"
        }
    )
    _rewrite_document(
        bundle,
        documents,
        grader_descriptor,
        grader_result,
        relaxed=True,
    )

    result_descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[result_descriptor["path"]])
    result["status"] = "task_failed"
    result["evaluation"]["task_outcome"] = "fail"
    result["evaluation"]["task_success"] = False
    result["evaluation"]["score"]["earned"] = 0
    result["evaluation"]["raw_result_digest"] = deepcopy(
        grader_descriptor["digest"]
    )
    _rewrite_document(bundle, documents, result_descriptor, result)
    _finish_bundle_rewrite(bundle)

    assert {
        path: data
        for path, data in documents.items()
        if path.startswith("attestations/")
    } == original_attestations
    with pytest.raises(
        ProtocolExportError,
        match="evaluation attestation did not verify",
    ):
        verify_public_protocol_export(
            bundle,
            documents,
            official_trust_policy=fixture.trust_policy,
        )


def test_public_verifier_rejects_consistent_run_rewrite_with_old_signed_subject():
    fixture = _fixture(official=True, task_count=50)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    bundle = deepcopy(exported.bundle)
    documents = dict(exported.documents)
    original_attestations = {
        path: data
        for path, data in documents.items()
        if path.startswith("attestations/")
    }
    forged_run_id = "forged-run-with-consistent-local-digests"

    request_descriptor = bundle["contents"]["trial_request"]
    request = json.loads(documents[request_descriptor["path"]])
    request["run_id"] = forged_run_id
    _rewrite_document(bundle, documents, request_descriptor, request)

    result_descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[result_descriptor["path"]])
    result["run_id"] = forged_run_id
    result["protocol"]["request"] = deepcopy(request_descriptor)
    _rewrite_document(bundle, documents, result_descriptor, result)

    bundle["run_id"] = forged_run_id
    _finish_bundle_rewrite(bundle)
    assert {
        path: data
        for path, data in documents.items()
        if path.startswith("attestations/")
    } == original_attestations
    with pytest.raises(
        ProtocolExportError,
        match="signed official bindings do not match canonical source documents",
    ):
        verify_public_protocol_export(
            bundle,
            documents,
            official_trust_policy=fixture.trust_policy,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_policy_id", "other-policy", "analysis model policy"),
        ("budget_profile_id", "other-budget", "analysis budget profile"),
    ],
)
def test_export_rejects_analysis_for_a_different_identity(
    field,
    value,
    message,
):
    fixture = _fixture(official=True, task_count=50)
    bad_analysis = replace(fixture.analysis, **{field: value})
    with pytest.raises(ProtocolExportError, match=message):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=bad_analysis,
            evidence=fixture.evidence,
        )


@pytest.mark.parametrize(
    ("source_kwargs", "target_kwargs", "message"),
    [
        (
            {
                "model_policy_id": "same-policy",
                "model_policy_digest_character": "1",
            },
            {
                "model_policy_id": "same-policy",
                "model_policy_digest_character": "2",
            },
            "analysis model policy identity",
        ),
        (
            {
                "budget_profile_id": "same-budget",
                "budget_max_cost_microusd": 500_000,
            },
            {
                "budget_profile_id": "same-budget",
                "budget_max_cost_microusd": 600_000,
            },
            "analysis budget profile identity",
        ),
    ],
)
def test_export_rejects_preexisting_analysis_reused_across_identity_change(
    source_kwargs,
    target_kwargs,
    message,
):
    source = _fixture(official=True, task_count=50, **source_kwargs)
    target = _fixture(official=True, task_count=50, **target_kwargs)

    with pytest.raises(ProtocolExportError, match=message):
        export_protocol_bundle(
            spec=target.spec,
            attempt=target.attempt,
            outcome=target.outcome,
            grade=target.grade,
            analysis=source.analysis,
            evidence=target.evidence,
        )


@pytest.mark.parametrize(
    ("source_kwargs", "target_kwargs"),
    [
        (
            {
                "model_policy_id": "policy-a",
                "model_policy_digest_character": "1",
            },
            {
                "model_policy_id": "policy-b",
                "model_policy_digest_character": "2",
            },
        ),
        (
            {
                "budget_profile_id": "budget-a",
                "budget_max_cost_microusd": 500_000,
            },
            {
                "budget_profile_id": "budget-b",
                "budget_max_cost_microusd": 600_000,
            },
        ),
    ],
)
def test_public_verifier_rejects_analysis_reused_across_identities(
    source_kwargs,
    target_kwargs,
):
    source = _fixture(official=True, task_count=50, **source_kwargs)
    source_export = export_protocol_bundle(
        spec=source.spec,
        attempt=source.attempt,
        outcome=source.outcome,
        grade=source.grade,
        analysis=source.analysis,
        evidence=source.evidence,
    )
    target = _fixture(official=True, task_count=50, **target_kwargs)
    target_export = export_protocol_bundle(
        spec=target.spec,
        attempt=target.attempt,
        outcome=target.outcome,
        grade=target.grade,
        analysis=target.analysis,
        evidence=target.evidence,
    )
    bundle = deepcopy(target_export.bundle)
    documents = dict(target_export.documents)
    analysis_descriptor = next(
        item
        for item in bundle["contents"]["logs"]
        if item["schema"] == "atv.paired-analysis/v1"
    )
    source_descriptor = next(
        item
        for item in source_export.bundle["contents"]["logs"]
        if item["schema"] == "atv.paired-analysis/v1"
    )
    source_data = source_export.documents[source_descriptor["path"]]
    documents[analysis_descriptor["path"]] = source_data
    analysis_descriptor["size_bytes"] = len(source_data)
    analysis_descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(source_data),
    }
    result_descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[result_descriptor["path"]])
    result["analysis"]["document"] = deepcopy(analysis_descriptor)
    _rewrite_document(bundle, documents, result_descriptor, result)
    _finish_bundle_rewrite(bundle)

    with pytest.raises(
        ProtocolExportError,
        match="different model-policy/budget identity",
    ):
        verify_public_protocol_export(
            bundle,
            documents,
            official_trust_policy=target.trust_policy,
        )


def test_public_verifier_rechecks_quality_gates_from_bundled_analysis():
    fixture = _fixture(official=True, task_count=50)
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    bundle = deepcopy(exported.bundle)
    documents = dict(exported.documents)
    descriptor = next(
        item
        for item in bundle["contents"]["logs"]
        if item["schema"] == "atv.paired-analysis/v1"
    )
    analysis = json.loads(documents[descriptor["path"]])
    analysis["publication_eligible"] = False
    analysis["publication_decision"] = "inconclusive"
    analysis["quality_gate_failures"] = [
        {"code": "tampered", "message": "tampered"}
    ]
    data = relaxed_json_bytes(analysis)
    documents[descriptor["path"]] = data
    descriptor["size_bytes"] = len(data)
    descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(data),
    }
    result_descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[result_descriptor["path"]])
    result["analysis"]["document"] = deepcopy(descriptor)
    _rewrite_document(bundle, documents, result_descriptor, result)
    _finish_bundle_rewrite(bundle)
    with pytest.raises(ProtocolExportError, match="publishable analysis"):
        verify_public_protocol_export(
            bundle,
            documents,
            official_trust_policy=fixture.trust_policy,
        )


def test_official_export_and_public_verifier_require_explicit_policy():
    fixture = _fixture(official=True, task_count=50)
    with pytest.raises(ProtocolExportError, match="OfficialTrustPolicy"):
        _export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=fixture.evidence,
        )
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    with pytest.raises(ProtocolExportError, match="OfficialTrustPolicy"):
        verify_public_protocol_export(
            json.loads(json.dumps(exported.bundle)),
            exported.documents,
        )


def test_unsigned_official_statements_and_fake_receipt_subclasses_fail():
    fixture = _fixture(official=True, task_count=50)
    unsigned = tuple(
        AttestationEvidence(
            role=item.role,
            document=EvidenceDocument.from_protocol_json(
                schema="in-toto.statement/v1",
                path=item.document.path,
                value=SignedDsseEnvelope.from_dict(
                    json.loads(item.document.data)
                ).statement(),
            ),
        )
        for item in fixture.evidence.attestations
    )
    with pytest.raises(ProtocolExportError, match="signed DSSE"):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=replace(fixture.evidence, attestations=unsigned),
        )

    class ForgedReceipt(TrustedRunnerLifecycleReceipt):
        execution_complete = True
        credentials_destroyed = True
        hidden_inputs_mounted_after_exit = True
        receipt_digest = "0" * 64

        def validate_for_grading(self):
            return None

    with pytest.raises(GradingStateError, match="VerifiedRunnerLifecycleReceipt"):
        FileAssertionsGrader.from_task(fixture.package).grade(
            fixture.package,
            next(
                path
                for gate, _, path, _ in fixture.package.validation_cases()
                if gate is TaskGate.ORACLE
            ),
            lifecycle_receipt=ForgedReceipt(),
            official_context=OfficialGradingContext(
                signer=fixture.signers[AttestationRole.EVALUATION],
                trust_policy=fixture.trust_policy,
                bindings=OfficialBindings(
                    **fixture.grade.evaluation_envelope.statement()[
                        "predicate"
                    ]["bindings"]
                ),
                issued_at=TIMESTAMP,
            ),
        )


def test_exporter_independently_reverifies_evaluation_envelope():
    fixture = _fixture(official=True, task_count=50)
    tampered = deepcopy(fixture.grade.evaluation_envelope.to_dict())
    tampered["signatures"][0]["sig"] = (
        "A" if tampered["signatures"][0]["sig"][0] != "A" else "B"
    ) + tampered["signatures"][0]["sig"][1:]
    tampered_envelope = SignedDsseEnvelope.from_dict(tampered)
    bad_grade = replace(
        fixture.grade,
        evaluation_envelope=tampered_envelope,
        evaluation_envelope_digest=tampered_envelope.digest,
        result_digest=sha256_json(
            {
                **fixture.grade.payload_dict(),
                "evaluation_envelope_digest": tampered_envelope.digest,
            }
        ),
    )
    with pytest.raises(ProtocolExportError):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=bad_grade,
            analysis=fixture.analysis,
            evidence=fixture.evidence,
        )


def test_two_smoke_tasks_cannot_export_official_rankable_winner():
    fixture = _fixture(official=True, task_count=2)
    assert fixture.analysis.descriptive_decision is Decision.A_BETTER
    assert fixture.analysis.publication_eligible is False
    with pytest.raises(ProtocolExportError, match="publication quality gates"):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=fixture.evidence,
        )


def test_complete_synthetic_50_task_5_trial_fixture_exports_official_rankable_winner():
    fixture = _fixture(official=True, task_count=50, repetitions=5)
    assert fixture.analysis.publication_eligible is True
    assert fixture.analysis.publication_decision is Decision.A_BETTER
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    result = exported.trial_result
    assert result["trust_tier"] == "official-attested"
    assert result["rankable"] is True
    assert result["status"] == "success"
    assert result["budget"]["profile_id"] == "equal-cost"
    assert result["budget"]["limits_digest"] == canonical_digest(
        fixture.evidence.trial_request["budget_limits"]
    )
    assert result["analysis"]["model_policy"] == {
        "id": fixture.spec.model_policy.id,
        "version": fixture.spec.model_policy.version,
        "policy_digest": _digest(fixture.spec.model_policy.digest[0])
        | {"value": fixture.spec.model_policy.digest},
    }
    assert result["analysis"]["budget"] == result["budget"]
    assert result["analysis"]["document"] in exported.bundle["contents"]["logs"]
    assert {item["role"] for item in result["attestations"]} == {
        "admission",
        "harness-build",
        "execution",
        "model",
        "evaluation",
    }
    assert exported.bundle["trust_tier"] == "official-attested"
    assert fixture.grade.evaluation_envelope_digest
    assert fixture.grade.lifecycle_receipt_digest
    evaluation_claims = fixture.grade.evaluation_envelope.statement()[
        "predicate"
    ]["claims"]
    assert evaluation_claims["lifecycle_receipt_digest"] == (
        fixture.grade.lifecycle_receipt_digest
    )
    assert evaluation_claims["grade_core_digest"] == fixture.grade.grade_core_digest
    model_attestation = next(
        item for item in fixture.evidence.attestations if item.role == "model"
    )
    model_claims = SignedDsseEnvelope.from_dict(
        json.loads(model_attestation.document.data)
    ).statement()["predicate"]["claims"]
    assert model_claims["internal_operator_evidence"]["algorithm"] == "HMAC-SHA256"
    assert model_claims["internal_operator_evidence"]["integrity_only"] is True
    verify_bundle_manifest(exported.bundle)


def test_complete_model_free_official_fixture_does_not_require_model_role():
    fixture = _fixture(
        official=True,
        task_count=50,
        repetitions=5,
        model_free=True,
    )
    exported = export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    result = exported.trial_result
    assert result["rankable"] is True
    assert result["models"] == []
    assert "model" not in {item["role"] for item in result["attestations"]}
    assert exported.bundle["contents"]["model_receipts"] == []


@pytest.mark.parametrize(
    "missing",
    ["trusted-lifecycle", "model", "model-attestation", "grader", "output-tree"],
)
def test_official_export_rejects_missing_required_evidence(missing):
    fixture = _fixture(official=True, task_count=50)
    evidence = fixture.evidence
    grade = fixture.grade
    if missing == "trusted-lifecycle":
        local = ControllerAssertedLifecycleReceipt.completed(
            controller_id="not-official"
        )
        evidence = replace(
            evidence,
            runner=replace(evidence.runner, lifecycle_receipt=local),
        )
    elif missing == "model":
        evidence = replace(evidence, models=())
    elif missing == "model-attestation":
        evidence = replace(
            evidence,
            attestations=tuple(
                item for item in evidence.attestations if item.role != "model"
            ),
        )
    elif missing == "grader":
        evidence = replace(evidence, grader=None)
    elif missing == "output-tree":
        evidence = replace(evidence, output_tree=None)
    with pytest.raises(ProtocolExportError):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=grade,
            analysis=fixture.analysis,
            evidence=evidence,
        )


def test_official_export_rejects_mismatched_content_digests():
    fixture = _fixture(official=True, task_count=50)
    bad_tree = EvidenceArtifact(
        path=fixture.evidence.output_tree.path,
        media_type=fixture.evidence.output_tree.media_type,
        data=b'{"files":[]}',
        role="output-tree",
    )
    with pytest.raises(ProtocolExportError, match="output-tree evidence"):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=replace(fixture.evidence, output_tree=bad_tree),
        )

    bad_request = deepcopy(fixture.evidence.trial_request)
    bad_request["task"]["manifest_digest"] = _digest("0")
    with pytest.raises(ProtocolExportError, match="trial request task"):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=replace(fixture.evidence, trial_request=bad_request),
        )


def test_official_export_rejects_mismatched_receipt_and_attestation_bindings():
    fixture = _fixture(official=True, task_count=50)
    bad_receipt = EvidenceDocument.from_protocol_json(
        schema="atv.model-receipt/v1",
        path="receipts/model-1.json",
        value={
            "schema": "atv.model-receipt/v1",
            "trial_id": fixture.spec.trial_id,
            "attempt_id": fixture.attempt.attempt_id,
            "requested_model": "provider/model-snapshot",
            "resolved_model": "provider/wrong-model",
            "provider_reported": "provider/model-snapshot",
            "provider": "provider",
            "request_ids": ["request-0001", "request-0002"],
        },
    )
    models = (replace(fixture.evidence.models[0], receipt=bad_receipt),)
    with pytest.raises(ProtocolExportError, match="model receipt"):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=replace(fixture.evidence, models=models),
        )

    admission = next(
        item for item in fixture.evidence.attestations if item.role == "admission"
    )
    bad_admission = AttestationEvidence(
        role="admission",
        document=EvidenceDocument.from_protocol_json(
            schema="in-toto.statement/v1",
            path=admission.document.path,
            value={
                "_type": "https://in-toto.io/Statement/v1",
                "subject": [],
                "predicateType": "https://atv-bench.org/admission/v1",
                "predicate": {
                    "role": "admission",
                    "trial_id": "wrong-trial",
                    "attempt_id": fixture.attempt.attempt_id,
                },
            },
        ),
    )
    attestations = tuple(
        bad_admission if item.role == "admission" else item
        for item in fixture.evidence.attestations
    )
    with pytest.raises(
        ProtocolExportError,
        match="signed DSSE envelope|did not verify",
    ):
        export_protocol_bundle(
            spec=fixture.spec,
            attempt=fixture.attempt,
            outcome=fixture.outcome,
            grade=fixture.grade,
            analysis=fixture.analysis,
            evidence=replace(fixture.evidence, attestations=attestations),
        )
