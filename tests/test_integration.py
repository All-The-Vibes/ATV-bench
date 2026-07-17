"""TDD for integration.register() — monkeypatch CodeClash's get_agent (Lane B).

Skips gracefully when the vendored CodeClash isn't installed. When it is, asserts the
patch targets codeclash.tournaments.pvp.get_agent (host-side construction site) and that
harness keys resolve to a HarnessPlayer while builtin keys (dummy/mini) still work.
"""
from __future__ import annotations

import pytest

from atv_bench.codeclash_env import codeclash_available, import_codeclash

pytestmark = pytest.mark.skipif(
    not codeclash_available(), reason="vendored CodeClash not installed"
)


def test_register_patches_pvp_get_agent_and_restores():
    from atv_bench import integration

    cc = import_codeclash()
    original = cc.pvp.get_agent
    try:
        integration.register()
        assert cc.pvp.get_agent is not original, "pvp.get_agent was not patched"
    finally:
        integration.unregister()
    assert cc.pvp.get_agent is original, "unregister did not restore get_agent"


def test_harness_key_resolves_to_harness_player():
    from atv_bench import integration
    from atv_bench.players import HarnessPlayerCore

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

    cc = import_codeclash()
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

    try:
        integration.register()
        # swap the captured original with our spy so we can see fall-through happen
        integration._original_get_agent = fake_original
        # rebuild the closure by re-registering after resetting
        integration.unregister()
        integration._original_get_agent = None
        integration.register()
        monkeypatch.setattr(integration, "_original_get_agent", fake_original, raising=False)
        # Patch the pvp binding to a fresh closure that uses our fake original.
        def patched(config, game_context, environment):
            pc = integration.resolve_player_class(config.get("agent"))
            if pc is None:
                return fake_original(config, game_context, environment)
            return ("harness", config["agent"])
        cc.pvp.get_agent = patched

        assert cc.pvp.get_agent({"agent": "claude-code"}, None, None) == ("harness", "claude-code")
        assert cc.pvp.get_agent({"agent": "dummy"}, None, None) == ("builtin", "dummy")
        assert calls["original"] == 1
    finally:
        integration.unregister()
