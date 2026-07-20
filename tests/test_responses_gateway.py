"""Hermetic tests for the strict Responses HTTP credential gateway."""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pytest

from atv_bench.security import (
    AttestationSigner,
    CapabilityMaterial,
    CredentialBroker,
    ProviderUsage,
    ResponsesBackendRequest,
    ResponsesBackendResponse,
    ResponsesBudgetLedger,
    ResponsesGateway,
    ResponsesGatewayConfig,
    ResponsesGatewayStatus,
    ResponsesHttpServer,
    ResponsesStreamBody,
    ResponsesStreamChunk,
    RouteDefinition,
    TrialBudget,
    TrialPolicy,
    UnderreportPolicy,
)

CANARY_SECRET = "ATV_RESPONSES_PROVIDER_SECRET_0b6c6c5f"
PUBLIC_MODEL = "atv-controlled-model"
PROVIDER_MODEL = "provider-model-snapshot-2026-07-20"
PROVIDER_ID = "provider-a"
ROUTE_ID = "controlled-route"


class Sequence:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.value = 0
        self.lock = threading.Lock()

    def __call__(self) -> str:
        with self.lock:
            result = f"{self.prefix}-{self.value:04d}"
            self.value += 1
            return result


class MutableClock:
    def __init__(self, value: float = 1_000.0):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            current = self.value
            self.value += 0.001
            return current


class FakeResponsesBackend:
    def __init__(
        self,
        *,
        create_outcomes: Iterable[
            ResponsesBackendResponse
            | Exception
            | Callable[[ResponsesBackendRequest], ResponsesBackendResponse]
        ] = (),
        stream_factory: Callable[
            [ResponsesBackendRequest],
            Iterable[ResponsesStreamChunk],
        ]
        | None = None,
    ):
        self.create_outcomes = list(create_outcomes)
        self.stream_factory = stream_factory
        self.requests: list[ResponsesBackendRequest] = []
        self.credential_matches: list[bool] = []
        self.cancelled_ids: list[str] = []
        self.create_count = 0
        self.stream_count = 0
        self.lock = threading.RLock()
        self.cancelled = threading.Event()

    def create(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> ResponsesBackendResponse:
        with self.lock:
            self.create_count += 1
            self.requests.append(request)
            self.credential_matches.append(credential == CANARY_SECRET)
            outcome = self.create_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome

    def stream(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> Iterable[ResponsesStreamChunk]:
        with self.lock:
            self.stream_count += 1
            self.requests.append(request)
            self.credential_matches.append(credential == CANARY_SECRET)
        if self.stream_factory is None:
            raise AssertionError("stream_factory was not configured")
        return self.stream_factory(request)

    def cancel(self, provider_request_id: str) -> None:
        with self.lock:
            self.cancelled_ids.append(provider_request_id)
        self.cancelled.set()


@dataclass
class System:
    broker: CredentialBroker
    handle: object
    policy: TrialPolicy
    route: RouteDefinition
    backend: FakeResponsesBackend
    signer: AttestationSigner
    ledger: ResponsesBudgetLedger
    gateway: ResponsesGateway


def trial_budget(**overrides: int) -> TrialBudget:
    values = {
        "max_model_calls": 10,
        "max_input_tokens": 1_000,
        "max_output_tokens": 1_000,
        "max_total_tokens": 2_000,
        "max_cost_microusd": 10_000,
    }
    values.update(overrides)
    return TrialBudget(**values)


def make_policy(
    *,
    budget: TrialBudget | None = None,
    underreport_policy: UnderreportPolicy = UnderreportPolicy.CLAMP_TO_OBSERVED,
) -> TrialPolicy:
    return TrialPolicy(
        trial_id="trial-responses-1",
        attempt_id="attempt-1",
        allowed_route_ids=(ROUTE_ID,),
        budget=budget or trial_budget(),
        underreport_policy=underreport_policy,
    )


def make_route() -> RouteDefinition:
    return RouteDefinition(
        route_id=ROUTE_ID,
        public_model=PUBLIC_MODEL,
        provider_id=PROVIDER_ID,
        provider_model=PROVIDER_MODEL,
        input_microusd_per_million=0,
        output_microusd_per_million=0,
    )


def function_call_response(
    *,
    request_id: str = "provider-request-1",
    response_id: str = "resp-1",
    model: str = PROVIDER_MODEL,
    provider_id: str = PROVIDER_ID,
    arguments: str = '{"path":"README.md"}',
    usage: ProviderUsage | None = None,
    extra_response_field: tuple[str, Any] | None = None,
    created_at: int | float = 1_721_500_000,
) -> ResponsesBackendResponse:
    response = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": "fc-1",
                "type": "function_call",
                "status": "completed",
                "arguments": arguments,
                "call_id": "call-1",
                "name": "read_file",
            }
        ],
        "usage": {
            "input_tokens": 2,
            "output_tokens": 2,
            "total_tokens": 4,
        },
    }
    if extra_response_field is not None:
        response[extra_response_field[0]] = extra_response_field[1]
    return ResponsesBackendResponse(
        provider_id=provider_id,
        model=model,
        request_id=request_id,
        response=response,
        usage=usage
        or ProviderUsage(
            input_tokens=4,
            output_tokens=4,
            cost_microusd=0,
        ),
    )


