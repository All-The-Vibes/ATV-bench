"""Trusted match REFEREE — the arena adjudicates, the bot only moves (FOLLOW_UPS item 1).

This closes the last trust-boundary item. Before this, the arena ran the bot and let
its STDOUT become the match result: a bot could print `{"outcome":"a_wins"}` and be
believed. Now the arena runs THIS referee as its entrypoint. The referee:

  1. runs the deterministic `TronEngine`,
  2. asks each player for ONE move per turn through a `MoveSource`,
  3. the untrusted bot is a `SubprocessMoveSource` — a sandboxed `python3 /work/main.py`
     that speaks a strict line protocol (one direction word per turn, per-turn timeout);
     the anchor is a TRUSTED in-process `TrustedGreedyBot`,
  4. and the REFEREE authors the schema-shaped result from the engine's verdict.

A bot's stdout is only ever parsed as a move token. Anything that is not a valid
direction — a fabricated result JSON, garbage, a hang, EOF — yields no move, which the
referee scores as a FORFEIT LOSS for that player. The bot can never inject an outcome.

The emitted record matches the publish-side `ok` contract (status/player_a/player_b/
outcome/match_id, plus game/seed and forfeit_reason on a forfeit), so the existing
trusted publish job binds and scores it unchanged. Identity is still spec-bound on the
publish side; the referee only supplies the honest outcome.
"""
from __future__ import annotations

import enum
import json
import subprocess
import sys
import threading
from typing import Any, Protocol

from atv_bench.arena.engine import Direction, GameState, Outcome, TronEngine


class ForfeitReason(str, enum.Enum):
    # Mirrors atv_bench.elo.ForfeitReason values so the publish side accepts them.
    TIMEOUT = "TIMEOUT"
    INVALID_DIFF = "INVALID_DIFF"
    NO_OP = "NO_OP"
    CRASH = "CRASH"


_VALID_MOVE_WORDS = {d.value: d for d in Direction}


class MoveSource(Protocol):
    """One player's move provider. `next_move` returns a Direction, or None to signal
    the player produced no legal move this turn (forfeit)."""

    def next_move(self, observation: dict[str, Any]) -> Direction | None: ...

    def close(self) -> None: ...


def _observation(state: GameState, engine: TronEngine, *, me: str) -> dict[str, Any]:
    """Build the per-turn observation from the trusted engine state (never bot input)."""
    if me == "a":
        mine, theirs = (state.pos_a, state.dir_a, state.trail_a), (state.pos_b, state.dir_b, state.trail_b)
    else:
        mine, theirs = (state.pos_b, state.dir_b, state.trail_b), (state.pos_a, state.dir_a, state.trail_a)
    return {
        "width": engine.width,
        "height": engine.height,
        "turn": state.turn,
        "you": {"pos": list(mine[0]), "dir": mine[1].value,
                "trail": [list(c) for c in sorted(mine[2])]},
        "opponent": {"pos": list(theirs[0]), "dir": theirs[1].value,
                     "trail": [list(c) for c in sorted(theirs[2])]},
    }


def parse_move(raw: str | None) -> Direction | None:
    """Strict move parser: exactly one recognized direction word, else None.

    This is the trust boundary. A fabricated result JSON, extra tokens, numbers, or an
    empty line all parse to None (which the referee scores as a forfeit). We accept a
    single lowercase direction word, optionally surrounded by whitespace.
    """
    if raw is None:
        return None
    tok = raw.strip().lower()
    return _VALID_MOVE_WORDS.get(tok)


class TrustedGreedyBot:
    """The anchor's in-process bot. Deterministic, never crashes when a move exists.

    Strategy: keep going straight if safe; otherwise turn to the first safe neighbor in
    a fixed priority order. Pure function of the observation; no I/O, no randomness."""

    def __init__(self, player: str = "a") -> None:
        self.player = player

    def next_move(self, observation: dict[str, Any]) -> Direction | None:
        w = observation["width"]
        h = observation["height"]
        you = observation["you"]
        opp = observation["opponent"]
        px, py = you["pos"]
        blocked = {tuple(c) for c in you["trail"]} | {tuple(c) for c in opp["trail"]}
        cur = _VALID_MOVE_WORDS.get(you["dir"], Direction.UP)

        def safe(d: Direction) -> bool:
            dx, dy = d.delta
            nx, ny = px + dx, py + dy
            if not (0 <= nx < w and 0 <= ny < h):
                return False
            return (nx, ny) not in blocked

        # Prefer to keep heading (straight); then a fixed priority of the rest, excluding
        # a direct reversal of the current heading (a 180 into our own neck).
        reverse = {Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP,
                   Direction.LEFT: Direction.RIGHT, Direction.RIGHT: Direction.LEFT}[cur]
        order = [cur] + [d for d in (Direction.UP, Direction.RIGHT, Direction.DOWN,
                                     Direction.LEFT) if d not in (cur, reverse)]
        for d in order:
            if safe(d):
                return d
        return cur  # trapped: keep heading (will crash) rather than return None

    def close(self) -> None:  # symmetry with MoveSource
        pass


