"""Hermetic security tests for credential custody, routing, budgets, and evidence."""
from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import asdict, dataclass

import pytest

from atv_bench.security import (
    AttestationSigner,
    BrokerError,
    BrokerErrorCode,
    CapabilityMaterial,
    CredentialBroker,
    FakeProvider,
    GatewayStatus,
    ModelGateway,
    NormalizedModelRequest,
    ProviderCallError,
    ProviderResponse,
    ProviderUsage,
    RouteDefinition,
    TrialBudget,
    TrialPolicy,
    UnderreportPolicy,
    canonical_json_bytes,
)

CANARY_SECRET = "ATV_CANARY_PROVIDER_SECRET_DO_NOT_LEAK_7f4c22"
ROUTE_ID = "route-primary"
PUBLIC_MODEL = "atv-controlled-model"
PROVIDER_ID = "provider-a"
PROVIDER_MODEL = "provider-model-v1"


class MutableClock:
    def __init__(self, value: float = 1_000.0):
        self.value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


class SequenceFactory:
    def __init__(self, prefix: str, width: int = 4):
        self.prefix = prefix
        self.width = width
        self.index = 0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            value = f"{self.prefix}-{self.index:0{self.width}d}"
            self.index += 1
            return value


class BlockingFactory:
    def __init__(self, value: str):
        self.value = value
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> str:
        self.entered.set()
        assert self.release.wait(timeout=5)
        return self.value


def count_words(text: str) -> int:
    return len(text.split()) if text else 0


def budget(**overrides) -> TrialBudget:
    values = {
        "max_model_calls": 10,
        "max_input_tokens": 100,
        "max_output_tokens": 100,
        "max_total_tokens": 200,
        "max_cost_microusd": 1_000,
    }
    values.update(overrides)
    return TrialBudget(**values)


def policy(
    *,
    trial_id: str = "trial-1",
    attempt_id: str = "attempt-1",
    allowed_routes=(ROUTE_ID,),
    trial_budget: TrialBudget | None = None,
    max_retries: int = 0,
    underreport_policy: UnderreportPolicy = UnderreportPolicy.REJECT,
) -> TrialPolicy:
    return TrialPolicy(
        trial_id=trial_id,
        attempt_id=attempt_id,
        allowed_route_ids=tuple(allowed_routes),
        budget=trial_budget or budget(),
        max_retries=max_retries,
        underreport_policy=underreport_policy,
    )


def route() -> RouteDefinition:
    # One micro-USD per token makes expected costs exact in tests.
    return RouteDefinition(
        route_id=ROUTE_ID,
        public_model=PUBLIC_MODEL,
        provider_id=PROVIDER_ID,
        provider_model=PROVIDER_MODEL,
        input_microusd_per_million=1_000_000,
        output_microusd_per_million=1_000_000,
    )


def response(
    *,
    request_id: str = "provider-request-1",
    text: str = "done",
    input_tokens: int = 2,
    output_tokens: int = 1,
    cost_microusd: int = 3,
    provider_id: str = PROVIDER_ID,
    model: str = PROVIDER_MODEL,
) -> ProviderResponse:
    return ProviderResponse(
        provider_id=provider_id,
        model=model,
        request_id=request_id,
        text=text,
        usage=ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
        ),
    )


@dataclass
class System:
    clock: MutableClock
    broker: CredentialBroker
    handle: object
    authorization: object
    provider: FakeProvider
    signer: AttestationSigner
    gateway: ModelGateway
    trial_policy: TrialPolicy