def request_payload(
    *,
    model: str = PUBLIC_MODEL,
    stream: bool = False,
    max_output_tokens: int = 8,
) -> dict[str, Any]:
    return {
        "model": model,
        "input": [{"role": "user", "content": "Inspect the repository."}],
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read one repository file.",
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
        "max_output_tokens": max_output_tokens,
        "stream": stream,
        "store": False,
    }


def make_system(
    backend: FakeResponsesBackend,
    *,
    policy: TrialPolicy | None = None,
    config: ResponsesGatewayConfig = ResponsesGatewayConfig(),
    ledger: ResponsesBudgetLedger | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> System:
    clock = MutableClock()
    capabilities = Sequence("opaque-responses-capability-" + "x" * 32)
    broker = CredentialBroker(
        clock=clock,
        handle_factory=lambda: CapabilityMaterial(
            capabilities(),
            entropy_bits=256,
        ),
        issuance_id_factory=Sequence("issuance"),
    )
    broker.register_provider(PROVIDER_ID, CANARY_SECRET)
    effective_policy = policy or make_policy()
    handle = broker.issue_trial(effective_policy, ttl_seconds=600)
    route = make_route()
    signer = AttestationSigner.create(
        key_id="responses-test-key",
        secret_factory=lambda: b"R" * 32,
    )
    effective_ledger = ledger or ResponsesBudgetLedger()
    gateway = ResponsesGateway(
        broker=broker,
        routes=[route],
        backends={PROVIDER_ID: backend},
        signer=signer,
        trial_id_resolver=lambda candidate: (
            effective_policy.trial_id
            if candidate == handle.value
            else (_ for _ in ()).throw(KeyError("unknown handle"))
        ),
        budget_ledger=effective_ledger,
        config=config,
        clock=clock,
        request_id_factory=Sequence("gateway-request"),
        provider_request_id_factory=Sequence("provider-request"),
        attestation_id_factory=Sequence("attestation"),
        token_counter=token_counter or (lambda text: 0 if not text else 1),
    )
    return System(
        broker=broker,
        handle=handle,
        policy=effective_policy,
        route=route,
        backend=backend,
        signer=signer,
        ledger=effective_ledger,
        gateway=gateway,
    )


def headers(system: System) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {system.handle.value}",
    }


def call(
    system: System,
    payload: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
):
    return system.gateway.handle_http(
        method="POST",
        path="/v1/responses",
        headers=headers(system),
        body=json.dumps(payload, separators=(",", ":")).encode(),
        cancel_event=cancel_event,
    )


