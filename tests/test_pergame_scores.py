"""Unit 3 (quickstart): per-game + overall scientific scores.

The eval must report BOTH a per-game breakdown and one overall harness-over-bare score. This
module filters the rating corpus by game, scores each arena, and pools for the overall lift —
failing closed (``insufficient``) on a game too thin for a defensible number.
"""
from __future__ import annotations

import pytest

from atv_bench.pergame import GameScore, overall_lift, per_game_scores


def _rows(harness, bare, model, game, n_wins, n_losses):
    rows = []
    for i in range(n_wins):
        rows.append({"harness_a": harness, "harness_b": bare, "model_a": model,
                     "model_b": model, "score_a": 1.0, "game": game, "match_id": f"{game}-w{i}"})
    for i in range(n_losses):
        rows.append({"harness_a": harness, "harness_b": bare, "model_a": model,
                     "model_b": model, "score_a": 0.0, "game": game, "match_id": f"{game}-l{i}"})
    return rows


def test_per_game_one_row_per_game():
    rows = _rows("claude-code", "bare:claude-code", "sonnet", "lightcycles", 4, 1) \
         + _rows("claude-code", "bare:claude-code", "sonnet", "chess", 2, 3)
    scores = per_game_scores(rows, harness="claude-code", baseline="bare:claude-code")
    games = {s.game for s in scores}
    assert games == {"lightcycles", "chess"}
    assert all(isinstance(s, GameScore) for s in scores)


def test_per_game_win_rate_from_harness_perspective():
    """win_rate is the harness's win fraction in that game, orientation-corrected."""
    rows = _rows("claude-code", "bare:claude-code", "sonnet", "lightcycles", 4, 1)
    s = next(s for s in per_game_scores(rows, "claude-code", "bare:claude-code")
             if s.game == "lightcycles")
    assert s.n == 5
    assert abs(s.win_rate - 0.8) < 1e-9


def test_per_game_orientation_corrected():
    """When the harness is seated as player_b, score_a is inverted for its win_rate."""
    rows = [{"harness_a": "bare:claude-code", "harness_b": "claude-code", "model_a": "sonnet",
             "model_b": "sonnet", "score_a": 0.0, "game": "ants", "match_id": f"a{i}"}
            for i in range(3)]  # bare loses all 3 => harness (seat b) won all 3
    s = next(s for s in per_game_scores(rows, "claude-code", "bare:claude-code")
             if s.game == "ants")
    assert abs(s.win_rate - 1.0) < 1e-9


def test_thin_game_marked_insufficient():
    """A game with too few trials for a defensible score is flagged, not fabricated."""
    rows = _rows("claude-code", "bare:claude-code", "sonnet", "gomoku", 1, 0)  # n=1
    s = next(s for s in per_game_scores(rows, "claude-code", "bare:claude-code",
                                        min_trials=5) if s.game == "gomoku")
    assert s.insufficient is True
    assert s.win_rate == s.win_rate  # still reports the raw rate, but flagged


def test_overall_lift_pools_all_games():
    """overall_lift pools every game into one harness-over-bare lift with a CI."""
    rows = _rows("claude-code", "bare:claude-code", "sonnet", "lightcycles", 5, 1) \
         + _rows("claude-code", "bare:claude-code", "sonnet", "chess", 4, 2) \
         + _rows("claude-code", "bare:claude-code", "sonnet", "ants", 5, 1)
    res = overall_lift(rows, harness="claude-code", baseline="bare:claude-code")
    assert res is not None
    assert res.lift == res.lift  # not NaN
    assert res.lift > 0  # harness wins the majority -> positive lift
    assert res.lo <= res.lift <= res.hi


def test_overall_lift_none_when_no_baseline():
    """No bare-baseline rows => overall lift is undefined (None), never a fabricated 0."""
    rows = [{"harness_a": "claude-code", "harness_b": "copilot-cli", "model_a": "sonnet",
             "model_b": "sonnet", "score_a": 1.0, "game": "lightcycles", "match_id": "x"}]
    assert overall_lift(rows, harness="claude-code", baseline="bare:claude-code") is None


def test_overall_lift_single_game_still_computes():
    """A single-game contrast (one cluster) must NOT be misreported as 'no baseline' — it falls
    back to the i.i.d. bootstrap and returns a real lift with a CI."""
    rows = _rows("claude-code", "bare:claude-code", "sonnet", "lightcycles", 8, 2)
    res = overall_lift(rows, harness="claude-code", baseline="bare:claude-code")
    assert res is not None, "single-game contrast with a real baseline must compute a lift"
    assert res.lift == res.lift  # not NaN
    assert res.lo <= res.lift <= res.hi