def build_system(
    *,
    outcomes=(),
    trial_policy: TrialPolicy | None = None,
    ttl_seconds: float = 60,
    before_invoke=None,
    credential_observer=None,
    provider_request_id_factory=None,
) -> System:
    clock = MutableClock()
    handles = SequenceFactory("opaque-capability-" + "x" * 32)

    def capabilities():
        return CapabilityMaterial(handles(), entropy_bits=256)

    broker = CredentialBroker(
        clock=clock,
        handle_factory=capabilities,
        issuance_id_factory=SequenceFactory("issuance"),
    )
    broker.register_provider(PROVIDER_ID, CANARY_SECRET)
    effective_policy = trial_policy or policy()
    handle = broker.issue_trial(effective_policy, ttl_seconds=ttl_seconds)
    authorization = broker.authorize(
        handle,
        trial_id=effective_policy.trial_id,
    )
    provider = FakeProvider(
        provider_id=PROVIDER_ID,
        outcomes=outcomes,
        before_invoke=before_invoke,
        credential_observer=credential_observer,
    )
    signer = AttestationSigner.create(
        key_id="benchmark-key-1",
        secret_factory=lambda: b"S" * 32,
    )
    gateway = ModelGateway(
        broker=broker,
        routes=[route()],
        providers={PROVIDER_ID: provider},
        signer=signer,
        clock=clock,
        request_id_factory=SequenceFactory("gateway-request"),
        provider_request_id_factory=(
            provider_request_id_factory or SequenceFactory("provider-attempt")
        ),
        attestation_id_factory=SequenceFactory("attestation"),
        token_counter=count_words,
    )
    return System(
        clock=clock,
        broker=broker,
        handle=handle,
        authorization=authorization,
        provider=provider,
        signer=signer,
        gateway=gateway,
        trial_policy=effective_policy,
    )


def request(system: System, **overrides) -> NormalizedModelRequest:
    values = {
        "handle": system.handle.value,
        "trial_id": system.trial_policy.trial_id,
        "model": PUBLIC_MODEL,
        "prompt": "sensitive task",
        "max_output_tokens": 4,
    }
    values.update(overrides)
    return NormalizedModelRequest(**values)


def serialized(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def test_provider_canary_is_absent_from_every_harness_visible_surface():
    observed = []

    def observe(secret):
        observed.append(secret == CANARY_SECRET)

    system = build_system(
        outcomes=[response()],
        credential_observer=observe,
    )
    model_request = request(system)
    result = system.gateway.execute(model_request)

    assert result.status is GatewayStatus.SUCCESS
    assert observed == [True]
    visible = {
        "request": model_request.harness_view(),
        "environment": system.handle.harness_env(),
        "result": result.harness_view(),
        "logs": [record.to_dict() for record in system.gateway.logs()],
        "provider_invocations": [
            asdict(item) for item in system.provider.invocations()
        ],
        "broker_repr": repr(system.broker),
        "handle_repr": repr(system.handle),
    }
    evidence = serialized(visible)
    assert CANARY_SECRET not in evidence
    assert "sensitive task" not in serialized(
        {
            "logs": visible["logs"],
            "attestation": result.attestation,
            "provider_invocations": visible["provider_invocations"],
        }
    )
    assert visible["environment"] == {
        "ATV_MODEL_GATEWAY_HANDLE": system.handle.value
    }
    assert system.handle.value not in serialized(result.attestation)
    assert system.handle.value not in serialized(visible["logs"])
    assert result.attestation["payload"]["trial_policy_digest"] == (
        system.authorization.policy_digest
    )
    assert result.attestation["payload"]["budget_identity"] == (
        system.authorization.budget_identity.to_dict()
    )
    assert result.attestation["payload"]["handle_issuance"] == (
        system.authorization.issuance.to_dict()
    )
    assert system.signer.verify(result.attestation).integrity_valid is True


@pytest.mark.parametrize(
    "outcome",
    [
        response(text=f"malicious echo {CANARY_SECRET}"),
        RuntimeError(f"provider exception {CANARY_SECRET}"),
    ],
)
def test_provider_secret_leak_attempt_becomes_generic_provider_failure(outcome):
    system = build_system(outcomes=[outcome])

    result = system.gateway.execute(request(system))

    assert result.status is GatewayStatus.PROVIDER_FAILURE
    assert result.output_text is None
    evidence = serialized(
        {
            "result": result.harness_view(),
            "logs": [record.to_dict() for record in system.gateway.logs()],
        }
    )
    assert CANARY_SECRET not in evidence
    assert "provider exception" not in evidence


def test_default_handles_are_high_entropy_opaque_and_unique():
    broker = CredentialBroker()
    first = broker.issue_trial(policy(trial_id="trial-a"), ttl_seconds=60)
    second = broker.issue_trial(policy(trial_id="trial-b"), ttl_seconds=60)

    assert first.value != second.value
    assert len(first.value) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first.value)
    assert first.value not in repr(first)
    assert first.value not in repr(broker)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: "x" * 43,
        lambda: CapabilityMaterial("short", entropy_bits=256),
        lambda: CapabilityMaterial("x" * 43, entropy_bits=128),
        lambda: CapabilityMaterial("!" * 43, entropy_bits=256),
    ],
)
def test_weak_or_malformed_injected_capability_factories_fail_closed(factory):
    broker = CredentialBroker(handle_factory=factory)

    with pytest.raises(BrokerError) as exc:
        broker.issue_trial(policy(), ttl_seconds=60)

    assert exc.value.code is BrokerErrorCode.INVALID_CAPABILITY


