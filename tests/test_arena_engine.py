"""RED->GREEN tests for the trusted Tron/lightcycles engine (FOLLOW_UPS item 1).

The engine is a PURE, deterministic adjudicator. It never trusts a bot: it takes
a move per player per tick and decides the outcome from real collision rules. This
is the core that lets the arena REFEREE a match rather than trust bot stdout for
win/loss/draw.

Rules (lightcycles / Tron):
  - Two players ride on a WxH grid, each leaving an impassable trail behind.
  - Each tick both players move one cell simultaneously in their chosen direction.
  - A player crashes if it moves into: a wall (off-board), any existing trail
    (own or opponent), the opponent's NEW head cell (same-cell collision = draw),
    or by swapping cells with the opponent (edge collision = draw).
  - A player may not reverse directly into its own immediate trail (treated as a
    crash — you cannot 180 into your own neck).
  - Last rider standing wins. Both crash same tick => draw. Reaching max_turns with
    both alive => draw (survival tie).
"""
from __future__ import annotations

import pytest

from atv_bench.arena.engine import Direction, GameState, TronEngine, Outcome


def make_engine(**kw):
    # Small board, deterministic start: A at left-center facing right, B at
    # right-center facing left, several columns apart.
    return TronEngine(
        width=kw.get("width", 9),
        height=kw.get("height", 5),
        start_a=kw.get("start_a", (1, 2)),
        start_b=kw.get("start_b", (7, 2)),
        dir_a=kw.get("dir_a", Direction.RIGHT),
        dir_b=kw.get("dir_b", Direction.LEFT),
        max_turns=kw.get("max_turns", 100),
    )


def test_initial_state_places_both_players_with_trails():
    eng = make_engine()
    st = eng.initial_state()
    assert st.pos_a == (1, 2)
    assert st.pos_b == (7, 2)
    assert (1, 2) in st.trail_a
    assert (7, 2) in st.trail_b
    assert not st.terminal
    assert st.outcome is None


def test_single_step_moves_and_extends_trail():
    eng = make_engine()
    st = eng.initial_state()
    st2 = eng.tick(st, Direction.RIGHT, Direction.LEFT)
    assert st2.pos_a == (2, 2)
    assert st2.pos_b == (6, 2)
    assert (1, 2) in st2.trail_a and (2, 2) in st2.trail_a
    assert not st2.terminal


def test_wall_crash_is_a_loss():
    # A starts at left edge x=1 facing right; send it LEFT toward the wall.
    eng = make_engine(start_a=(0, 2), dir_a=Direction.RIGHT)
    st = eng.initial_state()
    st2 = eng.tick(st, Direction.LEFT, Direction.UP)  # A off the left wall
    assert st2.terminal
    assert st2.outcome == Outcome.B_WINS  # A crashed, B survives


def test_reversing_into_own_neck_crashes():
    # A faces RIGHT, has moved once so its neck is behind it; a 180 (LEFT) hits its
    # own immediate trail cell.
    eng = make_engine()
    st = eng.initial_state()
    st = eng.tick(st, Direction.RIGHT, Direction.LEFT)  # A now at (2,2), neck at (1,2)
    st = eng.tick(st, Direction.LEFT, Direction.LEFT)   # A tries to 180 into (1,2)
    assert st.terminal
    assert st.outcome == Outcome.B_WINS


def test_running_into_opponent_trail_is_a_loss():
    # Build a vertical wall of B's trail, then drive A into it.
    eng = make_engine(width=9, height=7, start_a=(0, 0), start_b=(4, 0),
                      dir_a=Direction.RIGHT, dir_b=Direction.DOWN)
    st = eng.initial_state()
    # B lays a vertical trail at x=4 going down; A marches right along y=0 then...
    st = eng.tick(st, Direction.DOWN, Direction.DOWN)   # A (0,1) B (4,1)
    st = eng.tick(st, Direction.DOWN, Direction.DOWN)   # A (0,2) B (4,2)
    # Now A at (0,2). Drive A right across to hit B's trail column at x=4,y=2.
    st = eng.tick(st, Direction.RIGHT, Direction.RIGHT) # A (1,2) B (5,2)
    st = eng.tick(st, Direction.RIGHT, Direction.RIGHT) # A (2,2) B (6,2)
    st = eng.tick(st, Direction.RIGHT, Direction.RIGHT) # A (3,2) B (7,2)
    st = eng.tick(st, Direction.RIGHT, Direction.UP)    # A -> (4,2) which is B's old trail
    assert st.terminal
    assert st.outcome == Outcome.A_WINS or st.outcome == Outcome.B_WINS
    # Specifically A crashed into B's trail => B wins.
    assert st.outcome == Outcome.B_WINS


def test_head_on_same_cell_is_a_draw():
    # A at (3,2) facing right, B at (5,2) facing left, odd gap -> they meet mid-cell
    # on the SAME target cell (4,2) the same tick.
    eng = make_engine(start_a=(3, 2), start_b=(5, 2))
    st = eng.initial_state()
    st = eng.tick(st, Direction.RIGHT, Direction.LEFT)  # both target (4,2)
    assert st.terminal
    assert st.outcome == Outcome.DRAW


def test_swap_cells_is_a_draw():
    # Adjacent players that swap cells collide on the edge -> draw.
    eng = make_engine(start_a=(3, 2), start_b=(4, 2))
    st = eng.initial_state()
    st = eng.tick(st, Direction.RIGHT, Direction.LEFT)  # A->(4,2) B->(3,2) swap
    assert st.terminal
    assert st.outcome == Outcome.DRAW


def test_max_turns_with_both_alive_is_a_draw():
    # Tiny loop-free wander that never crashes within max_turns=2.
    eng = make_engine(width=20, height=3, start_a=(0, 0), start_b=(19, 0),
                      dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=2)
    st = eng.initial_state()
    st = eng.tick(st, Direction.RIGHT, Direction.LEFT)  # turn 1
    st = eng.tick(st, Direction.RIGHT, Direction.LEFT)  # turn 2 == max
    assert st.terminal
    assert st.outcome == Outcome.DRAW


def test_engine_is_deterministic():
    moves = [(Direction.RIGHT, Direction.LEFT), (Direction.UP, Direction.UP),
             (Direction.RIGHT, Direction.LEFT)]
    outs = []
    for _ in range(2):
        eng = make_engine()
        st = eng.initial_state()
        for ma, mb in moves:
            if st.terminal:
                break
            st = eng.tick(st, ma, mb)
        outs.append((st.pos_a, st.pos_b, st.outcome, st.terminal))
    assert outs[0] == outs[1]


def test_only_one_crashes_other_wins():
    # A drives into the wall while B makes a legal move; B must win outright.
    eng = make_engine(start_a=(0, 2), start_b=(7, 2))
    st = eng.initial_state()
    st = eng.tick(st, Direction.LEFT, Direction.UP)  # A into left wall, B up legally
    assert st.terminal
    assert st.outcome == Outcome.B_WINS