def header_map(response) -> dict[str, str]:
    return {name.lower(): value for name, value in response.headers}


def decode_receipt(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding))


def sse_events(body: ResponsesStreamBody) -> list[dict[str, Any]]:
    events = []
    for chunk in body:
        assert chunk.startswith(b"data: ")
        events.append(json.loads(chunk[6:].strip()))
    return events


def wait_for(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_request_schema_size_duplicates_and_unknown_fields_fail_closed():
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(
        backend,
        config=ResponsesGatewayConfig(max_request_bytes=1_024),
    )

    unknown = request_payload()
    unknown["unreviewed_control"] = True
    response = call(system, unknown)
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_request"

    nested_unknown = request_payload()
    nested_unknown["tools"][0]["provider_secret_passthrough"] = True
    response = call(system, nested_unknown)
    assert response.status_code == 400

    duplicate = (
        b'{"model":"atv-controlled-model","model":"other",'
        b'"input":"hello","max_output_tokens":1}'
    )
    response = system.gateway.handle_http(
        method="POST",
        path="/v1/responses",
        headers=headers(system),
        body=duplicate,
    )
    assert response.status_code == 400

    oversized = b"{" + b'"x":"' + b"a" * 1_024 + b'"}'
    response = system.gateway.handle_http(
        method="POST",
        path="/v1/responses",
        headers=headers(system),
        body=oversized,
    )
    assert response.status_code == 413
    assert backend.create_count == 0


def test_nonstream_tool_call_uses_bearer_handle_without_exposing_provider_secret():
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(backend)

    response = call(system, request_payload())

    assert response.status_code == 200
    assert isinstance(response.body, bytes)
    document = json.loads(response.body)
    assert document["model"] == PUBLIC_MODEL
    assert document["output"][0] == {
        "id": "fc-1",
        "type": "function_call",
        "status": "completed",
        "arguments": '{"path":"README.md"}',
        "call_id": "call-1",
        "name": "read_file",
    }
    assert backend.credential_matches == [True]
    backend_request = backend.requests[0]
    assert backend_request.provider_model == PROVIDER_MODEL
    assert backend_request.payload["model"] == PROVIDER_MODEL
    assert system.handle.value not in json.dumps(backend_request.payload)
    assert CANARY_SECRET not in json.dumps(backend_request.payload)

    response_headers = header_map(response)
    model_receipt = decode_receipt(response_headers["x-atv-model-receipt"])
    usage_receipt = decode_receipt(response_headers["x-atv-usage-receipt"])
    assert system.signer.verify(model_receipt).integrity_valid is True
    assert system.signer.verify(usage_receipt).integrity_valid is True
    assert model_receipt["payload"]["requested_model"] == PUBLIC_MODEL
    assert (
        model_receipt["payload"]["resolved_route"]["provider_model"] == PROVIDER_MODEL
    )
    assert usage_receipt["payload"]["budget_identity"]["trial_id"] == (
        system.policy.trial_id
    )

    visible = json.dumps(
        {
            "body": document,
            "headers": response_headers,
            "logs": [record.to_dict() for record in system.gateway.logs()],
        },
        sort_keys=True,
    )
    assert CANARY_SECRET not in visible
    assert system.handle.value not in visible
    assert "Inspect the repository." not in visible
    assert system.gateway.cumulative_usage(system.handle.value).model_calls == 1


def test_valid_finite_float_request_and_response_values_round_trip():
    backend = FakeResponsesBackend(
        create_outcomes=[function_call_response(created_at=1_721_500_000.25)]
    )
    system = make_system(backend)
    payload = request_payload()
    payload["temperature"] = 0.2
    payload["top_p"] = 0.95

    response = call(system, payload)

    assert response.status_code == 200
    document = json.loads(response.body)
    assert document["created_at"] == 1_721_500_000.25
    assert backend.requests[0].payload["temperature"] == 0.2
    assert backend.requests[0].payload["top_p"] == 0.95


def test_current_response_completion_cache_and_function_caller_fields_round_trip():
    provider_result = function_call_response()
    provider_response = dict(provider_result.response)
    provider_response.update(
        {
            "completed_at": 1_721_500_001.5,
            "prompt_cache_key": "benchmark-cache-bucket",
            "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
            "prompt_cache_retention": "24h",
        }
    )
    output = [dict(provider_response["output"][0])]
    output[0]["caller"] = {
        "type": "program",
        "caller_id": "program-call-1",
    }
    provider_response["output"] = output
    backend = FakeResponsesBackend(
        create_outcomes=[
            ResponsesBackendResponse(
                provider_id=provider_result.provider_id,
                model=provider_result.model,
                request_id=provider_result.request_id,
                response=provider_response,
                usage=provider_result.usage,
            )
        ]
    )
    system = make_system(backend)

    response = call(system, request_payload())

    assert response.status_code == 200
    document = json.loads(response.body)
    assert document["completed_at"] == 1_721_500_001.5
    assert document["prompt_cache_key"] == "benchmark-cache-bucket"
    assert document["prompt_cache_options"] == {
        "mode": "implicit",
        "ttl": "30m",
    }
    assert document["prompt_cache_retention"] == "24h"
    assert document["output"][0]["caller"] == {
        "type": "program",
        "caller_id": "program-call-1",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("store", True),
        ("previous_response_id", "resp-prior"),
        ("conversation", "conversation-1"),
        (
            "prompt",
            {
                "id": "pmpt-hosted",
                "version": "1",
                "variables": {},
            },
        ),
        ("prompt_cache_key", "stable-cache-key"),
        (
            "prompt_cache_options",
            {"mode": "explicit", "ttl": "30m"},
        ),
        ("prompt_cache_retention", "24h"),
    ],
)
def test_benchmark_requests_reject_provider_side_external_state(field, value):
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(backend)
    payload = request_payload()
    payload[field] = value

    response = call(system, payload)

    assert response.status_code == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_request"
    assert backend.create_count == 0


