"""Harness-driven CodeClash Player core (promoted from spikes/, Lane B).

The model-agnostic core of a harness-driven Player: pull the current bot TREE from the
arena container, run the real harness CLI headless to edit it, snapshot-capture what it
built, allowlist-scan the captured tree, and write the whole tree back into the
container. Build-once: the harness runs EXACTLY ONE model-driven build per player; the
frozen artifact is cached OUTSIDE instance scope and replayed for every subsequent round
(ENG-4/gap #9) so CodeClash calling run() per round cannot re-invoke the CLI.

Authoritative artifact = the materialized post-run tree (ENG-3). The git diff is
provenance/display only.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    HarnessAdapter,
)
from atv_bench.adapters.snapshot import capture_diff, seed_base
from atv_bench.capture import scan_captured_tree


@runtime_checkable
class TreeContainerLike(Protocol):
    """Minimal slice of CodeClash's DockerEnvironment we depend on (tree-level)."""

    def read_tree(self) -> dict[str, str]: ...
    def write_tree(self, files: dict[str, str]) -> None: ...


# Build-once artifact cache, keyed OUTSIDE any instance so per-round Player
# reconstruction cannot rebuild (ENG-4). Maps (player_id, game, prompt_version) ->
# (tree, result, diff).
_ARTIFACT_CACHE: dict[tuple[str, str, str], tuple[dict[str, str], AdapterResult, str]] = {}


def clear_artifact_cache() -> None:
    """Test/CLI hook to reset the process-wide build-once cache."""
    _ARTIFACT_CACHE.clear()


class HarnessPlayerCore:
    def __init__(
        self,
        adapter: HarnessAdapter,
        container: TreeContainerLike,
        *,
        bot_file: str = "main.py",
        goal: str,
        model: str = "auto",
        budget: Budget | None = None,
        player_id: str | None = None,
        game: str = "lightcycles",
        prompt_version: str = "edit@1",
    ) -> None:
        self.adapter = adapter
        self.container = container
        self.bot_file = bot_file
        self.goal = goal
        self.model = model
        self.budget = budget or Budget()
        self.player_id = player_id
        self.game = game
        self.prompt_version = prompt_version
        self.last_result: AdapterResult | None = None
        self.last_diff: str = ""

    @property
    def _cache_key(self) -> tuple[str, str, str] | None:
        if self.player_id is None:
            return None
        return (self.player_id, self.game, self.prompt_version)

    def edit_turn(self) -> AdapterResult:
        """One edit phase: build once (or replay the cached artifact), write tree back."""
        key = self._cache_key
        if key is not None and key in _ARTIFACT_CACHE:
            tree, result, diff = _ARTIFACT_CACHE[key]
            self.container.write_tree(tree)
            self.last_result, self.last_diff = result, diff
            return result

        original_tree = self.container.read_tree()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._materialize(repo, original_tree)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=atv",
                 "commit", "-qm", "init"], cwd=repo, check=True)
            base = seed_base(repo)

            req = AdapterRequest(
                repo_path=str(repo), goal=self.goal, model=self.model,
                budget=self.budget, bot_file=self.bot_file,
            )
            result = self.adapter.run(req)
            diff = capture_diff(repo, base)  # provenance/display only

            captured_tree = original_tree
            if result.status == AdapterStatus.OK:
                # Materialized post-run tree is authoritative (ENG-3). Allowlist-scan it
                # BEFORE it touches the container (ENG-7) — raises CaptureRejected.
                scan_captured_tree(repo)
                captured_tree = self._read_repo_tree(repo)
                self.container.write_tree(captured_tree)
            # NO_EDIT / ERROR / TIMEOUT: leave the container's original tree (forfeit),
            # never crash.

        self.last_result, self.last_diff = result, diff
        if key is not None:
            _ARTIFACT_CACHE[key] = (captured_tree, result, diff)
        return result

    def _materialize(self, repo: Path, tree: dict[str, str]) -> None:
        if not tree:
            # Guarantee at least the bot file exists so the harness has a target.
            (repo / self.bot_file).parent.mkdir(parents=True, exist_ok=True)
            (repo / self.bot_file).write_text("")
            return
        for rel, content in tree.items():
            dest = repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

    def _read_repo_tree(self, repo: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(repo.rglob("*")):
            if ".git" in p.parts:
                continue
            if p.is_file():
                out[p.relative_to(repo).as_posix()] = p.read_text()
        return out
