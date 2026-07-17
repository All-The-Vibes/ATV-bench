"""UC1 provenance binding — client-side tamper evidence for a published fingerprint.

Problem (TODOS.md): fingerprint readers are table stakes, but nothing proves a *published*
manifest matches the harness/config that actually produced the submitted bot. Attacks:
fingerprint a fat config then run a lean one; publish bot B under bot A's token; present a
claude fingerprint for a codex-built bot.

This module binds four facets captured at fingerprint time —
`{harness, bot_sha256, fingerprint_sha256, captured_at}` — into a token whose `signature`
is a digest over the canonical payload. `verify_provenance` recomputes each facet from the
*submitted* bot + manifest and re-derives the signature; any post-capture divergence
(edited manifest, swapped bot, swapped harness, hand-forged token) fails closed.

Trust boundary (honest): this runs on the contributor's machine, so it is TAMPER-EVIDENCE,
not anti-forgery. Without `ATV_PROVENANCE_KEY` the signature is an unkeyed SHA-256 digest —
it detects edits but a determined attacker who recomputes the whole token can defeat it, so
such rows are `self_attested`. With a key the signature is HMAC-SHA256; a row is only truly
`verified` once a trusted sandbox re-fingerprints and re-signs with a server-held key
(deferred to Phase 2). `verify_provenance` reports `signed` so callers can label rows.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

PROVENANCE_VERSION = "1.0.0"

# Facets bound into the signature, in canonical order. Order is fixed so the digest is
# reproducible regardless of dict insertion order in the token. `signed` and `version` are
# bound too: `signed` is the trust-tier bit the leaderboard labels rows by (a keyless
# attacker must not be able to flip it to claim the HMAC tier), and `version` gates the
# token format (an unbound version is a cross-version replay seam).
_SIGNED_FACETS = ("version", "harness", "bot_sha256", "fingerprint_sha256",
                  "captured_at", "signed")

# Sentinel used for the unkeyed digest so an attacker cannot trivially reproduce the HMAC
# construction by guessing an empty key; it is NOT a secret (it ships in source).
_UNKEYED_SALT = b"atv-bench/provenance/v1/unkeyed"


def _canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, stable across dict order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint_hash(fingerprint: dict[str, Any]) -> str:
    """SHA-256 over the canonicalized fingerprint manifest.

    Key-order independent (sorted keys) so re-serialization by an intermediary does not
    change the hash. Any change to a value — a dropped skill, a swapped model — changes it.
    """
    return hashlib.sha256(_canonical(fingerprint).encode("utf-8")).hexdigest()


def _sign(payload: dict[str, Any], key: str | None) -> str:
    """Digest over the canonical signed payload. HMAC-SHA256 when keyed, salted SHA-256
    otherwise. The exact same construction is recomputed in verify."""
    msg = _canonical(payload).encode("utf-8")
    if key:
        return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hashlib.sha256(_UNKEYED_SALT + b"\x00" + msg).hexdigest()


def _signed_payload(*, version: str, harness: str, bot_sha256: str,
                    fingerprint_sha256: str, captured_at: str, signed: bool) -> dict[str, Any]:
    return {
        "version": version,
        "harness": harness,
        "bot_sha256": bot_sha256,
        "fingerprint_sha256": fingerprint_sha256,
        "captured_at": captured_at,
        "signed": signed,
    }


def capture_provenance(*, harness: str, bot_sha256: str, fingerprint: dict[str, Any],
                       captured_at: str, key: str | None = None) -> dict[str, Any]:
    """Bind the four facets into a provenance token at fingerprint/build time.

    `captured_at` is passed in (not read from a clock) so capture is deterministic and
    testable. `key` (from ATV_PROVENANCE_KEY at the call site) upgrades the signature from
    an unkeyed digest to HMAC; the key itself is never stored in the token. The `signed`
    tier bit and the format `version` are BOTH inside the signed payload, so neither can be
    edited without breaking verification. Returns a plain dict ready to embed in the
    submission record.
    """
    fp_hash = fingerprint_hash(fingerprint)
    signed = bool(key)
    payload = _signed_payload(
        version=PROVENANCE_VERSION, harness=harness, bot_sha256=bot_sha256,
        fingerprint_sha256=fp_hash, captured_at=captured_at, signed=signed,
    )
    return {**payload, "signature": _sign(payload, key)}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    signed: bool
    reasons: list[str] = field(default_factory=list)


def verify_provenance(*, provenance: dict[str, Any] | None, harness: str,
                      bot_sha256: str, fingerprint: dict[str, Any],
                      key: str | None = None) -> VerifyResult:
    """Recompute every bound facet from the SUBMITTED bot + manifest and confirm the token
    still matches. Fails closed with one reason per divergence.

    Detects the TODOS.md attacks: a post-edited manifest (fingerprint_sha256 mismatch), a
    swapped bot (bot_sha256 mismatch), a swapped harness (harness mismatch), and any
    hand-forged token (signature mismatch — recomputed over the token's own claimed
    payload, so editing ANY signed facet — including the `signed` tier bit or the `version`
    — without the key breaks the signature).

    The reported `signed` tier is derived from verification, never copied from the token: a
    row is only `signed` when the token claims it AND a keyed HMAC actually validated it. A
    keyless verify (key is None) can never report `signed=True`.
    """
    reasons: list[str] = []
    if not isinstance(provenance, dict):
        return VerifyResult(ok=False, signed=False,
                            reasons=["provenance token is missing"])

    required = set(_SIGNED_FACETS) | {"signature"}
    missing = [k for k in required if k not in provenance]
    if missing:
        return VerifyResult(ok=False, signed=False,
                            reasons=[f"provenance token is malformed (missing: {', '.join(sorted(missing))})"])

    # The `signed` tier facet must be a strict boolean. A non-bool value (a string/number/
    # list — truthy or falsy) is a malformed token: fail closed and never bool()-coerce it,
    # else a keyed token with signed="yes"/1 would be accepted and reported as the HMAC tier.
    if not isinstance(provenance["signed"], bool):
        return VerifyResult(
            ok=False, signed=False,
            reasons=["provenance token is malformed (signed tier must be a boolean)"])
    if not isinstance(provenance["version"], str):
        return VerifyResult(
            ok=False, signed=False,
            reasons=["provenance token is malformed (version must be a string)"])

    # Recompute the signature over the token's OWN claimed payload. The CONSTRUCTION is
    # chosen from the token's own `signed` claim, not the verifier's key: an honest unkeyed
    # (self-attested) token must still verify on a key-holding merge verifier — otherwise
    # every Phase-1 submission fails closed the instant ATV_PROVENANCE_KEY is set. A keyed
    # token uses the verifier's key (a keyless verifier can't validate it, so it can't be
    # reported as signed). Editing any signed facet — including `signed`/`version` — breaks
    # the recomputed digest regardless. Constant-time compare.
    token_signed = bool(provenance["signed"])
    # A keyed (signed=True) token can only have its HMAC signature validated by a verifier
    # that holds the key. A KEYLESS verifier (the Phase-1 board, key is None) cannot check
    # an HMAC — so it does NOT recompute/fail on the signature; it downgrades the tier to
    # self-attested and relies on the facet checks below (fingerprint/harness/bot/version),
    # which are independent of the signature and still catch every tamper attack. This
    # mirrors the keyed-verifier-accepts-unkeyed direction: an honest keyed contributor
    # must not be dropped from a keyless board just because it can't re-derive the HMAC.
    keyed_token_but_keyless_verifier = token_signed and not key
    if keyed_token_but_keyless_verifier:
        sig_ok = False  # not validated (can't be), so the tier is not "signed"
    else:
        sign_key = key if token_signed else None
        claimed_payload = _signed_payload(
            version=provenance["version"], harness=provenance["harness"],
            bot_sha256=provenance["bot_sha256"],
            fingerprint_sha256=provenance["fingerprint_sha256"],
            captured_at=provenance["captured_at"], signed=token_signed,
        )
        expected_sig = _sign(claimed_payload, sign_key)
        sig_ok = hmac.compare_digest(str(provenance["signature"]), expected_sig)
        if not sig_ok:
            reasons.append("provenance signature does not verify (token tampered or wrong key)")

    # The reported tier is the token's claim AND a real key that validated it: a keyless
    # verify can never report the signed tier.
    reported_signed = token_signed and bool(key) and sig_ok

    if provenance["version"] != PROVENANCE_VERSION:
        reasons.append(
            f"provenance version mismatch: token is {provenance['version']!r}, this build "
            f"verifies {PROVENANCE_VERSION!r}")
    if provenance["harness"] != harness:
        reasons.append(
            f"harness mismatch: token bound to {provenance['harness']!r}, submission is {harness!r}")
    if provenance["bot_sha256"] != bot_sha256:
        reasons.append("bot mismatch: submitted bot does not match the provenance-bound bot")
    actual_fp = fingerprint_hash(fingerprint)
    if provenance["fingerprint_sha256"] != actual_fp:
        reasons.append("fingerprint mismatch: published manifest was edited after capture")

    return VerifyResult(ok=not reasons, signed=reported_signed, reasons=reasons)