def test_failed_capability_generation_does_not_partially_create_trial():
    materials = iter(
        [
            CapabilityMaterial("weak", entropy_bits=8),
            CapabilityMaterial(
                "valid_capability_material_" + "a" * 32,
                entropy_bits=256,
            ),
        ]
    )
    broker = CredentialBroker(handle_factory=lambda: next(materials))
    trial_policy = policy()

    with pytest.raises(BrokerError):
        broker.issue_trial(trial_policy, ttl_seconds=60)
    handle = broker.issue_trial(trial_policy, ttl_seconds=60)

    assert len(handle.value) >= 43


@pytest.mark.parametrize(
    "variable",
    ["", "1BAD", "BAD-NAME", "BAD=VALUE", "BAD.NAME", "A\x00B"],
)
def test_harness_environment_variable_name_is_validated(variable):
    system = build_system()
    with pytest.raises(ValueError):
        system.handle.harness_env(variable)


def test_only_one_active_handle_and_rotation_is_explicit():
    system = build_system()
    with pytest.raises(BrokerError) as exc:
        system.broker.issue_trial(system.trial_policy, ttl_seconds=60)
    assert exc.value.code is BrokerErrorCode.TRIAL_ALREADY_ACTIVE

    system.broker.revoke(system.handle)
    with pytest.raises(BrokerError) as exc:
        system.broker.issue_trial(system.trial_policy, ttl_seconds=60)
    assert exc.value.code is BrokerErrorCode.TRIAL_ALREADY_EXISTS

    replacement = system.broker.rotate_handle(
        trial_id=system.trial_policy.trial_id,
        attempt_id=system.trial_policy.attempt_id,
        ttl_seconds=60,
    )
    assert replacement.value != system.handle.value
    replacement_auth = system.broker.authorize(
        replacement,
        trial_id=system.trial_policy.trial_id,
    )
    assert replacement_auth.budget_identity == system.authorization.budget_identity
    assert replacement_auth.issuance.ordinal == 2
    assert (
        replacement_auth.issuance.parent_issuance_id
        == system.authorization.issuance.issuance_id
    )


def test_unknown_revoked_expired_and_replayed_handles_are_typed():
    unknown_system = build_system()
    unknown = unknown_system.gateway.execute(
        request(unknown_system, handle="not-a-real-handle")
    )
    assert unknown.status is GatewayStatus.UNKNOWN_HANDLE

    revoked_system = build_system()
    revoked_system.broker.revoke(revoked_system.handle)
    revoked = revoked_system.gateway.execute(request(revoked_system))
    assert revoked.status is GatewayStatus.REVOKED_HANDLE

    expired_system = build_system(ttl_seconds=5)
    expired_system.clock.advance(6)
    expired = expired_system.gateway.execute(request(expired_system))
    assert expired.status is GatewayStatus.EXPIRED_HANDLE

    replay_system = build_system()
    replay_system.broker.complete(replay_system.handle)
    replay = replay_system.gateway.execute(request(replay_system))
    assert replay.status is GatewayStatus.REPLAYED_HANDLE


