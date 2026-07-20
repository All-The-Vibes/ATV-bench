from __future__ import annotations

from copy import deepcopy

import pytest

from atv_bench.protocol import (
    CapabilityNegotiationError,
    build_accepted_event,
    canonical_digest,
    negotiate_capabilities,
)


def test_negotiation_selects_v1_and_builds_controller_acceptance(protocol_documents):
    documents = protocol_documents
    negotiated = negotiate_capabilities(
        documents["harness"], documents["request"], documents["hello"]
    )
    assert negotiated.version == 1
    accepted = build_accepted_event(
        documents["request"],
        negotiated,
        emitted_at="2026-07-19T12:00:01Z",
    )
    assert accepted["source"] == "controller"
    assert accepted["request_digest"] == canonical_digest(documents["request"])
    assert accepted["policy_digest"] == canonical_digest(documents["request"]["policy"])
    assert accepted["capabilities"] == documents["hello"]["capabilities"]
    assert accepted["effective_budget_limits"] == documents["request"]["budget_limits"]
    assert accepted["effective_protocol_limits"] == documents["request"]["protocol_limits"]


def test_runtime_cannot_overclaim_manifest_capabilities(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["harness"]["capabilities"]["resumable"] = False
    documents["request"]["harness"]["manifest_digest"] = canonical_digest(
        documents["harness"]
    )
    documents["hello"]["harness"] = deepcopy(documents["request"]["harness"])
    documents["hello"]["capabilities"]["resumable"] = True
    with pytest.raises(CapabilityNegotiationError, match="overclaims"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )


def test_required_capability_and_reporting_level_must_be_satisfied(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["request"]["required_capabilities"]["resumable"] = True
    with pytest.raises(CapabilityNegotiationError, match="not satisfied"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )

    documents = deepcopy(protocol_documents)
    documents["hello"]["capabilities"]["token_usage_reporting"] = "unsupported"
    with pytest.raises(CapabilityNegotiationError, match="token_usage_reporting"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )


def test_model_selection_capability_is_ordered(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["hello"]["capabilities"]["model_selection"] = "none"
    with pytest.raises(CapabilityNegotiationError, match="model_selection"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )


def test_forbidden_capability_fails_even_if_harness_supports_it(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["request"]["forbidden_capabilities"] = ["subagents"]
    with pytest.raises(CapabilityNegotiationError, match="forbidden"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )


def test_manifest_digest_is_bound_to_request(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["harness"]["display_name"] = "Tampered"
    with pytest.raises(CapabilityNegotiationError, match="digest"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )


def test_security_surface_is_negotiated_not_inherited(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["request"]["policy"]["credentials"].append(
        {
            "name": "UNDECLARED_SECRET",
            "handle": "atv-credential://trial-0001/undeclared",
        }
    )
    with pytest.raises(CapabilityNegotiationError, match="credentials"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )

    documents = deepcopy(protocol_documents)
    documents["request"]["policy"]["writable_paths"].append("/extra")
    with pytest.raises(CapabilityNegotiationError, match="writable paths"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )


def test_model_gateway_policy_binds_exact_destination(protocol_documents):
    documents = deepcopy(protocol_documents)
    documents["request"]["policy"]["network"]["allowed_destinations"] = [
        "other-gateway.internal:443"
    ]
    with pytest.raises(CapabilityNegotiationError, match="model policy gateway"):
        negotiate_capabilities(
            documents["harness"], documents["request"], documents["hello"]
        )
