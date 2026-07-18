"""Tests for UC1 provenance binding (fingerprint provenance).

The threat model comes straight from TODOS.md: nothing proves a *published* manifest
matches the harness/config that actually produced the submitted bot. Three concrete
attacks must be detectable by `verify_provenance`:

  1. post-edit  — capture a fingerprint, then hand-edit the published manifest (e.g. drop
     a fat-config skill) so the leaderboard shows a leaner stack than was run.
  2. bot-swap   — bind a fingerprint to bot A, then publish bot B under the same token.
  3. harness-swap — submit a claude fingerprint's provenance for a codex-built bot.

This is CLIENT tamper-evidence: it binds {harness, bot_sha256, fingerprint_hash,
captured_at} into a token and detects any post-capture divergence. With ATV_PROVENANCE_KEY
set the token is HMAC-signed (anti-tamper on the token itself); without a key it is an
unkeyed digest — still tamper-evident against edits, but self-attested (labelled as such).
It does NOT claim to stop a contributor who lies at capture time; that needs server-side
re-fingerprinting in the sandbox (deferred, Phase 2).
"""
from __future__ import annotations

import copy

import pytest

from atv_bench.fingerprint.provenance import (
    PROVENANCE_VERSION,
    capture_provenance,
    fingerprint_hash,
    verify_provenance,
)
from atv_bench.errors import AtvError, ErrorCode


FP_CLAUDE = {
    "harness": "claude-code",
    "model": "claude-sonnet-4",
    "gstack": True,
    "skills": ["brainstorming", "tdd"],
    "mcps": ["context7"],
    "plugins": ["superpowers"],
    "custom_agents_count": 3,
    "unknown": [],
    "probe_version": "1.0.0",
}
FP_CODEX = {**FP_CLAUDE, "harness": "codex", "plugins": []}

BOT_A = "a" * 64
BOT_B = "b" * 64
WHEN = "2026-07-17T00:00:00Z"


def _capture(fp=FP_CLAUDE, *, bot=BOT_A, harness="claude-code", when=WHEN, key=None):
    return capture_provenance(
        harness=harness, bot_sha256=bot, fingerprint=fp, captured_at=when, key=key
    )


# --- fingerprint_hash: stable, order-independent canonicalization ---

def test_fingerprint_hash_is_deterministic():
    assert fingerprint_hash(FP_CLAUDE) == fingerprint_hash(FP_CLAUDE)


def test_fingerprint_hash_is_key_order_independent():
    shuffled = dict(reversed(list(FP_CLAUDE.items())))
    assert fingerprint_hash(shuffled) == fingerprint_hash(FP_CLAUDE)


def test_fingerprint_hash_changes_when_a_value_changes():
    edited = {**FP_CLAUDE, "skills": ["tdd"]}  # dropped a skill
    assert fingerprint_hash(edited) != fingerprint_hash(FP_CLAUDE)


# --- capture_provenance: token shape ---

def test_capture_binds_all_four_facets():
    tok = _capture()
    assert tok["version"] == PROVENANCE_VERSION
    assert tok["harness"] == "claude-code"
    assert tok["bot_sha256"] == BOT_A
    assert tok["fingerprint_sha256"] == fingerprint_hash(FP_CLAUDE)
    assert tok["captured_at"] == WHEN
    assert tok["signature"]


def test_unkeyed_capture_is_self_attested():
    tok = _capture(key=None)
    assert tok["signed"] is False


def test_keyed_capture_is_signed():
    tok = _capture(key="s3cret-signing-key")
    assert tok["signed"] is True
    # keyed tokens carry an `hmac` anti-forgery layer; a different key yields a different
    # hmac over the same payload (the unkeyed `signature` is key-independent by design).
    assert tok.get("hmac")
    other = _capture(key="different-key")
    assert tok["hmac"] != other["hmac"]
    assert tok["signature"] == other["signature"]  # unkeyed digest is the same payload


def test_capture_never_embeds_the_key():
    tok = _capture(key="s3cret-signing-key")
    assert "s3cret-signing-key" not in str(tok)


# --- verify_provenance: happy path ---

def test_verify_accepts_untampered_unkeyed_token():
    tok = _capture()
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A, fingerprint=FP_CLAUDE
    )
    assert res.ok is True
    assert res.reasons == []
    assert res.signed is False


def test_verify_accepts_untampered_keyed_token():
    tok = _capture(key="k")
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key="k",
    )
    assert res.ok is True
    assert res.signed is True


# --- verify_provenance: the three attacks from TODOS.md ---

