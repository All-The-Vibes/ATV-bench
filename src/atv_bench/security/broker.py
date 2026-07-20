"""Credential custody, opaque capabilities, and immutable trial lineage.

Provider credentials are retained only by :class:`CredentialBroker`. A handle
rotation changes the bearer capability but not the immutable budget identity:
``(trial_id, attempt_id, policy_digest)``. Revoked or expired handles can be
rotated by trusted infrastructure without resetting usage. Completing an
attempt permanently closes it; only :meth:`create_retry_attempt` can create a
separately identified infrastructure retry.

Revocation semantics are explicit: a provider invocation atomically marked
``in_flight`` before revocation may finish, but no invocation can cross the
broker's start boundary after revocation.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from enum import Enum
from typing import Any

from atv_bench.security.attestation import canonical_json_bytes

CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_-]{43,}$")
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MIN_CAPABILITY_ENTROPY_BITS = 256


class UnderreportPolicy(str, Enum):
    REJECT = "reject"
    CLAMP_TO_OBSERVED = "clamp_to_observed"


class HandleState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    COMPLETED = "completed"


class BrokerErrorCode(str, Enum):
    UNKNOWN_HANDLE = "unknown_handle"
    EXPIRED_HANDLE = "expired_handle"
    REVOKED_HANDLE = "revoked_handle"
    REPLAYED_HANDLE = "replayed_handle"
    POLICY_DENIED = "policy_denied"
    TRIAL_ALREADY_ACTIVE = "trial_already_active"
    TRIAL_ALREADY_EXISTS = "trial_already_exists"
    TRIAL_COMPLETED = "trial_completed"
    ATTEMPT_NOT_FOUND = "attempt_not_found"
    ATTEMPT_ALREADY_EXISTS = "attempt_already_exists"
    ATTEMPT_IN_FLIGHT = "attempt_in_flight"
    ROTATION_NOT_ALLOWED = "rotation_not_allowed"
    INVALID_CAPABILITY = "invalid_capability"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    CREDENTIAL_LEAK = "credential_leak"


class BrokerError(Exception):
    def __init__(
        self,
        code: BrokerErrorCode,
        safe_message: str,
        *,
        provider_started: bool = False,
    ):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.provider_started = provider_started


@dataclass(frozen=True)
class CapabilityMaterial:
    """Trusted factory output with an explicit entropy assertion."""

    value: str
    entropy_bits: int


@dataclass(frozen=True)
class TrialBudget:
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_cost_microusd: int

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class TrialPolicy:
    trial_id: str
    allowed_route_ids: tuple[str, ...]
    budget: TrialBudget
    attempt_id: str = "attempt-1"
    max_retries: int = 0
    underreport_policy: UnderreportPolicy = UnderreportPolicy.REJECT

    def __post_init__(self) -> None:
        if not self.trial_id:
            raise ValueError("trial_id must be non-empty")
        if not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        if not self.allowed_route_ids or any(not route for route in self.allowed_route_ids):
            raise ValueError("allowed_route_ids must contain at least one non-empty route")
        if len(set(self.allowed_route_ids)) != len(self.allowed_route_ids):
            raise ValueError("allowed_route_ids must be unique")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")

    @property
    def policy_digest(self) -> str:
        payload = {
            "schema": "atv.trial-model-policy/v1",
            "allowed_route_ids": sorted(self.allowed_route_ids),
            "budget": asdict(self.budget),
            "max_retries": self.max_retries,
            "underreport_policy": self.underreport_policy.value,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class BudgetIdentity:
    trial_id: str
    attempt_id: str
    policy_digest: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "trial_id": self.trial_id,
                    "attempt_id": self.attempt_id,
                    "policy_digest": self.policy_digest,
                }
            )
        ).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "policy_digest": self.policy_digest,
            "budget_identity_digest": self.digest,
        }


@dataclass(frozen=True)
class HandleIssuanceLineage:
    issuance_id: str
    ordinal: int
    parent_issuance_id: str | None
    parent_attempt_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpaqueTrialHandle:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("opaque handle must be a non-empty string")

    def __repr__(self) -> str:
        return "OpaqueTrialHandle(<redacted>)"

    def harness_env(
        self,
        variable: str = "ATV_MODEL_GATEWAY_HANDLE",
    ) -> dict[str, str]:
        if not isinstance(variable, str) or not ENVIRONMENT_NAME_RE.fullmatch(variable):
            raise ValueError(
                "environment variable name must match [A-Za-z_][A-Za-z0-9_]*"
            )
        return {variable: self.value}


@dataclass(frozen=True)
class Authorization:
    trial_id: str
    attempt_id: str
    policy: TrialPolicy
    policy_digest: str
    budget_identity: BudgetIdentity
    issuance: HandleIssuanceLineage
    issued_at: float
    expires_at: float


@dataclass
class _Lease:
    handle: str
    attempt_key: tuple[str, str]
    issuance: HandleIssuanceLineage
    issued_at: float
    expires_at: float
    state: HandleState = HandleState.ACTIVE
    in_flight: int = 0


@dataclass
class _Attempt:
    policy: TrialPolicy
    policy_digest: str
    budget_identity: BudgetIdentity
    parent_attempt_id: str | None
    completed: bool = False
    current_handle: str | None = None
    issuance_count: int = 0
    last_issuance_id: str | None = None
    in_flight: int = 0


class _SecretMaterial:
    __slots__ = ("_value",)

    def __init__(self, value: str | bytes):
        if not isinstance(value, (str, bytes)) or not value:
            raise ValueError("provider credential must be non-empty bytes or text")
        self._value = bytes(value) if isinstance(value, bytes) else str(value)

    def reveal(self) -> str | bytes:
        return self._value

    def __repr__(self) -> str:
        return "<provider-credential redacted>"


def _default_handle_factory() -> CapabilityMaterial:
    return CapabilityMaterial(
        value=secrets.token_urlsafe(32),
        entropy_bits=MIN_CAPABILITY_ENTROPY_BITS,
    )


def _default_issuance_id_factory() -> str:
    return uuid.uuid4().hex


def _contains_secret(value: Any, secret: str | bytes, seen: set[int] | None = None) -> bool:
    seen = seen or set()
    object_id = id(value)
    if object_id in seen:
        return False
    seen.add(object_id)
    secret_bytes = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    secret_text = (
        secret.decode("utf-8", errors="ignore")
        if isinstance(secret, bytes)
        else secret
    )
    if isinstance(value, str):
        return bool(secret_text) and secret_text in value
    if isinstance(value, (bytes, bytearray)):
        return bool(secret_bytes) and secret_bytes in bytes(value)
    if is_dataclass(value):
        return any(
            _contains_secret(getattr(value, field.name), secret, seen)
            for field in fields(value)
        )
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secret, seen)
            or _contains_secret(item, secret, seen)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return any(_contains_secret(item, secret, seen) for item in value)
    return False


class CredentialBroker:
    """Thread-safe credential vault and trusted trial/attempt lifecycle authority."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        handle_factory: Callable[[], CapabilityMaterial] = _default_handle_factory,
        issuance_id_factory: Callable[[], str] = _default_issuance_id_factory,
    ):
        self._clock = clock
        self._handle_factory = handle_factory
        self._issuance_id_factory = issuance_id_factory
        self._lock = threading.RLock()
        self._credentials: dict[str, _SecretMaterial] = {}
        self._leases: dict[str, _Lease] = {}
        self._attempts: dict[tuple[str, str], _Attempt] = {}
        self._trial_attempts: dict[str, list[str]] = {}
        self._active_trials: dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            f"CredentialBroker(providers={len(self._credentials)}, "
            f"leases={len(self._leases)}, attempts={len(self._attempts)}, "
            "credentials=<redacted>)"
        )

    def register_provider(self, provider_id: str, credential: str | bytes) -> None:
        if not provider_id:
            raise ValueError("provider_id must be non-empty")
        with self._lock:
            if provider_id in self._credentials:
                raise ValueError(f"provider {provider_id!r} is already registered")
            self._credentials[provider_id] = _SecretMaterial(credential)

    def provider_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._credentials))

    def issue_trial(self, policy: TrialPolicy, *, ttl_seconds: float) -> OpaqueTrialHandle:
        """Create the first attempt for a never-before-issued trial identity."""
        self._validate_ttl(ttl_seconds)
        with self._lock:
            if policy.trial_id in self._trial_attempts:
                if policy.trial_id in self._active_trials:
                    raise BrokerError(
                        BrokerErrorCode.TRIAL_ALREADY_ACTIVE,
                        "trial already has an active opaque handle",
                    )
                latest = self._latest_attempt(policy.trial_id)
                code = (
                    BrokerErrorCode.TRIAL_COMPLETED
                    if latest.completed
                    else BrokerErrorCode.TRIAL_ALREADY_EXISTS
                )
                raise BrokerError(
                    code,
                    "trial identity already exists; rotate or create an explicit retry attempt",
                )
            attempt = self._create_attempt(policy, parent_attempt_id=None)
            try:
                return self._issue_handle(attempt, ttl_seconds)
            except Exception:
                self._rollback_attempt(attempt)
                raise

    def rotate_handle(
        self,
        *,
        trial_id: str,
        attempt_id: str,
        ttl_seconds: float,
    ) -> OpaqueTrialHandle:
        """Replace a revoked/expired handle without changing policy or budget identity."""
        self._validate_ttl(ttl_seconds)
        with self._lock:
            attempt = self._attempt(trial_id, attempt_id)
            if attempt.completed:
                raise BrokerError(
                    BrokerErrorCode.TRIAL_COMPLETED,
                    "completed attempt cannot receive a replacement handle",
                )
            if attempt.current_handle is None:
                raise BrokerError(
                    BrokerErrorCode.ROTATION_NOT_ALLOWED,
                    "attempt has no prior handle to rotate",
                )
            current = self._leases[attempt.current_handle]
            self._refresh_expiry(current)
            if current.state not in {HandleState.REVOKED, HandleState.EXPIRED}:
                raise BrokerError(
                    BrokerErrorCode.ROTATION_NOT_ALLOWED,
                    "handle rotation requires a revoked or expired current handle",
                )
            return self._issue_handle(attempt, ttl_seconds)

    def create_retry_attempt(
        self,
        *,
        trial_id: str,
        attempt_id: str,
        ttl_seconds: float,
    ) -> OpaqueTrialHandle:
        """Create a separately budgeted, explicitly identified infrastructure retry."""
        self._validate_ttl(ttl_seconds)
        if not attempt_id:
            raise ValueError("attempt_id must be non-empty")
        with self._lock:
            previous = self._latest_attempt(trial_id)
            if not previous.completed:
                raise BrokerError(
                    BrokerErrorCode.POLICY_DENIED,
                    "infrastructure retry requires the prior attempt to be completed",
                )
            key = (trial_id, attempt_id)
            if key in self._attempts:
                raise BrokerError(
                    BrokerErrorCode.ATTEMPT_ALREADY_EXISTS,
                    "attempt identity already exists",
                )
            retry_policy = replace(previous.policy, attempt_id=attempt_id)
            attempt = self._create_attempt(
                retry_policy,
                parent_attempt_id=previous.policy.attempt_id,
            )
            try:
                return self._issue_handle(attempt, ttl_seconds)
            except Exception:
                self._rollback_attempt(attempt)
                raise

    def authorize(
        self,
        handle: OpaqueTrialHandle | str,
        *,
        trial_id: str,
        route_id: str | None = None,
    ) -> Authorization:
        value = self._handle_value(handle)
        with self._lock:
            lease = self._lease_for_use(value)
            attempt = self._attempt_for_lease(lease)
            self._validate_scope(attempt, trial_id=trial_id, route_id=route_id)
            return self._authorization(lease, attempt)

    def revoke(self, handle: OpaqueTrialHandle | str) -> None:
        value = self._handle_value(handle)
        with self._lock:
            lease = self._raw_lease(value)
            self._refresh_expiry(lease)
            if lease.state is HandleState.ACTIVE:
                self._transition(lease, HandleState.REVOKED)

    def complete(self, handle: OpaqueTrialHandle | str) -> None:
        value = self._handle_value(handle)
        with self._lock:
            lease = self._lease_for_use(value)
            attempt = self._attempt_for_lease(lease)
            if attempt.in_flight:
                raise BrokerError(
                    BrokerErrorCode.ATTEMPT_IN_FLIGHT,
                    "attempt cannot complete while provider calls are in flight",
                )
            attempt.completed = True
            self._transition(lease, HandleState.COMPLETED)

    def invoke_provider(
        self,
        handle: OpaqueTrialHandle | str,
        *,
        trial_id: str,
        route_id: str,
        provider_id: str,
        invoker: Callable[[str | bytes, Any], Any],
        request: Any,
    ) -> Any:
        """Cross the provider-start boundary atomically with handle authorization."""
        value = self._handle_value(handle)
        with self._lock:
            lease = self._lease_for_use(value)
            attempt = self._attempt_for_lease(lease)
            self._validate_scope(attempt, trial_id=trial_id, route_id=route_id)
            material = self._credentials.get(provider_id)
            if material is None:
                raise BrokerError(
                    BrokerErrorCode.PROVIDER_NOT_CONFIGURED,
                    "provider credential is not configured",
                )
            credential = material.reveal()
            # Once incremented, this call is considered started and may finish even
            # if trusted infrastructure revokes the handle immediately afterward.
            lease.in_flight += 1
            attempt.in_flight += 1
        try:
            try:
                result = invoker(credential, request)
            except Exception as exc:
                if _contains_secret(exc.args, credential) or _contains_secret(
                    vars(exc),
                    credential,
                ):
                    raise BrokerError(
                        BrokerErrorCode.CREDENTIAL_LEAK,
                        "provider failure contained broker-held credential material",
                        provider_started=True,
                    ) from None
                raise
            if _contains_secret(result, credential):
                raise BrokerError(
                    BrokerErrorCode.CREDENTIAL_LEAK,
                    "provider response contained broker-held credential material",
                    provider_started=True,
                )
            return result
        finally:
            with self._lock:
                lease.in_flight -= 1
                attempt.in_flight -= 1

    def _create_attempt(
        self,
        policy: TrialPolicy,
        *,
        parent_attempt_id: str | None,
    ) -> _Attempt:
        key = (policy.trial_id, policy.attempt_id)
        if key in self._attempts:
            raise BrokerError(
                BrokerErrorCode.ATTEMPT_ALREADY_EXISTS,
                "attempt identity already exists",
            )
        identity = BudgetIdentity(
            trial_id=policy.trial_id,
            attempt_id=policy.attempt_id,
            policy_digest=policy.policy_digest,
        )
        attempt = _Attempt(
            policy=policy,
            policy_digest=policy.policy_digest,
            budget_identity=identity,
            parent_attempt_id=parent_attempt_id,
        )
        self._attempts[key] = attempt
        self._trial_attempts.setdefault(policy.trial_id, []).append(policy.attempt_id)
        return attempt

    def _issue_handle(
        self,
        attempt: _Attempt,
        ttl_seconds: float,
    ) -> OpaqueTrialHandle:
        if attempt.policy.trial_id in self._active_trials:
            raise BrokerError(
                BrokerErrorCode.TRIAL_ALREADY_ACTIVE,
                "trial already has an active opaque handle",
            )
        handle = self._generate_handle()
        issuance_id = self._generate_issuance_id()
        attempt.issuance_count += 1
        issuance = HandleIssuanceLineage(
            issuance_id=issuance_id,
            ordinal=attempt.issuance_count,
            parent_issuance_id=attempt.last_issuance_id,
            parent_attempt_id=attempt.parent_attempt_id,
        )
        issued_at = self._clock()
        lease = _Lease(
            handle=handle,
            attempt_key=(attempt.policy.trial_id, attempt.policy.attempt_id),
            issuance=issuance,
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
        )
        self._leases[handle] = lease
        attempt.current_handle = handle
        attempt.last_issuance_id = issuance_id
        self._active_trials[attempt.policy.trial_id] = handle
        return OpaqueTrialHandle(handle)

    def _rollback_attempt(self, attempt: _Attempt) -> None:
        key = (attempt.policy.trial_id, attempt.policy.attempt_id)
        self._attempts.pop(key, None)
        attempt_ids = self._trial_attempts.get(attempt.policy.trial_id, [])
        if attempt_ids and attempt_ids[-1] == attempt.policy.attempt_id:
            attempt_ids.pop()
        if not attempt_ids:
            self._trial_attempts.pop(attempt.policy.trial_id, None)

    def _generate_handle(self) -> str:
        for _ in range(8):
            material = self._handle_factory()
            if not isinstance(material, CapabilityMaterial):
                raise BrokerError(
                    BrokerErrorCode.INVALID_CAPABILITY,
                    "handle factory must return CapabilityMaterial",
                )
            if (
                isinstance(material.entropy_bits, bool)
                or not isinstance(material.entropy_bits, int)
                or material.entropy_bits < MIN_CAPABILITY_ENTROPY_BITS
                or not isinstance(material.value, str)
                or not CAPABILITY_RE.fullmatch(material.value)
            ):
                raise BrokerError(
                    BrokerErrorCode.INVALID_CAPABILITY,
                    "opaque handle lacks 256-bit-equivalent URL-safe capability strength",
                )
            if material.value not in self._leases:
                return material.value
        raise BrokerError(
            BrokerErrorCode.INVALID_CAPABILITY,
            "handle factory did not produce a unique opaque capability",
        )

    def _generate_issuance_id(self) -> str:
        for _ in range(8):
            candidate = self._issuance_id_factory()
            if (
                isinstance(candidate, str)
                and candidate
                and all(
                    lease.issuance.issuance_id != candidate
                    for lease in self._leases.values()
                )
            ):
                return candidate
        raise RuntimeError("issuance id factory did not produce a unique identifier")

    def _authorization(self, lease: _Lease, attempt: _Attempt) -> Authorization:
        return Authorization(
            trial_id=attempt.policy.trial_id,
            attempt_id=attempt.policy.attempt_id,
            policy=attempt.policy,
            policy_digest=attempt.policy_digest,
            budget_identity=attempt.budget_identity,
            issuance=lease.issuance,
            issued_at=lease.issued_at,
            expires_at=lease.expires_at,
        )

    @staticmethod
    def _validate_scope(
        attempt: _Attempt,
        *,
        trial_id: str,
        route_id: str | None,
    ) -> None:
        if attempt.policy.trial_id != trial_id:
            raise BrokerError(
                BrokerErrorCode.POLICY_DENIED,
                "opaque handle is not scoped to this trial",
            )
        if route_id is not None and route_id not in attempt.policy.allowed_route_ids:
            raise BrokerError(
                BrokerErrorCode.POLICY_DENIED,
                "route is not allowed by the trial policy",
            )

    @staticmethod
    def _validate_ttl(ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

    @staticmethod
    def _handle_value(handle: OpaqueTrialHandle | str) -> str:
        return handle.value if isinstance(handle, OpaqueTrialHandle) else handle

    def _attempt(self, trial_id: str, attempt_id: str) -> _Attempt:
        attempt = self._attempts.get((trial_id, attempt_id))
        if attempt is None:
            raise BrokerError(
                BrokerErrorCode.ATTEMPT_NOT_FOUND,
                "trial attempt identity is unknown",
            )
        return attempt

    def _latest_attempt(self, trial_id: str) -> _Attempt:
        attempt_ids = self._trial_attempts.get(trial_id)
        if not attempt_ids:
            raise BrokerError(
                BrokerErrorCode.ATTEMPT_NOT_FOUND,
                "trial identity is unknown",
            )
        return self._attempts[(trial_id, attempt_ids[-1])]

    def _attempt_for_lease(self, lease: _Lease) -> _Attempt:
        return self._attempts[lease.attempt_key]

    def _raw_lease(self, handle: str) -> _Lease:
        lease = self._leases.get(handle)
        if lease is None:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_HANDLE,
                "opaque handle is unknown",
            )
        return lease

    def _lease_for_use(self, handle: str) -> _Lease:
        lease = self._raw_lease(handle)
        self._refresh_expiry(lease)
        if lease.state is HandleState.EXPIRED:
            raise BrokerError(
                BrokerErrorCode.EXPIRED_HANDLE,
                "opaque handle has expired",
            )
        if lease.state is HandleState.REVOKED:
            raise BrokerError(
                BrokerErrorCode.REVOKED_HANDLE,
                "opaque handle has been revoked",
            )
        if lease.state is HandleState.COMPLETED:
            raise BrokerError(
                BrokerErrorCode.REPLAYED_HANDLE,
                "opaque handle belongs to a completed attempt and cannot be replayed",
            )
        return lease

    def _refresh_expiry(self, lease: _Lease) -> None:
        if lease.state is HandleState.ACTIVE and self._clock() >= lease.expires_at:
            self._transition(lease, HandleState.EXPIRED)

    def _transition(self, lease: _Lease, state: HandleState) -> None:
        lease.state = state
        attempt = self._attempt_for_lease(lease)
        if self._active_trials.get(attempt.policy.trial_id) == lease.handle:
            del self._active_trials[attempt.policy.trial_id]
