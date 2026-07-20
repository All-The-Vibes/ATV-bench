"""Ed25519 DSSE envelopes and role-scoped official trust policy.

Private keys exist only in :class:`Ed25519StatementSigner`. Public verification
uses role-scoped trust roots and never receives signing capability. A successful
signature proves integrity and possession of an allowed private key under the
policy metadata; it does not prove that the signing host was uncompromised.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from atv_bench.security.attestation import canonical_json_bytes

DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
OFFICIAL_PREDICATE_BASE = "https://atv-bench.org/attestations"
_VERIFIED_SEAL = object()


class SigningError(ValueError):
    """Signing input or envelope structure is invalid."""


class TrustPolicyError(ValueError):
    """An official envelope does not satisfy the public trust policy."""


class AttestationRole(str, Enum):
    ADMISSION = "admission"
    HARNESS_BUILD = "harness-build"
    EXECUTION = "execution"
    MODEL = "model"
    EVALUATION = "evaluation"

    @property
    def predicate_type(self) -> str:
        return f"{OFFICIAL_PREDICATE_BASE}/{self.value}/v1"


@dataclass(frozen=True, slots=True)
class OfficialBindings:
    benchmark_release: str
    trial_id: str
    attempt_id: str
    task_digest: str
    harness_digest: str
    model_digest: str
    budget_digest: str
    runner_digest: str
    grader_digest: str
    grader_image_digest: str
    output_digest: str
    result_digest: str

    def __post_init__(self) -> None:
        for name in ("benchmark_release", "trial_id", "attempt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        for name in (
            "task_digest",
            "harness_digest",
            "model_digest",
            "budget_digest",
            "runner_digest",
            "grader_digest",
            "grader_image_digest",
            "output_digest",
            "result_digest",
        ):
            _require_sha256(getattr(self, name), field=name)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def subject(self, role: AttestationRole) -> list[dict[str, Any]]:
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "atv.official-subject/v1",
                    "role": role.value,
                    "bindings": self.to_dict(),
                }
            )
        ).hexdigest()
        return [
            {
                "name": f"atv-trial/{self.trial_id}/{self.attempt_id}/{role.value}",
                "digest": {"sha256": digest},
            }
        ]


def _require_sha256(value: str, *, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase sha256 digest")


def _parse_time(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TrustPolicyError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TrustPolicyError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise TrustPolicyError(f"{field} must include UTC timezone")
    return parsed.astimezone(timezone.utc)


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise TrustPolicyError(f"{field} must be base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TrustPolicyError(f"{field} is not valid base64") from exc


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE pre-authentication encoding."""
    if not isinstance(payload_type, str) or not payload_type:
        raise SigningError("payload_type must be non-empty text")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    encoded_type = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(encoded_type)).encode("ascii")
        + b" "
        + encoded_type
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


@dataclass(frozen=True, slots=True)
class DsseSignature:
    keyid: str
    sig: str

    def to_dict(self) -> dict[str, str]:
        return {"keyid": self.keyid, "sig": self.sig}


