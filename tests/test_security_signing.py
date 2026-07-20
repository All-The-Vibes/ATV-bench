"""Ed25519 DSSE official-trust tests."""
from __future__ import annotations

import copy

import pytest

from atv_bench.security import VerificationResult
from atv_bench.security.attestation import TrustAssumptions
from atv_bench.security.signing import (
    AttestationRole,
    Ed25519StatementSigner,
    OfficialBindings,
    OfficialTrustPolicy,
    SignedDsseEnvelope,
    TrustedEd25519Key,
    TrustPolicyError,
    VerifiedOfficialStatement,
    build_official_statement,
    canonical_json_bytes,
)

ISSUED = "2026-07-19T12:00:00Z"
VERIFY_AT = "2026-07-19T12:10:00Z"


def _bindings(**overrides) -> OfficialBindings:
    values = {
        "benchmark_release": "ATV-2026.07.1",
        "trial_id": "1" * 64,
        "attempt_id": "2" * 64,
        "task_digest": "3" * 64,
        "harness_digest": "4" * 64,
        "model_digest": "5" * 64,
        "budget_digest": "6" * 64,
        "runner_digest": "7" * 64,
        "grader_digest": "8" * 64,
        "grader_image_digest": "9" * 64,
        "output_digest": "a" * 64,
        "result_digest": "b" * 64,
    }
    values.update(overrides)
    return OfficialBindings(**values)


def _material():
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
        role_key_ids={
            role: (signers[role].key_id,) for role in AttestationRole
        },
        verification_time=VERIFY_AT,
    )
    return signers, policy


def _envelope(role, signer, bindings=None, claims=None):
    statement = build_official_statement(
        role=role,
        bindings=bindings or _bindings(),
        issued_at=ISSUED,
        claims=claims,
    )
    return signer.sign_statement(statement)


def test_public_policy_verifies_deterministic_dsse_without_private_key():
    signers, policy = _material()
    envelope = _envelope(
        AttestationRole.ADMISSION,
        signers[AttestationRole.ADMISSION],
    )
    repeated = _envelope(
        AttestationRole.ADMISSION,
        signers[AttestationRole.ADMISSION],
    )

    verified = policy.verify(
        envelope,
        role=AttestationRole.ADMISSION,
        bindings=_bindings(),
    )

    assert isinstance(verified, VerifiedOfficialStatement)
    assert verified.key_id == signers[AttestationRole.ADMISSION].key_id
    assert envelope.to_dict() == repeated.to_dict()
    assert envelope.canonical_bytes == canonical_json_bytes(envelope.to_dict())
    assert "private" not in str(policy.public_metadata).lower()
    assert "private_key=<redacted>" in repr(signers[AttestationRole.ADMISSION])


def test_plain_statement_and_caller_constructed_verification_objects_are_rejected():
    signers, policy = _material()
    statement = build_official_statement(
        role=AttestationRole.ADMISSION,
        bindings=_bindings(),
        issued_at=ISSUED,
    )
    with pytest.raises((TrustPolicyError, TypeError, ValueError)):
        policy.verify(
            statement,
            role=AttestationRole.ADMISSION,
            bindings=_bindings(),
        )

    forged = VerificationResult(
        integrity_valid=True,
        key_id=signers[AttestationRole.ADMISSION].key_id,
        reason="caller says valid",
        trust_assumptions=TrustAssumptions(),
    )
    with pytest.raises((TrustPolicyError, TypeError, ValueError)):
        policy.verify(
            forged,  # type: ignore[arg-type]
            role=AttestationRole.ADMISSION,
            bindings=_bindings(),
        )
    with pytest.raises(TypeError):
        VerifiedOfficialStatement(
            role=AttestationRole.ADMISSION,
            key_id="forged",
            issued_at=ISSUED,
            statement=statement,
            envelope_digest="0" * 64,
            _seal=object(),
        )