def test_omitted_store_is_normalized_to_false_before_provider_call():
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(backend)
    payload = request_payload()
    del payload["store"]

    response = call(system, payload)

    assert response.status_code == 200
    assert backend.requests[0].payload["store"] is False


def test_precancelled_nonstream_request_commits_zero_usage_and_truthful_receipt():
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(backend)
    cancelled = threading.Event()
    cancelled.set()

    response = call(
        system,
        request_payload(),
        cancel_event=cancelled,
    )

    assert response.status_code == 499
    assert backend.create_count == 0
    assert backend.cancelled_ids == []
    assert system.gateway.cumulative_usage(system.handle.value).to_dict() == {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_microusd": 0,
    }
    usage_receipt = decode_receipt(header_map(response)["x-atv-usage-receipt"])
    assert usage_receipt["payload"]["status"] == "cancelled"
    assert usage_receipt["payload"]["usage"] == {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_microusd": 0,
    }
    assert usage_receipt["payload"]["cumulative_usage"] == {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_microusd": 0,
    }
    assert system.signer.verify(usage_receipt).integrity_valid is True


def test_precancelled_stream_request_never_starts_provider_and_commits_zero_usage():
    backend = FakeResponsesBackend(
        stream_factory=lambda _request: pytest.fail("provider must not start")
    )
    system = make_system(backend)
    cancelled = threading.Event()
    cancelled.set()

    response = call(
        system,
        request_payload(stream=True),
        cancel_event=cancelled,
    )

    assert isinstance(response.body, ResponsesStreamBody)
    assert list(response.body) == []
    wait_for(lambda: len(system.gateway.logs()) == 1)
    assert backend.stream_count == 0
    assert backend.cancelled_ids == []
    log = system.gateway.logs()[0]
    assert log.status is ResponsesGatewayStatus.CANCELLED
    assert log.usage.to_dict() == {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_microusd": 0,
    }
    assert system.signer.verify(log.attestation).integrity_valid is True
    assert log.attestation["payload"]["usage"] == log.usage.to_dict()
    assert system.gateway.cumulative_usage(system.handle.value) == log.usage


