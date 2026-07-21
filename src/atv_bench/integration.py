"""Wire ATV-bench harnesses into CodeClash's tournament (Lane B).

`register()` monkeypatch-REPLACES `codeclash.tournaments.pvp.get_agent` (the host-side
construction site verified by the gating spike — agents are built in
PvpTournament.__init__, run() executes host-side). A config whose `agent` key is an
ATV-bench harness (claude-code / copilot-cli) resolves to a HarnessPlayer bound to that
harness's adapter; any other key falls through to CodeClash's original get_agent, so
`dummy` / `mini` are never clobbered.

The HarnessPlayer is constructed lazily against the real CodeClash Player base so this
module imports without Docker; the Docker tree-container shim adapts DockerEnvironment
to the TreeContainerLike protocol players.py expects.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from atv_bench.adapters.contract import ADAPTERS
from atv_bench.codeclash_env import import_codeclash
from atv_bench.isolation import isolated_home
from atv_bench.players import HarnessPlayerCore

# Harness keys ATV-bench can BUILD a bot with (fingerprint-only harnesses excluded).
BUILDER_HARNESSES = tuple(ADAPTERS.keys())

_original_get_agent = None  # set on first register(), restored on unregister()
_player_class_cache: dict[str, type] = {}
# Per-harness config root (seed for the isolated HOME), threaded from run_live_match.
_harness_homes: dict[str, Path | None] = {}


def set_harness_homes(homes: dict[str, Path | None] | None) -> None:
    """Record the per-harness config roots used to seed each isolated HOME.

    Called by run_live_match before the tournament runs. Keys are harness/adapter
    keys (claude-code / copilot-cli); values are the cloned config root (or None to
    auto-detect / fall back to no seed).
    """
    _harness_homes.clear()
    if homes:
        _harness_homes.update(homes)


def _ensure_container_origin(environment) -> None:
    """Ensure the arena container's bot repo has an `origin` remote before the CodeClash
    Player runs `git fetch origin` in its __init__.

    Most arenas `git clone` their starter kit (so `origin` already exists), but a few
    (`cyborg`, `bomberland`) `git init` a fresh repo with NO remote. CodeClash's
    Player.__init__ unconditionally runs `git fetch origin` for the non-push path, which
    exits 128 ("'origin' does not appear to be a git repository") and kills the match
    before the bot is ever built. We don't push, so a self-referential `origin` (the repo
    itself) makes that fetch a harmless no-op. This is reuse-enabling container plumbing —
    it does not touch any arena's referee/adjudication.

    Idempotent and best-effort: if `origin` already exists we leave it; any failure here
    is swallowed so we never break the arenas that were already fine.
    """
    try:
        has_origin = environment.execute("git remote get-url origin")
        if has_origin.get("returncode", 1) == 0:
            return  # origin already configured (cloned arenas) — don't touch it
        # Point origin at the working tree itself; `git fetch origin` becomes a no-op.
        environment.execute(
            "cd $(git rev-parse --show-toplevel 2>/dev/null || echo .) && "
            "git remote add origin \"$(pwd)\" 2>/dev/null || true"
        )
    except Exception:
        pass  # best-effort; the fetch will surface a clear error if it still fails


def run_isolated_edit_turn(
    *,
    adapter,
    container,
    home: Path | None,
    goal: str,
    model: str,
    player_id: str | None,
    game: str,
    prompt_version: str,
    bot_file: str,
):
    """Production seam: run ONE build under a fresh per-run isolated HOME.

    Enters ``isolated_home(home)`` for the WHOLE build so the adapter subprocess runs
    under the per-run temp HOME/XDG dirs (never the shared host $HOME), then threads
    the yielded env dict into HarnessPlayerCore(env=...). The context manager keeps the
    isolated dir alive for the entire edit_turn and cleans it up afterwards.
    """
    with isolated_home(home) as env:
        core = HarnessPlayerCore(
            adapter=adapter,
            container=container,
            bot_file=bot_file,
            goal=goal,
            model=model,
            player_id=player_id,
            game=game,
            prompt_version=prompt_version,
            env=env,
        )
        return core.edit_turn()


def resolve_player_class(agent_key: str):
    """Return the HarnessPlayer class for a harness key, or None for a builtin key.

    None signals "fall through to CodeClash's own get_agent" (dummy/mini).
    """
    if agent_key not in BUILDER_HARNESSES:
        return None
    if agent_key not in _player_class_cache:
        _player_class_cache[agent_key] = _make_harness_player(agent_key)
    return _player_class_cache[agent_key]


def register() -> None:
    """Patch codeclash.tournaments.pvp.get_agent to resolve ATV-bench harnesses.

    Idempotent: a second call does not double-wrap.
    """
    global _original_get_agent
    cc = import_codeclash()
    if _original_get_agent is not None:
        return  # already registered
    _original_get_agent = cc.pvp.get_agent
    original = _original_get_agent

    def patched_get_agent(config, game_context, environment):
        player_cls = resolve_player_class(config.get("agent"))
        if player_cls is None:
            return original(config, game_context, environment)
        _ensure_container_origin(environment)
        return player_cls(config, environment, game_context)

    cc.pvp.get_agent = patched_get_agent


def unregister() -> None:
    """Restore CodeClash's original get_agent."""
    global _original_get_agent
    if _original_get_agent is None:
        return
    cc = import_codeclash()
    cc.pvp.get_agent = _original_get_agent
    _original_get_agent = None
    _harness_homes.clear()


