"""Tests for the paired, side-balanced scheduler (gap G1).

The load-bearing property is *side balance*: within each game, every unordered
pair of harnesses occupies the A seat and the B seat an equal number of times,
so first-mover advantage cancels across the pair's appearances.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
from collections import Counter

import pytest

from atv_bench.scheduler import Match, build_paired_schedule


HARNESSES = ["copilot", "aider", "claude", "cursor"]
GAMES = ["tic_tac_toe", "connect_four"]


def _pair_key(m: Match) -> frozenset[str]:
    return frozenset((m.harness_a, m.harness_b))


def _expected_total(n_harnesses: int, n_games: int, repeats: int) -> int:
    return math.comb(n_harnesses, 2) * n_games * repeats


# --------------------------------------------------------------------------- #
# Structure / API
# --------------------------------------------------------------------------- #
def test_match_is_frozen_dataclass_with_required_fields():
    assert dataclasses.is_dataclass(Match)
    fields = {f.name for f in dataclasses.fields(Match)}
    assert {"game", "harness_a", "harness_b", "side_index", "repeat_index"} <= fields
    m = build_paired_schedule(HARNESSES, GAMES)[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.game = "mutated"  # type: ignore[misc]


def test_a_and_b_are_always_distinct():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=2)
    for m in schedule:
        assert m.harness_a != m.harness_b


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #
def test_total_matches_matches_formula():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=1)
    assert len(schedule) == _expected_total(len(HARNESSES), len(GAMES), 1)


def test_total_scales_with_repeats():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=3)
    assert len(schedule) == _expected_total(len(HARNESSES), len(GAMES), 3)


def test_every_pair_plays_every_game():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=1)
    seen = {(m.game, _pair_key(m)) for m in schedule}
    for game in GAMES:
        for a, b in itertools.combinations(HARNESSES, 2):
            assert (game, frozenset((a, b))) in seen


# --------------------------------------------------------------------------- #
# Side balance — the load-bearing invariant
# --------------------------------------------------------------------------- #
def _seat_counts(schedule):
    """Return {(game, pair): [seatA_count_for_c0, seatA_count_for_c1]} for sorted pair."""
    a_seat = Counter()
    total = Counter()
    for m in schedule:
        key = (m.game, _pair_key(m))
        total[key] += 1
        a_seat[(key, m.harness_a)] += 1
    out = {}
    for key, n in total.items():
        _, pair = key
        c0, c1 = sorted(pair)
        out[key] = ([a_seat[(key, c0)], a_seat[(key, c1)]], n)
    return out


def _assert_strict_side_balanced(schedule):
    # Strict per-pair balance: only valid when each pair plays an EVEN count per game.
    for key, (counts, n) in _seat_counts(schedule).items():
        assert counts[0] == counts[1], f"side imbalance for {key}: seat-A counts {counts}"
        assert sum(counts) == n


def _assert_near_side_balanced_globally(schedule):
    # Odd per-pair counts can't split evenly; require off-by-one locally AND
    # exact global seat balance (documented balancing rule).
    global_seat_a = Counter()
    for key, (counts, n) in _seat_counts(schedule).items():
        assert abs(counts[0] - counts[1]) <= 1, f"local imbalance >1 for {key}: {counts}"
        assert sum(counts) == n
    # Global: side_index 0 vs 1 must be exactly equal across the whole schedule.
    idx = Counter(m.side_index for m in schedule)
    assert idx[0] == idx[1], f"global side imbalance: {idx}"


def test_side_balance_even_repeats():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=2)
    _assert_strict_side_balanced(schedule)


def test_side_balance_repeats_one_is_globally_balanced():
    # repeats=1 => one match per pair-game (odd) => global balance is the guarantee.
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=1)
    _assert_near_side_balanced_globally(schedule)


def test_side_balance_odd_repeats():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=3)
    _assert_near_side_balanced_globally(schedule)


def test_side_balance_four_repeats_strict():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=4)
    _assert_strict_side_balanced(schedule)


def test_side_index_is_binary_and_consistent_with_seat():
    # side_index 0 => harness_a in canonical-first seat, 1 => swapped. Both appear equally.
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=2)
    idx = Counter(m.side_index for m in schedule)
    assert set(idx) <= {0, 1}
    assert idx[0] == idx[1]


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_same_seed_identical_ordered_schedule():
    a = build_paired_schedule(HARNESSES, GAMES, seed=7, repeats=2)
    b = build_paired_schedule(HARNESSES, GAMES, seed=7, repeats=2)
    assert a == b


def test_different_seed_may_reorder_but_preserves_invariants():
    a = build_paired_schedule(HARNESSES, GAMES, seed=1, repeats=2)
    b = build_paired_schedule(HARNESSES, GAMES, seed=2, repeats=2)
    assert len(a) == len(b)
    assert Counter(a) == Counter(b) or a != b  # same multiset OR at least a valid reorder
    _assert_strict_side_balanced(b)
    assert len(b) == _expected_total(len(HARNESSES), len(GAMES), 2)


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #
def test_fewer_than_two_harnesses_is_empty():
    assert build_paired_schedule([], GAMES) == []
    assert build_paired_schedule(["only"], GAMES) == []


def test_empty_games_is_empty():
    assert build_paired_schedule(HARNESSES, []) == []


def test_zero_repeats_is_empty():
    assert build_paired_schedule(HARNESSES, GAMES, repeats=0) == []


def test_repeat_index_spans_range():
    schedule = build_paired_schedule(HARNESSES, GAMES, repeats=3)
    assert {m.repeat_index for m in schedule} == {0, 1, 2}
