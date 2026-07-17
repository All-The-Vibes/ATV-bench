"""API-drift smoke test for the vendored CodeClash pin (Eng Decision #1).

CodeClash's `get_agent` and `Player` are INTERNAL, unstable APIs we monkeypatch.
If upstream changes their shape under us, `integration.register()` breaks silently
(KeyError in-container, or a no-op patch). These tests fail loudly at the seam so a
`vendor/CodeClash` bump can't quietly regress the harness-vs-harness spine.

All tests are skipped (not failed) when CodeClash isn't installed, so the base
suite stays green on machines without the vendored dep. The runner's own
preflight covers the "not installed" user path.
"""
from __future__ import annotations

import inspect

import pytest

from atv_bench.codeclash_env import codeclash_available, import_codeclash

pytestmark = pytest.mark.skipif(
    not codeclash_available(), reason="vendored CodeClash not installed"
)


def test_get_agent_signature_is_stable():
    cc = import_codeclash()
    params = list(inspect.signature(cc.get_agent).parameters)
    assert params == ["config", "game_context", "environment"], (
        "CodeClash get_agent signature drifted; update codeclash_env + integration.register"
    )


def test_get_agent_maps_a_function_local_literal_not_a_module_dict():
    # The whole reason register() must REPLACE get_agent (not extend a dict): the
    # mapping is built inside the function body. Assert there is no module-level
    # registry we could have mutated instead.
    cc = import_codeclash()
    assert not hasattr(cc.agents, "AGENTS"), "unexpected module-level agent registry appeared"
    src = inspect.getsource(cc.get_agent)
    assert '"dummy"' in src and '"mini"' in src, "get_agent's builtin agent literal drifted"


def test_pvp_binds_get_agent_into_its_own_namespace():
    # pvp.py does `from codeclash.agents import get_agent`, so the authoritative
    # monkeypatch site is codeclash.tournaments.pvp.get_agent. Prove the binding
    # exists and is the same object (host-side construction).
    cc = import_codeclash()
    assert hasattr(cc.pvp, "get_agent"), "pvp no longer imports get_agent by name"
    assert cc.pvp.get_agent is cc.get_agent


def test_player_init_signature_is_stable():
    cc = import_codeclash()
    params = list(inspect.signature(cc.Player.__init__).parameters)
    assert params == ["self", "config", "environment", "game_context"], (
        "CodeClash Player.__init__ signature drifted; update players.py wrapper"
    )


def test_lightcycles_and_battlesnake_arenas_import():
    # Game #1 (lightcycles) + game #2 (battlesnake) must both be reachable via the
    # pinned dep so `run` supports both with zero new engine.
    from codeclash.arenas.battlesnake.battlesnake import BattleSnakeArena
    from codeclash.arenas.lightcycles.lightcycles import LightCyclesArena

    assert LightCyclesArena is not None
    assert BattleSnakeArena is not None