def _make_harness_player(adapter_key: str):
    """Build a CodeClash Player subclass bound to a harness adapter."""
    cc = import_codeclash()
    adapter_cls = ADAPTERS[adapter_key]

    class HarnessPlayer(cc.Player):
        def run(self) -> None:
            cfg = self.config.get("config", {}) if isinstance(self.config, dict) else {}
            # Seed the isolated HOME from this harness's config root (threaded via
            # set_harness_homes before the tournament runs). Isolation is applied in
            # the production seam so the adapter subprocess never inherits host $HOME.
            home = _harness_homes.get(adapter_key)
            run_isolated_edit_turn(
                adapter=adapter_cls(),
                container=_DockerTreeContainer(self.environment, self._workdir()),
                home=home,
                goal=self.game_context.prompts.get("edit", "Improve the bot."),
                model=cfg.get("model", "auto"),
                player_id=self.name,
                game=self.game_context.name,
                prompt_version=self.game_context.prompts.get("_version", "edit@1"),
                bot_file=cfg.get("bot_file", "main.py"),
            )

        def _workdir(self) -> str:
            wd = getattr(self.game_context, "working_dir", None)
            return wd or "/workdir"

    HarnessPlayer.__name__ = f"HarnessPlayer_{adapter_key}"
    return HarnessPlayer


class _DockerTreeContainer:  # pragma: no cover - requires Docker
    """Adapts CodeClash's DockerEnvironment to players.TreeContainerLike (tree-level)."""

    def __init__(self, env, workdir: str):
        self.env = env
        self.workdir = workdir

    def read_tree(self) -> dict[str, str]:
        # docker cp the workdir to a host temp dir (binary-safe, unlike tar-via-execute).
        from codeclash.utils.environment import copy_from_container

        out: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "work"
            copy_from_container(self.env, self.workdir, dest)
            # `docker cp <dir>` yields dest/<basename>/... — find the real root.
            base = dest if dest.is_dir() else Path(tmp)
            for p in sorted(base.rglob("*")):
                if not p.is_file():
                    continue
                if ".git" in p.relative_to(base).parts:
                    continue
                try:
                    out[p.relative_to(base).as_posix()] = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue  # skip binaries; the bot itself is text
        return out

    def write_tree(self, files: dict[str, str]) -> None:
        from codeclash.utils.environment import create_file_in_container

        for rel, content in files.items():
            dest = f"{self.workdir}/{rel}"
            create_file_in_container(container=self.env, content=content, dest_path=dest)
