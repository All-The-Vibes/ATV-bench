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


def _distinct_names(a: str, b: str) -> tuple[str, str]:
    if a != b:
        return (a, b)
    return (f"{a}-A", f"{b}-B")
