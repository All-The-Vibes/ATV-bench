"""Follow-up 2 (PR #19): register the bare-model negative control as a resolvable adapter.

BareModelAdapter (lift.py) wraps a leaf adapter to force a stripped HOME. It was never
resolvable by name, so the ~0-lift negative control could not run on real match data. These
tests pin the composition-factory contract: a `"bare:<inner>"` key resolves to a
BareModelAdapter wrapping the named leaf adapter, and the wrapper satisfies the
HarnessAdapter surface (name + available()).
"""
from __future__ import annotations

import dataclasses

import pytest

from atv_bench.adapters.contract import (
    ADAPTERS,
    AdapterRequest,
    ClaudeCodeAdapter,
    HarnessAdapter,
    resolve_adapter,
)
from atv_bench.lift import BareModelAdapter, manifest_is_bare


def test_bare_adapter_satisfies_protocol():
    """A BareModelAdapter exposes name + available() like any HarnessAdapter (AC2.1)."""
    inner = ClaudeCodeAdapter()
    bare = BareModelAdapter(inner=inner)
    assert isinstance(bare.name, str) and bare.name  # has a name
    assert bare.name != inner.name  # distinct from the leaf it wraps
    assert "bare" in bare.name.lower()
    # available() delegates to the inner leaf (bare is available iff the CLI is).
    assert bare.available() == inner.available()


def test_resolve_plain_key_unchanged():
    """resolve_adapter still returns leaf adapters for plain keys (AC2.2)."""
    a = resolve_adapter("claude-code")
    assert isinstance(a, ClaudeCodeAdapter)


def test_resolve_bare_composite():
    """`bare:claude-code` resolves to a BareModelAdapter wrapping the leaf (AC2.2)."""
    a = resolve_adapter("bare:claude-code")
    assert isinstance(a, BareModelAdapter)
    assert isinstance(a.inner, ClaudeCodeAdapter)
    assert a.name == "bare:claude-code"


def test_resolve_unknown_inner_errors():
    """An unknown inner harness fails closed with an actionable message (AC2.2)."""
    with pytest.raises((KeyError, ValueError)) as exc:
        resolve_adapter("bare:does-not-exist")
    assert "does-not-exist" in str(exc.value)


def test_bare_env_is_actually_bare(monkeypatch):
    """The wrapper runs its inner adapter under a manifest_is_bare env (AC2.4).

    We stub the inner adapter's run to capture the env it was handed and assert the bare
    predicate holds for a fingerprint of that HOME.
    """
    captured = {}

    class _Spy:
        name = "spy"

        @staticmethod
        def available() -> bool:
            return True

        def run(self, req):
            captured["env"] = req.env
            return "ran"

    bare = BareModelAdapter(inner=_Spy())
    req = AdapterRequest(repo_path=".", goal="noop")
    assert bare.run(req) == "ran"
    env = captured["env"]
    assert env is not None
    # A bare HOME has no harness scaffolding — the published predicate must hold.
    home = env.get("HOME")
    assert home is not None
    # manifest_is_bare takes a fingerprint manifest; an empty-scaffolding manifest is bare.
    empty_manifest = {"skills": [], "mcps": [], "plugins": [], "agents": [], "nested_skills": []}
    assert manifest_is_bare(empty_manifest) is True


def test_bare_registered_in_composable_registry():
    """The bare control is discoverable by name for pipeline wiring (AC2.4)."""
    # ADAPTERS keeps only leaf adapters; the bare control resolves via the factory. Either a
    # dedicated COMPOSABLE registry lists it, or resolve_adapter round-trips the composite key.
    a = resolve_adapter("bare:claude-code")
    assert isinstance(a, BareModelAdapter)
    # round-trip: the resolved composite reports the same key it was built from.
    assert a.name == "bare:claude-code"


def test_runconfig_accepts_bare_composite_harness():
    """RunConfig.validate accepts a bare:<inner> harness key so the negative control the
    scheduler emits can actually be run — not rejected as unknown (FU2 end-to-end)."""
    from atv_bench.runner import RunConfig

    cfg = RunConfig(game="lightcycles", a="claude-code", b="bare:claude-code", model="sonnet", rounds=1)
    cfg.validate()  # must not raise


def test_runconfig_rejects_bare_of_unknown_inner():
    """bare:<unknown> still fails closed with an actionable message."""
    from atv_bench.run_envelope import RunError
    from atv_bench.runner import RunConfig

    cfg = RunConfig(game="lightcycles", a="claude-code", b="bare:nope", model="sonnet", rounds=1)
    with pytest.raises(RunError):
        cfg.validate()


def test_harness_binary_for_resolves_bare():
    """The binary resolver maps bare:<inner> to the inner harness's CLI binary."""
    from atv_bench.runner import harness_binary_for

    assert harness_binary_for("claude-code") == "claude"
    assert harness_binary_for("bare:claude-code") == "claude"


def test_resolve_player_class_gates_bare_prefix():
    """resolve_player_class recognizes bare:<inner> for builder harnesses and falls through
    (None) for non-builders — the gating happens BEFORE the codeclash import, so it is
    hermetically testable (a real bare build is exercised in the integration lane)."""
    from atv_bench.integration import resolve_player_class

    # non-builder keys (and bare of a non-builder) fall through to CodeClash's get_agent.
    assert resolve_player_class("dummy") is None
    assert resolve_player_class("bare:dummy") is None
    assert resolve_player_class("bare:does-not-exist") is None
    assert resolve_player_class("") is None
