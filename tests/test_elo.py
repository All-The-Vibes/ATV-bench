"""ELO engine tests (master test plan: 'ELO / variance gate').

The board is recomputed from full match history on every publish. These tests pin
the four properties that keep the public number honest:
  - zero-opponent provisional (first submitter never crashes / NaNs)
  - forfeit scored as loss + reason enum (never dropped -> else ELO skews)
  - deterministic recompute-from-history (order-independent, byte-identical)
  - A/A variance gate with numeric teeth (identical bots -> no publishable delta)

Design: docs/COMMUNITY_LEAGUE.md 'ELO'.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.elo import (
    ForfeitReason,
    MatchResult,
    Outcome,
    compute_leaderboard,
    variance_gate,
)

SEED = 1500


def _match(a, b, outcome, **kw):
    return MatchResult(player_a=a, player_b=b, outcome=outcome, **kw)


# --- zero-opponent provisional (eng T9) ---

def test_zero_opponent_provisional():
    board = compute_leaderboard([], entrants=["alice"])
    row = board["alice"]
    assert row["elo"] == SEED
    assert row["rated"] is False
    assert row["match_count"] == 0
    assert row["status"] == "waiting_for_opponent"
    # no NaN, JSON-serializable
    assert json.loads(json.dumps(board)) == board


# --- anchor is pinned (plan #11/#12: byok strict anchor, 1500, excluded from updates) ---

def test_anchor_rating_is_pinned_at_seed():
    """A forged/asserted win vs the anchor must NOT move the anchor's rating — else the
    anchor's ELO feeds every later entrant's expected score and one entrant's dishonest
    outcome bleeds into third parties. The anchor is a fixed 1500 reference."""
    matches = [_match("alice", "byok-anchor", Outcome.A_WINS)]
    board = compute_leaderboard(matches, entrants=["alice", "byok-anchor"],
                                anchors=["byok-anchor"])
    assert board["byok-anchor"]["elo"] == SEED  # pinned, unmoved by alice's win
    assert board["alice"]["elo"] > SEED          # entrant still gains


def test_anchor_pin_isolates_third_parties():
    """Two entrants each 'beat' the anchor. Because the anchor is pinned, the second
    entrant's rating delta is computed against the SAME 1500 baseline as the first —
    entrant A's asserted win cannot change entrant B's outcome."""
    a_only = compute_leaderboard([_match("alice", "byok-anchor", Outcome.A_WINS)],
                                 entrants=["alice", "byok-anchor"], anchors=["byok-anchor"])
    both = compute_leaderboard(
        [_match("alice", "byok-anchor", Outcome.A_WINS),
         _match("bob", "byok-anchor", Outcome.A_WINS)],
        entrants=["alice", "bob", "byok-anchor"], anchors=["byok-anchor"])
    # alice's rating is identical whether or not bob also played the anchor
    assert both["alice"]["elo"] == a_only["alice"]["elo"]
    assert both["byok-anchor"]["elo"] == SEED


def test_single_match_becomes_rated():
    matches = [_match("alice", "bob", Outcome.A_WINS)]
    board = compute_leaderboard(matches)
    assert board["alice"]["rated"] is True
    assert board["bob"]["rated"] is True
    assert board["alice"]["elo"] > board["bob"]["elo"]
    assert board["alice"]["match_count"] == 1


# --- forfeit = loss + reason enum, never dropped (eng T12) ---

def test_forfeit_scored_not_dropped():
    matches = [
        _match("alice", "bob", Outcome.FORFEIT_B, forfeit_reason=ForfeitReason.TIMEOUT),
    ]
    board = compute_leaderboard(matches)
    # bob forfeited -> counts as bob loss, alice win. NOT dropped.
    assert board["alice"]["elo"] > SEED
    assert board["bob"]["elo"] < SEED
    assert board["alice"]["match_count"] == 1
    assert board["bob"]["match_count"] == 1
    assert board["bob"]["forfeits"] == 1


@pytest.mark.parametrize("reason", list(ForfeitReason))
def test_all_forfeit_reasons_valid(reason):
    m = _match("a", "b", Outcome.FORFEIT_A, forfeit_reason=reason)
    board = compute_leaderboard([m])
    assert board["b"]["elo"] > board["a"]["elo"]


def test_forfeit_requires_reason():
    with pytest.raises(ValueError):
        _match("a", "b", Outcome.FORFEIT_A)  # no reason -> rejected


# --- deterministic recompute-from-history (eng T11) ---

def test_elo_deterministic_order_independent():
    matches = [
        _match("alice", "bob", Outcome.A_WINS, match_id="m1", seed=1),
        _match("bob", "carol", Outcome.DRAW, match_id="m2", seed=2),
        _match("carol", "alice", Outcome.A_WINS, match_id="m3", seed=3),
        _match("alice", "bob", Outcome.B_WINS, match_id="m4", seed=4),
    ]
    b1 = compute_leaderboard(matches)
    b2 = compute_leaderboard(list(reversed(matches)))
    # recompute-from-history is order-independent -> byte-identical JSON
    assert json.dumps(b1, sort_keys=True) == json.dumps(b2, sort_keys=True)


def test_elo_recompute_stable_across_runs():
    matches = [
        _match("alice", "bob", Outcome.A_WINS),
        _match("bob", "carol", Outcome.B_WINS),
    ]
    runs = {json.dumps(compute_leaderboard(matches), sort_keys=True) for _ in range(5)}
    assert len(runs) == 1  # byte-identical every time


# --- A/A variance gate with numeric teeth (eng T10) ---

def test_aa_gate_blocks_noise():
    # identical bots (same underlying player) split ~50/50 over seeded matches.
    matches = []
    for i in range(20):
        outcome = Outcome.A_WINS if i % 2 == 0 else Outcome.B_WINS
        matches.append(_match("aa_1", "aa_2", outcome, seed=i, match_id=f"m{i:02d}"))
    gate = variance_gate(matches, player_pair=("aa_1", "aa_2"))
    # spread between two identical harnesses must be below the publishable threshold
    assert gate["publishable"] is False
    assert gate["reason"] == "insufficient_signal"
    assert "elo_spread" in gate and "threshold" in gate


def test_variance_gate_min_matches():
    # too few matches -> not publishable regardless of spread
    matches = [_match("x", "y", Outcome.A_WINS)]
    gate = variance_gate(matches, player_pair=("x", "y"))
    assert gate["publishable"] is False
    assert gate["reason"] in ("insufficient_matches", "insufficient_signal")


def test_variance_gate_passes_real_signal():
    # a dominant player over many matches -> real, publishable signal
    matches = [_match("strong", "weak", Outcome.A_WINS, seed=i, match_id=f"s{i:02d}")
               for i in range(30)]
    gate = variance_gate(matches, player_pair=("strong", "weak"))
    assert gate["publishable"] is True


# --- CI width surfaced (design low-confidence treatment) ---

def test_board_reports_confidence_interval():
    matches = [_match("alice", "bob", Outcome.A_WINS) for _ in range(3)]
    board = compute_leaderboard(matches)
    assert "ci" in board["alice"]
    assert set(board["alice"]["ci"]) == {"lo", "hi"}
    assert board["alice"]["ci"]["lo"] <= board["alice"]["elo"] <= board["alice"]["ci"]["hi"]
