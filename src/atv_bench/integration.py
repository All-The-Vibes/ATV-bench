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

import tarfile
import tempfile
from pathlib import Path

from atv_bench.adapters.contract import ADAPTERS
from atv_bench.codeclash_env import import_codeclash
from atv_bench.players import HarnessPlayerCore

# Harness keys ATV-bench can BUILD a bot with (fingerprint-only harnesses excluded).
BUILDER_HARNESSES = tuple(ADAPTERS.keys())

_original_get_agent = None  # set on first register(), restored on unregister()
_player_class_cache: dict[str, type] = {}


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


def _make_harness_player(adapter_key: str):
    """Build a CodeClash Player subclass bound to a harness adapter."""
    cc = import_codeclash()
    adapter_cls = ADAPTERS[adapter_key]

    class HarnessPlayer(cc.Player):
        def run(self) -> None:
            cfg = self.config.get("config", {}) if isinstance(self.config, dict) else {}
            core = HarnessPlayerCore(
                adapter=adapter_cls(),
                container=_DockerTreeContainer(self.environment, self._workdir()),
                bot_file=cfg.get("bot_file", "main.py"),
                goal=self.game_context.prompts.get("edit", "Improve the bot."),
                model=cfg.get("model", "auto"),
                player_id=self.name,
                game=self.game_context.name,
                prompt_version=self.game_context.prompts.get("_version", "edit@1"),
            )
            core.edit_turn()

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
        # tar the workdir out of the container, read text files.
        res = self.env.execute(f"tar -C {self.workdir} -cf - .")
        raw = res["output"] if isinstance(res, dict) else res
        out: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = Path(tmp) / "t.tar"
            tar_path.write_bytes(raw.encode("latin-1") if isinstance(raw, str) else raw)
            with tarfile.open(tar_path) as tf:
                tf.extractall(tmp)
            base = Path(tmp)
            for p in base.rglob("*"):
                if p.is_file() and p.name != "t.tar" and ".git" not in p.parts:
                    try:
                        out[p.relative_to(base).as_posix()] = p.read_text()
                    except UnicodeDecodeError:
                        continue
        return out

    def write_tree(self, files: dict[str, str]) -> None:
        from codeclash.utils.environment import create_file_in_container

        for rel, content in files.items():
            dest = f"{self.workdir}/{rel}"
            create_file_in_container(container=self.env, content=content, dest_path=dest)