def test_completion_closes_trial_and_only_explicit_retry_creates_new_attempt():
    system = build_system(
        outcomes=[response(), response(request_id="provider-retry-attempt")],
        trial_policy=policy(trial_budget=budget(max_model_calls=1)),
    )
    first = system.gateway.execute(request(system))
    assert first.status is GatewayStatus.SUCCESS
    system.broker.complete(system.handle)

    with pytest.raises(BrokerError) as exc:
        system.broker.issue_trial(system.trial_policy, ttl_seconds=60)
    assert exc.value.code is BrokerErrorCode.TRIAL_COMPLETED
    with pytest.raises(BrokerError) as exc:
        system.broker.rotate_handle(
            trial_id=system.trial_policy.trial_id,
            attempt_id=system.trial_policy.attempt_id,
            ttl_seconds=60,
        )
    assert exc.value.code is BrokerErrorCode.TRIAL_COMPLETED

    retry_handle = system.broker.create_retry_attempt(
        trial_id=system.trial_policy.trial_id,
        attempt_id="infra-retry-2",
        ttl_seconds=60,
    )
    retry_auth = system.broker.authorize(
        retry_handle,
        trial_id=system.trial_policy.trial_id,
    )
    assert retry_auth.attempt_id == "infra-retry-2"
    assert retry_auth.policy_digest == system.authorization.policy_digest
    assert retry_auth.budget_identity != system.authorization.budget_identity
    assert retry_auth.issuance.parent_attempt_id == system.trial_policy.attempt_id

    system.handle = retry_handle
    retried = system.gateway.execute(request(system))
    assert retried.status is GatewayStatus.SUCCESS
    assert retried.attestation["payload"]["budget_identity"]["attempt_id"] == (
        "infra-retry-2"
    )
    assert retried.attestation["payload"]["handle_issuance"][
        "parent_attempt_id"
    ] == system.trial_policy.attempt_id


def test_trial_and_route_policy_scope_are_enforced_before_provider():
    trial_system = build_system()
    wrong_trial = trial_system.gateway.execute(
        request(trial_system, trial_id="other-trial")
    )
    assert wrong_trial.status is GatewayStatus.POLICY_DENIED
    assert trial_system.provider.call_count == 0

    route_system = build_system(
        trial_policy=policy(allowed_routes=("different-route",))
    )
    denied_route = route_system.gateway.execute(request(route_system))
    assert denied_route.status is GatewayStatus.POLICY_DENIED
    assert route_system.provider.call_count == 0


def test_route_hint_mismatch_is_rejected_before_provider_invocation():
    system = build_system()

    result = system.gateway.execute(
        request(system, route_hint="attacker-selected-route")
    )

    assert result.status is GatewayStatus.ROUTE_MISMATCH
    assert system.provider.call_count == 0
    assert result.usage.model_calls == 0


def test_provider_returning_wrong_route_is_rejected_and_disclosed():
    system = build_system(
        outcomes=[response(provider_id="other-provider")]
    )

    result = system.gateway.execute(request(system))

    assert result.status is GatewayStatus.ROUTE_MISMATCH
    assert system.provider.call_count == 1
    assert result.provider_request_ids == ("provider-request-1",)


def test_provider_exceeding_requested_output_limit_is_charged_and_rejected():
    system = build_system(
        outcomes=[
            response(
                text="one two three four five",
                output_tokens=5,
                cost_microusd=7,
            )
        ]
    )

    result = system.gateway.execute(request(system, max_output_tokens=4))

    assert result.status is GatewayStatus.PROVIDER_FAILURE
    assert result.output_text is None
    assert result.usage.output_tokens == 5
    assert (
        result.attestation["payload"]["attempts"][0]["outcome"]
        == "provider_output_limit_violation"
    )


@pytest.mark.parametrize(
    ("trial_budget", "expected"),
    [
        (budget(max_model_calls=0), GatewayStatus.CALL_BUDGET_EXCEEDED),
        (budget(max_input_tokens=1), GatewayStatus.INPUT_TOKEN_BUDGET_EXCEEDED),
        (budget(max_output_tokens=3), GatewayStatus.OUTPUT_TOKEN_BUDGET_EXCEEDED),
        (budget(max_total_tokens=5), GatewayStatus.TOTAL_TOKEN_BUDGET_EXCEEDED),
        (budget(max_cost_microusd=5), GatewayStatus.COST_BUDGET_EXCEEDED),
    ],
)
def test_each_budget_limit_has_a_typed_terminal_status(trial_budget, expected):
    system = build_system(
        trial_policy=policy(trial_budget=trial_budget)
    )

    result = system.gateway.execute(request(system))

    assert result.status is expected
    assert system.provider.call_count == 0
    assert result.error_message


def test_cumulative_usage_blocks_later_call_before_provider():
    system = build_system(
        outcomes=[response(), response(request_id="provider-request-2")],
        trial_policy=policy(
            trial_budget=budget(
                max_model_calls=2,
                max_total_tokens=6,
            )
        ),
    )

    first = system.gateway.execute(request(system))
    second = system.gateway.execute(request(system))

    assert first.status is GatewayStatus.SUCCESS
    assert second.status is GatewayStatus.TOTAL_TOKEN_BUDGET_EXCEEDED
    assert system.provider.call_count == 1
    assert system.gateway.cumulative_usage(
        system.handle.value,
        trial_id=system.trial_policy.trial_id,
    ).total_tokens == 3