def test_nonstream_provider_secret_echo_is_replaced_with_safe_failure():
    backend = FakeResponsesBackend(
        create_outcomes=[
            function_call_response(arguments=f'{{"value":"{CANARY_SECRET}"}}')
        ]
    )
    system = make_system(backend)

    response = call(system, request_payload())

    assert response.status_code == 502
    visible = (
        response.body
        + json.dumps([record.to_dict() for record in system.gateway.logs()]).encode()
    )
    assert CANARY_SECRET.encode() not in visible
    assert json.loads(response.body)["error"]["code"] == "provider_failure"


def test_model_allowlist_and_trial_route_policy_stop_before_backend():
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(backend)

    response = call(system, request_payload(model="attacker-selected-model"))

    assert response.status_code == 403
    assert backend.create_count == 0


def test_central_ledger_reserves_concurrently_and_blocks_double_spend():
    entered = threading.Event()
    release = threading.Event()

    def blocking_result(_request: ResponsesBackendRequest):
        entered.set()
        assert release.wait(timeout=3)
        return function_call_response()

    backend = FakeResponsesBackend(create_outcomes=[blocking_result])
    system = make_system(
        backend,
        policy=make_policy(
            budget=trial_budget(
                max_model_calls=1,
                max_output_tokens=8,
                max_total_tokens=20,
            )
        ),
    )
    first_box = []
    first_thread = threading.Thread(
        target=lambda: first_box.append(call(system, request_payload())),
    )
    first_thread.start()
    assert entered.wait(timeout=3)

    second = call(system, request_payload())
    assert second.status_code == 429
    assert json.loads(second.body)["error"]["code"] == "call_budget_exceeded"
    assert backend.create_count == 1

    release.set()
    first_thread.join(timeout=3)
    assert not first_thread.is_alive()
    assert first_box[0].status_code == 200


def test_streaming_tool_call_events_are_wire_compatible_and_attested():
    final = function_call_response()

    def stream(_request: ResponsesBackendRequest):
        yield ResponsesStreamChunk(
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {
                    "id": "resp-1",
                    "object": "response",
                    "created_at": 1_721_500_000,
                    "status": "in_progress",
                    "model": PROVIDER_MODEL,
                    "output": [],
                },
            }
        )
        yield ResponsesStreamChunk(
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": {
                    "id": "fc-1",
                    "type": "function_call",
                    "status": "in_progress",
                    "arguments": "",
                    "call_id": "call-1",
                    "name": "read_file",
                },
            }
        )
        yield ResponsesStreamChunk(
            {
                "type": "response.function_call_arguments.delta",
                "sequence_number": 2,
                "output_index": 0,
                "item_id": "fc-1",
                "delta": '{"path":"README.md"}',
            }
        )
        yield ResponsesStreamChunk(
            {
                "type": "response.completed",
                "sequence_number": 3,
                "response": final.response,
            },
            final_response=final,
        )

    backend = FakeResponsesBackend(stream_factory=stream)
    system = make_system(backend)

    response = call(system, request_payload(stream=True))
    assert response.status_code == 200
    assert isinstance(response.body, ResponsesStreamBody)
    events = sse_events(response.body)

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.completed",
    ]
    assert events[0]["response"]["model"] == PUBLIC_MODEL
    assert events[2]["delta"] == '{"path":"README.md"}'
    assert events[-1]["response"]["model"] == PUBLIC_MODEL
    assert "atv_receipt" not in events[-1]
    assert backend.credential_matches == [True]
    response.body.close()
    assert backend.cancelled_ids == []
    assert len(system.gateway.logs()) == 1
    log = system.gateway.logs()[0]
    assert log.status is ResponsesGatewayStatus.SUCCESS
    assert log.streamed_events == 4
    assert system.signer.verify(log.attestation).integrity_valid is True
    model_receipt = decode_receipt(header_map(response)["x-atv-model-receipt"])
    assert system.signer.verify(model_receipt).integrity_valid is True


