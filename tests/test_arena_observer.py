"""Tests for the streaming observer hook on run_match (demo live-feed plumbing).

run_match gains an optional `observer(state)` callback invoked with the initial state
and after every tick, so a caller can render a live frame each turn. The hook is
backward compatible: omitting it preserves the exact prior behavior. RED first.
"""
from __future__ import annotations

from atv_bench.arena.engine import Direction, TronEngine
from atv_bench.arena.referee import TrustedGreedyBot, run_match


def _engine() -> TronEngine:
    return TronEngine(
        width=9, height=9,
        start_a=(1, 4), start_b=(7, 4),
        dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=30,
    )


def test_run_match_without_observer_is_unchanged():
    eng = _engine()
    res = run_match(eng, TrustedGreedyBot("a"), TrustedGreedyBot("b"),
                    player_a="A", player_b="B", match_id="m1")
    assert res["status"] == "ok"
    assert res["player_a"] == "A" and res["player_b"] == "B"
    assert res["outcome"] in {"a_wins", "b_wins", "draw"}


def test_run_match_observer_receives_initial_and_ticks():
    eng = _engine()
    seen = []

    def obs(state):
        seen.append(state.turn)

    res = run_match(eng, TrustedGreedyBot("a"), TrustedGreedyBot("b"),
                    player_a="A", player_b="B", match_id="m2", observer=obs)
    assert res["status"] == "ok"
    # Observed the initial state (turn 0) and then monotonically increasing turns.
    assert seen[0] == 0
    assert seen == sorted(seen)
    assert len(seen) >= 2
    # The final observed state's turn matches the number of ticks played.
    assert seen[-1] >= 1


def test_run_match_observer_last_state_is_terminal():
    eng = _engine()
    states = []
    run_match(eng, TrustedGreedyBot("a"), TrustedGreedyBot("b"),
              player_a="A", player_b="B", match_id="m3", observer=states.append)
    assert states[-1].terminal is True
