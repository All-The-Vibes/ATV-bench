"""Local model-backed evaluation operator.

This module deliberately produces local, self-attested, non-rankable evidence.
It composes the existing task/harness validators, paired scheduler,
``TrialController``, credential broker, Responses gateway, and OCI network
policy without changing their trust claims.

The operator is provider-neutral. A caller must inject ``ResponsesBackend``
implementations and the corresponding broker-held provider credentials.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import math
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from atv_bench.control_plane import (
    ControllerLedger,
    ControllerModelPolicy,
    ControllerRunRequest,
    ControllerTaskSet,
    TrialController,
    TrialControllerResult,
)
from atv_bench.eval import (
    Budget,
    BudgetProfile,
    FileAssertionsGrader,
    HarnessRef,
    ModelPolicyRef,
    ScheduledTrial,
    TaskPackage,
    TaskPackageValidator,
    TaskRef,
    build_paired_schedule,
)
from atv_bench.eval.bundle import ContentAddressedStore
from atv_bench.harness_manifest import (
    HarnessManifestRegistry,
    LoadedHarnessManifest,
)
from atv_bench.protocol import (
    canonical_digest,
    canonical_json_bytes,
    canonical_jsonl,
    sha256_bytes,
    strict_json_loads,
)
from atv_bench.sandbox import OciNetworkPolicy, OciTrack
from atv_bench.security import (
    AttestationSigner,
    CredentialBroker,
    OpaqueTrialHandle,
    ResponsesBackend,
    ResponsesBudgetLedger,
    ResponsesGateway,
    ResponsesGatewayConfig,
    ResponsesGatewayLogRecord,
    ResponsesGatewayStatus,
    ResponsesHttpResponse,
    ResponsesHttpServer,
    RouteDefinition,
    TrialBudget,
    UnderreportPolicy,
    UsageSummary,
)


POLICY_SCHEMA = "atv.model-backed-eval-policy/v1"
PLAN_SCHEMA = "atv.model-backed-eval-plan/v1"
RUN_SCHEMA = "atv.model-backed-local-run/v1"
OUTPUT_MANIFEST_SCHEMA = "atv.immutable-output-manifest/v1"
TRUST_TIER = "local-self-attested"
EXPECTED_CONTROLLER_EXPORT_GAP = "model_evidence_export_unavailable"


class ModelBackedOperatorError(RuntimeError):
    """Typed, safe operator failure."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.safe_message = message


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def _require_exact(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    field_name: str,
) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise ModelBackedOperatorError(
            "policy_shape_invalid",
            f"{field_name} has missing={missing} extra={extra}",
        )