def test_stream_close_cancels_backend_and_charges_conservative_reservation():
    started = threading.Event()

    def stream(request: ResponsesBackendRequest):
        started.set()
        yield ResponsesStreamChunk(
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {
                    "id": "resp-cancel",
                    "object": "response",
                    "created_at": 1,
                    "status": "in_progress",
                    "model": PROVIDER_MODEL,
                    "output": [],
                },
            }
        )
        while not request.cancel_event.wait(timeout=0.01):
            pass

    backend = FakeResponsesBackend(stream_factory=stream)
    system = make_system(backend)
    response = call(system, request_payload(stream=True))
    assert isinstance(response.body, ResponsesStreamBody)
    iterator = iter(response.body)
    first = next(iterator)
    assert b"response.created" in first
    assert started.is_set()

    response.body.close()

    wait_for(lambda: bool(backend.cancelled_ids))
    wait_for(lambda: len(system.gateway.logs()) == 1)
    assert system.gateway.logs()[0].status is ResponsesGatewayStatus.CANCELLED
    usage = system.gateway.cumulative_usage(system.handle.value)
    assert usage.model_calls == 1
    assert usage.output_tokens == request_payload()["max_output_tokens"]


def test_unknown_stream_event_fails_closed_without_forwarding_provider_content():
    def unknown_stream(_request: ResponsesBackendRequest):
        yield ResponsesStreamChunk(
            {
                "type": "response.unreviewed.provider_extension",
                "sequence_number": 0,
                "delta": "provider-only detail",
            }
        )

    backend = FakeResponsesBackend(stream_factory=unknown_stream)
    system = make_system(backend)
    response = call(system, request_payload(stream=True))
    assert isinstance(response.body, ResponsesStreamBody)

    serialized = b"".join(response.body)

    assert b"provider-only detail" not in serialized
    assert b'"type":"error"' in serialized
    wait_for(lambda: len(system.gateway.logs()) == 1)
    assert system.gateway.logs()[0].status is ResponsesGatewayStatus.PROVIDER_FAILURE


def test_supported_stream_event_cannot_echo_broker_credential():
    def secret_stream(_request: ResponsesBackendRequest):
        yield ResponsesStreamChunk(
            {
                "type": "response.output_text.delta",
                "sequence_number": 0,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg-1",
                "delta": CANARY_SECRET,
                "logprobs": [],
            }
        )

    backend = FakeResponsesBackend(stream_factory=secret_stream)
    system = make_system(backend)
    response = call(system, request_payload(stream=True))
    assert isinstance(response.body, ResponsesStreamBody)

    serialized = b"".join(response.body)

    assert CANARY_SECRET.encode() not in serialized
    assert b'"code":"provider_failure"' in serialized


def test_provider_error_stream_event_is_replaced_with_safe_gateway_error():
    def provider_error(_request: ResponsesBackendRequest):
        yield ResponsesStreamChunk(
            {
                "type": "error",
                "sequence_number": 0,
                "code": "provider_internal",
                "message": "internal host provider-17.corp.example",
                "param": None,
            }
        )

    backend = FakeResponsesBackend(stream_factory=provider_error)
    system = make_system(backend)
    response = call(system, request_payload(stream=True))
    assert isinstance(response.body, ResponsesStreamBody)

    events = sse_events(response.body)

    assert events == [
        {
            "type": "error",
            "sequence_number": 0,
            "code": "provider_failure",
            "message": "model provider request failed",
            "param": None,
            "response": {"id": "gateway-request-0000"},
        }
    ]
    assert "corp.example" not in json.dumps(events)