class SubprocessMoveSource:
    """Untrusted-bot transport. Spawns the bot once and speaks a per-turn line protocol.

    Each turn: write one JSON observation line to the bot's stdin, then read exactly one
    line from stdout with a per-turn timeout. The line is parsed by `parse_move`; a
    timeout, EOF, dead process, or non-direction line all yield None (forfeit). The bot
    process is long-lived across turns so trail state accrues on its side if it wants.

    stderr is discarded (an untrusted bot's stderr must never reach a public log). The
    process is hard-killed on close.
    """

    def __init__(self, argv: list[str], *, per_turn_timeout: float = 2.0) -> None:
        self.per_turn_timeout = per_turn_timeout
        self._dead = False
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered
        )

    def next_move(self, observation: dict[str, Any]) -> Direction | None:
        if self._dead or self.proc.poll() is not None:
            return None
        line_holder: list[str | None] = [None]

        def _pump() -> None:
            try:
                self.proc.stdin.write(json.dumps(observation) + "\n")
                self.proc.stdin.flush()
                line_holder[0] = self.proc.stdout.readline()
            except (BrokenPipeError, ValueError, OSError):
                line_holder[0] = None

        t = threading.Thread(target=_pump, daemon=True)
        t.start()
        t.join(self.per_turn_timeout)
        if t.is_alive():
            # Hung this turn — kill the process and forfeit. A killed bot can never be
            # asked again (poll() != None), so the whole match forfeits cleanly.
            self._kill()
            return None
        raw = line_holder[0]
        if not raw:  # EOF / empty => no move
            return None
        return parse_move(raw)

    def _kill(self) -> None:
        self._dead = True
        try:
            self.proc.kill()
        except Exception:
            pass

    def close(self) -> None:
        self._kill()
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass


def _forfeit_record(*, player_a: str, player_b: str, match_id: str,
                    who: str, reason: ForfeitReason, game: str, seed: int) -> dict[str, Any]:
    outcome = "forfeit_a" if who == "a" else "forfeit_b"
    return {
        "status": "ok",
        "player_a": player_a,
        "player_b": player_b,
        "outcome": outcome,
        "forfeit_reason": reason.value,
        "match_id": match_id,
        "game": game,
        "seed": seed,
    }


def _frame(state: GameState) -> dict[str, Any]:
    """Serialize one tick's geometry for visualization/replay (pure, deterministic)."""
    return {
        "turn": state.turn,
        "a": {"pos": list(state.pos_a),
              "trail": [list(c) for c in sorted(state.trail_a)]},
        "b": {"pos": list(state.pos_b),
              "trail": [list(c) for c in sorted(state.trail_b)]},
    }


def run_match(engine: TronEngine, source_a: MoveSource, source_b: MoveSource, *,
              player_a: str, player_b: str, match_id: str,
              game: str = "lightcycles", seed: int = 0,
              record: bool = False) -> dict[str, Any]:
    """Run a full refereed match and return the trusted, schema-shaped result.

    Both players are asked for a move each turn from the trusted engine's observation.
    A player that returns None (no legal/parseable move) FORFEITS immediately — scored a
    loss with a reason, never a draw and never dropped. If both forfeit the same turn it
    is a draw (mutual no-move). Otherwise the engine adjudicates the collision outcome
    and the referee translates it to an a/b-relative result record.

    With `record=True` the result gains a `frames` list (one entry per tick, initial
    state first) and a `board` dict — enough to animate/replay the match deterministically.
    Recording never changes the adjudicated outcome.
    """
    frames: list[dict[str, Any]] = []

    def _emit(st: GameState) -> None:
        if record:
            frames.append(_frame(st))

    def _finalize(result: dict[str, Any]) -> dict[str, Any]:
        if record:
            result = dict(result)
            result["frames"] = frames
            result["board"] = {"width": engine.width, "height": engine.height}
        return result

    state = engine.initial_state()
    _emit(state)
    while not state.terminal:
        obs_a = _observation(state, engine, me="a")
        obs_b = _observation(state, engine, me="b")
        move_a = source_a.next_move(obs_a)
        move_b = source_b.next_move(obs_b)

        a_forfeit = move_a is None
        b_forfeit = move_b is None
        if a_forfeit or b_forfeit:
            if a_forfeit and b_forfeit:
                # Mutual no-move: a genuine draw (neither could act).
                return _finalize({
                    "status": "ok", "player_a": player_a, "player_b": player_b,
                    "outcome": Outcome.DRAW.value, "match_id": match_id,
                    "game": game, "seed": seed,
                })
            who = "a" if a_forfeit else "b"
            return _finalize(_forfeit_record(player_a=player_a, player_b=player_b,
                                   match_id=match_id, who=who,
                                   reason=ForfeitReason.CRASH, game=game, seed=seed))

        state = engine.tick(state, move_a, move_b)
        _emit(state)

    # Engine reached a terminal collision/turn-cap verdict.
    outcome = state.outcome or Outcome.DRAW
    return _finalize({
        "status": "ok",
        "player_a": player_a,
        "player_b": player_b,
        "outcome": outcome.value,
        "match_id": match_id,
        "game": game,
        "seed": seed,
    })