@pytest.mark.parametrize("terminal_state", ["revoked", "expired"])
def test_sequential_handle_rotation_preserves_committed_budget_and_lineage(
    terminal_state,
):
    system = build_system(
        outcomes=[response(), response(request_id="must-not-run")],
        trial_policy=policy(trial_budget=budget(max_model_calls=1)),
        ttl_seconds=5,
    )
    first = system.gateway.execute(request(system))
    assert first.status is GatewayStatus.SUCCESS

    if terminal_state == "revoked":
        system.broker.revoke(system.handle)
    else:
        system.clock.advance(6)
        expired = system.gateway.execute(request(system))
        assert expired.status is GatewayStatus.EXPIRED_HANDLE

    rotated = system.broker.rotate_handle(
        trial_id=system.trial_policy.trial_id,
        attempt_id=system.trial_policy.attempt_id,
        ttl_seconds=60,
    )
    rotated_auth = system.broker.authorize(
        rotated,
        trial_id=system.trial_policy.trial_id,
    )
    system.handle = rotated
    second = system.gateway.execute(request(system))

    assert second.status is GatewayStatus.CALL_BUDGET_EXCEEDED
    assert system.provider.call_count == 1
    assert rotated_auth.budget_identity == system.authorization.budget_identity
    lineage = second.attestation["payload"]["handle_issuance"]
    assert lineage["ordinal"] == 2
    assert lineage["parent_issuance_id"] == (
        system.authorization.issuance.issuance_id
    )
    assert second.attestation["payload"]["trial_policy_digest"] == (
        system.authorization.policy_digest
    )
    assert second.attestation["payload"]["cumulative_usage"]["model_calls"] == 1
    assert system.handle.value not in serialized(second.attestation)


def _run_atomic_race(*, trial_budget: TrialBudget, expected_second: GatewayStatus):
    entered = threading.Event()
    release = threading.Event()

    def block(_request, sequence):
        if sequence == 0:
            entered.set()
            assert release.wait(timeout=5)

    system = build_system(
        outcomes=[response(), response(request_id="provider-request-2")],
        trial_policy=policy(trial_budget=trial_budget),
        before_invoke=block,
    )
    first_result = []

    def first_call():
        first_result.append(system.gateway.execute(request(system)))

    thread = threading.Thread(target=first_call)
    thread.start()
    assert entered.wait(timeout=5)
    second = system.gateway.execute(request(system))
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert first_result[0].status is GatewayStatus.SUCCESS
    assert second.status is expected_second
    assert system.provider.call_count == 1


def test_concurrent_call_budget_reservation_is_atomic():
    _run_atomic_race(
        trial_budget=budget(max_model_calls=1),
        expected_second=GatewayStatus.CALL_BUDGET_EXCEEDED,
    )


def test_concurrent_token_budget_reservation_is_atomic():
    _run_atomic_race(
        trial_budget=budget(max_model_calls=2, max_total_tokens=6),
        expected_second=GatewayStatus.TOTAL_TOKEN_BUDGET_EXCEEDED,
    )


def test_revocation_race_allows_started_call_to_finish_but_starts_no_new_call():
    entered = threading.Event()
    release = threading.Event()

    def block(_request, sequence):
        if sequence == 0:
            entered.set()
            assert release.wait(timeout=5)

    system = build_system(
        outcomes=[response(), response(request_id="must-not-start")],
        before_invoke=block,
    )
    first_result = []

    thread = threading.Thread(
        target=lambda: first_result.append(system.gateway.execute(request(system)))
    )
    thread.start()
    assert entered.wait(timeout=5)
    system.broker.revoke(system.handle)

    denied = system.gateway.execute(request(system))
    assert denied.status is GatewayStatus.REVOKED_HANDLE
    assert denied.usage.model_calls == 0
    assert system.provider.call_count == 1

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_result[0].status is GatewayStatus.SUCCESS
    assert system.provider.call_count == 1


