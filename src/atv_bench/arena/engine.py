"""Trusted, pure, deterministic lightcycles/Tron engine (FOLLOW_UPS item 1).

This is the adjudicator that makes the arena a REFEREE instead of a stdout-truster.
It has NO I/O and imports NOTHING that runs a bot: it takes one move per player per
tick and decides win/loss/draw from real collision rules. The referee
(`arena.referee`) feeds it moves collected from sandboxed bot subprocesses; the
engine — never the bot — authors the outcome.

Determinism is load-bearing: the same start + same move sequence always yields the
same terminal state, so a match is reproducible from its seed and move log.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, replace


class Direction(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"

    @property
    def delta(self) -> tuple[int, int]:
        return {
            Direction.UP: (0, -1),
            Direction.DOWN: (0, 1),
            Direction.LEFT: (-1, 0),
            Direction.RIGHT: (1, 0),
        }[self]


class Outcome(str, enum.Enum):
    A_WINS = "a_wins"
    B_WINS = "b_wins"
    DRAW = "draw"


Cell = tuple[int, int]


@dataclass(frozen=True)
class GameState:
    """Immutable snapshot. `tick` returns a new state; nothing is mutated in place."""
    pos_a: Cell
    pos_b: Cell
    dir_a: Direction
    dir_b: Direction
    trail_a: frozenset[Cell]
    trail_b: frozenset[Cell]
    turn: int = 0
    terminal: bool = False
    outcome: Outcome | None = None


@dataclass(frozen=True)
class TronEngine:
    width: int
    height: int
    start_a: Cell
    start_b: Cell
    dir_a: Direction = Direction.RIGHT
    dir_b: Direction = Direction.LEFT
    max_turns: int = 100

    def initial_state(self) -> GameState:
        return GameState(
            pos_a=self.start_a,
            pos_b=self.start_b,
            dir_a=self.dir_a,
            dir_b=self.dir_b,
            trail_a=frozenset({self.start_a}),
            trail_b=frozenset({self.start_b}),
            turn=0,
            terminal=False,
            outcome=None,
        )

    def _on_board(self, cell: Cell) -> bool:
        x, y = cell
        return 0 <= x < self.width and 0 <= y < self.height

    def tick(self, state: GameState, move_a: Direction, move_b: Direction) -> GameState:
        """Advance one simultaneous tick. Returns a new (possibly terminal) state.

        A ticking a terminal state is a no-op (returns it unchanged) so a referee loop
        that over-runs by one is safe.
        """
        if state.terminal:
            return state

        dxa, dya = move_a.delta
        dxb, dyb = move_b.delta
        new_a = (state.pos_a[0] + dxa, state.pos_a[1] + dya)
        new_b = (state.pos_b[0] + dxb, state.pos_b[1] + dyb)

        # Existing obstacles are all trail cells laid so far (own + opponent). A player's
        # own neck is already in its trail, so a 180-into-neck is a self-trail crash.
        obstacles = state.trail_a | state.trail_b

        a_crash = (not self._on_board(new_a)) or (new_a in obstacles)
        b_crash = (not self._on_board(new_b)) or (new_b in obstacles)

        # Head-to-head collisions decided on the NEW cells (both crash => draw):
        #   same target cell, or a straight swap of cells.
        same_cell = new_a == new_b
        swap = (new_a == state.pos_b) and (new_b == state.pos_a)
        if same_cell or swap:
            a_crash = True
            b_crash = True

        if a_crash or b_crash:
            if a_crash and b_crash:
                outcome = Outcome.DRAW
            elif a_crash:
                outcome = Outcome.B_WINS
            else:
                outcome = Outcome.A_WINS
            # On a crash, positions/trails are frozen at pre-move (the crash is fatal).
            return replace(state, turn=state.turn + 1, terminal=True, outcome=outcome)

        moved = GameState(
            pos_a=new_a,
            pos_b=new_b,
            dir_a=move_a,
            dir_b=move_b,
            trail_a=state.trail_a | {new_a},
            trail_b=state.trail_b | {new_b},
            turn=state.turn + 1,
            terminal=False,
            outcome=None,
        )
        if moved.turn >= self.max_turns:
            # Survival tie: both still alive at the turn cap.
            return replace(moved, terminal=True, outcome=Outcome.DRAW)
        return moved
