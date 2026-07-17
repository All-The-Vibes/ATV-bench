"""Pure ASCII frame renderer for the demo live-feed (Act 2).

`render_frame(state, engine, ...)` turns an engine `GameState` into a deterministic,
human-watchable board frame — no I/O, no time, no randomness. The live demo feed and CI
render byte-identical output for the same state, so the feed is fully testable.

Glyphs:
  A / B  = the two players' heads (whichever direction they face)
  o / x  = player A / player B trails
  ·      = empty cell
"""
from __future__ import annotations

from atv_bench.arena.engine import GameState, Outcome, TronEngine

_HEAD_A = "A"
_HEAD_B = "B"
_TRAIL_A = "o"
_TRAIL_B = "x"
_EMPTY = "·"


def render_frame(
    state: GameState,
    engine: TronEngine,
    *,
    label_a: str = "A",
    label_b: str = "B",
) -> str:
    """Render one board frame as a multi-line string. Pure and deterministic."""
    w, h = engine.width, engine.height
    grid = [[_EMPTY for _ in range(w)] for _ in range(h)]

    for (x, y) in state.trail_a:
        if 0 <= x < w and 0 <= y < h:
            grid[y][x] = _TRAIL_A
    for (x, y) in state.trail_b:
        if 0 <= x < w and 0 <= y < h:
            grid[y][x] = _TRAIL_B
    # Heads drawn last so they win over their own trail cell.
    ax, ay = state.pos_a
    bx, by = state.pos_b
    if 0 <= ax < w and 0 <= ay < h:
        grid[ay][ax] = _HEAD_A
    if 0 <= bx < w and 0 <= by < h:
        grid[by][bx] = _HEAD_B

    border = "+" + "-" * w + "+"
    lines = [
        f"  turn {state.turn}   {label_a} [{_HEAD_A}/{_TRAIL_A}]  vs  {label_b} [{_HEAD_B}/{_TRAIL_B}]",
        border,
    ]
    for row in grid:
        lines.append("|" + "".join(row) + "|")
    lines.append(border)

    if state.terminal:
        lines.append("  " + _outcome_line(state.outcome, label_a, label_b))
    return "\n".join(lines)


def _outcome_line(outcome: Outcome | None, label_a: str, label_b: str) -> str:
    if outcome == Outcome.A_WINS:
        return f"★ {label_a} wins"
    if outcome == Outcome.B_WINS:
        return f"★ {label_b} wins"
    return f"— draw between {label_a} and {label_b}"