def test_unknown_nested_stream_response_field_fails_closed():
    def stream(_request: ResponsesBackendRequest):
        yield ResponsesStreamChunk(
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {
                    "id": "resp-1",
                    "object": "response",
                    "created_at": 1,
                    "status": "in_progress",
                    "model": PROVIDER_MODEL,
                    "output": [],
                    "provider_debug": "must not cross",
                },
            }
        )

    backend = FakeResponsesBackend(stream_factory=stream)
    system = make_system(backend)
    response = call(system, request_payload(stream=True))
    assert isinstance(response.body, ResponsesStreamBody)

    events = sse_events(response.body)

    assert [event["type"] for event in events] == ["error"]
    assert "provider_debug" not in json.dumps(events)


def test_stream_output_limit_cancels_before_forwarding_excess_delta():
    def stream(_request: ResponsesBackendRequest):
        yield ResponsesStreamChunk(
            {
                "type": "response.output_text.delta",
                "sequence_number": 0,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg-1",
                "delta": "one",
                "logprobs": [],
            }
        )
        yield ResponsesStreamChunk(
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg-1",
                "delta": " two",
                "logprobs": [],
            }
        )

    backend = FakeResponsesBackend(stream_factory=stream)
    system = make_system(
        backend,
        token_counter=lambda text: len(text.split()) if text else 0,
    )
    response = call(
        system,
        request_payload(stream=True, max_output_tokens=1),
    )
    assert isinstance(response.body, ResponsesStreamBody)

    events = sse_events(response.body)

    assert [event["type"] for event in events] == [
        "response.output_text.delta",
        "error",
    ]
    assert events[0]["delta"] == "one"
    assert "two" not in json.dumps(events)
    assert backend.cancelled_ids == ["provider-request-0000"]
    assert system.gateway.logs()[0].status is ResponsesGatewayStatus.PROVIDER_FAILURE


def test_provider_response_unknown_fields_and_model_mismatch_fail_closed():
    backend = FakeResponsesBackend(
        create_outcomes=[
            function_call_response(extra_response_field=("provider_debug", "x")),
            function_call_response(
                request_id="provider-request-2",
                model="wrong-provider-model",
            ),
        ]
    )
    system = make_system(backend)

    unknown = call(system, request_payload())
    mismatch = call(system, request_payload())

    assert unknown.status_code == 502
    assert mismatch.status_code == 502
    assert json.loads(unknown.body)["error"]["code"] == "provider_failure"
    assert json.loads(mismatch.body)["error"]["code"] == "provider_failure"


def test_underreported_usage_rejects_and_accounts_observed_usage():
    backend = FakeResponsesBackend(
        create_outcomes=[
            function_call_response(
                usage=ProviderUsage(
                    input_tokens=0,
                    output_tokens=0,
                    cost_microusd=0,
                )
            )
        ]
    )
    system = make_system(
        backend,
        policy=make_policy(underreport_policy=UnderreportPolicy.REJECT),
    )

    response = call(system, request_payload())

    assert response.status_code == 502
    assert json.loads(response.body)["error"]["code"] == "usage_underreported"
    usage = system.gateway.cumulative_usage(system.handle.value)
    assert usage.input_tokens >= 1
    assert usage.output_tokens >= 1


def test_logs_are_bounded_and_never_store_request_content():
    backend = FakeResponsesBackend(
        create_outcomes=[
            function_call_response(request_id=f"provider-{index}") for index in range(3)
        ]
    )
    system = make_system(
        backend,
        config=ResponsesGatewayConfig(max_log_records=2),
    )

    for _ in range(3):
        assert call(system, request_payload()).status_code == 200

    logs = system.gateway.logs()
    assert len(logs) == 2
    assert [log.provider_request_id for log in logs] == [
        "provider-1",
        "provider-2",
    ]
    visible = json.dumps([record.to_dict() for record in logs], sort_keys=True)
    assert "Inspect the repository." not in visible
    assert '{"path":"README.md"}' not in visible
    assert CANARY_SECRET not in visible
    assert system.handle.value not in visible


