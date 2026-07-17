"""RED->GREEN tests for the selectable local opponent registry (`atv_bench.bots`).

The demo failure was that the only playable opponent was the in-process greedy anchor,
so there was no "series of bots" a user could run the visualization against. This
registry exposes named, deterministic, in-process opponents (all implement the referee's
`MoveSource` protocol) that `atv-bench play` can select. Determinism is load-bearing:
a bot's moves are a pure function of the observation (+ a fixed seed), so a replay is
reproducible.
"""
from __future__ import annotations

import pytest

from atv_bench.arena.engine import Direction, Outcome, TronEngine
from atv_bench.arena.referee import run_match
from atv_bench.bots import BOTS, DEFAULT_OPPONENT, get_bot, make_bot, bot_keys


def _engine(**kw):
    return TronEngine(
        width=kw.get("width", 15),
        height=kw.get("height", 15),
        start_a=kw.get("start_a", (1, 7)),
        start_b=kw.get("start_b", (13, 7)),
        dir_a=Direction.RIGHT,
        dir_b=Direction.LEFT,
        max_turns=kw.get("max_turns", 200),
    )


def test_registry_lists_named_bots():
    keys = bot_keys()
    # The core series the UX promises: greedy standard + wall hugger + a bare baseline.
    assert "greedy" in keys
    assert "wall_hugger" in keys
    assert "bare" in keys
    assert DEFAULT_OPPONENT in keys


def test_every_registered_bot_has_metadata():
    for b in BOTS:
        assert b.key
        assert b.title
        assert b.summary
        assert callable(b.factory)


def test_get_bot_unknown_returns_none():
    assert get_bot("no-such-bot") is None


def test_make_bot_unknown_raises_actionable():
    with pytest.raises(ValueError) as ei:
        make_bot("no-such-bot")
    assert "no-such-bot" in str(ei.value)


def test_bots_produce_only_legal_direction_moves():
    """Each bot returns a Direction (or None) — never garbage — for a fresh observation."""
    obs = {
        "width": 15, "height": 15, "turn": 0,
        "you": {"pos": [1, 7], "dir": "right", "trail": [[1, 7]]},
        "opponent": {"pos": [13, 7], "dir": "left", "trail": [[13, 7]]},
    }
    for key in bot_keys():
        mv = make_bot(key, player="a").next_move(obs)
        assert mv is None or isinstance(mv, Direction)


def test_bots_are_deterministic_same_moves_twice():
    """Same bot + same observation sequence => identical move (seeded, reproducible)."""
    obs = {
        "width": 15, "height": 15, "turn": 3,
        "you": {"pos": [5, 7], "dir": "right", "trail": [[4, 7], [5, 7]]},
        "opponent": {"pos": [9, 7], "dir": "left", "trail": [[10, 7], [9, 7]]},
    }
    for key in bot_keys():
        m1 = make_bot(key, player="a").next_move(obs)
        m2 = make_bot(key, player="a").next_move(obs)
        assert m1 == m2, f"{key} not deterministic"


def test_two_bots_play_a_real_match_to_terminal():
    """Any two registered bots produce a terminal, adjudicated outcome (no hang, no error)."""
    eng = _engine()
    a = make_bot("greedy", player="a")
    b = make_bot("wall_hugger", player="b")
    result = run_match(eng, a, b, player_a="greedy", player_b="wall_hugger",
                       match_id="t", game="lightcycles", seed=0)
    assert result["status"] == "ok"
    assert result["outcome"] in {o.value for o in Outcome} | {"forfeit_a", "forfeit_b"}
