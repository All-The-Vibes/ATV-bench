"""TDD for config.py — build a CodeClash pvp config for a harness-vs-harness match."""
from __future__ import annotations

import pytest

from atv_bench.config import GAME_SPECS, build_pvp_config, resolve_game


def test_resolve_game_lightcycles():
    spec = resolve_game("lightcycles")
    assert spec.codeclash_name == "LightCycles"
    assert spec.bot_file == "main.py"


def test_resolve_game_battlesnake():
    spec = resolve_game("battlesnake")
    assert spec.codeclash_name == "BattleSnake"


def test_resolve_unknown_game_raises_with_valid_set():
    with pytest.raises(ValueError) as exc:
        resolve_game("pong")
    # did-you-mean / full valid set (DX-5)
    assert "lightcycles" in str(exc.value)


def test_build_pvp_config_shape():
    cfg = build_pvp_config(
        game="lightcycles", a="copilot-cli", b="claude-code",
        model="claude-opus-4.8", rounds=3,
    )
    assert cfg["game"]["name"] == "LightCycles"
    assert cfg["tournament"]["rounds"] == 3
    # players routed by agent key -> our monkeypatched get_agent resolves HarnessPlayer
    agents = [p["agent"] for p in cfg["players"]]
    assert agents == ["copilot-cli", "claude-code"]
    # model + bot_file threaded through so the harness edit turn knows both
    for p in cfg["players"]:
        assert p["config"]["model"] == "claude-opus-4.8"
        assert p["config"]["bot_file"] == "main.py"
    assert "edit" in cfg["prompts"]


def test_same_harness_both_sides_gets_distinct_names():
    # A/A self-play: names must differ so containers/branches don't collide.
    cfg = build_pvp_config(
        game="lightcycles", a="copilot-cli", b="copilot-cli",
        model="claude-opus-4.8", rounds=1,
    )
    names = [p["name"] for p in cfg["players"]]
    assert names[0] != names[1]


def test_model_parity_same_model_both_sides():
    # All harness arena competitions run on the SAME model for parity (locked req).
    cfg = build_pvp_config(
        game="lightcycles", a="copilot-cli", b="claude-code",
        model="claude-opus-4.8", rounds=1,
    )
    models = {p["config"]["model"] for p in cfg["players"]}
    assert models == {"claude-opus-4.8"}


def test_prompt_version_and_game_version_present():
    cfg = build_pvp_config(
        game="lightcycles", a="copilot-cli", b="claude-code",
        model="m", rounds=1,
    )
    assert cfg["_meta"]["game_version"]
    assert cfg["_meta"]["prompt_version"]