def test_shared_ledger_carries_usage_across_gateway_frontend_instances():
    ledger = ResponsesBudgetLedger()
    first_backend = FakeResponsesBackend(
        create_outcomes=[function_call_response(request_id="provider-first")]
    )
    first = make_system(
        first_backend,
        policy=make_policy(budget=trial_budget(max_model_calls=1)),
        ledger=ledger,
    )
    assert call(first, request_payload()).status_code == 200

    # A second frontend over the same broker/handle and central ledger must not
    # create a fresh accounting island.
    second_backend = FakeResponsesBackend(
        create_outcomes=[function_call_response(request_id="must-not-run")]
    )
    second_gateway = ResponsesGateway(
        broker=first.broker,
        routes=[first.route],
        backends={PROVIDER_ID: second_backend},
        signer=first.signer,
        trial_id_resolver=lambda candidate: (
            first.policy.trial_id
            if candidate == first.handle.value
            else (_ for _ in ()).throw(KeyError("unknown"))
        ),
        budget_ledger=ledger,
        request_id_factory=Sequence("second-gateway"),
        provider_request_id_factory=Sequence("second-provider"),
        attestation_id_factory=Sequence("second-attestation"),
        token_counter=lambda text: 0 if not text else 1,
    )
    response = second_gateway.handle_http(
        method="POST",
        path="/v1/responses",
        headers=headers(first),
        body=json.dumps(request_payload(), separators=(",", ":")).encode(),
    )

    assert response.status_code == 429
    assert second_backend.create_count == 0


def test_stdlib_http_server_executes_nonstream_request_end_to_end():
    backend = FakeResponsesBackend(create_outcomes=[function_call_response()])
    system = make_system(backend)
    server = ResponsesHttpServer(system.gateway)
    server.start()
    try:
        body = json.dumps(request_payload(), separators=(",", ":")).encode()
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
        connection.request(
            "POST",
            "/v1/responses",
            body=body,
            headers=headers(system),
        )
        response = connection.getresponse()
        document = json.loads(response.read())
        connection.close()
    finally:
        server.stop()

    assert response.status == 200
    assert document["model"] == PUBLIC_MODEL
    assert document["output"][0]["type"] == "function_call"
    assert backend.create_count == 1


def test_http_server_stream_disconnect_invokes_cancellation():
    yielded = threading.Event()

    def stream(request: ResponsesBackendRequest):
        yielded.set()
        sequence_number = 0
        yield ResponsesStreamChunk(
            {
                "type": "response.created",
                "sequence_number": sequence_number,
                "response": {
                    "id": "resp-disconnect",
                    "object": "response",
                    "created_at": 1,
                    "status": "in_progress",
                    "model": PROVIDER_MODEL,
                    "output": [],
                },
            }
        )
        sequence_number += 1
        while not request.cancel_event.wait(timeout=0.01):
            yield ResponsesStreamChunk(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": sequence_number,
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": "msg-1",
                    "delta": "x",
                    "logprobs": [],
                }
            )
            sequence_number += 1

    backend = FakeResponsesBackend(stream_factory=stream)
    system = make_system(backend)
    server = ResponsesHttpServer(system.gateway)
    server.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    try:
        body = json.dumps(
            request_payload(stream=True),
            separators=(",", ":"),
        ).encode()
        connection.request(
            "POST",
            "/v1/responses",
            body=body,
            headers=headers(system),
        )
        response = connection.getresponse()
        assert response.status == 200
        assert yielded.wait(timeout=3)
        assert response.fp.read(32)
        response.close()
        connection.close()
        wait_for(lambda: bool(backend.cancelled_ids))
    finally:
        connection.close()
        server.stop()

    assert backend.cancelled_ids
