"""Spike 2: CodeClash decoupling.

Proves ATV-bench harnesses plug into CodeClash's tournament as `Player`
subclasses whose `run()` drives a harness CLI, WITHOUT touching the arena,
scoring, Docker engine, or viewer.

CodeClash's seam (verified in vendor/CodeClash):
  - `codeclash.agents.player.Player` is abstract with one required method:
    `run(self) -> None`  ("given the observation/recap, update the codebase").
  - `codeclash.agents.get_agent(config, ctx, env)` maps `config['agent']` to a
    Player class. The compete phase (`arena.run_round`) never calls a model.

So a harness adapter Player only needs to:
  1. materialize the current bot file from its container to a host workdir,
  2. run the harness adapter (headless CLI) to edit it,
  3. write the edit back into the container.

To keep this spike importable and unit-testable without Docker, the container is
an injected object implementing a tiny `ContainerLike` protocol. In production
this is CodeClash's `DockerEnvironment`; in tests it's a fake.
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


@runtime_checkable
class ContainerLike(Protocol):
    """Minimal slice of CodeClash's DockerEnvironment we depend on."""

    def read_file(self, path: str) -> str: ...
    def write_file(self, path: str, content: str) -> None: ...


class HarnessPlayerCore:
    """The model-agnostic core of a harness-driven CodeClash Player.

    This is deliberately NOT a subclass of codeclash.agents.player.Player so it
    imports with zero CodeClash/Docker dependencies. The thin production wrapper
    (`make_codeclash_player`) mixes this into the real Player at runtime. The
    editing logic — the part that must be model-decoupled — lives here and is
    fully unit-tested.
    """

    def __init__(
        self,
        adapter: HarnessAdapter,
        container: ContainerLike,
        *,
        bot_file: str = "main.py",
        goal: str,
        model: str = "auto",
        budget: Budget | None = None,
    ) -> None:
        self.adapter = adapter
        self.container = container
        self.bot_file = bot_file
        self.goal = goal
        self.model = model
        self.budget = budget or Budget()
        self.last_result: AdapterResult | None = None

    def edit_turn(self) -> AdapterResult:
        """One edit phase: pull bot from container, run harness, push edit back.

        Returns the AdapterResult (also stored on self.last_result). The
        tournament's compete phase then scores the resulting codebase — no model
        involvement.
        """
        original = self.container.read_file(self.bot_file)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            bot_path = repo / self.bot_file
            bot_path.parent.mkdir(parents=True, exist_ok=True)
            bot_path.write_text(original)
            # Isolated git repo so the adapter's git-diff works and edits are scoped.
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=atv",
                 "commit", "-qm", "init"],
                cwd=repo,
                check=True,
            )
            req = AdapterRequest(
                repo_path=str(repo),
                goal=self.goal,
                model=self.model,
                budget=self.budget,
                bot_file=self.bot_file,
            )
            result = self.adapter.run(req)
            # Push the (possibly edited) bot back into the container.
            edited = bot_path.read_text()
            if edited != original and result.status == AdapterStatus.OK:
                self.container.write_file(self.bot_file, edited)
        self.last_result = result
        return result


def make_codeclash_player(adapter_name: str):
    """Produce a CodeClash `Player` subclass bound to a harness adapter.

    Imported lazily so this module stays importable without CodeClash installed.
    Registering the returned classes in `codeclash.agents.get_agent` is a
    two-line change (documented in the spike report) — the point is that NO arena
    or scoring code changes.
    """
    from codeclash.agents.player import Player  # lazy: requires codeclash env
    from atv_bench.adapters.contract import ADAPTERS

    adapter_cls = ADAPTERS[adapter_name]

    class HarnessPlayer(Player):  # pragma: no cover - requires Docker/codeclash
        def run(self) -> None:
            core = HarnessPlayerCore(
                adapter=adapter_cls(),
                container=_DockerContainerShim(self.environment),
                bot_file=self.config.get("bot_file", "main.py"),
                goal=self.game_context.prompts.get("edit", "Improve the bot."),
                model=self.config.get("config", {}).get("model", "auto"),
            )
            core.edit_turn()

    HarnessPlayer.__name__ = f"HarnessPlayer_{adapter_name}"
    return HarnessPlayer


class _DockerContainerShim:  # pragma: no cover - requires Docker
    """Adapts CodeClash's DockerEnvironment to ContainerLike."""

    def __init__(self, env) -> None:
        self.env = env

    def read_file(self, path: str) -> str:
        return self.env.execute(f"cat {path}")["output"]

    def write_file(self, path: str, content: str) -> None:
        from codeclash.utils.environment import create_file_in_container

        create_file_in_container(container=self.env, content=content, dest_path=path)
