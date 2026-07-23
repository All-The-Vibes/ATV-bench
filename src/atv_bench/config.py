"""Build a CodeClash pvp config for a harness-vs-harness match (Implementation step 3).

Per-game seed repo + bot protocol (gap #11): each game has its own bot file + a prompt
that describes that game's bot contract — lightcycles' `main.py` `get_move(obs)` is NOT
battlesnake's contract. We reuse CodeClash's own game_description prompts (vendored) so
the harness edits the right protocol; the `edit` prompt is the concrete instruction the
adapter passes to the harness CLI.

`agent: <harness-key>` routes each player through our monkeypatched
`codeclash.tournaments.pvp.get_agent` (see integration.register) to a HarnessPlayer.
"""
from __future__ import annotations

import dataclasses
from typing import Any

# Prompt + game protocol versions — part of the schema-v2 identity key (gap #14).
PROMPT_VERSION = "edit@1"


@dataclasses.dataclass(frozen=True)
class GameSpec:
    key: str
    codeclash_name: str
    bot_file: str
    version: str
    edit_prompt: str
    sims_per_round: int
    args: dict[str, Any]


GAME_SPECS: dict[str, GameSpec] = {
    "lightcycles": GameSpec(
        key="lightcycles",
        codeclash_name="LightCycles",
        bot_file="main.py",
        version="lightcycles@1",
        edit_prompt=(
            "Improve `main.py` so your Tron light-cycle bot wins more games. It must "
            "implement `get_move(obs) -> str` returning one of 'N','S','E','W'. Avoid "
            "walls, your own trail, and opponents' trails; try to survive longest and "
            "claim the most territory. Edit only the bot; keep the function signature."
        ),
        sims_per_round=10,
        args={},
    ),
    "ants": GameSpec(
        key="ants",
        codeclash_name="Ants",
        bot_file="main.py",
        version="ants@1",
        edit_prompt=(
            "Improve `main.py` so your ant-colony bot wins more games. It must implement "
            "`do_turn(obs: dict) -> list` returning a list of `[row, col, dir]` moves, "
            "where `dir` is one of 'N','S','E','W'. `obs` is a fog-of-war view and the "
            "process is long-lived, so you may keep state in globals across turns. "
            "Gather food, expand your colony, and avoid collisions. Edit only the bot; "
            "keep the `do_turn` signature."
        ),
        sims_per_round=10,
        args={},
    ),
    "dummy": GameSpec(
        key="dummy",
        codeclash_name="Dummy",
        bot_file="main.py",
        version="dummy@1",
        edit_prompt=(
            "Improve `main.py` for the Dummy smoke arena. This is a minimal plumbing "
            "check with no game-specific bot contract — keep the bot readable and make "
            "sure it runs cleanly per turn. Edit only the bot file."
        ),
        sims_per_round=1,
        args={},
    ),
    "gomoku": GameSpec(
        key="gomoku",
        codeclash_name="Gomoku",
        bot_file="main.py",
        version="gomoku@1",
        edit_prompt=(
            "Improve `main.py` so your Gomoku (five-in-a-row) bot wins more games. It "
            "must implement `get_move(board: list[list[int]], color: str) -> "
            "tuple[int, int]` returning the `(row, col)` of your next stone on the 15x15 "
            "board, where board cells are 0=empty, 1=black, 2=white and `color` is "
            "'black' or 'white'. Only play on empty cells; block the opponent and build "
            "toward five in a row. Edit only the bot; keep the `get_move` signature."
        ),
        sims_per_round=10,
        args={},
    ),
    "paintvolley": GameSpec(
        key="paintvolley",
        codeclash_name="PaintVolley",
        bot_file="main.py",
        version="paintvolley@1",
        edit_prompt=(
            "Improve `main.py` so your PaintVolley bot wins more games. It must "
            "implement `get_action(obs: dict) -> str` returning one of 'LEFT','RIGHT',"
            "'JUMP','JUMP_LEFT','JUMP_RIGHT','NONE' each turn. Read the ball and player "
            "positions from `obs`, keep the ball on the opponent's side, and cover more "
            "of the court. Edit only the bot; keep the `get_action` signature."
        ),
        sims_per_round=10,
        args={},
    ),
    "battlesnake": GameSpec(
        key="battlesnake",
        codeclash_name="BattleSnake",
        bot_file="main.py",
        version="battlesnake@1",
        edit_prompt=(
            "Improve your BattleSnake bot so it survives longer and beats the opponent. "
            "Follow the BattleSnake move API in the seed project; avoid walls, your own "
            "body, and the enemy. Edit only the bot files in the working dir."
        ),
        sims_per_round=1,
        args={"width": 11, "height": 11, "browser": False},
    ),
    # --- Wave C: arenas proven live by a real end-to-end scored match (see docs/arenas.md
    # § "Wave C — end-to-end verification"). Each reuses CodeClash's EXISTING referee; the
    # harness edits the arena's own submission (source in any language — the arena's Docker
    # image compiles/runs it) and CodeClash adjudicates a decisive 1-v-1 winner. bot_file is
    # the submission path (a directory for multi-file source bots); the harness edits the
    # seeded tree in place and reads the in-container docs/ for the exact contract.
    "corewar": GameSpec(
        key="corewar", codeclash_name="CoreWar", bot_file="warrior.red",
        version="corewar@1",
        edit_prompt=(
            "Improve your CoreWar warrior in `warrior.red` — a program in the Redcode "
            "assembly language executed inside the MARS virtual machine. Read ./docs/ for "
            "the Redcode instruction set. Craft tactics (bombers, scanners, replicators) "
            "to make the opponent's process terminate first. Edit only `warrior.red`."
        ),
        sims_per_round=10, args={},
    ),
    "robotrumble": GameSpec(
        key="robotrumble", codeclash_name="RobotRumble", bot_file="robot.py",
        version="robotrumble@1",
        edit_prompt=(
            "Improve your Robot Rumble bot in `robot.py`. It must implement "
            "`def robot(state, unit)` — called once per unit per turn — returning that "
            "unit's action. Command your team of robots to move, attack, and outmaneuver "
            "the opponent over the 100-turn match. Read ./docs/ for the state/unit API. "
            "Keep the `robot(state, unit)` signature; edit only `robot.py`."
        ),
        sims_per_round=10, args={"raw": True},
    ),
    "huskybench": GameSpec(
        key="huskybench", codeclash_name="HuskyBench", bot_file="client/player.py",
        version="huskybench@1",
        edit_prompt=(
            "Improve your poker bot in `client/player.py`. Read ./docs/ for the betting "
            "API and game flow. Make better bet/fold/raise decisions to win more chips "
            "over the simulated rounds. Edit the client bot; keep its entry-point shape."
        ),
        sims_per_round=10, args={},
    ),
    "scml": GameSpec(
        key="scml", codeclash_name="SCML", bot_file="scml_agent.py",
        version="scml@1",
        edit_prompt=(
            "Improve your SCML supply-chain negotiation agent in `scml_agent.py`. It must "
            "implement the `decide(observation)` negotiation contract — read ./docs/ for "
            "the ANAC SCML OneShot API. Negotiate better buy/sell contracts to maximize "
            "profit. Edit only `scml_agent.py`; keep the agent entry point."
        ),
        sims_per_round=2, args={},
    ),
    "cyborg": GameSpec(
        key="cyborg", codeclash_name="CybORG", bot_file="cyborg_agent.py",
        version="cyborg@1",
        edit_prompt=(
            "Improve your CybORG cyber-defense agent in `cyborg_agent.py`. It must "
            "implement `def decide(observation, action_space)` returning an integer action "
            "(or None). Read ./docs/ for the CAGE drone-swarm environment. Defend your "
            "drones and maximize reward. Keep the `decide` signature; edit only the agent."
        ),
        sims_per_round=2, args={},
    ),
    "bomberland": GameSpec(
        key="bomberland", codeclash_name="Bomberland", bot_file="bomberland_agent.py",
        version="bomberland@1",
        edit_prompt=(
            "Improve your Bomberland agent in `bomberland_agent.py`. It must implement "
            "`def next_actions(game_state)` returning your units' actions each tick. Read "
            "./docs/ for the Bomberman-style API. Place bombs, collect powerups, and "
            "outlast the opponent. Keep the `next_actions` signature; edit only the agent."
        ),
        sims_per_round=2, args={},
    ),
    "chess": GameSpec(
        key="chess", codeclash_name="Chess", bot_file="src/",
        version="chess@1",
        edit_prompt=(
            "Improve the Kojiro chess engine — C++ source in `src/`, compiled with "
            "`make native` and speaking the UCI protocol. Read ./docs/. Improve the "
            "evaluation function, search, and move ordering to win more games. Edit the "
            "C++ source in `src/`; it must still compile with `make native`."
        ),
        sims_per_round=2, args={},
    ),
    "halite": GameSpec(
        key="halite", codeclash_name="Halite", bot_file="submission",
        version="halite@1",
        edit_prompt=(
            "Improve your Halite bot in the `submission/` folder — a territory-capture "
            "strategy game on a grid. Read ./docs/ for the frame protocol. The folder may "
            "be any supported language and is compiled per round; keep it to one bot. "
            "Capture more territory and strength to win. Edit files under `submission/`."
        ),
        sims_per_round=2, args={},
    ),
    "halite2": GameSpec(
        key="halite2", codeclash_name="Halite2", bot_file="submission",
        version="halite2@1",
        edit_prompt=(
            "Improve your Halite II bot in the `submission/` folder — pilot a fleet of "
            "ships mining planets in a continuous universe. Read ./docs/ and "
            "`submission/<lang>/runGame.sh` for how the bot is compiled and run. Expand "
            "control and win. Edit files under `submission/`; keep a single bot."
        ),
        sims_per_round=2, args={},
    ),
    "halite3": GameSpec(
        key="halite3", codeclash_name="Halite3", bot_file="submission",
        version="halite3@1",
        edit_prompt=(
            "Improve your Halite III bot in the `submission/` folder — collect halite on a "
            "grid and return it to base. Read ./docs/ for the frame protocol. The bot is "
            "compiled per round; keep it to a single bot. Edit files under `submission/`."
        ),
        sims_per_round=2, args={},
    ),
    "battlecode23": GameSpec(
        key="battlecode23", codeclash_name="BattleCode23", bot_file="src/mysubmission",
        version="battlecode23@1",
        edit_prompt=(
            "Improve your Battlecode 2023 (Tempest) Java bot in `src/mysubmission` — a "
            "real-time strategy game to conquer sky islands with reality anchors. Read "
            "./docs/ for the RobotController API. Improve robot logic to win. Edit the Java "
            "source under `src/mysubmission`; it must still compile."
        ),
        sims_per_round=2, args={},
    ),
    "battlecode24": GameSpec(
        key="battlecode24", codeclash_name="BattleCode24", bot_file="src/mysubmission",
        version="battlecode24@1",
        edit_prompt=(
            "Improve your Battlecode 2024 (Breadwars) Java bot in `src/mysubmission` — a "
            "real-time strategy game to capture the opponent's flags. Read ./docs/ for the "
            "RobotController API. Improve robot logic to win. Edit the Java source under "
            "`src/mysubmission`; it must still compile."
        ),
        sims_per_round=2, args={},
    ),
    "figgie": GameSpec(
        key="figgie", codeclash_name="Figgie", bot_file="main.py",
        version="figgie@1",
        edit_prompt=(
            "Improve your Figgie trading bot in `main.py`. It must implement "
            "`def get_action(state: dict)` returning a market action each tick. Read "
            "./docs/ for the card-market rules. Trade to maximize profit against the other "
            "players. Keep the `get_action` signature; edit only `main.py`."
        ),
        sims_per_round=2, args={},
    ),
    "bridge": GameSpec(
        key="bridge", codeclash_name="Bridge", bot_file="bridge_agent.py",
        version="bridge@1",
        edit_prompt=(
            "Improve your Bridge bot in `bridge_agent.py`. It implements the bidding and "
            "card-play contract (`get_bid`/`play_card`) for a partnership card game. Read "
            "./docs/ for the API. Bid and play to win more tricks with your partner. Edit "
            "only `bridge_agent.py`; keep its entry points."
        ),
        sims_per_round=2, args={},
    ),
}


