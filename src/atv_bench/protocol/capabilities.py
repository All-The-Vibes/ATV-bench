"""Protocol-version and capability negotiation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .canonical import canonical_digest
from .errors import CapabilityNegotiationError
from .schemas import SchemaKind, SchemaStore, default_schema_store
from .types import NegotiatedProtocol

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


def _ensure_runtime_does_not_overclaim(
    declared: Mapping[str, Any], runtime: Mapping[str, Any]
) -> None:
    for field in _BOOLEAN_CAPABILITIES:
        if runtime[field] and not declared[field]:
            raise CapabilityNegotiationError(
                f"runtime hello overclaims undeclared capability {field!r}"
            )
    if (
        _MODEL_SELECTION_RANK[runtime["model_selection"]]
        > _MODEL_SELECTION_RANK[declared["model_selection"]]
    ):
        raise CapabilityNegotiationError(
            "runtime hello overclaims 'model_selection': "
            f"declared={declared['model_selection']!r}, "
            f"runtime={runtime['model_selection']!r}"
        )
    for field in _REPORTING_CAPABILITIES:
        if _REPORTING_RANK[runtime[field]] > _REPORTING_RANK[declared[field]]:
            raise CapabilityNegotiationError(
                f"runtime hello overclaims {field!r}: "
                f"declared={declared[field]!r}, runtime={runtime[field]!r}"
            )


def _ensure_requirements(
    runtime: Mapping[str, Any], required: Mapping[str, Any]
) -> None:
    missing: list[str] = []
    for field in _BOOLEAN_CAPABILITIES:
        if required[field] and not runtime[field]:
            missing.append(field)
    if (
        _MODEL_SELECTION_RANK[runtime["model_selection"]]
        < _MODEL_SELECTION_RANK[required["model_selection"]]
    ):
        missing.append(f"model_selection>={required['model_selection']}")
    for field in _REPORTING_CAPABILITIES:
        if _REPORTING_RANK[runtime[field]] < _REPORTING_RANK[required[field]]:
            missing.append(f"{field}>={required[field]}")
    if missing:
        raise CapabilityNegotiationError(
            "required capabilities are not satisfied: " + ", ".join(missing)
        )


def negotiate_capabilities(
    harness_manifest: Mapping[str, Any],
    trial_request: Mapping[str, Any],
    hello_event: Mapping[str, Any],
    *,
    store: SchemaStore | None = None,
) -> NegotiatedProtocol:
    """Validate identities and return the selected v1 capability profile."""
    active_store = store or default_schema_store()
    active_store.validate(harness_manifest, SchemaKind.HARNESS)
    active_store.validate(trial_request, SchemaKind.TRIAL_REQUEST)
    active_store.validate(hello_event, SchemaKind.EVENT)

    if hello_event["type"] != "hello":
        raise CapabilityNegotiationError("capability negotiation requires a hello event")
    requested_version = trial_request["protocol_version"]
    protocol = harness_manifest["protocol"]
    if not protocol["minimum_version"] <= requested_version <= protocol["maximum_version"]:
        raise CapabilityNegotiationError(
            f"protocol v{requested_version} is outside the harness range "
            f"{protocol['minimum_version']}..{protocol['maximum_version']}"
        )
    if requested_version not in hello_event["supported_protocol_versions"]:
        raise CapabilityNegotiationError(
            f"runtime hello does not support requested protocol v{requested_version}"
        )

    request_harness = trial_request["harness"]
    if harness_manifest["id"] != request_harness["id"]:
        raise CapabilityNegotiationError("harness id does not match the trial request")
    if harness_manifest["version"] != request_harness["version"]:
        raise CapabilityNegotiationError("harness version does not match the trial request")
    if canonical_digest(harness_manifest) != request_harness["manifest_digest"]:
        raise CapabilityNegotiationError(
            "harness manifest digest does not match the trial request"
        )
    if hello_event["harness"] != request_harness:
        raise CapabilityNegotiationError(
            "runtime harness identity does not match the trial request"
        )
    if hello_event["trial_id"] != trial_request["trial_id"]:
        raise CapabilityNegotiationError("hello trial_id does not match the request")
    if hello_event["attempt_id"] != trial_request["attempt_id"]:
        raise CapabilityNegotiationError("hello attempt_id does not match the request")

    declared = harness_manifest["capabilities"]
    runtime = hello_event["capabilities"]
    required = trial_request["required_capabilities"]
    _ensure_runtime_does_not_overclaim(declared, runtime)
    _ensure_requirements(runtime, required)
    forbidden = trial_request["forbidden_capabilities"]
    violated = [field for field in forbidden if runtime[field]]
    if violated:
        raise CapabilityNegotiationError(
            "forbidden capabilities are enabled: " + ", ".join(violated)
        )

    security = harness_manifest["security"]
    request_policy = trial_request["policy"]
    credential_names = {item["name"] for item in request_policy["credentials"]}
    undeclared_credentials = credential_names - set(security["env_allowlist"])
    if undeclared_credentials:
        raise CapabilityNegotiationError(
            "trial credentials are not declared by the harness manifest: "
            + ", ".join(sorted(undeclared_credentials))
        )
    undeclared_paths = set(request_policy["writable_paths"]) - set(
        security["writable_paths"]
    )
    if undeclared_paths:
        raise CapabilityNegotiationError(
            "trial writable paths are not declared by the harness manifest: "
            + ", ".join(sorted(undeclared_paths))
        )

    network = request_policy["network"]
    gateway = trial_request["model_policy"]["gateway"]
    if network["mode"] == "model-gateway-only":
        if network["allowed_destinations"] != [gateway]:
            raise CapabilityNegotiationError(
                "model-gateway-only policy must name exactly the model policy gateway"
            )
    requirement = security["network_requirement"]
    if requirement == "model-gateway-only":
        if network["mode"] == "none" or gateway not in network["allowed_destinations"]:
            raise CapabilityNegotiationError(
                "harness requires model-gateway-only network access"
            )
    return NegotiatedProtocol(
        version=requested_version,
        capabilities=deepcopy(runtime),
    )


def build_accepted_event(
    trial_request: Mapping[str, Any],
    negotiation: NegotiatedProtocol,
    *,
    emitted_at: str,
    recorded_at: str | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
    """Build the controller-authored accepted event for a negotiated request."""
    return {
        "schema": "atv.event/v1",
        "type": "accepted",
        "source": "controller",
        "protocol_version": negotiation.version,
        "trial_id": trial_request["trial_id"],
        "attempt_id": trial_request["attempt_id"],
        "sequence": sequence,
        "emitted_at": emitted_at,
        "recorded_at": recorded_at or emitted_at,
        "selected_protocol_version": negotiation.version,
        "capabilities": deepcopy(dict(negotiation.capabilities)),
        "effective_budget_limits": deepcopy(trial_request["budget_limits"]),
        "effective_protocol_limits": deepcopy(trial_request["protocol_limits"]),
        "request_digest": canonical_digest(trial_request),
        "policy_digest": canonical_digest(trial_request["policy"]),
    }
