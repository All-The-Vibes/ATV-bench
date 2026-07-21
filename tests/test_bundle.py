"""Tests for the content-addressed immutable result bundle (G7 + G9)."""
from __future__ import annotations

import copy
import json

import pytest

from atv_bench.bundle import build_bundle, verify_bundle
from atv_bench.rating import RatingMatch


def _matches() -> list[RatingMatch]:
    """A tiny model-locked roster with a bare baseline per base model.

    Two harnesses (H, bare) on model M1, two (K, bare) on model M2. The bare control
    plays each real harness so lift is defined.
    """
    rows: list[RatingMatch] = []
    # H beats its bare baseline on M1 most of the time.
    for _ in range(8):
        rows.append(RatingMatch("H", "bareM1", "M1", "M1", 1.0))
    for _ in range(2):
        rows.append(RatingMatch("H", "bareM1", "M1", "M1", 0.0))
    # K vs its bare baseline on M2, closer to even.
    for _ in range(6):
        rows.append(RatingMatch("K", "bareM2", "M2", "M2", 1.0))
    for _ in range(4):
        rows.append(RatingMatch("K", "bareM2", "M2", "M2", 0.0))
    return rows


def _meta(**over) -> dict:
    m = {
        "seed": 7,
        "n_boot": 64,
        "baselines": {"H": "bareM1", "K": "bareM2"},
        "versions": {"atv_bench": "0.1.0", "numpy": "1.26"},
        "cluster_policy": "iid",
    }
    m.update(over)
    return m


def _ratings_doc() -> dict:
    return {"harnesses": ["H", "K", "bareM1", "bareM2"], "attributed": False}


# ---------------------------------------------------------------------------
# G7 — content-addressed immutable bundle + offline reproduce
# ---------------------------------------------------------------------------


def test_build_bundle_has_content_id_and_reproduction_tuple():
    b = build_bundle(_ratings_doc(), _matches(), _meta())
    assert isinstance(b["content_id"], str)
    assert len(b["content_id"]) == 64  # sha256 hex
    rep = b["reproduce"]
    assert rep["seed"] == 7
    assert rep["n_boot"] == 64
    assert rep["baselines"] == {"H": "bareM1", "K": "bareM2"}
    assert rep["cluster_policy"] == "iid"
    assert rep["versions"]["atv_bench"] == "0.1.0"


def test_round_trip_verifies_true():
    b = build_bundle(_ratings_doc(), _matches(), _meta())
    assert verify_bundle(b) is True


def test_content_id_stable_across_repeated_builds():
    b1 = build_bundle(_ratings_doc(), _matches(), _meta())
    b2 = build_bundle(_ratings_doc(), _matches(), _meta())
    assert b1["content_id"] == b2["content_id"]


def test_content_id_excludes_itself():
    b = build_bundle(_ratings_doc(), _matches(), _meta())
    # Recompute canonical bytes over payload sans content_id -> must equal stored id.
    import hashlib

    payload = {k: v for k, v in b.items() if k != "content_id"}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canon).hexdigest() == b["content_id"]


def test_one_byte_mutation_of_payload_fails_verify():
    b = build_bundle(_ratings_doc(), _matches(), _meta())
    tampered = copy.deepcopy(b)
    # Flip a published scalar slightly but leave content_id stale.
    h = next(iter(tampered["published"]))
    tampered["published"][h] = tampered["published"][h] + 1.0
    assert verify_bundle(tampered) is False


def test_mutating_a_match_fails_verify():
    b = build_bundle(_ratings_doc(), _matches(), _meta())
    tampered = copy.deepcopy(b)
    tampered["matches"][0]["score_a"] = 0.0 if tampered["matches"][0]["score_a"] else 1.0
    assert verify_bundle(tampered) is False


def test_recomputed_published_scalar_matches_stored():
    from atv_bench.lift import compute_lift

    meta = _meta()
    b = build_bundle(_ratings_doc(), _matches(), meta)
    lifts = compute_lift(
        _matches(), meta["baselines"], seed=meta["seed"], n_boot=meta["n_boot"]
    )
    for h, res in lifts.items():
        assert b["published"][h] == pytest.approx(res.lift, abs=1e-9)


# ---------------------------------------------------------------------------
# G9 — track + trust-tier schema fields
# ---------------------------------------------------------------------------


def test_defaults_are_fail_closed():
    b = build_bundle(_ratings_doc(), _matches(), _meta())
    assert b["track"] == "league"
    assert b["trust_tier"] == "local-self-attested"
    assert b["rankable"] is False


def test_explicit_track_and_trust_tier_survive_round_trip():
    b = build_bundle(
        _ratings_doc(),
        _matches(),
        _meta(track="controlled", trust_tier="reproduced", rankable=True),
    )
    assert b["track"] == "controlled"
    assert b["trust_tier"] == "reproduced"
    assert b["rankable"] is True
    assert verify_bundle(b) is True


def test_track_is_part_of_content_id():
    a = build_bundle(_ratings_doc(), _matches(), _meta(track="league"))
    c = build_bundle(_ratings_doc(), _matches(), _meta(track="systems"))
    assert a["content_id"] != c["content_id"]


def test_trust_tier_is_part_of_content_id():
    a = build_bundle(_ratings_doc(), _matches(), _meta(trust_tier="local-self-attested"))
    c = build_bundle(_ratings_doc(), _matches(), _meta(trust_tier="attested"))
    assert a["content_id"] != c["content_id"]


def test_invalid_track_raises():
    with pytest.raises(ValueError):
        build_bundle(_ratings_doc(), _matches(), _meta(track="pro-league"))


def test_invalid_trust_tier_raises():
    with pytest.raises(ValueError):
        build_bundle(_ratings_doc(), _matches(), _meta(trust_tier="blockchain"))
