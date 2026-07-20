"""Credential-free model routing, budgets, retries, and safe evidence.

The gateway is trusted benchmark infrastructure. Harness code submits a
normalized request containing only an opaque trial handle. Route resolution,
provider/model allowlisting, provider credentials, cumulative budgets, and
attestation signing remain server-side.

Provider usage is not treated as cryptographic truth. The gateway independently
counts prompt and response tokens with an injected trusted counter, computes a
minimum route cost, and either rejects or clamps underreported usage according
to the trial policy.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

from atv_bench.security.attestation import (
    AttestationSigner,
    canonical_json_bytes,
)
from atv_bench.security.broker import (
    Authorization,
    BudgetIdentity,
    BrokerError,
    BrokerErrorCode,
    CredentialBroker,
    TrialBudget,
    TrialPolicy,
    UnderreportPolicy,
)


class GatewayStatus(str, Enum):
    SUCCESS = "success"
    UNKNOWN_HANDLE = "unknown_handle"
    EXPIRED_HANDLE = "expired_handle"
    REVOKED_HANDLE = "revoked_handle"
    REPLAYED_HANDLE = "replayed_handle"
    POLICY_DENIED = "policy_denied"
    ROUTE_MISMATCH = "route_mismatch"
    PROVIDER_FAILURE = "provider_failure"
    USAGE_UNDERREPORTED = "usage_underreported"
    CALL_BUDGET_EXCEEDED = "call_budget_exceeded"
    INPUT_TOKEN_BUDGET_EXCEEDED = "input_token_budget_exceeded"
    OUTPUT_TOKEN_BUDGET_EXCEEDED = "output_token_budget_exceeded"
    TOTAL_TOKEN_BUDGET_EXCEEDED = "total_token_budget_exceeded"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"


SAFE_MESSAGES = {
    GatewayStatus.SUCCESS: None,
    GatewayStatus.UNKNOWN_HANDLE: "opaque trial handle is unknown",
    GatewayStatus.EXPIRED_HANDLE: "opaque trial handle has expired",
    GatewayStatus.REVOKED_HANDLE: "opaque trial handle has been revoked",
    GatewayStatus.REPLAYED_HANDLE: "completed trial handle cannot be replayed",
    GatewayStatus.POLICY_DENIED: "request is denied by the trial model policy",
    GatewayStatus.ROUTE_MISMATCH: "requested or returned route does not match policy",
    GatewayStatus.PROVIDER_FAILURE: "provider invocation failed",
    GatewayStatus.USAGE_UNDERREPORTED: "provider usage was lower than gateway observation",
    GatewayStatus.CALL_BUDGET_EXCEEDED: "model-call budget exceeded",
    GatewayStatus.INPUT_TOKEN_BUDGET_EXCEEDED: "input-token budget exceeded",
    GatewayStatus.OUTPUT_TOKEN_BUDGET_EXCEEDED: "output-token budget exceeded",
    GatewayStatus.TOTAL_TOKEN_BUDGET_EXCEEDED: "total-token budget exceeded",
    GatewayStatus.COST_BUDGET_EXCEEDED: "model cost budget exceeded",
}


class GatewayTerminalError(Exception):
    def __init__(self, status: GatewayStatus):
        super().__init__(SAFE_MESSAGES[status])
        self.status = status
        self.safe_message = SAFE_MESSAGES[status]


@dataclass(frozen=True)
class RouteDefinition:
    route_id: str
    public_model: str
    provider_id: str
    provider_model: str
    input_microusd_per_million: int
    output_microusd_per_million: int

    def __post_init__(self) -> None:
        for name in ("route_id", "public_model", "provider_id", "provider_model"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        for name in (
            "input_microusd_per_million",
            "output_microusd_per_million",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def minimum_cost(self, input_tokens: int, output_tokens: int) -> int:
        numerator = (
            input_tokens * self.input_microusd_per_million
            + output_tokens * self.output_microusd_per_million
        )
        return (numerator + 999_999) // 1_000_000 if numerator else 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "public_model": self.public_model,
            "provider_id": self.provider_id,
            "provider_model": self.provider_model,
        }


@dataclass(frozen=True)
class NormalizedModelRequest:
    handle: str = field(repr=False)
    trial_id: str
    model: str
    prompt: str = field(repr=False)
    max_output_tokens: int
    route_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.handle or not self.trial_id or not self.model:
            raise ValueError("handle, trial_id, and model must be non-empty")
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be text")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")

    def harness_view(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "trial_id": self.trial_id,
            "model": self.model,
            "prompt": self.prompt,
            "max_output_tokens": self.max_output_tokens,
            "route_hint": self.route_hint,
        }


@dataclass(frozen=True)
class ProviderRequest:
    gateway_request_id: str
    provider_request_id: str
    provider_model: str
    prompt: str = field(repr=False)
    max_output_tokens: int


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    cost_microusd: int

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cost_microusd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class ProviderResponse:
    provider_id: str
    model: str
    request_id: str
    text: str = field(repr=False)
    usage: ProviderUsage

    def __post_init__(self) -> None:
        for name in ("provider_id", "model", "request_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty text")
        if not isinstance(self.text, str):
            raise TypeError("provider response text must be text")


class ProviderCallError(Exception):
    """Typed provider failure; its message is intentionally fixed and secret-free."""

    def __init__(
        self,
        *,
        retryable: bool,
        request_id: str | None = None,
    ):
        super().__init__("provider call failed")
        self.retryable = bool(retryable)
        self.request_id = request_id


class ModelProvider(Protocol):
    def invoke(
        self,
        credential: str | bytes,
        request: ProviderRequest,
    ) -> ProviderResponse:
        ...


@dataclass(frozen=True)
class UsageSummary:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: int = 0

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("usage fields must be non-negative integers")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def plus(self, other: "UsageSummary") -> "UsageSummary":
        return UsageSummary(
            model_calls=self.model_calls + other.model_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_microusd=self.cost_microusd + other.cost_microusd,
        )


ZERO_USAGE = UsageSummary()


@dataclass(frozen=True)
class AttemptRecord:
    sequence: int
    outcome: str
    provider_request_id: str
    usage: UsageSummary
    retryable: bool
    underreported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "outcome": self.outcome,
            "provider_request_id": self.provider_request_id,
            "usage": self.usage.to_dict(),
            "retryable": self.retryable,
            "underreported": self.underreported,
        }


@dataclass(frozen=True)
class GatewayLogRecord:
    request_id: str
    trial_id: str
    attempt_id: str | None
    trial_policy_digest: str | None
    budget_identity_digest: str | None
    handle_issuance_id: str | None
    request_digest: str
    status: GatewayStatus
    route_id: str | None
    provider_request_ids: tuple[str, ...]
    retry_count: int
    usage: UsageSummary
    started_at_ms: int
    completed_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "trial_policy_digest": self.trial_policy_digest,
            "budget_identity_digest": self.budget_identity_digest,
            "handle_issuance_id": self.handle_issuance_id,
            "request_digest": self.request_digest,
            "status": self.status.value,
            "route_id": self.route_id,
            "provider_request_ids": list(self.provider_request_ids),
            "retry_count": self.retry_count,
            "usage": self.usage.to_dict(),
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
        }


@dataclass(frozen=True)
class GatewayResult:
    status: GatewayStatus
    request_id: str
    output_text: str | None = field(default=None, repr=False)
    public_model: str | None = None
    route_id: str | None = None
    usage: UsageSummary = ZERO_USAGE
    retry_count: int = 0
    provider_request_ids: tuple[str, ...] = ()
    error_message: str | None = None
    attestation: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status is GatewayStatus.SUCCESS

    def harness_view(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "request_id": self.request_id,
            "output_text": self.output_text,
            "public_model": self.public_model,
            "route_id": self.route_id,
            "usage": self.usage.to_dict(),
            "retry_count": self.retry_count,
            "provider_request_ids": list(self.provider_request_ids),
            "error_message": self.error_message,
            "attestation": self.attestation,
        }


@dataclass
class _Counters:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: int = 0

    def add(self, usage: UsageSummary) -> None:
        self.calls += usage.model_calls
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens
        self.cost_microusd += usage.cost_microusd

    def subtract(self, usage: UsageSummary) -> None:
        self.calls -= usage.model_calls
        self.input_tokens -= usage.input_tokens
        self.output_tokens -= usage.output_tokens
        self.total_tokens -= usage.total_tokens
        self.cost_microusd -= usage.cost_microusd

    def to_usage(self) -> UsageSummary:
        return UsageSummary(
            model_calls=self.calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            cost_microusd=self.cost_microusd,
        )


@dataclass(frozen=True)
class _Reservation:
    identity: BudgetIdentity
    planned: UsageSummary


@dataclass
class _Ledger:
    committed: _Counters = field(default_factory=_Counters)
    reserved: _Counters = field(default_factory=_Counters)


class _BudgetBook:
    def __init__(self):
        self._lock = threading.RLock()
        self._ledgers: dict[BudgetIdentity, _Ledger] = {}

    def reserve(
        self,
        identity: BudgetIdentity,
        policy: TrialPolicy,
        planned: UsageSummary,
    ) -> _Reservation:
        with self._lock:
            ledger = self._ledgers.setdefault(identity, _Ledger())
            combined = ledger.committed.to_usage().plus(ledger.reserved.to_usage()).plus(planned)
            self._raise_if_exceeded(combined, policy.budget)
            ledger.reserved.add(planned)
            return _Reservation(identity, planned)

    def finalize(
        self,
        reservation: _Reservation,
        policy: TrialPolicy,
        actual: UsageSummary,
    ) -> GatewayStatus | None:
        with self._lock:
            ledger = self._ledgers[reservation.identity]
            ledger.reserved.subtract(reservation.planned)
            ledger.committed.add(actual)
            try:
                self._raise_if_exceeded(ledger.committed.to_usage(), policy.budget)
            except GatewayTerminalError as exc:
                return exc.status
            return None

    def cancel(self, reservation: _Reservation) -> None:
        with self._lock:
            ledger = self._ledgers[reservation.identity]
            ledger.reserved.subtract(reservation.planned)

    def snapshot(self, identity: BudgetIdentity) -> UsageSummary:
        with self._lock:
            ledger = self._ledgers.get(identity)
            return ledger.committed.to_usage() if ledger else ZERO_USAGE

    def state(
        self,
        identity: BudgetIdentity,
    ) -> tuple[UsageSummary, UsageSummary]:
        with self._lock:
            ledger = self._ledgers.get(identity)
            if ledger is None:
                return ZERO_USAGE, ZERO_USAGE
            return ledger.committed.to_usage(), ledger.reserved.to_usage()

    @staticmethod
    def _raise_if_exceeded(usage: UsageSummary, budget: TrialBudget) -> None:
        if usage.model_calls > budget.max_model_calls:
            raise GatewayTerminalError(GatewayStatus.CALL_BUDGET_EXCEEDED)
        if usage.input_tokens > budget.max_input_tokens:
            raise GatewayTerminalError(GatewayStatus.INPUT_TOKEN_BUDGET_EXCEEDED)
        if usage.output_tokens > budget.max_output_tokens:
            raise GatewayTerminalError(GatewayStatus.OUTPUT_TOKEN_BUDGET_EXCEEDED)
        if usage.total_tokens > budget.max_total_tokens:
            raise GatewayTerminalError(GatewayStatus.TOTAL_TOKEN_BUDGET_EXCEEDED)
        if usage.cost_microusd > budget.max_cost_microusd:
            raise GatewayTerminalError(GatewayStatus.COST_BUDGET_EXCEEDED)


def _default_id() -> str:
    return uuid.uuid4().hex


def conservative_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


BROKER_STATUS = {
    BrokerErrorCode.UNKNOWN_HANDLE: GatewayStatus.UNKNOWN_HANDLE,
    BrokerErrorCode.EXPIRED_HANDLE: GatewayStatus.EXPIRED_HANDLE,
    BrokerErrorCode.REVOKED_HANDLE: GatewayStatus.REVOKED_HANDLE,
    BrokerErrorCode.REPLAYED_HANDLE: GatewayStatus.REPLAYED_HANDLE,
    BrokerErrorCode.POLICY_DENIED: GatewayStatus.POLICY_DENIED,
}


class ModelGateway:
    def __init__(
        self,
        *,
        broker: CredentialBroker,
        routes: Iterable[RouteDefinition],
        providers: Mapping[str, ModelProvider],
        signer: AttestationSigner,
        clock: Callable[[], float] = time.time,
        request_id_factory: Callable[[], str] = _default_id,
        provider_request_id_factory: Callable[[], str] = _default_id,
        attestation_id_factory: Callable[[], str] = _default_id,
        token_counter: Callable[[str], int] = conservative_token_count,
    ):
        self._broker = broker
        self._providers = dict(providers)
        self._signer = signer
        self._clock = clock
        self._request_id_factory = request_id_factory
        self._provider_request_id_factory = provider_request_id_factory
        self._attestation_id_factory = attestation_id_factory
        self._token_counter = token_counter
        self._budgets = _BudgetBook()
        self._log_lock = threading.RLock()
        self._logs: list[GatewayLogRecord] = []

        self._routes_by_model: dict[str, RouteDefinition] = {}
        self._routes_by_id: dict[str, RouteDefinition] = {}
        for route in routes:
            if route.public_model in self._routes_by_model:
                raise ValueError(f"duplicate public model route {route.public_model!r}")
            if route.route_id in self._routes_by_id:
                raise ValueError(f"duplicate route id {route.route_id!r}")
            if route.provider_id not in self._providers:
                raise ValueError(f"route provider {route.provider_id!r} has no adapter")
            self._routes_by_model[route.public_model] = route
            self._routes_by_id[route.route_id] = route

    def execute(self, request: NormalizedModelRequest) -> GatewayResult:
        started_at_ms = int(self._clock() * 1000)
        request_id = self._request_id_factory()
        request_digest = self._request_digest(request)
        route: RouteDefinition | None = None
        attempts: list[AttemptRecord] = []
        authorization: Authorization | None = None
        policy: TrialPolicy | None = None

        try:
            authorization = self._broker.authorize(
                request.handle,
                trial_id=request.trial_id,
            )
            policy = authorization.policy
        except BrokerError as exc:
            return self._finish(
                request=request,
                request_id=request_id,
                request_digest=request_digest,
                started_at_ms=started_at_ms,
                status=BROKER_STATUS.get(exc.code, GatewayStatus.POLICY_DENIED),
                route=None,
                attempts=attempts,
                authorization=None,
            )

        route = self._routes_by_model.get(request.model)
        if route is None:
            return self._finish(
                request=request,
                request_id=request_id,
                request_digest=request_digest,
                started_at_ms=started_at_ms,
                status=GatewayStatus.POLICY_DENIED,
                route=None,
                attempts=attempts,
                authorization=authorization,
            )
        if request.route_hint is not None and request.route_hint != route.route_id:
            return self._finish(
                request=request,
                request_id=request_id,
                request_digest=request_digest,
                started_at_ms=started_at_ms,
                status=GatewayStatus.ROUTE_MISMATCH,
                route=route,
                attempts=attempts,
                authorization=authorization,
            )
        try:
            authorization = self._broker.authorize(
                request.handle,
                trial_id=request.trial_id,
                route_id=route.route_id,
            )
            policy = authorization.policy
        except BrokerError as exc:
            return self._finish(
                request=request,
                request_id=request_id,
                request_digest=request_digest,
                started_at_ms=started_at_ms,
                status=BROKER_STATUS.get(exc.code, GatewayStatus.POLICY_DENIED),
                route=route,
                attempts=attempts,
                authorization=authorization,
            )

        input_tokens = self._count_tokens(request.prompt)
        planned = UsageSummary(
            model_calls=1,
            input_tokens=input_tokens,
            output_tokens=request.max_output_tokens,
            total_tokens=input_tokens + request.max_output_tokens,
            cost_microusd=route.minimum_cost(
                input_tokens,
                request.max_output_tokens,
            ),
        )
        provider = self._providers[route.provider_id]

        for sequence in range(policy.max_retries + 1):
            try:
                reservation = self._budgets.reserve(
                    authorization.budget_identity,
                    policy,
                    planned,
                )
            except GatewayTerminalError as exc:
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=exc.status,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )

            provider_request = ProviderRequest(
                gateway_request_id=request_id,
                provider_request_id=self._provider_request_id_factory(),
                provider_model=route.provider_model,
                prompt=request.prompt,
                max_output_tokens=request.max_output_tokens,
            )
            try:
                response = self._broker.invoke_provider(
                    request.handle,
                    trial_id=request.trial_id,
                    route_id=route.route_id,
                    provider_id=route.provider_id,
                    invoker=provider.invoke,
                    request=provider_request,
                )
            except ProviderCallError as exc:
                overage = self._budgets.finalize(reservation, policy, planned)
                attempts.append(
                    AttemptRecord(
                        sequence=sequence,
                        outcome="provider_failure",
                        provider_request_id=exc.request_id
                        or provider_request.provider_request_id,
                        usage=planned,
                        retryable=exc.retryable,
                    )
                )
                if overage is not None:
                    return self._finish(
                        request=request,
                        request_id=request_id,
                        request_digest=request_digest,
                        started_at_ms=started_at_ms,
                        status=overage,
                        route=route,
                        attempts=attempts,
                        authorization=authorization,
                    )
                if exc.retryable and sequence < policy.max_retries:
                    continue
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=GatewayStatus.PROVIDER_FAILURE,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )
            except BrokerError as exc:
                if exc.provider_started:
                    charged_usage = planned
                    overage = self._budgets.finalize(
                        reservation,
                        policy,
                        charged_usage,
                    )
                else:
                    charged_usage = ZERO_USAGE
                    overage = None
                    self._budgets.cancel(reservation)
                status = BROKER_STATUS.get(exc.code, GatewayStatus.PROVIDER_FAILURE)
                attempts.append(
                    AttemptRecord(
                        sequence=sequence,
                        outcome=status.value,
                        provider_request_id=provider_request.provider_request_id,
                        usage=charged_usage,
                        retryable=False,
                    )
                )
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=overage or status,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )
            except Exception:
                overage = self._budgets.finalize(reservation, policy, planned)
                attempts.append(
                    AttemptRecord(
                        sequence=sequence,
                        outcome="provider_failure",
                        provider_request_id=provider_request.provider_request_id,
                        usage=planned,
                        retryable=False,
                    )
                )
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=overage or GatewayStatus.PROVIDER_FAILURE,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )

            if not isinstance(response, ProviderResponse):
                overage = self._budgets.finalize(reservation, policy, planned)
                attempts.append(
                    AttemptRecord(
                        sequence=sequence,
                        outcome="provider_failure",
                        provider_request_id=provider_request.provider_request_id,
                        usage=planned,
                        retryable=False,
                    )
                )
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=overage or GatewayStatus.PROVIDER_FAILURE,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )

            if (
                response.provider_id != route.provider_id
                or response.model != route.provider_model
            ):
                overage = self._budgets.finalize(reservation, policy, planned)
                attempts.append(
                    AttemptRecord(
                        sequence=sequence,
                        outcome="route_mismatch",
                        provider_request_id=response.request_id,
                        usage=planned,
                        retryable=False,
                    )
                )
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=overage or GatewayStatus.ROUTE_MISMATCH,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )

            output_tokens = self._count_tokens(response.text)
            minimum_cost = route.minimum_cost(input_tokens, output_tokens)
            underreported = (
                response.usage.input_tokens < input_tokens
                or response.usage.output_tokens < output_tokens
                or response.usage.cost_microusd < minimum_cost
            )
            actual_input = max(input_tokens, response.usage.input_tokens)
            actual_output = max(output_tokens, response.usage.output_tokens)
            actual = UsageSummary(
                model_calls=1,
                input_tokens=actual_input,
                output_tokens=actual_output,
                total_tokens=actual_input + actual_output,
                cost_microusd=max(
                    response.usage.cost_microusd,
                    route.minimum_cost(actual_input, actual_output),
                ),
            )
            output_limit_violated = actual_output > request.max_output_tokens
            overage = self._budgets.finalize(reservation, policy, actual)
            attempts.append(
                AttemptRecord(
                    sequence=sequence,
                    outcome=(
                        "provider_output_limit_violation"
                        if output_limit_violated
                        else "success" if not underreported else "usage_underreported"
                    ),
                    provider_request_id=response.request_id,
                    usage=actual,
                    retryable=False,
                    underreported=underreported,
                )
            )
            if overage is not None:
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=overage,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )
            if output_limit_violated:
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=GatewayStatus.PROVIDER_FAILURE,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )
            if (
                underreported
                and policy.underreport_policy is UnderreportPolicy.REJECT
            ):
                return self._finish(
                    request=request,
                    request_id=request_id,
                    request_digest=request_digest,
                    started_at_ms=started_at_ms,
                    status=GatewayStatus.USAGE_UNDERREPORTED,
                    route=route,
                    attempts=attempts,
                    authorization=authorization,
                )
            return self._finish(
                request=request,
                request_id=request_id,
                request_digest=request_digest,
                started_at_ms=started_at_ms,
                status=GatewayStatus.SUCCESS,
                route=route,
                attempts=attempts,
                authorization=authorization,
                output_text=response.text,
            )

        raise AssertionError("retry loop exhausted without returning")

    def logs(self) -> tuple[GatewayLogRecord, ...]:
        with self._log_lock:
            return tuple(self._logs)

    def cumulative_usage(
        self,
        handle: str,
        *,
        trial_id: str,
    ) -> UsageSummary:
        authorization = self._broker.authorize(handle, trial_id=trial_id)
        return self._budgets.snapshot(authorization.budget_identity)

    def cumulative_usage_for_identity(
        self,
        identity: BudgetIdentity,
    ) -> UsageSummary:
        """Trusted-controller lookup that remains valid after handle termination."""
        return self._budgets.snapshot(identity)

    def _count_tokens(self, text: str) -> int:
        value = self._token_counter(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token_counter must return a non-negative integer")
        return value

    @staticmethod
    def _request_digest(request: NormalizedModelRequest) -> str:
        safe_request = {
            "trial_id": request.trial_id,
            "model": request.model,
            "route_hint": request.route_hint,
            "max_output_tokens": request.max_output_tokens,
            "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
        }
        return hashlib.sha256(canonical_json_bytes(safe_request)).hexdigest()

    def _finish(
        self,
        *,
        request: NormalizedModelRequest,
        request_id: str,
        request_digest: str,
        started_at_ms: int,
        status: GatewayStatus,
        route: RouteDefinition | None,
        attempts: list[AttemptRecord],
        authorization: Authorization | None,
        output_text: str | None = None,
    ) -> GatewayResult:
        completed_at_ms = int(self._clock() * 1000)
        usage = ZERO_USAGE
        for attempt in attempts:
            usage = usage.plus(attempt.usage)
        provider_request_ids = tuple(
            attempt.provider_request_id for attempt in attempts
        )
        retry_count = max(0, len(attempts) - 1)
        policy = authorization.policy if authorization is not None else None
        if authorization is not None:
            cumulative, reserved = self._budgets.state(
                authorization.budget_identity
            )
        else:
            cumulative, reserved = ZERO_USAGE, ZERO_USAGE
        payload = {
            "schema": "atv.model-route-usage/v1",
            "attestation_id": self._attestation_id_factory(),
            "trial_id": request.trial_id,
            "gateway_request_id": request_id,
            "request_digest": request_digest,
            "status": status.value,
            "route": route.public_dict() if route is not None else None,
            "usage": usage.to_dict(),
            "cumulative_usage": cumulative.to_dict(),
            "in_flight_reserved_usage": reserved.to_dict(),
            "attempts": [attempt.to_dict() for attempt in attempts],
            "retry_count": retry_count,
            "budget": asdict(policy.budget) if policy is not None else None,
            "underreport_policy": (
                policy.underreport_policy.value if policy is not None else None
            ),
            "trial_policy_digest": (
                authorization.policy_digest if authorization is not None else None
            ),
            "budget_identity": (
                authorization.budget_identity.to_dict()
                if authorization is not None
                else None
            ),
            "handle_issuance": (
                authorization.issuance.to_dict()
                if authorization is not None
                else None
            ),
            "started_at_ms": started_at_ms,
            "completed_at_ms": completed_at_ms,
            "trust_assumptions": self._signer.trust_assumptions.to_dict(),
        }
        attestation = self._signer.sign(payload).to_dict()
        record = GatewayLogRecord(
            request_id=request_id,
            trial_id=request.trial_id,
            attempt_id=(
                authorization.attempt_id if authorization is not None else None
            ),
            trial_policy_digest=(
                authorization.policy_digest if authorization is not None else None
            ),
            budget_identity_digest=(
                authorization.budget_identity.digest
                if authorization is not None
                else None
            ),
            handle_issuance_id=(
                authorization.issuance.issuance_id
                if authorization is not None
                else None
            ),
            request_digest=request_digest,
            status=status,
            route_id=route.route_id if route else None,
            provider_request_ids=provider_request_ids,
            retry_count=retry_count,
            usage=usage,
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
        )
        with self._log_lock:
            self._logs.append(record)
        return GatewayResult(
            status=status,
            request_id=request_id,
            output_text=output_text if status is GatewayStatus.SUCCESS else None,
            public_model=route.public_model if route else request.model,
            route_id=route.route_id if route else None,
            usage=usage,
            retry_count=retry_count,
            provider_request_ids=provider_request_ids,
            error_message=SAFE_MESSAGES[status],
            attestation=attestation,
        )


@dataclass(frozen=True)
class FakeInvocation:
    sequence: int
    gateway_request_id: str
    provider_request_id: str
    provider_model: str
    max_output_tokens: int
    prompt_sha256: str
    credential_supplied: bool


class FakeProvider:
    """Deterministic provider adapter for hermetic gateway tests.

    Invocation records contain only prompt digests and a credential-present bit.
    The provider credential is never retained.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        outcomes: Iterable[
            ProviderResponse
            | ProviderCallError
            | Callable[[ProviderRequest], ProviderResponse]
        ] = (),
        credential_observer: Callable[[str | bytes], None] | None = None,
        before_invoke: Callable[[ProviderRequest, int], None] | None = None,
    ):
        self.provider_id = provider_id
        self._outcomes = list(outcomes)
        self._credential_observer = credential_observer
        self._before_invoke = before_invoke
        self._lock = threading.RLock()
        self._call_count = 0
        self._invocations: list[FakeInvocation] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    def invocations(self) -> tuple[FakeInvocation, ...]:
        with self._lock:
            return tuple(self._invocations)

    def invoke(
        self,
        credential: str | bytes,
        request: ProviderRequest,
    ) -> ProviderResponse:
        with self._lock:
            sequence = self._call_count
            self._call_count += 1
            outcome = self._outcomes.pop(0) if self._outcomes else None
            self._invocations.append(
                FakeInvocation(
                    sequence=sequence,
                    gateway_request_id=request.gateway_request_id,
                    provider_request_id=request.provider_request_id,
                    provider_model=request.provider_model,
                    max_output_tokens=request.max_output_tokens,
                    prompt_sha256=hashlib.sha256(
                        request.prompt.encode("utf-8")
                    ).hexdigest(),
                    credential_supplied=bool(credential),
                )
            )
        if self._credential_observer is not None:
            self._credential_observer(credential)
        if self._before_invoke is not None:
            self._before_invoke(request, sequence)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request)
        if isinstance(outcome, ProviderResponse):
            return outcome
        return ProviderResponse(
            provider_id=self.provider_id,
            model=request.provider_model,
            request_id=f"fake-provider-{sequence}",
            text="ok",
            usage=ProviderUsage(
                input_tokens=1,
                output_tokens=1,
                cost_microusd=1,
            ),
        )
