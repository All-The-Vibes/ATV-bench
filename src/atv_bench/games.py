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
        live=True,
        entrypoint="main.py",
        summary="Grid snake survival. Your bot's HTTP server (started by the arena from "
        "your source) picks a move each turn; the trusted arena adjudicates collisions.",
    ),
    # --- Wave C: reuse CodeClash's EXISTING referee. Each proven live by a real
    # end-to-end scored match (Docker build + live harness bots + arena adjudication);
    # see docs/arenas.md § "Wave C — end-to-end verification". `entrypoint` is the
    # submission the harness edits (a directory for multi-file source bots).
    Game(
        key="corewar", title="CoreWar", live=True, entrypoint="warrior.red",
        summary="Redcode assembly warriors battle in the MARS VM. The trusted arena runs "
        "the warriors and adjudicates which process survives.",
    ),
    Game(
        key="robotrumble", title="Robot Rumble", live=True, entrypoint="robot.py",
        summary="Turn-based grid battle where one bot commands a team of units. The "
        "trusted arena runs 100 turns and adjudicates Blue vs Red.",
    ),
    Game(
        key="huskybench", title="HuskyBench (poker)", live=True,
        entrypoint="client/player.py",
        summary="Poker sim. Your bot bets/folds/raises each hand; the trusted arena "
        "adjudicates chips won across rounds.",
    ),
    Game(
        key="scml", title="SCML (supply chain)", live=True, entrypoint="scml_agent.py",
        summary="ANAC supply-chain negotiation. Your agent negotiates contracts; the "
        "trusted runtime adjudicates profit.",
    ),
    Game(
        key="cyborg", title="CybORG (cyber defense)", live=True,
        entrypoint="cyborg_agent.py",
        summary="CAGE drone-swarm cyber-defense sim. Your agent's decide() controls the "
        "drones; the trusted runtime scores reward and picks the winner.",
    ),
    Game(
        key="bomberland", title="Bomberland", live=True, entrypoint="bomberland_agent.py",
        summary="Bomberman-style arena. Your agent's next_actions() drives your units; "
        "the trusted runtime adjudicates the match.",
    ),
    Game(
        key="chess", title="Chess (Kojiro)", live=True, entrypoint="src/",
        summary="UCI chess. You improve the Kojiro C++ engine (compiled in-arena with "
        "`make native`); the trusted arena plays engine-vs-engine and scores wins.",
    ),
    Game(
        key="halite", title="Halite", live=True, entrypoint="submission",
        summary="Grid territory-capture. Your compiled-from-source bot plays the frame "
        "protocol; the trusted arena adjudicates the winner.",
    ),
    Game(
        key="halite2", title="Halite II", live=True, entrypoint="submission",
        summary="Space fleet strategy. Your compiled-from-source bot pilots ships; the "
        "trusted arena adjudicates control of the map.",
    ),
    Game(
        key="halite3", title="Halite III", live=True, entrypoint="submission",
        summary="Halite-collection strategy. Your compiled-from-source bot plays the "
        "frame protocol; the trusted arena adjudicates the winner.",
    ),
    Game(
        key="battlecode23", title="Battlecode 2023 (Tempest)", live=True,
        entrypoint="src/mysubmission",
        summary="Java RTS to conquer sky islands. Your bot (compiled in-arena) is driven "
        "by the Battlecode engine; the trusted arena adjudicates the winner.",
    ),
    Game(
        key="battlecode24", title="Battlecode 2024 (Breadwars)", live=True,
        entrypoint="src/mysubmission",
        summary="Java RTS to capture flags. Your bot (compiled in-arena) is driven by the "
        "Battlecode engine; the trusted arena adjudicates the winner.",
    ),
    Game(
        key="figgie", title="Figgie (4-player market)", live=True, entrypoint="main.py",
        summary="4-player card-market trading. Your bot's get_action() trades each tick; "
        "the trusted arena adjudicates profit. Runs with 4 seats filled by harness/model "
        "variants.",
    ),
    Game(
        key="bridge", title="Bridge (4-player)", live=True, entrypoint="bridge_agent.py",
        summary="4-player partnership card game. Your bot bids and plays; the trusted "
        "arena adjudicates tricks. Runs with 4 seats filled by harness/model variants.",
    ),
    # --- Wave C: reuse-ready referee but BLOCKED by an upstream CodeClash bug. robocode
    # and battlecode25 have `max(scores)` with no empty guard (their siblings
    # battlecode23/24 guard it); a round with no decisive sim leaves scores empty and the
    # referee raises ValueError. Both crashed in real e2e runs, so they stay non-live until
    # upstream guards the empty case. NOT an architectural mismatch — see docs/arenas.md.
    Game(
        key="robocode", title="RoboCode", live=False, entrypoint="robots/custom/",
        summary="Not live: reusable JVM referee, but CodeClash's robocode get_results() "
        "crashes on an empty-scores round (unguarded max()). Awaiting upstream fix.",
    ),
    Game(
        key="battlecode25", title="Battlecode 2025", live=False,
        entrypoint="src/mysubmission",
        summary="Not live: reusable referee, but CodeClash's battlecode25 get_results() "
        "crashes on an empty-scores round (unguarded max()). Awaiting upstream fix.",
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
