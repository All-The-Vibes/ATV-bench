"""API and source-asset drift tests for the immutable CodeClash pin.

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
import subprocess
from pathlib import Path

import pytest

from atv_bench.codeclash_env import (
    CODECLASH_PIN,
    codeclash_available,
    import_codeclash,
    resolve_codeclash_source,
)

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


def test_arena_classes_are_bound_to_exact_pinned_source_assets():
    import codeclash
    from codeclash.arenas.battlesnake.battlesnake import BattleSnakeArena
    from codeclash.arenas.lightcycles.lightcycles import LightCyclesArena

    import_codeclash()
    source_root = resolve_codeclash_source()
    assert codeclash.REPO_DIR == source_root
    assert source_root.name

    for arena_class in (LightCyclesArena, BattleSnakeArena):
        source_file = Path(inspect.getfile(arena_class)).resolve()
        assert source_root in source_file.parents
        dockerfile = source_file.parent / f"{arena_class.name}.Dockerfile"
        assert dockerfile.is_file()
        assert not dockerfile.is_symlink()

    head = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    assert head == CODECLASH_PIN


def test_codeclash_container_paths_remain_posix_on_windows_hosts():
    cc = import_codeclash()
    from codeclash.arenas import arena as arena_module

    assert str(cc.pvp.DIR_WORK) == "/workspace"
    assert str(arena_module.DIR_WORK) == "/workspace"
    assert str(cc.pvp.DIR_LOGS) == "/logs"
    assert str(arena_module.DIR_LOGS) == "/logs"