def test_wrong_role_unknown_and_revoked_keys_fail():
    signers, policy = _material()
    wrong_role = _envelope(
        AttestationRole.ADMISSION,
        signers[AttestationRole.MODEL],
    )
    with pytest.raises(TrustPolicyError, match="not allowed|wrong role"):
        policy.verify(
            wrong_role,
            role=AttestationRole.ADMISSION,
            bindings=_bindings(),
        )

    unknown = Ed25519StatementSigner.generate()
    with pytest.raises(TrustPolicyError, match="not allowed|unknown"):
        policy.verify(
            _envelope(AttestationRole.ADMISSION, unknown),
            role=AttestationRole.ADMISSION,
            bindings=_bindings(),
        )

    admission = signers[AttestationRole.ADMISSION]
    revoked_key = next(
        key for key in policy._keys.values() if key.key_id == admission.key_id
    ).revoked(at=VERIFY_AT, reason="incident")
    revoked_policy = OfficialTrustPolicy(
        keys=tuple(
            revoked_key if key.key_id == admission.key_id else key
            for key in policy._keys.values()
        ),
        role_key_ids=policy._role_key_ids,
        verification_time=VERIFY_AT,
    )
    with pytest.raises(TrustPolicyError, match="revoked"):
        revoked_policy.verify(
            _envelope(AttestationRole.ADMISSION, admission),
            role=AttestationRole.ADMISSION,
            bindings=_bindings(),
        )


def test_signature_tamper_replay_and_digest_mismatch_fail():
    signers, policy = _material()
    envelope = _envelope(
        AttestationRole.EXECUTION,
        signers[AttestationRole.EXECUTION],
        claims={
            "execution_complete": True,
            "credentials_destroyed": True,
            "hidden_inputs_mounted_after_exit": True,
        },
    )
    tampered = copy.deepcopy(envelope.to_dict())
    tampered["signatures"][0]["sig"] = (
        "A" if tampered["signatures"][0]["sig"][0] != "A" else "B"
    ) + tampered["signatures"][0]["sig"][1:]
    with pytest.raises(TrustPolicyError, match="signature"):
        policy.verify(
            SignedDsseEnvelope.from_dict(tampered),
            role=AttestationRole.EXECUTION,
            bindings=_bindings(),
        )

    for mismatched in (
        _bindings(trial_id="c" * 64),
        _bindings(attempt_id="d" * 64),
        _bindings(output_digest="e" * 64),
        _bindings(result_digest="f" * 64),
    ):
        with pytest.raises(TrustPolicyError, match="subject|bindings"):
            policy.verify(
                envelope,
                role=AttestationRole.EXECUTION,
                bindings=mismatched,
            )


def test_key_rotation_metadata_and_model_hmac_nesting_remain_explicit():
    old = Ed25519StatementSigner.generate()
    new = Ed25519StatementSigner.generate()
    other_signers = {
        role: Ed25519StatementSigner.generate()
        for role in AttestationRole
        if role is not AttestationRole.MODEL
    }
    old_key = TrustedEd25519Key.from_signer(
        old,
        roles=(AttestationRole.MODEL,),
        valid_from="2026-06-01T00:00:00Z",
        valid_until="2026-07-18T23:59:59Z",
    )
    new_key = TrustedEd25519Key.from_signer(
        new,
        roles=(AttestationRole.MODEL,),
        valid_from="2026-07-19T00:00:00Z",
        supersedes=(old.key_id,),
    )
    policy = OfficialTrustPolicy(
        keys=(
            old_key,
            new_key,
            *(
                TrustedEd25519Key.from_signer(
                    signer,
                    roles=(role,),
                    valid_from="2026-07-01T00:00:00Z",
                )
                for role, signer in other_signers.items()
            ),
        ),
        role_key_ids={
            role: (
                (new.key_id,)
                if role is AttestationRole.MODEL
                else (other_signers[role].key_id,)
            )
            for role in AttestationRole
        },
        verification_time=VERIFY_AT,
    )
    assert new_key.supersedes == (old.key_id,)
    with pytest.raises(TrustPolicyError, match="not allowed"):
        policy.verify(
            _envelope(AttestationRole.MODEL, old),
            role=AttestationRole.MODEL,
            bindings=_bindings(),
        )
    assert policy.verify(
        _envelope(AttestationRole.MODEL, new),
        role=AttestationRole.MODEL,
        bindings=_bindings(),
    ).key_id == new.key_id

    signers, policy = _material()
    internal_hmac = {
        "algorithm": "HMAC-SHA256",
        "integrity_only": True,
        "signature": "deadbeef",
    }
    envelope = _envelope(
        AttestationRole.MODEL,
        signers[AttestationRole.MODEL],
        claims={"internal_operator_evidence": internal_hmac},
    )
    verified = policy.verify(
        envelope,
        role=AttestationRole.MODEL,
        bindings=_bindings(),
        required_claims={"internal_operator_evidence": internal_hmac},
    )
    assert verified.statement["predicate"]["claims"][
        "internal_operator_evidence"
    ] == internal_hmac