def test_revocation_between_authorization_and_provider_start_cancels_reservation():
    provider_ids = BlockingFactory("provider-attempt-after-race")
    system = build_system(
        outcomes=[response(request_id="must-not-start")],
        provider_request_id_factory=provider_ids,
    )
    result_box = []
    thread = threading.Thread(
        target=lambda: result_box.append(system.gateway.execute(request(system)))
    )
    thread.start()
    assert provider_ids.entered.wait(timeout=5)

    system.broker.revoke(system.handle)
    provider_ids.release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    result = result_box[0]
    assert result.status is GatewayStatus.REVOKED_HANDLE
    assert result.usage.model_calls == 0
    assert system.provider.call_count == 0
    assert system.gateway.cumulative_usage_for_identity(
        system.authorization.budget_identity
    ).model_calls == 0
    assert result.attestation["payload"]["in_flight_reserved_usage"][
        "model_calls"
    ] == 0


def test_completion_is_rejected_while_provider_call_is_in_flight():
    entered = threading.Event()
    release = threading.Event()

    def block(_request, _sequence):
        entered.set()
        assert release.wait(timeout=5)

    system = build_system(outcomes=[response()], before_invoke=block)
    result_box = []
    thread = threading.Thread(
        target=lambda: result_box.append(system.gateway.execute(request(system)))
    )
    thread.start()
    assert entered.wait(timeout=5)

    with pytest.raises(BrokerError) as exc:
        system.broker.complete(system.handle)
    assert exc.value.code is BrokerErrorCode.ATTEMPT_IN_FLIGHT

    release.set()
    thread.join(timeout=5)
    assert result_box[0].status is GatewayStatus.SUCCESS
    system.broker.complete(system.handle)


def test_concurrent_handle_rotation_preserves_inflight_budget_reservation():
    entered = threading.Event()
    release = threading.Event()

    def block(_request, sequence):
        if sequence == 0:
            entered.set()
            assert release.wait(timeout=5)

    system = build_system(
        outcomes=[response(), response(request_id="must-not-start")],
        trial_policy=policy(trial_budget=budget(max_model_calls=1)),
        before_invoke=block,
    )
    first_result = []
    old_handle = system.handle
    thread = threading.Thread(
        target=lambda: first_result.append(system.gateway.execute(request(system)))
    )
    thread.start()
    assert entered.wait(timeout=5)

    system.broker.revoke(old_handle)
    rotated = system.broker.rotate_handle(
        trial_id=system.trial_policy.trial_id,
        attempt_id=system.trial_policy.attempt_id,
        ttl_seconds=60,
    )
    rotated_auth = system.broker.authorize(
        rotated,
        trial_id=system.trial_policy.trial_id,
    )
    system.handle = rotated
    second = system.gateway.execute(request(system))

    assert second.status is GatewayStatus.CALL_BUDGET_EXCEEDED
    assert system.provider.call_count == 1
    assert rotated_auth.budget_identity == system.authorization.budget_identity
    assert second.attestation["payload"]["in_flight_reserved_usage"][
        "model_calls"
    ] == 1
    with pytest.raises(BrokerError) as exc:
        system.broker.complete(rotated)
    assert exc.value.code is BrokerErrorCode.ATTEMPT_IN_FLIGHT

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_result[0].status is GatewayStatus.SUCCESS
    assert system.provider.call_count == 1
    system.broker.complete(rotated)


def test_underreported_usage_reject_policy_detects_and_accounts():
    system = build_system(
        outcomes=[
            response(
                text="two words",
                input_tokens=0,
                output_tokens=0,
                cost_microusd=0,
            )
        ],
        trial_policy=policy(underreport_policy=UnderreportPolicy.REJECT),
    )

    result = system.gateway.execute(request(system))

    assert result.status is GatewayStatus.USAGE_UNDERREPORTED
    assert result.output_text is None
    assert result.usage.input_tokens == 2
    assert result.usage.output_tokens == 2
    assert result.usage.cost_microusd == 4
    attempt = result.attestation["payload"]["attempts"][0]
    assert attempt["underreported"] is True
    assert result.attestation["payload"]["underreport_policy"] == "reject"


