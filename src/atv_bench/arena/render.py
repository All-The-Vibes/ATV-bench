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


def frame_to_dict(
    state: GameState,
    engine: TronEngine,
    *,
    label_a: str = "A",
    label_b: str = "B",
) -> dict:
    """A pure, JSON-serializable snapshot of one frame for the browser SSE live feed.

    Mirrors `render_frame` (same state → same data) but emits primitives the browser
    canvas draws: board dimensions, both trails as `[x, y]` cell lists, both heads, and
    the terminal/outcome. No I/O, no time, no randomness — unit-testable without a
    server, and `json.dumps`-safe (lists not tuples/sets, enum value not the enum).
    """
    def _cells(trail) -> list[list[int]]:
        # Sorted for determinism (frozenset iteration order is not guaranteed).
        return [[int(x), int(y)] for (x, y) in sorted(trail)]

    ax, ay = state.pos_a
    bx, by = state.pos_b
    return {
        "turn": int(state.turn),
        "width": int(engine.width),
        "height": int(engine.height),
        "label_a": label_a,
        "label_b": label_b,
        "head_a": [int(ax), int(ay)],
        "head_b": [int(bx), int(by)],
        "dir_a": state.dir_a.value,
        "dir_b": state.dir_b.value,
        "trail_a": _cells(state.trail_a),
        "trail_b": _cells(state.trail_b),
        "terminal": bool(state.terminal),
        "outcome": state.outcome.value if state.outcome is not None else None,
    }


def _outcome_line(outcome: Outcome | None, label_a: str, label_b: str) -> str:
    if outcome == Outcome.A_WINS:
        return f"★ {label_a} wins"
    if outcome == Outcome.B_WINS:
        return f"★ {label_b} wins"
    return f"— draw between {label_a} and {label_b}"
