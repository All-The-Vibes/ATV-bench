"""The live-arena execute timeout must be generous enough that a real match's engine
adjudication is not killed mid-run on a slow host.

Root cause this guards: mini-swe-agent's DockerEnvironment.config.timeout defaults to 30s,
and CodeClash's ClashDockerEnvironment.execute passes timeout=None straight through — so the
lightcycles `engine.py -r 10` adjudication (which can take >30s under Docker on a loaded or
modest host) reproducibly failed with `RuntimeError: Command failed with exit code -1: ...
timed out after 30 seconds`. That is a false forfeit of an HONEST match — a trust bug, not a
real outcome. We raise the per-command default and let it be tuned via ATV_ARENA_EXEC_TIMEOUT.
"""
from __future__ import annotations

import pytest

from atv_bench.codeclash_env import codeclash_available, import_codeclash
from atv_bench.integration import arena_execute_timeout, register, unregister

pytestmark = pytest.mark.skipif(
    not codeclash_available(), reason="vendored CodeClash not installed"
)


def test_default_arena_timeout_is_well_above_30s(monkeypatch):
    monkeypatch.delenv("ATV_ARENA_EXEC_TIMEOUT", raising=False)
    # The 30s mini-swe-agent default is the bug; our default must clear a real adjudication.
    assert arena_execute_timeout() >= 600


def test_arena_timeout_is_env_tunable(monkeypatch):
    monkeypatch.setenv("ATV_ARENA_EXEC_TIMEOUT", "1234")
    assert arena_execute_timeout() == 1234


def test_arena_timeout_ignores_garbage_env(monkeypatch):
    monkeypatch.setenv("ATV_ARENA_EXEC_TIMEOUT", "not-an-int")
    assert arena_execute_timeout() >= 600  # falls back to the safe default, never crashes


def test_register_raises_clash_execute_timeout(monkeypatch):
    """After register(), a ClashDockerEnvironment.execute called with timeout=None substitutes
    the generous arena default instead of mini-swe-agent's 30s."""
    monkeypatch.delenv("ATV_ARENA_EXEC_TIMEOUT", raising=False)
    from codeclash.utils.environment import ClashDockerEnvironment

    seen = {}

    # Stand in for mini-swe-agent's DockerEnvironment.execute to capture the effective timeout
    # without touching Docker.
    def fake_super_execute(self, action, cwd="", *, timeout=None):
        seen["timeout"] = timeout
        return {"output": "", "returncode": 0}

    monkeypatch.setattr(
        "minisweagent.environments.docker.DockerEnvironment.execute",
        fake_super_execute, raising=True,
    )
    try:
        register()
        env = ClashDockerEnvironment.__new__(ClashDockerEnvironment)
        # Call with no explicit timeout — the patch must inject the arena default.
        ClashDockerEnvironment.execute(env, "echo hi")
        assert seen["timeout"] == arena_execute_timeout()
        assert seen["timeout"] >= 600
    finally:
        unregister()


def test_explicit_timeout_is_respected(monkeypatch):
    """A caller that DOES pass a timeout keeps it — the patch only fills the None default."""
    monkeypatch.delenv("ATV_ARENA_EXEC_TIMEOUT", raising=False)
    from codeclash.utils.environment import ClashDockerEnvironment

    seen = {}

    def fake_super_execute(self, action, cwd="", *, timeout=None):
        seen["timeout"] = timeout
        return {"output": "", "returncode": 0}

    monkeypatch.setattr(
        "minisweagent.environments.docker.DockerEnvironment.execute",
        fake_super_execute, raising=True,
    )
    try:
        register()
        env = ClashDockerEnvironment.__new__(ClashDockerEnvironment)
        ClashDockerEnvironment.execute(env, "echo hi", timeout=5)
        assert seen["timeout"] == 5
    finally:
        unregister()