def resolve_game(game: str) -> GameSpec:
    spec = GAME_SPECS.get(game)
    if spec is None:
        valid = ", ".join(sorted(GAME_SPECS))
        raise ValueError(f"unknown game {game!r}. Valid games: {valid}.")
    return spec


def build_pvp_config(
    *, game: str, a: str, b: str, model: str, rounds: int
) -> dict[str, Any]:
    """Build the CodeClash pvp config dict for a two-harness match.

    Both players run on the SAME model for parity (locked requirement). Player names
    are made distinct even for A/A self-play so containers/branches don't collide.
    """
    spec = resolve_game(game)
    names = _distinct_names(a, b)
    players = [
        {
            "agent": harness,   # routed by monkeypatched get_agent -> HarnessPlayer
            "name": name,
            "config": {
                "model": model,
                "bot_file": spec.bot_file,
                "harness": harness,
            },
        }
        for harness, name in zip((a, b), names)
    ]
    return {
        "tournament": {"rounds": rounds},
        "game": {
            "name": spec.codeclash_name,
            "sims_per_round": spec.sims_per_round,
            "args": spec.args,
        },
        "players": players,
        "prompts": {"edit": spec.edit_prompt},
        "_meta": {
            "game_version": spec.version,
            "prompt_version": PROMPT_VERSION,
            "model": model,
        },
    }


def _codeclash_name(harness: str) -> str:
    """Map a harness key to a git-ref-safe CodeClash player name.

    CodeClash derives a git branch ``PvpTournament.<Game>.<ts>.<name>`` from the player
    name, so the name must be a valid git ref component. The bare negative control's key
    is ``bare:<inner>`` (``BARE_PREFIX = "bare:"``) and a colon is illegal in a git ref,
    which made every live match with a bare control abort with git exit 128. Replacing the
    ``:`` with ``-`` is a 1:1 transform over the harness keyspace (leaf keys never contain
    ``:``; ``bare:`` appears once), so it never collides two distinct harnesses.
    """
    return harness.replace(":", "-")


def _distinct_names(a: str, b: str) -> tuple[str, str]:
    na, nb = _codeclash_name(a), _codeclash_name(b)
    if a != b:
        return (na, nb)
    return (f"{na}-A", f"{nb}-B")
