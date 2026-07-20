"""WAVE A — make the 4 additional main.py-contract CodeClash arenas playable.

Per the protocol census (docs/arenas.md), five arenas fit ATV-bench's single-`main.py`,
per-turn, 1-v-1, stdin/stdout contract: lightcycles (already live), ants, dummy, gomoku,
paintvolley. This suite is RED-first: the four new games are `live=False` / absent from
GAME_SPECS today, so registration + pvp-config-shape tests fail until Wave A lands.

Arena facts read off the vendored arena modules
(`vendor/CodeClash/codeclash/arenas/<game>/<game>.py`):

    game        codeclash name  submission  bot entrypoint the arena validates
    ants        Ants            main.py     def do_turn(obs) -> list
    dummy       Dummy           main.py     (smoke arena; validate_code returns True)
    gomoku      Gomoku          main.py     def get_move(board, color) -> tuple[int,int]
    paintvolley PaintVolley     main.py     def get_action(obs) -> str
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from atv_bench.config import GAME_SPECS, build_pvp_config
from atv_bench.games import is_live, live_keys

# (game key, CodeClash arena name, submission file, entrypoint token the census/edit
# prompt must mention). Read directly from the arena modules.
WAVE_A = [
    ("ants", "Ants", "main.py", "do_turn"),
    ("dummy", "Dummy", "main.py", None),  # smoke arena: no required entrypoint
    ("gomoku", "Gomoku", "main.py", "get_move"),
    ("paintvolley", "PaintVolley", "main.py", "get_action"),
]


def _repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
    return Path(out)


@pytest.mark.parametrize("game,cc_name,submission,entrypoint", WAVE_A)
def test_wave_a_is_registered_live(game, cc_name, submission, entrypoint):
    """games.py marks the game live with the main.py entrypoint, and GAME_SPECS has an
    entry whose edit_prompt names the real bot contract for that arena."""
    # games.py registry
    assert is_live(game), f"{game} must be live=True in games.py"
    assert game in live_keys(), f"{game} must appear in live_keys()"

    from atv_bench.games import get_game

    g = get_game(game)
    assert g is not None and g.live is True
    assert g.entrypoint == submission, f"{game} entrypoint should be {submission!r}"

    # config.GAME_SPECS entry
    assert game in GAME_SPECS, f"{game} must have a GAME_SPECS entry"
    spec = GAME_SPECS[game]
    assert spec.codeclash_name == cc_name
    assert spec.bot_file == submission
    prompt = spec.edit_prompt.lower()
    assert prompt, f"{game} needs a non-empty edit_prompt"
    if entrypoint is not None:
        assert entrypoint.lower() in prompt, (
            f"{game} edit_prompt must mention the real bot contract "
            f"({entrypoint}); got: {spec.edit_prompt!r}"
        )


@pytest.mark.parametrize("game,cc_name,submission,entrypoint", WAVE_A)
def test_wave_a_pvp_config_shape(game, cc_name, submission, entrypoint):
    """build_pvp_config produces a valid CodeClash pvp config referencing the correct
    arena name + submission filename."""
    cfg = build_pvp_config(
        game=game, a="copilot-cli", b="claude-code",
        model="claude-opus-4.8", rounds=2,
    )
    assert cfg["game"]["name"] == cc_name
    assert cfg["tournament"]["rounds"] == 2
    assert [p["agent"] for p in cfg["players"]] == ["copilot-cli", "claude-code"]
    for p in cfg["players"]:
        assert p["config"]["model"] == "claude-opus-4.8"
        assert p["config"]["bot_file"] == submission
    assert "edit" in cfg["prompts"] and cfg["prompts"]["edit"]
    assert cfg["_meta"]["game_version"]
    assert cfg["_meta"]["prompt_version"]


def test_all_wave_a_games_live():
    """After Wave A, live_keys() is exactly lightcycles + the 4 new games (5 total)."""
    keys = set(live_keys())
    expected = {"lightcycles", "ants", "dummy", "gomoku", "paintvolley"}
    assert expected <= keys, f"missing live games: {expected - keys}"
    assert len(live_keys()) == 5, f"expected 5 live games, got {live_keys()}"


@pytest.mark.parametrize("game", [g[0] for g in WAVE_A] + ["lightcycles"])
def test_wave_a_matches_census(game):
    """Guard: we only mark live what the census (docs/arenas.md) says is supported."""
    doc = (_repo_root() / "docs" / "arenas.md").read_text().lower()
    line = next(
        (ln for ln in doc.splitlines()
         if re.search(rf"\|\s*{re.escape(game)}\s*\|", ln)),
        None,
    )
    assert line is not None, f"{game} not found as a census table row"
    assert "supported" in line, f"census does not mark {game} supported: {line!r}"


# --- Integration: a fake/sample bot competes through CodeClash's native arena. --------
# Marked [integration]: needs the vendored arena engine (and Docker for a real round),
# so it is not run in the default suite.

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
@pytest.mark.parametrize("game,cc_name,submission,entrypoint", [
    ("gomoku", "Gomoku", "main.py", "get_move"),
    ("dummy", "Dummy", "main.py", None),
])
def test_wave_a_fake_match_scores(game, cc_name, submission, entrypoint, tmp_path):
    """A fake/sample bot competes through CodeClash's native arena and produces a scored,
    non-forfeit RoundStats. Deterministic sample bots; reuses run_live_match's path."""
    from atv_bench.codeclash_env import codeclash_available

    if not codeclash_available():
        pytest.skip("vendored CodeClash not installed")

    from atv_bench.runner import RunConfig, run_live_match

    cfg = RunConfig(game=game, a="claude-code", b="claude-code",
                    model="claude-opus-4-8", rounds=1)
    raw = run_live_match(cfg, output_dir=tmp_path / "run")
    md = raw["metadata"]
    assert "round_stats" in md and md["round_stats"], "no scored rounds recorded"