@dataclass(frozen=True, slots=True)
class SignedDsseEnvelope:
    payload_type: str
    payload: str
    signatures: tuple[DsseSignature, ...]

    def __post_init__(self) -> None:
        if self.payload_type != DSSE_PAYLOAD_TYPE:
            raise SigningError("unsupported DSSE payload type")
        if not self.signatures:
            raise SigningError("DSSE envelope requires a signature")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SignedDsseEnvelope":
        if set(value) != {"payloadType", "payload", "signatures"}:
            raise TrustPolicyError("DSSE envelope fields are invalid")
        signatures = value["signatures"]
        if not isinstance(signatures, list) or len(signatures) != 1:
            raise TrustPolicyError("official DSSE envelope requires exactly one signature")
        raw_signature = signatures[0]
        if not isinstance(raw_signature, Mapping) or set(raw_signature) != {
            "keyid",
            "sig",
        }:
            raise TrustPolicyError("DSSE signature fields are invalid")
        return cls(
            payload_type=value["payloadType"],
            payload=value["payload"],
            signatures=(
                DsseSignature(
                    keyid=raw_signature["keyid"],
                    sig=raw_signature["sig"],
                ),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "payloadType": self.payload_type,
            "payload": self.payload,
            "signatures": [signature.to_dict() for signature in self.signatures],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def statement(self) -> Mapping[str, Any]:
        payload = _b64decode(self.payload, field="DSSE payload")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrustPolicyError("DSSE payload is not JSON") from exc
        if not isinstance(value, dict):
            raise TrustPolicyError("in-toto statement must be an object")
        if canonical_json_bytes(value) != payload:
            raise TrustPolicyError("in-toto statement payload is not canonical JSON")
        return value


def build_official_statement(
    *,
    role: AttestationRole,
    bindings: OfficialBindings,
    issued_at: str,
    claims: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _parse_time(issued_at, field="issued_at")
    return {
        "_type": IN_TOTO_STATEMENT_TYPE,
        "subject": bindings.subject(role),
        "predicateType": role.predicate_type,
        "predicate": {
            "schema": "atv.official-attestation/v1",
            "role": role.value,
            "issued_at": issued_at,
            "bindings": bindings.to_dict(),
            "claims": dict(claims or {}),
        },
    }


class Ed25519StatementSigner:
    """Signer-only holder of an Ed25519 private key."""

    __slots__ = ("_private_key", "key_id")

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        *,
        key_id: str | None = None,
    ):
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("private_key must be Ed25519PrivateKey")
        self._private_key = private_key
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        derived = "ed25519:" + hashlib.sha256(public_raw).hexdigest()
        if key_id is not None and not hmac.compare_digest(key_id, derived):
            raise ValueError("key_id must equal the deterministic public-key identifier")
        self.key_id = derived

    def __repr__(self) -> str:
        return f"Ed25519StatementSigner(key_id={self.key_id!r}, private_key=<redacted>)"

    @classmethod
    def generate(cls) -> "Ed25519StatementSigner":
        return cls(Ed25519PrivateKey.generate())

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def sign_statement(
        self,
        statement: Mapping[str, Any],
    ) -> SignedDsseEnvelope:
        if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
            raise SigningError("only in-toto Statement/v1 payloads can be signed")
        payload = canonical_json_bytes(dict(statement))
        signature = self._private_key.sign(dsse_pae(DSSE_PAYLOAD_TYPE, payload))
        return SignedDsseEnvelope(
            payload_type=DSSE_PAYLOAD_TYPE,
            payload=_b64encode(payload),
            signatures=(
                DsseSignature(keyid=self.key_id, sig=_b64encode(signature)),
            ),
        )


@dataclass(frozen=True, slots=True)
class TrustedEd25519Key:
    key_id: str
    public_key: str
    roles: tuple[AttestationRole, ...]
    valid_from: str
    valid_until: str | None = None
    revoked_at: str | None = None
    revocation_reason: str | None = None
    supersedes: tuple[str, ...] = ()

    @classmethod
    def from_signer(
        cls,
        signer: Ed25519StatementSigner,
        *,
        roles: tuple[AttestationRole, ...],
        valid_from: str,
        valid_until: str | None = None,
        supersedes: tuple[str, ...] = (),
    ) -> "TrustedEd25519Key":
        return cls(
            key_id=signer.key_id,
            public_key=_b64encode(signer.public_key_bytes()),
            roles=roles,
            valid_from=valid_from,
            valid_until=valid_until,
            supersedes=supersedes,
        )

    def __post_init__(self) -> None:
        if not self.key_id or not self.roles:
            raise ValueError("trusted key requires key_id and roles")
        _parse_time(self.valid_from, field="valid_from")
        if self.valid_until is not None:
            _parse_time(self.valid_until, field="valid_until")
        if self.revoked_at is not None:
            _parse_time(self.revoked_at, field="revoked_at")
            if not self.revocation_reason:
                raise ValueError("revoked key requires revocation_reason")
        raw = _b64decode(self.public_key, field="public_key")
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 raw bytes")
        derived = "ed25519:" + hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(derived, self.key_id):
            raise ValueError("key_id does not match public key")

    def revoked(
        self,
        *,
        at: str,
        reason: str,
    ) -> "TrustedEd25519Key":
        return TrustedEd25519Key(
            key_id=self.key_id,
            public_key=self.public_key,
            roles=self.roles,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            revoked_at=at,
            revocation_reason=reason,
            supersedes=self.supersedes,
        )


class VerifiedOfficialStatement:
    """Opaque successful verification result created only by OfficialTrustPolicy."""

    __slots__ = (
        "role",
        "key_id",
        "issued_at",
        "statement",
        "envelope_digest",
        "_seal",
    )

    def __init__(
        self,
        *,
        role: AttestationRole,
        key_id: str,
        issued_at: str,
        statement: Mapping[str, Any],
        envelope_digest: str,
        _seal: object,
    ):
        if _seal is not _VERIFIED_SEAL:
            raise TypeError("VerifiedOfficialStatement is policy-produced only")
        self.role = role
        self.key_id = key_id
        self.issued_at = issued_at
        self.statement = MappingProxyType(dict(statement))
        self.envelope_digest = envelope_digest
        self._seal = _seal


class OfficialTrustPolicy:
    """Public trust roots, role assignments, and revocation metadata."""

    def __init__(
        self,
        *,
        keys: tuple[TrustedEd25519Key, ...],
        role_key_ids: Mapping[AttestationRole, tuple[str, ...]],
        verification_time: str,
        max_statement_age_seconds: int = 7 * 24 * 60 * 60,
    ):
        if not keys:
            raise ValueError("official trust policy requires public keys")
        if max_statement_age_seconds <= 0:
            raise ValueError("max_statement_age_seconds must be positive")
        self._keys = MappingProxyType({key.key_id: key for key in keys})
        if len(self._keys) != len(keys):
            raise ValueError("duplicate official key id")
        normalized: dict[AttestationRole, tuple[str, ...]] = {}
        for role in AttestationRole:
            allowed = tuple(role_key_ids.get(role, ()))
            if not allowed:
                raise ValueError(f"role {role.value} has no allowed key ids")
            for key_id in allowed:
                key = self._keys.get(key_id)
                if key is None:
                    raise ValueError(f"role {role.value} references unknown key {key_id}")
                if role not in key.roles:
                    raise ValueError(
                        f"key {key_id} metadata does not authorize role {role.value}"
                    )
            normalized[role] = allowed
        self._role_key_ids = MappingProxyType(normalized)
        self.verification_time = verification_time
        self._verification_time = _parse_time(
            verification_time,
            field="verification_time",
        )
        self.max_statement_age_seconds = max_statement_age_seconds

    @property
    def public_metadata(self) -> dict[str, Any]:
        return {
            "verification_time": self.verification_time,
            "max_statement_age_seconds": self.max_statement_age_seconds,
            "role_key_ids": {
                role.value: list(key_ids)
                for role, key_ids in self._role_key_ids.items()
            },
            "keys": [
                {
                    **asdict(key),
                    "roles": [role.value for role in key.roles],
                }
                for key in self._keys.values()
            ],
        }

    def verify(
        self,
        envelope: SignedDsseEnvelope | Mapping[str, Any],
        *,
        role: AttestationRole,
        bindings: OfficialBindings,
        required_claims: Mapping[str, Any] | None = None,
    ) -> VerifiedOfficialStatement:
        candidate = (
            envelope
            if isinstance(envelope, SignedDsseEnvelope)
            else SignedDsseEnvelope.from_dict(envelope)
        )
        signature = candidate.signatures[0]
        allowed = self._role_key_ids[role]
        if not any(hmac.compare_digest(signature.keyid, item) for item in allowed):
            raise TrustPolicyError(f"key is not allowed for role {role.value}")
        key = self._keys.get(signature.keyid)
        if key is None:
            raise TrustPolicyError("signature key is unknown")
        if key.revoked_at is not None:
            raise TrustPolicyError(
                f"signature key is revoked: {key.revocation_reason}"
            )
        if role not in key.roles:
            raise TrustPolicyError(f"signature key has wrong role for {role.value}")

        payload = _b64decode(candidate.payload, field="DSSE payload")
        signature_bytes = _b64decode(signature.sig, field="DSSE signature")
        public_key = Ed25519PublicKey.from_public_bytes(
            _b64decode(key.public_key, field="public_key")
        )
        try:
            public_key.verify(
                signature_bytes,
                dsse_pae(candidate.payload_type, payload),
            )
        except InvalidSignature as exc:
            raise TrustPolicyError("Ed25519 signature verification failed") from exc

        statement = candidate.statement()
        predicate = statement.get("predicate")
        if not isinstance(predicate, Mapping):
            raise TrustPolicyError("in-toto predicate is missing")
        if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
            raise TrustPolicyError("in-toto statement type is invalid")
        if statement.get("predicateType") != role.predicate_type:
            raise TrustPolicyError("predicate type does not match role")
        if statement.get("subject") != bindings.subject(role):
            raise TrustPolicyError("statement subject does not match official bindings")
        if predicate.get("schema") != "atv.official-attestation/v1":
            raise TrustPolicyError("official predicate schema is invalid")
        if predicate.get("role") != role.value:
            raise TrustPolicyError("predicate role does not match expected role")
        if predicate.get("bindings") != bindings.to_dict():
            raise TrustPolicyError("predicate digest bindings do not match")
        issued_at = predicate.get("issued_at")
        issued = _parse_time(issued_at, field="issued_at")
        valid_from = _parse_time(key.valid_from, field="valid_from")
        if issued < valid_from:
            raise TrustPolicyError("statement predates key validity")
        if key.valid_until is not None and issued > _parse_time(
            key.valid_until,
            field="valid_until",
        ):
            raise TrustPolicyError("statement postdates key validity")
        if issued > self._verification_time:
            raise TrustPolicyError("statement issued in the future")
        age = (self._verification_time - issued).total_seconds()
        if age > self.max_statement_age_seconds:
            raise TrustPolicyError("statement exceeds maximum accepted age")
        claims = predicate.get("claims")
        if not isinstance(claims, Mapping):
            raise TrustPolicyError("official predicate claims must be an object")
        for name, expected in (required_claims or {}).items():
            if claims.get(name) != expected:
                raise TrustPolicyError(f"required claim {name!r} does not match")
        return VerifiedOfficialStatement(
            role=role,
            key_id=signature.keyid,
            issued_at=issued_at,
            statement=statement,
            envelope_digest=candidate.digest,
            _seal=_VERIFIED_SEAL,
        )