def test_detects_fingerprint_post_edit():
    tok = _capture(fp=FP_CLAUDE)
    edited = {**FP_CLAUDE, "skills": ["tdd"]}  # published a leaner stack than captured
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A, fingerprint=edited
    )
    assert res.ok is False
    assert "fingerprint" in " ".join(res.reasons).lower()


def test_detects_bot_swap():
    tok = _capture(bot=BOT_A)
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_B, fingerprint=FP_CLAUDE
    )
    assert res.ok is False
    assert "bot" in " ".join(res.reasons).lower()


def test_detects_harness_swap():
    # a claude token re-presented for a codex-built bot
    tok = _capture(fp=FP_CLAUDE, harness="claude-code")
    res = verify_provenance(
        provenance=tok, harness="codex", bot_sha256=BOT_A, fingerprint=FP_CODEX
    )
    assert res.ok is False
    assert "harness" in " ".join(res.reasons).lower()


def test_detects_signature_tamper_on_keyed_token():
    tok = _capture(key="k")
    forged = {**tok, "signature": "deadbeef"}
    res = verify_provenance(
        provenance=forged, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key="k",
    )
    assert res.ok is False
    assert "signature" in " ".join(res.reasons).lower()


def test_detects_payload_tamper_even_without_key():
    # attacker edits the embedded fingerprint_sha256 to match their edited manifest,
    # but the unkeyed digest is over the whole payload, so the signature no longer matches.
    tok = _capture()
    edited = {**FP_CLAUDE, "skills": ["tdd"]}
    forged = {**tok, "fingerprint_sha256": fingerprint_hash(edited)}
    res = verify_provenance(
        provenance=forged, harness="claude-code", bot_sha256=BOT_A, fingerprint=edited
    )
    assert res.ok is False
    assert "signature" in " ".join(res.reasons).lower()


def test_keyed_token_cannot_be_reforged_without_key():
    # capture signed with the real key; attacker edits fingerprint + recomputes the
    # UNKEYED digest, hoping verify (with the key) accepts it. It must not.
    tok = _capture(key="real-key")
    edited = {**FP_CLAUDE, "skills": ["tdd"]}
    forged = {**tok, "fingerprint_sha256": fingerprint_hash(edited), "signed": False}
    res = verify_provenance(
        provenance=forged, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=edited, key="real-key",
    )
    assert res.ok is False


# --- verify_provenance: malformed / missing token ---

def test_missing_provenance_reports_not_ok():
    res = verify_provenance(
        provenance=None, harness="claude-code", bot_sha256=BOT_A, fingerprint=FP_CLAUDE
    )
    assert res.ok is False
    assert res.reasons


def test_malformed_provenance_missing_field_reports_not_ok():
    tok = _capture()
    del tok["signature"]
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A, fingerprint=FP_CLAUDE
    )
    assert res.ok is False
    assert res.reasons


def test_verify_result_is_immutable_reasons_list_per_call():
    # each call returns its own reasons; mutating one must not bleed into another
    tok = _capture()
    r1 = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_B, fingerprint=FP_CLAUDE
    )
    r2 = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A, fingerprint=FP_CLAUDE
    )
    assert r1.ok is False and r2.ok is True


# --- adversarial hardening (skeptic workflow 2026-07-17) ---

def test_forged_signed_flag_is_rejected_without_key():
    """F-signed: the `signed` trust bit must never yield the HMAC tier to a keyless
    attacker. A keyless attacker flipping signed→True is INDISTINGUISHABLE to a keyless
    verifier from an honest keyed contributor (both are 'signed=True tokens I hold no key
    for'), so the keyless verifier accepts both as a self-attested DOWNGRADE and must never
    report signed=True. The security property is the TIER (signed=False) — the attacker
    gains nothing, and an honest keyed row is not dropped. A KEYED verifier rejects the
    forgery outright (the bit is bound into the HMAC)."""
    tok = _capture(key=None)
    assert tok["signed"] is False
    forged = {**tok, "signed": True}
    res = verify_provenance(
        provenance=forged, harness="claude-code", bot_sha256=BOT_A, fingerprint=FP_CLAUDE
    )
    assert res.signed is False  # the forged tier is never granted — this is what matters
    res_keyed = verify_provenance(
        provenance=forged, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key="server-key",
    )
    assert res_keyed.ok is False
    assert res_keyed.signed is False


def test_reported_signed_tier_is_never_true_without_a_key():
    """Even a well-formed token cannot be reported as signed when verify holds no key —
    the signed tier is reserved for keyed (HMAC) verification."""
    tok = _capture(key="k")
    # verify with NO key: a keyed token cannot be validated, so it must not pass...
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A, fingerprint=FP_CLAUDE,
        key=None,
    )
    assert res.signed is False


