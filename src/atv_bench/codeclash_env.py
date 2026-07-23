"""Import shim + version pin for the vendored CodeClash dependency.

CodeClash is a research repo, not published to PyPI, so it is vendored under
`vendor/CodeClash` at a pinned commit and installed editable. This module is the
ONE place ATV-bench imports CodeClash internals, so a drift in its (internal,
unstable) API surface fails loudly here — see `tests/test_codeclash_drift.py` —
instead of deep inside the runner.

Pinned commit (see vendor/CodeClash `git rev-parse HEAD`):
    f0694c64ecf6abfca2bc867bad2de9333fef5be8

Seam facts verified against this pin (the drift test asserts they still hold):
  * `codeclash.agents.get_agent(config, game_context, environment) -> Player`
    maps a function-local literal {"dummy", "mini"} — NOT a module dict, so it
    cannot be extended; `integration.register()` must REPLACE it.
  * `codeclash.tournaments.pvp` does `from codeclash.agents import get_agent`,
    binding the name into its own module namespace. Agents are constructed
    HOST-SIDE in `PvpTournament.__init__` (verified: `agent.run()` executes in
    the tournament process via ThreadPoolExecutor, talking to Docker through
    `environment.execute`). So the authoritative monkeypatch site is
    `codeclash.tournaments.pvp.get_agent`.
  * `Player.__init__(self, config, environment, game_context)`.
"""
from __future__ import annotations

# Pinned commit of vendor/CodeClash this build is verified against.
CODECLASH_PIN = "f0694c64ecf6abfca2bc867bad2de9333fef5be8"

# Short version tag stamped into match records / leaderboard rows (schema v2).
CODECLASH_VERSION = f"vendored@{CODECLASH_PIN[:12]}"


def import_codeclash():
    """Import the CodeClash internals ATV-bench depends on, or raise a clear error.

    Returns a namespace object with the seam handles. Raises `CodeClashUnavailable`
    (an ImportError subclass) with an actionable message if CodeClash is not
    installed — the runner turns this into exit code 9 (codeclash-dep).
    """
    try:
        from codeclash import agents as cc_agents
        from codeclash.agents.player import Player
        from codeclash.agents.utils import GameContext
        from codeclash.tournaments import pvp as cc_pvp
    except Exception as exc:  # pragma: no cover - exercised via CodeClashUnavailable
        raise CodeClashUnavailable(
            "CodeClash is not importable. It ships in the `run` extra as a git "
            f"dependency at pin {CODECLASH_PIN[:12]}; install that extra to pull it: "
            "`uv tool install --reinstall --from 'atv-bench[run] @ git+https://github.com/All-The-Vibes/ATV-bench' atv-bench` "
            "(a plain reinstall of `atv-bench` will NOT pull it); from a source "
            "checkout run `uv pip install -e '.[run]'`, "
            "or run `atv-bench doctor` for a full prerequisite report."
        ) from exc

    return _Seam(
        agents=cc_agents,
        pvp=cc_pvp,
        Player=Player,
        GameContext=GameContext,
        get_agent=cc_agents.get_agent,
    )


class CodeClashUnavailable(ImportError):
    """Raised when the vendored CodeClash dependency cannot be imported."""


class _Seam:
    __slots__ = ("agents", "pvp", "Player", "GameContext", "get_agent")

    def __init__(self, *, agents, pvp, Player, GameContext, get_agent):
        self.agents = agents
        self.pvp = pvp
        self.Player = Player
        self.GameContext = GameContext
        self.get_agent = get_agent


def codeclash_available() -> bool:
    try:
        import_codeclash()
        return True
    except CodeClashUnavailable:
        return False
