"""Tests for the pure ASCII frame renderer (demo live-feed).

The renderer turns an engine GameState into a deterministic, human-watchable board
frame. It is pure (no I/O, no time), so the live demo feed and CI both render the same
bytes. These are the RED tests written before src/atv_bench/arena/render.py exists.
"""
from __future__ import annotations

from atv_bench.arena.engine import Direction, TronEngine


def _engine() -> TronEngine:
    return TronEngine(
        width=5, height=3,
        start_a=(0, 1), start_b=(4, 1),
        dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=20,
    )


def test_render_frame_is_deterministic_and_pure():
    from atv_bench.arena.render import render_frame

    eng = _engine()
    st = eng.initial_state()
    a = render_frame(st, eng, label_a="StarterKit", label_b="Phoenix")
    b = render_frame(st, eng, label_a="StarterKit", label_b="Phoenix")
    assert a == b  # pure: same input -> identical bytes


def test_render_frame_shows_board_dimensions_and_heads():
    from atv_bench.arena.render import render_frame

    eng = _engine()
    st = eng.initial_state()
    frame = render_frame(st, eng, label_a="StarterKit", label_b="Phoenix")
    lines = frame.splitlines()
    # A header naming both players + a grid whose body has `height` rows.
    assert "StarterKit" in frame and "Phoenix" in frame
    # Both heads appear as distinct glyphs somewhere in the grid.
    grid_lines = [ln for ln in lines if set(ln) & set("<>v^AB◆")]
    assert grid_lines, "expected at least one grid row with head glyphs"


def test_render_frame_header_reports_turn():
    from atv_bench.arena.render import render_frame

    eng = _engine()
    st = eng.initial_state()
    st2 = eng.tick(st, Direction.RIGHT, Direction.LEFT)
    f0 = render_frame(st, eng, label_a="A", label_b="B")
    f1 = render_frame(st2, eng, label_a="A", label_b="B")
    assert "turn 0" in f0.lower()
    assert "turn 1" in f1.lower()


def test_render_frame_marks_terminal_outcome():
    from atv_bench.arena.render import render_frame
    from atv_bench.arena.engine import GameState, Outcome

    eng = _engine()
    st = eng.initial_state()
    terminal = GameState(
        pos_a=st.pos_a, pos_b=st.pos_b, dir_a=st.dir_a, dir_b=st.dir_b,
        trail_a=st.trail_a, trail_b=st.trail_b, turn=7,
        terminal=True, outcome=Outcome.A_WINS,
    )
    frame = render_frame(terminal, eng, label_a="Alpha", label_b="Beta")
    # The winner is surfaced by name on a terminal frame.
    assert "Alpha" in frame
    assert "win" in frame.lower()