def test_forged_version_facet_is_rejected():
    """F-version: `version` is advertised as a signed facet, so editing it must break
    verification (on both unkeyed and keyed tokens)."""
    for key in (None, "k"):
        tok = _capture(key=key)
        forged = {**tok, "version": "99.0.0-EVIL"}
        res = verify_provenance(
            provenance=forged, harness="claude-code", bot_sha256=BOT_A,
            fingerprint=FP_CLAUDE, key=key,
        )
        assert res.ok is False, f"mutated version verified with key={key!r}"


def test_keyed_verifier_accepts_legit_unkeyed_self_attested_token():
    """F-construction (skeptic round 2): a key-holding merge verifier must still ACCEPT an
    honest unkeyed self-attested token (verify it as self-attested), not hard-reject it.
    The construction is chosen from the token's own `signed` claim, not the verifier's key.
    Otherwise every Phase-1 submission fails closed the instant ATV_PROVENANCE_KEY is set."""
    tok = _capture(key=None)  # honest self-attested contributor
    assert tok["signed"] is False
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key="server-side-key",  # verifier holds a key
    )
    assert res.ok is True          # accepted, not rejected as "signature does not verify"
    assert res.signed is False     # but still reported as the self-attested tier




@pytest.mark.parametrize("bad_signed", ["yes", 1, 0, "", [], {}, None, 1.0])
def test_nonbool_signed_facet_rejected(bad_signed):
    """Santa PR#10 (reviewer B): the `signed` tier facet must be a strict BOOLEAN. A
    non-bool value (truthy or falsy) must fail closed as malformed — never be bool()-
    coerced and accepted, and a keyed token must never report signed=True off a non-bool
    tier bit."""
    tok = _capture(key="server-key")            # honest keyed token
    tok = copy.deepcopy(tok)
    tok["signed"] = bad_signed
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key="server-key",
    )
    assert res.ok is False, f"non-bool signed {bad_signed!r} verified"
    assert res.signed is False, f"non-bool signed {bad_signed!r} reported signed=True"


def test_keyless_verifier_accepts_honest_keyed_token_as_self_attested():
    """Santa PR#10 round 2 (reviewer A): a keyless verifier (the Phase-1 board holds no
    key) CANNOT validate an HMAC signature, so an honest KEYED token must be accepted as a
    self-attested downgrade — NOT rejected — mirroring the keyed-verifier-accepts-unkeyed
    direction. Otherwise a contributor who follows the CLI's 'set ATV_PROVENANCE_KEY'
    advice loses their leaderboard row. Facet tampering is still caught independently."""
    tok = _capture(key="contributor-key")   # honest keyed (signed=True) token
    assert tok["signed"] is True
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key=None,      # keyless board
    )
    assert res.ok is True, res.reasons        # accepted, not quarantined
    assert res.signed is False                # downgraded to self-attested tier


def test_keyless_verifier_still_catches_tamper_on_keyed_token():
    """The keyless downgrade must NOT weaken facet checks: a tampered fingerprint on a
    keyed token is still rejected by the keyless verifier (via the facet mismatch, which is
    independent of the unverifiable signature)."""
    tok = _capture(key="contributor-key")
    res = verify_provenance(
        provenance=tok, harness="claude-code", bot_sha256=BOT_A,
        fingerprint={**FP_CLAUDE, "skills": []},  # leaner than captured
        key=None,
    )
    assert res.ok is False
    assert any("fingerprint" in r.lower() for r in res.reasons), res.reasons


def test_keyed_token_captured_at_tamper_caught_by_keyless_verifier():
    """Santa PR#10 round 3 (reviewer B): captured_at is advertised as a BOUND facet, so a
    keyed token with only captured_at edited must be caught even by a KEYLESS verifier.
    The token carries an always-checkable unkeyed digest binding EVERY facet (incl.
    captured_at); the HMAC is an additional keyed layer for the signed tier only. A keyless
    board still validates the unkeyed digest, so a captured_at edit fails closed."""
    tok = _capture(key="contributor-key")  # keyed token
    tampered = {**tok, "captured_at": "1999-01-01T00:00:00Z"}
    res = verify_provenance(
        provenance=tampered, harness="claude-code", bot_sha256=BOT_A,
        fingerprint=FP_CLAUDE, key=None,   # keyless board
    )
    assert res.ok is False, res.reasons
    assert res.signed is False
