"""Single source of truth for the arenas ATV-bench can play.

The README, CONTRIBUTING, and CLI historically defaulted to `battlesnake`, but the only
arena with a real, trusted engine + referee in this repo is `lightcycles` (Tron). A bot
submitted for a game with no engine can never be adjudicated, so `submit`/`validate-game`
must reject it with an actionable message instead of accepting a dead submission.

Keep this list honest: a game is `live` only when `src/atv_bench/arena/` can referee it.
Adding a game (a real engine + referee + shape check) flips its status here; nothing else
in the CLI needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    """One arena. `live` is False for a planned game with no engine yet."""

    key: str
    title: str
    live: bool
    entrypoint: str  # the bot file the arena drives (e.g. main.py)
    summary: str


# Ordered: live games first, then planned. `lightcycles` is the shipped arena — its
# engine (arena/engine.py) + trusted referee (arena/referee.py) adjudicate real gameplay.
GAMES: tuple[Game, ...] = (
    Game(
        key="lightcycles",
        title="Lightcycles (Tron)",
        live=True,
        entrypoint="main.py",
        summary="Deterministic Tron/lightcycles. Your bot emits one move per turn; the "
        "trusted referee adjudicates from board geometry, not the bot's word.",
    ),
    Game(
        key="ants",
        title="Ants",
        live=True,
        entrypoint="main.py",
        summary="Fog-of-war ant colony skirmish. Your long-lived bot emits a list of "
        "moves each turn; the trusted arena adjudicates collisions and scoring.",
    ),
    Game(
        key="dummy",
        title="Dummy (smoke arena)",
        live=True,
        entrypoint="main.py",
        summary="Minimal smoke arena used to validate the harness plumbing end-to-end; "
        "no game-specific bot contract.",
    ),
    Game(
        key="gomoku",
        title="Gomoku (five-in-a-row)",
        live=True,
        entrypoint="main.py",
        summary="Two-player 15x15 five-in-a-row. Your bot returns one move per turn; the "
        "trusted arena adjudicates from board state, not the bot's word.",
    ),
    Game(
        key="paintvolley",
        title="PaintVolley",
        live=True,
        entrypoint="main.py",
        summary="Paint-splattering volleyball. Your bot emits one action per turn; the "
        "trusted arena adjudicates physics and scoring.",
    ),
    Game(
        key="battlesnake",
        title="Battlesnake",
        live=False,
        entrypoint="main.py",
        summary="Planned. No trusted engine in this repo yet — submissions are rejected "
        "until an arena + referee ships (see CONTRIBUTING → Add a game).",
    ),
)

DEFAULT_GAME = "lightcycles"

_BY_KEY: dict[str, Game] = {g.key: g for g in GAMES}


def get_game(key: str) -> Game | None:
    """Return the Game for `key`, or None if unknown."""
    return _BY_KEY.get(key)


def is_live(key: str) -> bool:
    """True only if `key` is a known game with a real, playable arena."""
    g = _BY_KEY.get(key)
    return bool(g and g.live)


def live_keys() -> list[str]:
    """Keys of games that can actually be played/adjudicated right now."""
    return [g.key for g in GAMES if g.live]


def assert_playable(key: str) -> None:
    """Raise ValueError with an actionable message if `key` can't be played.

    Used by submit/validate-game to fail closed: an unknown game or a planned game
    (no engine) must never produce a submission that can't be adjudicated.
    """
    g = _BY_KEY.get(key)
    if g is None:
        raise ValueError(
            f"unknown game {key!r}. Available: {', '.join(live_keys())} "
            f"(see `atv-bench games`)."
        )
    if not g.live:
        raise ValueError(
            f"game {key!r} is planned, not playable yet — it has no trusted arena in "
            f"this repo, so a bot for it can't be adjudicated. Use one of: "
            f"{', '.join(live_keys())} (see `atv-bench games`)."
        )