def _require_text(value: Any, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ModelBackedOperatorError(
            "policy_value_invalid",
            f"{field_name} must be non-empty trimmed text",
        )
    return value


def _require_int(
    value: Any,
    *,
    field_name: str,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelBackedOperatorError(
            "policy_value_invalid",
            f"{field_name} must be an integer >= {minimum}",
        )
    return value


def _safe_output_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ModelBackedOperatorError(
            "output_manifest_invalid",
            f"unsafe output path: {value!r}",
        )
    return path


def _canonical_safe(value: Any) -> Any:
    """Convert local floating-point evidence to explicit decimal strings."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelBackedOperatorError(
                "non_finite_evidence",
                "evidence contains a non-finite floating-point value",
            )
        return format(value, ".17g")
    if isinstance(value, Mapping):
        return {str(key): _canonical_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_safe(item) for item in value]
    return value


def _strict_json_file(
    path: Path, *, max_bytes: int = 4 * 1024 * 1024
) -> dict[str, Any]:
    source = Path(os.path.abspath(os.fspath(path)))
    if source.is_symlink() or not source.is_file():
        raise ModelBackedOperatorError(
            "policy_path_invalid",
            "policy must be a regular non-symlink file",
        )
    data = source.read_bytes()
    if len(data) > max_bytes:
        raise ModelBackedOperatorError(
            "policy_too_large",
            f"policy exceeds {max_bytes} bytes",
        )
    try:
        value = strict_json_loads(data.decode("utf-8"))
    except Exception as exc:
        raise ModelBackedOperatorError(
            "policy_json_invalid",
            f"policy is not strict UTF-8 JSON: {type(exc).__name__}",
        ) from None
    if not isinstance(value, dict):
        raise ModelBackedOperatorError(
            "policy_json_invalid",
            "policy root must be an object",
        )
    return value


@dataclass(frozen=True, slots=True)
class ProviderBindings:
    """Dependency-injected provider adapters and broker-held credentials."""

    backends: Mapping[str, ResponsesBackend] = field(repr=False)
    credentials: Mapping[str, str | bytes] = field(repr=False)

    def __post_init__(self) -> None:
        backends = dict(self.backends)
        credentials = dict(self.credentials)
        if not backends:
            raise ModelBackedOperatorError(
                "provider_bindings_empty",
                "at least one provider backend is required",
            )
        if set(backends) != set(credentials):
            raise ModelBackedOperatorError(
                "provider_bindings_mismatch",
                "backend and credential provider IDs must match exactly",
            )
        for provider_id, credential in credentials.items():
            _require_text(provider_id, field_name="provider ID")
            if not isinstance(credential, (str, bytes)) or not credential:
                raise ModelBackedOperatorError(
                    "provider_credential_invalid",
                    f"provider {provider_id!r} has an empty credential",
                )
        object.__setattr__(self, "backends", backends)
        object.__setattr__(self, "credentials", credentials)


@dataclass(frozen=True, slots=True)
class GatewayEndpointContract:
    """Explicit container-network contract for the brokered Responses endpoint."""

    network_name: str
    host: str
    port: int
    tls: bool
    healthcheck: Callable[
        [OpaqueTrialHandle, ControllerRunRequest],
        None,
    ] = field(repr=False, compare=False)
    cleanup: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name in ("network_name", "host"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(character in value for character in "\x00\r\n")
            ):
                raise ModelBackedOperatorError(
                    "gateway_endpoint_invalid",
                    f"{name} must be non-empty trimmed text",
                )
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65_535
        ):
            raise ModelBackedOperatorError(
                "gateway_endpoint_invalid",
                "port must be an integer from 1 through 65535",
            )
        if self.tls is not True:
            raise ModelBackedOperatorError(
                "gateway_endpoint_tls_required",
                "the container endpoint contract must require TLS",
            )
        if not callable(self.healthcheck):
            raise ModelBackedOperatorError(
                "gateway_endpoint_invalid",
                "healthcheck must be callable",
            )
        if self.cleanup is not None and not callable(self.cleanup):
            raise ModelBackedOperatorError(
                "gateway_endpoint_invalid",
                "cleanup must be callable when provided",
            )
        normalized_host = self.host.rstrip(".").lower()
        if (
            normalized_host == "localhost"
            or normalized_host.endswith(".localhost")
            or ":" in normalized_host
        ):
            raise ModelBackedOperatorError(
                "gateway_endpoint_unreachable",
                "the advertised endpoint is not a container-reachable DNS or IPv4 host",
            )
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or address.is_multicast
        ):
            raise ModelBackedOperatorError(
                "gateway_endpoint_unreachable",
                "the advertised endpoint is not reachable from the OCI network",
            )

    def validate_for(self, policy: "ModelBackedEvalPolicy") -> None:
        if self.network_name != policy.network_name:
            raise ModelBackedOperatorError(
                "gateway_endpoint_network_mismatch",
                "the endpoint contract names a different OCI network",
            )
        if self.host != policy.gateway_identity:
            raise ModelBackedOperatorError(
                "gateway_endpoint_identity_mismatch",
                "the endpoint host must equal the preregistered gateway identity",
            )

    def close(self) -> None:
        """Release resources provisioned by the injected endpoint factory."""

        if self.cleanup is None:
            return
        try:
            self.cleanup()
        except ModelBackedOperatorError:
            raise
        except Exception as exc:
            raise ModelBackedOperatorError(
                "gateway_endpoint_cleanup_failed",
                "gateway endpoint cleanup failed",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_name": self.network_name,
            "host": self.host,
            "port": self.port,
            "tls": self.tls,
            "healthcheck_required": True,
        }


@dataclass(frozen=True, slots=True)
class ModelBackedEvalPolicy:
    source_path: Path
    id: str
    version: str
    benchmark_release: str
    protocol_version: str
    track: OciTrack
    task_set_id: str
    task_set_version: str
    default_model: str
    routes: tuple[RouteDefinition, ...]
    budget_profile_id: str
    wall_time_seconds: int
    budget: TrialBudget
    repetitions: int
    seed: int
    workers: tuple[str, ...]
    network_name: str
    gateway_identity: str
    max_retries: int
    underreport_policy: UnderreportPolicy
    handle_ttl_seconds: int
    digest: str
    _document: Mapping[str, Any] = field(repr=False)

    @classmethod
    def load(cls, path: Path | str) -> "ModelBackedEvalPolicy":
        source = Path(path)
        document = _strict_json_file(source)
        _require_exact(
            document,
            {
                "schema",
                "id",
                "version",
                "benchmark_release",
                "protocol_version",
                "track",
                "task_set",
                "default_model",
                "routes",
                "budget_profile",
                "repetitions",
                "seed",
                "workers",
                "network",
                "max_retries",
                "underreport_policy",
                "handle_ttl_seconds",
                "policy_digest",
            },
            field_name="policy",
        )
        if document["schema"] != POLICY_SCHEMA:
            raise ModelBackedOperatorError(
                "policy_schema_unsupported",
                f"expected {POLICY_SCHEMA}",
            )

        task_set = document["task_set"]
        if not isinstance(task_set, dict):
            raise ModelBackedOperatorError(
                "policy_shape_invalid",
                "task_set must be an object",
            )
        _require_exact(task_set, {"id", "version"}, field_name="task_set")

        network = document["network"]
        if not isinstance(network, dict):
            raise ModelBackedOperatorError(
                "policy_shape_invalid",
                "network must be an object",
            )
        _require_exact(
            network,
            {"name", "gateway_identity"},
            field_name="network",
        )

        budget_profile = document["budget_profile"]
        if not isinstance(budget_profile, dict):
            raise ModelBackedOperatorError(
                "policy_shape_invalid",
                "budget_profile must be an object",
            )
        _require_exact(
            budget_profile,
            {
                "id",
                "wall_time_seconds",
                "max_model_calls",
                "max_input_tokens",
                "max_output_tokens",
                "max_total_tokens",
                "max_cost_microusd",
            },
            field_name="budget_profile",
        )

        raw_routes = document["routes"]
        if not isinstance(raw_routes, list) or not raw_routes:
            raise ModelBackedOperatorError(
                "policy_routes_invalid",
                "routes must be a non-empty array",
            )
        routes: list[RouteDefinition] = []
        for index, raw_route in enumerate(raw_routes):
            if not isinstance(raw_route, dict):
                raise ModelBackedOperatorError(
                    "policy_routes_invalid",
                    f"routes[{index}] must be an object",
                )
            _require_exact(
                raw_route,
                {
                    "route_id",
                    "public_model",
                    "provider_id",
                    "provider_model",
                    "input_microusd_per_million",
                    "output_microusd_per_million",
                },
                field_name=f"routes[{index}]",
            )
            routes.append(
                RouteDefinition(
                    route_id=_require_text(
                        raw_route["route_id"],
                        field_name=f"routes[{index}].route_id",
                    ),
                    public_model=_require_text(
                        raw_route["public_model"],
                        field_name=f"routes[{index}].public_model",
                    ),
                    provider_id=_require_text(
                        raw_route["provider_id"],
                        field_name=f"routes[{index}].provider_id",
                    ),
                    provider_model=_require_text(
                        raw_route["provider_model"],
                        field_name=f"routes[{index}].provider_model",
                    ),
                    input_microusd_per_million=_require_int(
                        raw_route["input_microusd_per_million"],
                        field_name=(f"routes[{index}].input_microusd_per_million"),
                    ),
                    output_microusd_per_million=_require_int(
                        raw_route["output_microusd_per_million"],
                        field_name=(f"routes[{index}].output_microusd_per_million"),
                    ),
                )
            )
        for label, values in (
            ("route IDs", [item.route_id for item in routes]),
            ("public models", [item.public_model for item in routes]),
        ):
            if len(values) != len(set(values)):
                raise ModelBackedOperatorError(
                    "policy_routes_invalid",
                    f"{label} must be unique",
                )

        workers = document["workers"]
        if not isinstance(workers, list) or not workers:
            raise ModelBackedOperatorError(
                "policy_workers_invalid",
                "workers must be a non-empty array",
            )
        worker_ids = tuple(
            _require_text(item, field_name=f"workers[{index}]")
            for index, item in enumerate(workers)
        )
        if len(worker_ids) != len(set(worker_ids)):
            raise ModelBackedOperatorError(
                "policy_workers_invalid",
                "workers must be unique",
            )

        default_model = _require_text(
            document["default_model"],
            field_name="default_model",
        )
        if default_model not in {item.public_model for item in routes}:
            raise ModelBackedOperatorError(
                "policy_default_model_invalid",
                "default_model must name one preregistered public model",
            )

        max_input = _require_int(
            budget_profile["max_input_tokens"],
            field_name="budget_profile.max_input_tokens",
            minimum=1,
        )
        max_output = _require_int(
            budget_profile["max_output_tokens"],
            field_name="budget_profile.max_output_tokens",
            minimum=1,
        )
        max_total = _require_int(
            budget_profile["max_total_tokens"],
            field_name="budget_profile.max_total_tokens",
            minimum=1,
        )
        if max_total > max_input + max_output:
            raise ModelBackedOperatorError(
                "policy_budget_invalid",
                "max_total_tokens cannot exceed input plus output caps",
            )
        budget = TrialBudget(
            max_model_calls=_require_int(
                budget_profile["max_model_calls"],
                field_name="budget_profile.max_model_calls",
                minimum=1,
            ),
            max_input_tokens=max_input,
            max_output_tokens=max_output,
            max_total_tokens=max_total,
            max_cost_microusd=_require_int(
                budget_profile["max_cost_microusd"],
                field_name="budget_profile.max_cost_microusd",
                minimum=1,
            ),
        )

        digest_payload = {
            key: value for key, value in document.items() if key != "policy_digest"
        }
        observed_digest = _require_text(
            document["policy_digest"],
            field_name="policy_digest",
        )
        expected_digest = canonical_digest(digest_payload)["value"]
        if observed_digest != expected_digest:
            raise ModelBackedOperatorError(
                "policy_digest_mismatch",
                "policy_digest does not bind the current policy content",
            )

        try:
            track = OciTrack(_require_text(document["track"], field_name="track"))
        except ValueError:
            raise ModelBackedOperatorError(
                "policy_track_invalid",
                "track must be controlled or systems",
            ) from None
        try:
            underreport_policy = UnderreportPolicy(
                _require_text(
                    document["underreport_policy"],
                    field_name="underreport_policy",
                )
            )
        except ValueError:
            raise ModelBackedOperatorError(
                "policy_underreport_invalid",
                "unsupported underreport_policy",
            ) from None

        return cls(
            source_path=Path(os.path.abspath(os.fspath(source))),
            id=_require_text(document["id"], field_name="id"),
            version=_require_text(document["version"], field_name="version"),
            benchmark_release=_require_text(
                document["benchmark_release"],
                field_name="benchmark_release",
            ),
            protocol_version=_require_text(
                document["protocol_version"],
                field_name="protocol_version",
            ),
            track=track,
            task_set_id=_require_text(task_set["id"], field_name="task_set.id"),
            task_set_version=_require_text(
                task_set["version"],
                field_name="task_set.version",
            ),
            default_model=default_model,
            routes=tuple(routes),
            budget_profile_id=_require_text(
                budget_profile["id"],
                field_name="budget_profile.id",
            ),
            wall_time_seconds=_require_int(
                budget_profile["wall_time_seconds"],
                field_name="budget_profile.wall_time_seconds",
                minimum=1,
            ),
            budget=budget,
            repetitions=_require_int(
                document["repetitions"],
                field_name="repetitions",
                minimum=1,
            ),
            seed=_require_int(document["seed"], field_name="seed"),
            workers=worker_ids,
            network_name=_require_text(
                network["name"],
                field_name="network.name",
            ),
            gateway_identity=_require_text(
                network["gateway_identity"],
                field_name="network.gateway_identity",
            ),
            max_retries=_require_int(
                document["max_retries"],
                field_name="max_retries",
            ),
            underreport_policy=underreport_policy,
            handle_ttl_seconds=_require_int(
                document["handle_ttl_seconds"],
                field_name="handle_ttl_seconds",
                minimum=1,
            ),
            digest=observed_digest,
            _document=json.loads(canonical_json_bytes(document)),
        )

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(canonical_json_bytes(self._document))

    @property
    def allowed_models(self) -> tuple[str, ...]:
        return (
            self.default_model,
            *(
                item.public_model
                for item in self.routes
                if item.public_model != self.default_model
            ),
        )

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.provider_id for item in self.routes}))

    @property
    def budget_profile(self) -> BudgetProfile:
        return BudgetProfile(
            self.budget_profile_id,
            Budget(
                self.wall_time_seconds,
                self.budget.max_total_tokens,
                self.budget.max_model_calls,
                self.budget.max_cost_microusd,
            ),
        )

    def controller_policy(
        self,
        *,
        gateway_port: int,
        gateway_host: str | None = None,
    ) -> ControllerModelPolicy:
        return ControllerModelPolicy(
            id=self.id,
            version=self.version,
            model_required=True,
            allowed_models=self.allowed_models,
            allowed_route_ids=tuple(item.route_id for item in self.routes),
            gateway=f"{gateway_host or self.gateway_identity}:{gateway_port}",
            budget=self.budget,
            max_retries=self.max_retries,
            underreport_policy=self.underreport_policy,
            handle_ttl_seconds=float(self.handle_ttl_seconds),
        )

    def network_policy(self) -> OciNetworkPolicy:
        return OciNetworkPolicy.model_gateway_only(
            self.network_name,
            allowed_gateway_identities=(self.gateway_identity,),
        )


@dataclass(frozen=True, slots=True)
class ModelBackedEvalPlan:
    policy: ModelBackedEvalPolicy
    tasks: tuple[TaskPackage, ...]
    harnesses: tuple[LoadedHarnessManifest, ...]
    task_validation: tuple[Mapping[str, Any], ...]
    schedule: tuple[ScheduledTrial, ...]
    task_set_digest: str
    digest: str
    _document: Mapping[str, Any] = field(repr=False)

    @classmethod
    def load(
        cls,
        *,
        policy_path: Path | str,
        task_paths: Sequence[Path | str],
        harness_paths: Sequence[Path | str],
    ) -> "ModelBackedEvalPlan":
        policy = ModelBackedEvalPolicy.load(policy_path)
        if not task_paths:
            raise ModelBackedOperatorError(
                "task_set_empty",
                "at least one task package is required",
            )
        if len(harness_paths) < 2:
            raise ModelBackedOperatorError(
                "harness_set_too_small",
                "paired evaluation requires at least two harnesses",
            )

        tasks: list[TaskPackage] = []
        task_reports: list[Mapping[str, Any]] = []
        seen_tasks: set[tuple[str, str]] = set()
        for path in task_paths:
            try:
                task = TaskPackage.load(path)
                grader: Any = FileAssertionsGrader.from_task(task)
                report = TaskPackageValidator().validate(
                    task,
                    grader,
                )
            except Exception as exc:
                raise ModelBackedOperatorError(
                    "task_validation_failed",
                    f"{Path(path).name}: {type(exc).__name__}",
                ) from None
            if not report.eligible:
                raise ModelBackedOperatorError(
                    "task_ineligible",
                    f"{task.id}@{task.version} failed validation gates",
                )
            identity = (task.id, task.version)
            if identity in seen_tasks:
                raise ModelBackedOperatorError(
                    "task_identity_duplicate",
                    f"duplicate task identity {task.id}@{task.version}",
                )
            seen_tasks.add(identity)
            tasks.append(task)
            task_reports.append(_canonical_safe(report.to_dict()))

        registry = HarnessManifestRegistry()
        harnesses: list[LoadedHarnessManifest] = []
        for path in harness_paths:
            try:
                harness = registry.load(path)
            except Exception as exc:
                raise ModelBackedOperatorError(
                    "harness_validation_failed",
                    f"{Path(path).name}: {type(exc).__name__}",
                ) from None
            document = harness.as_dict()
            if harness.runtime_kind != "oci":
                raise ModelBackedOperatorError(
                    "harness_runtime_invalid",
                    f"{harness.id} is not an OCI harness",
                )
            security = document["security"]
            if (
                security["network_requirement"] != "model-gateway-only"
                or "ATV_MODEL_GATEWAY_HANDLE" not in security["env_allowlist"]
            ):
                raise ModelBackedOperatorError(
                    "harness_model_gateway_invalid",
                    f"{harness.id} does not require the brokered model gateway",
                )
            harnesses.append(harness)

        if policy.track is OciTrack.CONTROLLED:
            task_images = {str(task.manifest["environment"]["image"]) for task in tasks}
            harness_images = {
                str(harness.as_dict()["runtime"]["image"]) for harness in harnesses
            }
            if len(task_images | harness_images) != 1:
                raise ModelBackedOperatorError(
                    "controlled_image_mismatch",
                    "controlled track requires one common digest-pinned image",
                )

        task_refs = tuple(
            TaskRef(
                task.id,
                task.version,
                canonical_digest(task.manifest)["value"],
            )
            for task in tasks
        )
        harness_refs = tuple(
            HarnessRef(item.id, item.version, item.digest) for item in harnesses
        )

        # The controller digest intentionally covers broker policy semantics only.
        # The complete preregistered policy remains independently bound by
        # ``policy.digest`` in this plan.
        provisional_controller_policy = policy.controller_policy(gateway_port=1)
        model_ref = ModelPolicyRef(
            policy.id,
            policy.version,
            provisional_controller_policy.digest,
        )
        schedule = build_paired_schedule(
            benchmark_release=policy.benchmark_release,
            protocol_version=policy.protocol_version,
            tasks=task_refs,
            harnesses=harness_refs,
            model_policies=(model_ref,),
            budget_profiles=(policy.budget_profile,),
            repetitions=policy.repetitions,
            seed=policy.seed,
            workers=policy.workers,
        )

        task_set_document = {
            "schema": "atv.operator-task-set/v1",
            "id": policy.task_set_id,
            "version": policy.task_set_version,
            "tasks": [item.to_dict() for item in task_refs],
            "validation": list(task_reports),
        }
        task_set_digest = canonical_digest(task_set_document)["value"]
        plan_document = {
            "schema": PLAN_SCHEMA,
            "policy": policy.document,
            "policy_digest": policy.digest,
            "task_set": {
                **task_set_document,
                "manifest_digest": task_set_digest,
            },
            "harnesses": [
                {
                    "id": item.id,
                    "version": item.version,
                    "digest": item.digest,
                    "manifest": item.as_dict(),
                }
                for item in harnesses
            ],
            "schedule": [item.to_dict() for item in schedule],
            "trust_tier": TRUST_TIER,
            "rankable": False,
            "official_verified": False,
        }
        plan_digest = canonical_digest(plan_document)["value"]
        return cls(
            policy=policy,
            tasks=tuple(tasks),
            harnesses=tuple(harnesses),
            task_validation=tuple(task_reports),
            schedule=schedule,
            task_set_digest=task_set_digest,
            digest=plan_digest,
            _document=json.loads(canonical_json_bytes(plan_document)),
        )

    @property
    def document(self) -> dict[str, Any]:
        return {
            **json.loads(canonical_json_bytes(self._document)),
            "plan_digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class GatewayIngressAudit:
    sequence: int
    trial_id: str | None
    attempt_id: str | None
    request_digest: str
    requested_model: str | None
    status_code: int
    policy_violation: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class _HandleRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_handle: dict[str, tuple[str, str]] = {}
        self._fingerprints: dict[str, str] = {}

    def register(
        self,
        handle: OpaqueTrialHandle,
        request: ControllerRunRequest,
    ) -> None:
        value = handle.value
        identity = (
            request.scheduled.spec.trial_id,
            request.scheduled.attempt.attempt_id,
        )
        with self._lock:
            existing = self._by_handle.get(value)
            if existing is not None and existing != identity:
                raise ModelBackedOperatorError(
                    "opaque_handle_reused",
                    "one opaque handle was assigned to multiple attempts",
                )
            fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()
            if fingerprint in self._fingerprints.values():
                owner = next(
                    attempt
                    for attempt, observed in self._fingerprints.items()
                    if observed == fingerprint
                )
                if owner != identity[1]:
                    raise ModelBackedOperatorError(
                        "opaque_handle_reused",
                        "paired attempts received the same opaque capability",
                    )
            self._by_handle[value] = identity
            self._fingerprints[identity[1]] = fingerprint

    def resolve_trial(self, handle: str) -> str:
        with self._lock:
            return self._by_handle[handle][0]

    def identity(self, handle: str) -> tuple[str, str] | None:
        with self._lock:
            return self._by_handle.get(handle)

    def fingerprint(self, attempt_id: str) -> str | None:
        with self._lock:
            return self._fingerprints.get(attempt_id)

    def raw_handles(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._by_handle)


class _AuditedResponsesGateway(ResponsesGateway):
    """Record all HTTP attempts, including pre-route policy denials."""

    def __init__(
        self,
        *,
        handle_registry: _HandleRegistry,
        allowed_models: Sequence[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._operator_handles = handle_registry
        self._operator_allowed_models = frozenset(allowed_models)
        self._operator_audits: list[GatewayIngressAudit] = []
        self._operator_audit_lock = threading.RLock()

    def audits(self) -> tuple[GatewayIngressAudit, ...]:
        with self._operator_audit_lock:
            return tuple(self._operator_audits)

    def handle_http(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        cancel_event: threading.Event | None = None,
    ) -> ResponsesHttpResponse:
        requested_model: str | None = None
        try:
            document = strict_json_loads(body.decode("utf-8"))
            if isinstance(document, dict) and isinstance(
                document.get("model"),
                str,
            ):
                requested_model = document["model"]
        except Exception:
            pass
        handle: str | None = None
        for name, value in headers.items():
            if name.lower() == "authorization":
                pieces = value.split(" ")
                if len(pieces) == 2 and pieces[0].lower() == "bearer":
                    handle = pieces[1]
                break
        identity = self._operator_handles.identity(handle) if handle else None
        response = super().handle_http(
            method=method,
            path=path,
            headers=headers,
            body=body,
            cancel_event=cancel_event,
        )
        with self._operator_audit_lock:
            self._operator_audits.append(
                GatewayIngressAudit(
                    sequence=len(self._operator_audits),
                    trial_id=identity[0] if identity else None,
                    attempt_id=identity[1] if identity else None,
                    request_digest=sha256_bytes(body),
                    requested_model=requested_model,
                    status_code=response.status_code,
                    policy_violation=(
                        requested_model is not None
                        and requested_model not in self._operator_allowed_models
                    ),
                )
            )
        return response


def _sum_usage(records: Sequence[ResponsesGatewayLogRecord]) -> UsageSummary:
    total = UsageSummary()
    for record in records:
        total = total.plus(record.usage)
    return total


def _route_map(
    routes: Sequence[RouteDefinition],
) -> dict[str, RouteDefinition]:
    return {item.route_id: item for item in routes}


def _attestation_payload(record: ResponsesGatewayLogRecord) -> Mapping[str, Any]:
    payload = record.attestation.get("payload")
    if not isinstance(payload, Mapping):
        raise ModelBackedOperatorError(
            "gateway_receipt_invalid",
            f"request {record.request_id} has no attestation payload",
        )
    return payload


def _attempt_evidence(
    *,
    result: TrialControllerResult,
    records: Sequence[ResponsesGatewayLogRecord],
    audits: Sequence[GatewayIngressAudit],
    policy: ModelBackedEvalPolicy,
    signer: AttestationSigner,
    handle_fingerprint: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempt_id = result.request.scheduled.attempt.attempt_id
    trial_id = result.request.scheduled.spec.trial_id
    routes = _route_map(policy.routes)
    violations: list[dict[str, Any]] = []

    if handle_fingerprint is None:
        violations.append(
            {
                "code": "opaque_handle_missing",
                "evidence": attempt_id,
            }
        )
    for audit in audits:
        if audit.attempt_id != attempt_id or audit.trial_id != trial_id:
            violations.append(
                {
                    "code": "gateway_request_identity_mismatch",
                    "evidence": audit.to_dict(),
                }
            )
        if audit.policy_violation:
            violations.append(
                {
                    "code": "model_policy_violation",
                    "evidence": audit.to_dict(),
                }
            )
        if audit.status_code >= 400:
            violations.append(
                {
                    "code": "gateway_request_failed",
                    "evidence": audit.to_dict(),
                }
            )

    for record in records:
        if record.attempt_id != attempt_id or record.trial_id != trial_id:
            violations.append(
                {
                    "code": "gateway_receipt_identity_mismatch",
                    "evidence": record.request_id,
                }
            )
            continue
        route = routes.get(record.route_id)
        if (
            route is None
            or record.requested_model != route.public_model
            or record.resolved_provider_model != route.provider_model
        ):
            violations.append(
                {
                    "code": "model_resolution_outside_policy",
                    "evidence": record.to_dict(),
                }
            )
        if record.status is not ResponsesGatewayStatus.SUCCESS:
            violations.append(
                {
                    "code": "gateway_terminal_failure",
                    "evidence": record.to_dict(),
                }
            )
        verification = signer.verify(record.attestation)
        if not verification.integrity_valid:
            violations.append(
                {
                    "code": "gateway_receipt_signature_invalid",
                    "evidence": record.request_id,
                }
            )
        payload = _attestation_payload(record)
        resolved_route = payload.get("resolved_route")
        expected_route = route.public_dict() if route is not None else None
        if (
            payload.get("trial_id") != trial_id
            or payload.get("attempt_id") != attempt_id
            or payload.get("requested_model") != record.requested_model
            or resolved_route != expected_route
            or payload.get("status") != record.status.value
        ):
            violations.append(
                {
                    "code": "gateway_receipt_content_invalid",
                    "evidence": record.request_id,
                }
            )

    usage = _sum_usage(records)
    if usage.model_calls > policy.budget.max_model_calls:
        violations.append(
            {
                "code": "model_call_budget_exceeded",
                "evidence": usage.to_dict(),
            }
        )
    if usage.input_tokens > policy.budget.max_input_tokens:
        violations.append(
            {
                "code": "input_token_budget_exceeded",
                "evidence": usage.to_dict(),
            }
        )
    if usage.output_tokens > policy.budget.max_output_tokens:
        violations.append(
            {
                "code": "output_token_budget_exceeded",
                "evidence": usage.to_dict(),
            }
        )
    if usage.total_tokens > policy.budget.max_total_tokens:
        violations.append(
            {
                "code": "total_token_budget_exceeded",
                "evidence": usage.to_dict(),
            }
        )
    if usage.cost_microusd > policy.budget.max_cost_microusd:
        violations.append(
            {
                "code": "model_cost_budget_exceeded",
                "evidence": usage.to_dict(),
            }
        )

    controller_problem = result.problem.code if result.problem else None
    controller_handoff = controller_problem == EXPECTED_CONTROLLER_EXPORT_GAP
    if result.problem is not None and not controller_handoff:
        violations.append(
            {
                "code": "controller_failure",
                "evidence": result.problem.to_dict(),
            }
        )
    if (
        result.outcome is not None
        and result.outcome.harness_status.value == "completed"
        and not records
    ):
        violations.append(
            {
                "code": "completed_model_trial_without_receipt",
                "evidence": attempt_id,
            }
        )
    if controller_handoff and (
        result.internal_bundle is None or result.outcome is None or result.grade is None
    ):
        violations.append(
            {
                "code": "controller_export_handoff_incomplete",
                "evidence": attempt_id,
            }
        )

    summary = {
        "schema": "atv.model-backed-attempt/v1",
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "block_id": result.request.scheduled.block_id,
        "sequence_index": result.request.scheduled.sequence_index,
        "order_index": result.request.scheduled.order_index,
        "worker_id": result.request.scheduled.worker_id,
        "task": result.request.scheduled.spec.task.to_dict(),
        "harness": result.request.scheduled.spec.harness.to_dict(),
        "model_policy": {
            **result.request.scheduled.spec.model_policy.to_dict(),
            "complete_preregistered_policy_digest": policy.digest,
        },
        "budget_profile": result.request.scheduled.spec.budget_profile.to_dict(),
        "network": result.request.network.to_dict(),
        "selected_model": result.request.selected_model,
        "opaque_handle_sha256": handle_fingerprint,
        "controller": {
            "state": result.state.value,
            "problem": result.problem.to_dict() if result.problem else None,
            "expected_export_handoff": controller_handoff,
            "internal_bundle_digest": (
                result.internal_bundle.digest if result.internal_bundle else None
            ),
            "protocol_export_present": result.protocol_export is not None,
            "outcome": result.outcome.to_dict() if result.outcome else None,
            "grade": result.grade.to_dict() if result.grade else None,
        },
        "gateway": {
            "request_count": len(audits),
            "receipt_count": len(records),
            "usage": usage.to_dict(),
            "all_receipts_signed": bool(records)
            and all(
                signer.verify(record.attestation).integrity_valid for record in records
            ),
        },
        "violations": violations,
        "trust_tier": TRUST_TIER,
        "rankable": False,
        "official_verified": False,
    }
    return summary, violations


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ModelBackedOperatorError(
                    "output_write_failed",
                    f"write made no progress for {path.name}",
                )
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_exclusive(path, canonical_json_bytes(_canonical_safe(value)) + b"\n")


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    _write_exclusive(
        path, canonical_jsonl([_canonical_safe(value) for value in values])
    )


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"manifest.json", "manifest.sha256"}:
            continue
        metadata = os.lstat(path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ModelBackedOperatorError(
                "output_file_unsafe",
                f"output contains an unsafe file: {relative}",
            )
        data = path.read_bytes()
        records.append(
            {
                "path": relative,
                "sha256": sha256_bytes(data),
                "size": len(data),
            }
        )
    return records


def _scan_for_secrets(
    root: Path,
    *,
    credentials: Mapping[str, str | bytes],
    handles: Sequence[str],
) -> None:
    canaries = [
        value.encode("utf-8") if isinstance(value, str) else bytes(value)
        for value in credentials.values()
    ]
    canaries.extend(value.encode("utf-8") for value in handles)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(canary and canary in data for canary in canaries):
            raise ModelBackedOperatorError(
                "secret_leak_detected",
                f"sensitive material appeared in {path.relative_to(root).as_posix()}",
            )


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            os.chmod(path, stat.S_IREAD)


@dataclass(frozen=True, slots=True)
class ModelBackedRunResult:
    root: Path
    succeeded: bool
    plan_digest: str
    schedule_id: str
    attempts: tuple[Mapping[str, Any], ...]

    @property
    def rankable(self) -> bool:
        return False

    @property
    def official_verified(self) -> bool:
        return False

    @property
    def trust_tier(self) -> str:
        return TRUST_TIER

    def verify(self) -> Mapping[str, Any]:
        manifest_path = self.root / "manifest.json"
        digest_path = self.root / "manifest.sha256"
        if not manifest_path.is_file() or not digest_path.is_file():
            raise ModelBackedOperatorError(
                "output_manifest_missing",
                "immutable output manifest is incomplete",
            )
        manifest_bytes = manifest_path.read_bytes()
        expected = digest_path.read_text(encoding="ascii").strip()
        if expected != sha256_bytes(manifest_bytes):
            raise ModelBackedOperatorError(
                "output_manifest_tampered",
                "manifest.sha256 does not match manifest.json",
            )
        try:
            manifest = strict_json_loads(manifest_bytes.decode("utf-8"))
        except Exception:
            raise ModelBackedOperatorError(
                "output_manifest_invalid",
                "manifest.json is not strict canonical JSON",
            ) from None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != OUTPUT_MANIFEST_SCHEMA
            or manifest.get("trust_tier") != TRUST_TIER
            or manifest.get("rankable") is not False
            or manifest.get("official_verified") is not False
        ):
            raise ModelBackedOperatorError(
                "output_trust_claim_invalid",
                "local output changed its trust or rankability claim",
            )
        expected_status = "completed" if self.succeeded else "failed"
        if (
            manifest.get("plan_digest") != self.plan_digest
            or manifest.get("schedule_id") != self.schedule_id
            or manifest.get("status") != expected_status
        ):
            raise ModelBackedOperatorError(
                "output_identity_mismatch",
                "manifest identity does not match the completed run result",
            )
        listed: set[str] = set()
        for record in manifest.get("files", []):
            if not isinstance(record, dict) or set(record) != {
                "path",
                "sha256",
                "size",
            }:
                raise ModelBackedOperatorError(
                    "output_manifest_invalid",
                    "file record has an unsupported shape",
                )
            relative = _safe_output_relative(str(record["path"]))
            value = relative.as_posix()
            if value in listed:
                raise ModelBackedOperatorError(
                    "output_manifest_invalid",
                    f"duplicate file record: {value}",
                )
            listed.add(value)
            path = self.root.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file():
                raise ModelBackedOperatorError(
                    "output_file_missing",
                    value,
                )
            data = path.read_bytes()
            if len(data) != record["size"] or sha256_bytes(data) != record["sha256"]:
                raise ModelBackedOperatorError(
                    "output_file_tampered",
                    value,
                )
        actual = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
            and path.relative_to(self.root).as_posix()
            not in {"manifest.json", "manifest.sha256"}
        }
        if actual != listed:
            raise ModelBackedOperatorError(
                "output_manifest_incomplete",
                f"listed={len(listed)} actual={len(actual)}",
            )
        run = strict_json_loads((self.root / "run.json").read_text(encoding="utf-8"))
        if (
            not isinstance(run, dict)
            or run.get("schema") != RUN_SCHEMA
            or run.get("rankable") is not False
            or run.get("official_verified") is not False
        ):
            raise ModelBackedOperatorError(
                "run_record_invalid",
                "run.json contains an invalid trust claim",
            )
        expected_attempts = json.loads(
            canonical_json_bytes(_canonical_safe(list(self.attempts)))
        )
        if (
            run.get("plan_digest") != self.plan_digest
            or run.get("schedule_id") != self.schedule_id
            or run.get("status") != expected_status
            or run.get("attempts") != expected_attempts
            or run.get("executed_trials") != len(expected_attempts)
        ):
            raise ModelBackedOperatorError(
                "run_record_mismatch",
                "run.json identity, status, or attempts do not match the result",
            )
        return manifest


RunnerFactory = Callable[[Path, ResponsesHttpServer], Any]
ServerFactory = Callable[[ResponsesGateway], ResponsesHttpServer]
GatewayEndpointFactory = Callable[
    [ResponsesHttpServer, ModelBackedEvalPolicy],
    GatewayEndpointContract,
]


class ModelBackedOperator:
    """Execute one preregistered paired schedule through ``TrialController``."""

    def __init__(
        self,
        *,
        providers: ProviderBindings,
        oci_runner_factory: RunnerFactory,
        signer: AttestationSigner | None = None,
        broker_factory: Callable[[], CredentialBroker] = CredentialBroker,
        server_factory: ServerFactory = ResponsesHttpServer,
        gateway_endpoint_factory: GatewayEndpointFactory | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.providers = providers
        self.oci_runner_factory = oci_runner_factory
        self.signer = signer or AttestationSigner.create(
            key_id="atv-local-operator-ephemeral",
        )
        self.broker_factory = broker_factory
        self.server_factory = server_factory
        self.gateway_endpoint_factory = gateway_endpoint_factory
        self.clock = clock

    def run(
        self,
        plan: ModelBackedEvalPlan,
        output: Path | str,
    ) -> ModelBackedRunResult:
        output_path = Path(os.path.abspath(os.fspath(output)))
        if output_path.exists():
            raise ModelBackedOperatorError(
                "output_exists",
                "refusing to overwrite an existing output path",
            )
        expected_providers = set(plan.policy.provider_ids)
        if set(self.providers.backends) != expected_providers:
            raise ModelBackedOperatorError(
                "provider_policy_mismatch",
                "injected providers must match preregistered routes exactly",
            )

        temporary = output_path.with_name(
            f".{output_path.name}.partial-{uuid.uuid4().hex}"
        )
        temporary.mkdir(parents=True, exist_ok=False)
        server: ResponsesHttpServer | None = None
        endpoint: GatewayEndpointContract | None = None

        def close_endpoint() -> None:
            nonlocal endpoint
            active_endpoint = endpoint
            endpoint = None
            if active_endpoint is not None:
                active_endpoint.close()

        try:
            _write_json(temporary / "plan.json", plan.document)
            broker = self.broker_factory()
            for provider_id, credential in self.providers.credentials.items():
                broker.register_provider(provider_id, credential)

            handles = _HandleRegistry()
            budget_ledger = ResponsesBudgetLedger()
            max_logs = max(
                1_024,
                len(plan.schedule) * (plan.policy.budget.max_model_calls + 2),
            )
            gateway = _AuditedResponsesGateway(
                handle_registry=handles,
                allowed_models=plan.policy.allowed_models,
                broker=broker,
                routes=plan.policy.routes,
                backends=self.providers.backends,
                signer=self.signer,
                trial_id_resolver=handles.resolve_trial,
                budget_ledger=budget_ledger,
                config=ResponsesGatewayConfig(max_log_records=max_logs),
            )
            server = self.server_factory(gateway)
            server.start()
            active_server = server
            if self.gateway_endpoint_factory is None:
                raise ModelBackedOperatorError(
                    "gateway_endpoint_contract_required",
                    "the default host-loopback Responses server is not reachable "
                    "from a real OCI network; supply an explicit container endpoint "
                    "contract",
                )
            candidate_endpoint = self.gateway_endpoint_factory(
                active_server,
                plan.policy,
            )
            if not isinstance(candidate_endpoint, GatewayEndpointContract):
                raise ModelBackedOperatorError(
                    "gateway_endpoint_invalid",
                    "gateway_endpoint_factory must return GatewayEndpointContract",
                )
            endpoint = candidate_endpoint
            endpoint.validate_for(plan.policy)
            endpoint_document = endpoint.to_dict()
            controller_policy = plan.policy.controller_policy(
                gateway_host=endpoint.host,
                gateway_port=endpoint.port,
            )
            schedule_policy_digest = (
                plan.schedule[0].spec.model_policy.digest if plan.schedule else None
            )
            if schedule_policy_digest != controller_policy.digest:
                raise ModelBackedOperatorError(
                    "controller_policy_drift",
                    "runtime controller policy differs from the preregistered schedule",
                )

            network = plan.policy.network_policy()
            work_root = temporary / ".work"
            work_root.mkdir()
            runner = self.oci_runner_factory(work_root, active_server)
            ledger = ControllerLedger(temporary / "controller-ledger.jsonl")
            store = ContentAddressedStore(temporary / "cas")

            def gateway_healthcheck(
                handle: OpaqueTrialHandle,
                request: ControllerRunRequest,
            ) -> None:
                handles.register(handle, request)
                endpoint.healthcheck(handle, request)

            controller = TrialController(
                oci_runner=runner,
                ledger=ledger,
                store=store,
                broker=broker,
                gateway_healthcheck=gateway_healthcheck,
                clock=self.clock,
            )
            task_by_id = {item.id: item for item in plan.tasks}
            harness_by_id = {item.id: item for item in plan.harnesses}
            task_set = ControllerTaskSet(
                plan.policy.task_set_id,
                plan.policy.task_set_version,
                plan.task_set_digest,
            )
            run_id = f"local-model-backed-{plan.digest[:24]}"
            attempt_summaries: list[Mapping[str, Any]] = []
            all_violations: list[Mapping[str, Any]] = []
            for scheduled in plan.schedule:
                log_start = len(gateway.logs())
                audit_start = len(gateway.audits())
                request = ControllerRunRequest(
                    scheduled=scheduled,
                    task=task_by_id[scheduled.spec.task.id],
                    harness=harness_by_id[scheduled.spec.harness.id],
                    model_policy=controller_policy,
                    task_set=task_set,
                    run_id=run_id,
                    track=plan.policy.track,
                    network=network,
                    selected_model=plan.policy.default_model,
                )
                result = controller.run(request)
                records = tuple(gateway.logs()[log_start:])
                audits = tuple(gateway.audits()[audit_start:])
                attempt_id = scheduled.attempt.attempt_id
                summary, violations = _attempt_evidence(
                    result=result,
                    records=records,
                    audits=audits,
                    policy=plan.policy,
                    signer=self.signer,
                    handle_fingerprint=handles.fingerprint(attempt_id),
                )
                attempt_root = temporary / "attempts" / attempt_id
                _write_json(attempt_root / "summary.json", summary)
                _write_jsonl(
                    attempt_root / "gateway-records.jsonl",
                    [record.to_dict() for record in records],
                )
                _write_jsonl(
                    attempt_root / "gateway-receipts.jsonl",
                    [dict(record.attestation) for record in records],
                )
                _write_jsonl(
                    attempt_root / "gateway-ingress.jsonl",
                    [audit.to_dict() for audit in audits],
                )
                _write_jsonl(
                    attempt_root / "controller-ledger.jsonl",
                    [entry.to_dict() for entry in result.ledger_entries],
                )
                attempt_summaries.append(summary)
                all_violations.extend(violations)
                if violations:
                    break

            close_endpoint()
            server.stop()
            server = None
            shutil.rmtree(work_root, ignore_errors=True)
            lock_path = temporary / "controller-ledger.jsonl.lock"
            if lock_path.exists():
                lock_path.unlink()

            succeeded = not all_violations and len(attempt_summaries) == len(
                plan.schedule
            )
            schedule_id = plan.schedule[0].spec.schedule_id if plan.schedule else ""
            run_document = {
                "schema": RUN_SCHEMA,
                "run_id": run_id,
                "created_at": self.clock(),
                "status": "completed" if succeeded else "failed",
                "plan_digest": plan.digest,
                "complete_preregistered_policy_digest": plan.policy.digest,
                "schedule_id": schedule_id,
                "gateway_endpoint": endpoint_document,
                "scheduled_trials": len(plan.schedule),
                "executed_trials": len(attempt_summaries),
                "attempts": list(attempt_summaries),
                "violations": list(all_violations),
                "limitations": [
                    {
                        "code": "local_self_attested",
                        "detail": (
                            "Outputs are locally generated and have no independent "
                            "runner, provider, timestamp, or transparency-log proof."
                        ),
                    },
                    {
                        "code": "controller_model_export_handoff",
                        "detail": (
                            "The current TrialController cannot emit canonical "
                            "model-backed ProtocolExport evidence; this operator "
                            "preserves its internal bundle and gateway receipts."
                        ),
                    },
                    {
                        "code": "container_endpoint_contract_self_attested",
                        "detail": (
                            "The explicit endpoint contract and healthcheck are "
                            "operator supplied; this local run has no independent "
                            "proof that the endpoint was attached to the declared "
                            "private OCI network."
                        ),
                    },
                ],
                "trust_tier": TRUST_TIER,
                "rankable": False,
                "official_verified": False,
            }
            _write_json(temporary / "run.json", run_document)
            _scan_for_secrets(
                temporary,
                credentials=self.providers.credentials,
                handles=handles.raw_handles(),
            )
            files = _file_manifest(temporary)
            manifest = {
                "schema": OUTPUT_MANIFEST_SCHEMA,
                "created_at": self.clock(),
                "run_schema": RUN_SCHEMA,
                "plan_digest": plan.digest,
                "schedule_id": schedule_id,
                "status": run_document["status"],
                "files": files,
                "trust_tier": TRUST_TIER,
                "rankable": False,
                "official_verified": False,
            }
            manifest_bytes = canonical_json_bytes(manifest) + b"\n"
            _write_exclusive(temporary / "manifest.json", manifest_bytes)
            _write_exclusive(
                temporary / "manifest.sha256",
                (sha256_bytes(manifest_bytes) + "\n").encode("ascii"),
            )
            _make_read_only(temporary)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, output_path)
            completed = ModelBackedRunResult(
                root=output_path,
                succeeded=succeeded,
                plan_digest=plan.digest,
                schedule_id=schedule_id,
                attempts=tuple(attempt_summaries),
            )
            completed.verify()
            return completed
        except Exception as exc:
            try:
                close_endpoint()
            except Exception:
                exc.add_note("gateway endpoint cleanup also failed")
            if server is not None:
                server.stop()
            shutil.rmtree(temporary, ignore_errors=True)
            raise


__all__ = [
    "GatewayEndpointContract",
    "ModelBackedEvalPlan",
    "ModelBackedEvalPolicy",
    "ModelBackedOperator",
    "ModelBackedOperatorError",
    "ModelBackedRunResult",
    "ProviderBindings",
]
