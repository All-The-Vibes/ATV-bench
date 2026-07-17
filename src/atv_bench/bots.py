"""Selectable local opponents for `atv-bench play` (the "series of bots").

The demo's core UX gap: the only opponent was the in-process greedy anchor, so a user
had nothing to run the visualization *against*. This registry exposes named, in-process,
deterministic opponents that all implement the referee's `MoveSource` protocol
(`next_move(observation) -> Direction | None`, `close()`).

Every bot here is a PURE function of the per-turn observation — no I/O, no wall-clock, no
randomness — so a match is fully deterministic and therefore replayable. These are
*reference* opponents, not the trusted adjudicator: the engine (never a bot) still
authors the outcome.

`bare` is the "no-harness baseline": a plain, obvious strategy standing in for a model
with no harness scaffolding. It is intentionally simple so a harness-built bot that
can't beat it has learned nothing. (A future adapter-backed live `bare` — a raw model
with no skills/MCP — plugs in here behind the same protocol.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atv_bench.arena.engine import Direction
from atv_bench.arena.referee import MoveSource, TrustedGreedyBot

_VALID = {d.value: d for d in Direction}
_REVERSE = {
    Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP,
    Direction.LEFT: Direction.RIGHT, Direction.RIGHT: Direction.LEFT,
}


def _blocked_and_pos(observation: dict[str, Any]):
    you = observation["you"]
    opp = observation["opponent"]
    blocked = {tuple(c) for c in you["trail"]} | {tuple(c) for c in opp["trail"]}
    px, py = you["pos"]
    cur = _VALID.get(you["dir"], Direction.UP)
    return blocked, px, py, cur


def _safe(observation: dict[str, Any], px: int, py: int, blocked, d: Direction) -> bool:
    w, h = observation["width"], observation["height"]
    dx, dy = d.delta
    nx, ny = px + dx, py + dy
    if not (0 <= nx < w and 0 <= ny < h):
        return False
    return (nx, ny) not in blocked


class WallHuggerBot:
    """Hugs walls: prefers turning to keep a wall/trail on one hand, filling space.

    Deterministic priority: try to turn (fixed handed order), fall back to straight,
    then any safe direction. A distinct playstyle from greedy so matches are decisive.
    """

    def __init__(self, player: str = "a") -> None:
        self.player = player

    def next_move(self, observation: dict[str, Any]) -> Direction | None:
        blocked, px, py, cur = _blocked_and_pos(observation)
        # Prefer to turn right relative to heading, then straight, then left, then back.
        turn_right = {Direction.UP: Direction.RIGHT, Direction.RIGHT: Direction.DOWN,
                      Direction.DOWN: Direction.LEFT, Direction.LEFT: Direction.UP}[cur]
        turn_left = _REVERSE[turn_right]
        order = [turn_right, cur, turn_left]
        order += [d for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT)
                  if d not in order and d != _REVERSE[cur]]
        for d in order:
            if _safe(observation, px, py, blocked, d):
                return d
        return cur

    def close(self) -> None:
        pass


class BareBaselineBot:
    """The "no-harness" baseline: keep straight while safe, else first safe turn.

    Deliberately minimal — the floor a real harness must clear. Deterministic and
    obvious: no lookahead, no space-filling, just don't crash if a move exists.
    """

    def __init__(self, player: str = "a") -> None:
        self.player = player

    def next_move(self, observation: dict[str, Any]) -> Direction | None:
        blocked, px, py, cur = _blocked_and_pos(observation)
        order = [cur] + [d for d in (Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT)
                         if d != cur and d != _REVERSE[cur]]
        for d in order:
            if _safe(observation, px, py, blocked, d):
                return d
        return cur

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class Bot:
    """One selectable opponent. `factory(player)` builds a fresh MoveSource."""

    key: str
    title: str
    summary: str
    factory: Callable[[str], MoveSource]


# Ordered by increasing strength: bare < wall_hugger < greedy. `greedy` is the same
# trusted anchor the sandboxed arena uses, so beating it locally means something.
BOTS: tuple[Bot, ...] = (
    Bot(
        key="greedy",
        title="Greedy (arena anchor)",
        summary="The trusted reference anchor: keeps heading while safe, else first safe "
        "turn in a fixed priority. Same bot the sandboxed arena plays — the yardstick.",
        factory=lambda player: TrustedGreedyBot(player=player),
    ),
    Bot(
        key="wall_hugger",
        title="Wall Hugger",
        summary="Turns to hug walls and its own trail, filling space to box the opponent "
        "in. A distinct, more aggressive playstyle than greedy.",
        factory=lambda player: WallHuggerBot(player=player),
    ),
    Bot(
        key="bare",
        title="Bare baseline (no harness)",
        summary="The no-harness floor: go straight, only turn to avoid a crash. Stands in "
        "for a raw model with no skills/MCP/agents. If you can't beat it, the harness added "
        "nothing.",
        factory=lambda player: BareBaselineBot(player=player),
    ),
)

DEFAULT_OPPONENT = "greedy"

_BY_KEY: dict[str, Bot] = {b.key: b for b in BOTS}


def bot_keys() -> list[str]:
    """Keys of all selectable opponents, strongest-anchor first."""
    return [b.key for b in BOTS]


def get_bot(key: str) -> Bot | None:
    """Return the Bot for `key`, or None if unknown."""
    return _BY_KEY.get(key)


def make_bot(key: str, player: str = "a") -> MoveSource:
    """Instantiate the opponent `key` as a fresh MoveSource, or raise ValueError.

    Fail closed on an unknown bot with an actionable message rather than silently
    substituting a default — the caller (play command) picks the opponent explicitly.
    """
    b = _BY_KEY.get(key)
    if b is None:
        raise ValueError(
            f"unknown bot {key!r}. Available: {', '.join(bot_keys())} "
            f"(see `atv-bench bots`)."
        )
    return b.factory(player)
