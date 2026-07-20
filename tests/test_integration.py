"""TDD for integration.register() — monkeypatch CodeClash's get_agent (Lane B).

Skips gracefully when the vendored CodeClash isn't installed. When it is, asserts the
patch targets codeclash.tournaments.pvp.get_agent (host-side construction site) and that
harness keys resolve to a HarnessPlayer while builtin keys (dummy/mini) still work.
"""
from __future__ import annotations

import logging

import pytest

from atv_bench.codeclash_env import codeclash_available, import_codeclash

pytestmark = pytest.mark.skipif(
    not codeclash_available(), reason="vendored CodeClash not installed"
)


def test_register_patches_pvp_get_agent_and_restores():
    from atv_bench import integration
    from codeclash.arenas import arena as arena_module
    from codeclash.arenas.lightcycles.lightcycles import LightCyclesArena
    from codeclash.tournaments.pvp import PvpTournament
    from codeclash.utils import atomic_write as atomic_write_module
    from codeclash.utils import environment as environment_module

    cc = import_codeclash()
    original = cc.pvp.get_agent
    original_arena_copy = arena_module.copy_between_containers
    original_pvp_copy = cc.pvp.copy_between_containers
    original_environment_copy = environment_module.copy_between_containers
    original_pvp_copy_to = cc.pvp.copy_to_container
    original_environment_copy_to = environment_module.copy_to_container
    original_pvp_atomic = cc.pvp.atomic_write
    original_module_atomic = atomic_write_module.atomic_write
    original_build_image = LightCyclesArena.build_image
    original_end = PvpTournament.end
    original_cleanup = arena_module.ClashDockerEnvironment.cleanup
    original_raise_exceptions = logging.raiseExceptions
    try:
        integration.register()
        assert cc.pvp.get_agent is not original, "pvp.get_agent was not patched"
        assert arena_module.copy_between_containers is integration._bounded_copy_between_containers
        assert cc.pvp.copy_between_containers is integration._bounded_copy_between_containers
        assert (
            environment_module.copy_between_containers
            is integration._bounded_copy_between_containers
        )
        assert cc.pvp.copy_to_container is integration._bounded_copy_to_container
        assert (
            environment_module.copy_to_container
            is integration._bounded_copy_to_container
        )
        assert cc.pvp.atomic_write is integration._atomic_write_text
        assert atomic_write_module.atomic_write is integration._atomic_write_text
        assert LightCyclesArena.build_image is integration._build_pinned_lightcycles_image
        assert PvpTournament.end is not original_end
        assert (
            arena_module.ClashDockerEnvironment.cleanup
            is integration._cleanup_codeclash_environment_best_effort
        )
        assert logging.raiseExceptions is False
    finally:
        integration.unregister()
    assert cc.pvp.get_agent is original, "unregister did not restore get_agent"
    assert arena_module.copy_between_containers is original_arena_copy
    assert cc.pvp.copy_between_containers is original_pvp_copy
    assert environment_module.copy_between_containers is original_environment_copy
    assert cc.pvp.copy_to_container is original_pvp_copy_to
    assert environment_module.copy_to_container is original_environment_copy_to
    assert cc.pvp.atomic_write is original_pvp_atomic
    assert atomic_write_module.atomic_write is original_module_atomic
    assert LightCyclesArena.build_image is original_build_image
    assert PvpTournament.end is original_end
    assert arena_module.ClashDockerEnvironment.cleanup is original_cleanup
    assert logging.raiseExceptions is original_raise_exceptions


def test_harness_key_resolves_to_harness_player():
    from atv_bench import integration

    cc = import_codeclash()
    try:
        integration.register()
        # Build a config selecting our harness agent; environment/game_context are the
        # real CodeClash types but we don't run Docker — we only check the resolved class.
        player_cls = integration.resolve_player_class("claude-code")
        assert player_cls is not None
        assert issubclass(player_cls, cc.Player)
    finally:
        integration.unregister()


def test_unknown_key_falls_through_to_builtins():
    from atv_bench import integration

    import_codeclash()
    try:
        integration.register()
        # 'dummy' / 'mini' must still route to CodeClash's own agents (not clobbered).
        # resolve_player_class returns None for a non-harness key, signalling fall-through.
        assert integration.resolve_player_class("dummy") is None
        assert integration.resolve_player_class("mini") is None
    finally:
        integration.unregister()


def test_register_is_idempotent():
    from atv_bench import integration

    cc = import_codeclash()
    original = cc.pvp.get_agent
    try:
        integration.register()
        patched = cc.pvp.get_agent
        integration.register()  # second call must not double-wrap
        assert cc.pvp.get_agent is patched
    finally:
        integration.unregister()
    assert cc.pvp.get_agent is original


def test_windows_safe_atomic_write_replaces_existing_file(tmp_path):
    from atv_bench.integration import _atomic_write_text

    destination = tmp_path / "metadata.json"
    destination.write_text("old", encoding="utf-8")
    _atomic_write_text(destination, "new\n")

    assert destination.read_bytes() == b"new\n"
    assert not list(tmp_path.glob(".metadata.json.*.tmp"))


def test_patched_get_agent_routes_harness_and_builtin_end_to_end(monkeypatch):
    """gap #10: prove the patched call site routes BOTH a harness key (→HarnessPlayer)
    and a builtin key (→original get_agent) without any Docker, exercising the exact
    function CodeClash's PvpTournament.__init__ calls."""
    from atv_bench import integration

    cc = import_codeclash()
    calls = {"original": 0}

    def fake_original(config, game_context, environment):
        calls["original"] += 1
        return ("builtin", config["agent"])

    class FakeHarness:
        def __new__(cls, config, environment, game_context):
            return ("harness", config["agent"])

    monkeypatch.setattr(cc.pvp, "get_agent", fake_original)
    monkeypatch.setattr(
        integration,
        "resolve_player_class",
        lambda key: FakeHarness if key == "claude-code" else None,
    )
    try:
        integration.register()
        assert cc.pvp.get_agent(
            {"agent": "claude-code"},
            None,
            None,
        ) == ("harness", "claude-code")
        assert cc.pvp.get_agent({"agent": "dummy"}, None, None) == ("builtin", "dummy")
        assert calls["original"] == 1
    finally:
        integration.unregister()