def test_underreported_usage_clamp_policy_succeeds_with_observed_usage():
    system = build_system(
        outcomes=[
            response(
                text="two words",
                input_tokens=0,
                output_tokens=0,
                cost_microusd=0,
            )
        ],
        trial_policy=policy(
            underreport_policy=UnderreportPolicy.CLAMP_TO_OBSERVED
        ),
    )

    result = system.gateway.execute(request(system))

    assert result.status is GatewayStatus.SUCCESS
    assert result.output_text == "two words"
    assert result.usage.total_tokens == 4
    assert result.attestation["payload"]["attempts"][0]["underreported"] is True


def test_retry_is_disclosed_with_request_ids_and_charged_usage():
    system = build_system(
        outcomes=[
            ProviderCallError(retryable=True, request_id="provider-failure-1"),
            response(request_id="provider-success-2"),
        ],
        trial_policy=policy(
            max_retries=1,
            trial_budget=budget(
                max_model_calls=3,
                max_input_tokens=20,
                max_output_tokens=20,
                max_total_tokens=40,
                max_cost_microusd=40,
            ),
        ),
    )

    result = system.gateway.execute(request(system))

    assert result.status is GatewayStatus.SUCCESS
    assert result.retry_count == 1
    assert result.provider_request_ids == (
        "provider-failure-1",
        "provider-success-2",
    )
    assert result.usage.model_calls == 2
    assert result.usage.total_tokens == 9  # failed attempt reserves 6 + success uses 3
    log = system.gateway.logs()[0]
    assert log.retry_count == 1
    assert log.provider_request_ids == result.provider_request_ids
    attempts = result.attestation["payload"]["attempts"]
    assert [item["outcome"] for item in attempts] == [
        "provider_failure",
        "success",
    ]


def test_nonretryable_provider_failure_is_typed_and_secret_free():
    system = build_system(
        outcomes=[
            ProviderCallError(
                retryable=False,
                request_id="provider-terminal-1",
            )
        ]
    )

    result = system.gateway.execute(request(system))

    assert result.status is GatewayStatus.PROVIDER_FAILURE
    assert result.retry_count == 0
    assert result.provider_request_ids == ("provider-terminal-1",)
    assert CANARY_SECRET not in serialized(result.harness_view())


def test_attestation_verifies_and_fails_on_any_tamper():
    system = build_system(outcomes=[response()])
    result = system.gateway.execute(request(system))

    valid = system.signer.verify(result.attestation)
    assert valid.integrity_valid is True
    assert "provider" in valid.trust_assumptions.usage_authority
    assert "verified" not in result.attestation

    tampered_usage = copy.deepcopy(result.attestation)
    tampered_usage["payload"]["usage"]["cost_microusd"] += 1
    assert system.signer.verify(tampered_usage).integrity_valid is False

    tampered_route = copy.deepcopy(result.attestation)
    tampered_route["payload"]["route"]["provider_model"] = "other-model"
    assert system.signer.verify(tampered_route).integrity_valid is False

    tampered_key = copy.deepcopy(result.attestation)
    tampered_key["key_id"] = "other-key"
    assert system.signer.verify(tampered_key).integrity_valid is False

    tampered_policy = copy.deepcopy(result.attestation)
    tampered_policy["payload"]["trial_policy_digest"] = "0" * 64
    assert system.signer.verify(tampered_policy).integrity_valid is False

    tampered_lineage = copy.deepcopy(result.attestation)
    tampered_lineage["payload"]["handle_issuance"]["ordinal"] += 1
    assert system.signer.verify(tampered_lineage).integrity_valid is False


def test_canonicalization_and_signatures_are_deterministic():
    first = {
        "z": [3, {"b": 2, "a": 1}],
        "a": {"y": True, "x": None},
    }
    second = {
        "a": {"x": None, "y": True},
        "z": [3, {"a": 1, "b": 2}],
    }
    signer = AttestationSigner.create(
        key_id="deterministic",
        secret_factory=lambda: b"K" * 32,
    )

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert signer.sign(first).signature == signer.sign(second).signature
    assert CANARY_SECRET not in signer.sign(first).to_dict().__repr__()


def test_injected_clock_and_id_factories_make_evidence_reproducible():
    system = build_system(outcomes=[response()])

    result = system.gateway.execute(request(system))

    assert result.request_id == "gateway-request-0000"
    payload = result.attestation["payload"]
    assert payload["attestation_id"] == "attestation-0000"
    assert payload["started_at_ms"] == 1_000_000
    assert payload["completed_at_ms"] == 1_000_000
    assert result.attestation["key_id"] == "benchmark-key-1"
