"""Canonical HMAC attestations for gateway route and usage evidence.

An HMAC proves that an envelope was produced by a holder of the benchmark
operator's signing key and that its signed fields were not modified. It does
not prove which model a provider actually executed, that provider-side usage
was honest, or that the host running the gateway was uncompromised. Those trust
assumptions are explicit in :class:`TrustAssumptions` and are included in every
gateway attestation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

ATTESTATION_VERSION = 1
ALGORITHM = "HMAC-SHA256"


@dataclass(frozen=True)
class TrustAssumptions:
    """What a valid signature does and does not establish."""

    credential_custody: str = (
        "provider credentials are confined to the trusted credential broker and "
        "transiently exposed only to the trusted provider adapter"
    )
    route_authority: str = (
        "the gateway route registry and trial policy are controlled by the "
        "benchmark operator"
    )
    usage_authority: str = (
        "provider-reported usage is reconciled with gateway-observed token counts; "
        "this is not cryptographic proof of provider internals"
    )
    signature_scope: str = (
        "integrity and operator-key authenticity only; host compromise or signing-key "
        "compromise invalidates the assurance"
    )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


DEFAULT_TRUST_ASSUMPTIONS = TrustAssumptions()


@dataclass(frozen=True)
class SignedAttestation:
    version: int
    algorithm: str
    key_id: str
    payload: dict[str, Any]
    signature: str

    def to_dict(self) -> dict[str, Any]:
        # Round-trip through canonical JSON so callers cannot mutate our payload
        # through a shared nested reference.
        return json.loads(canonical_json_bytes(asdict(self)).decode("utf-8"))


@dataclass(frozen=True)
class VerificationResult:
    """Cryptographic integrity result, deliberately not a generic ``verified`` flag."""

    integrity_valid: bool
    key_id: str | None
    reason: str
    trust_assumptions: TrustAssumptions


def _normalize(value: Any) -> Any:
    if is_dataclass(value):
        return _normalize(asdict(value))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("floating-point values are not allowed in canonical attestations")
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical attestation object keys must be strings")
            normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    raise TypeError(f"unsupported canonical attestation value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes with sorted keys and no whitespace."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class AttestationSigner:
    """HMAC signer/verifier with an opaque key and explicit trust statement."""

    __slots__ = ("_key", "key_id", "trust_assumptions")

    def __init__(
        self,
        key: bytes,
        *,
        key_id: str,
        trust_assumptions: TrustAssumptions = DEFAULT_TRUST_ASSUMPTIONS,
    ):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("attestation HMAC key must be at least 32 bytes")
        if not key_id or not isinstance(key_id, str):
            raise ValueError("key_id must be a non-empty string")
        self._key = bytes(key)
        self.key_id = key_id
        self.trust_assumptions = trust_assumptions

    def __repr__(self) -> str:
        return f"AttestationSigner(key_id={self.key_id!r}, key=<redacted>)"

    @classmethod
    def create(
        cls,
        *,
        key_id: str,
        secret_factory: Callable[[], bytes] = lambda: secrets.token_bytes(32),
        trust_assumptions: TrustAssumptions = DEFAULT_TRUST_ASSUMPTIONS,
    ) -> "AttestationSigner":
        return cls(
            secret_factory(),
            key_id=key_id,
            trust_assumptions=trust_assumptions,
        )

    def sign(self, payload: Mapping[str, Any]) -> SignedAttestation:
        normalized_payload = _normalize(payload)
        if not isinstance(normalized_payload, dict):
            raise TypeError("attestation payload must be an object")
        protected = {
            "version": ATTESTATION_VERSION,
            "algorithm": ALGORITHM,
            "key_id": self.key_id,
            "payload": normalized_payload,
        }
        signature = hmac.new(
            self._key,
            canonical_json_bytes(protected),
            hashlib.sha256,
        ).hexdigest()
        return SignedAttestation(signature=signature, **protected)

    def verify(
        self,
        envelope: SignedAttestation | Mapping[str, Any],
    ) -> VerificationResult:
        try:
            raw = envelope.to_dict() if isinstance(envelope, SignedAttestation) else dict(envelope)
            signature = raw.pop("signature")
            if not isinstance(signature, str):
                raise TypeError("signature must be a string")
            if raw.get("version") != ATTESTATION_VERSION:
                return self._invalid(raw.get("key_id"), "unsupported attestation version")
            if raw.get("algorithm") != ALGORITHM:
                return self._invalid(raw.get("key_id"), "unsupported attestation algorithm")
            if raw.get("key_id") != self.key_id:
                return self._invalid(raw.get("key_id"), "attestation key id does not match")
            expected = hmac.new(
                self._key,
                canonical_json_bytes(raw),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return self._invalid(raw.get("key_id"), "attestation signature mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            return self._invalid(None, f"malformed attestation: {exc}")
        return VerificationResult(
            integrity_valid=True,
            key_id=self.key_id,
            reason="HMAC integrity valid under the stated trust assumptions",
            trust_assumptions=self.trust_assumptions,
        )

    def _invalid(self, key_id: Any, reason: str) -> VerificationResult:
        return VerificationResult(
            integrity_valid=False,
            key_id=key_id if isinstance(key_id, str) else None,
            reason=reason,
            trust_assumptions=self.trust_assumptions,
        )
