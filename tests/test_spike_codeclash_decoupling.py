"""Unit tests for Spike 2: CodeClash decoupling (fast, no Docker, no model).

Proves the harness-Player editing core drives an arbitrary adapter and writes
the edit back to the container, with zero coupling to any model or arena code.
A fake adapter stands in for a real CLI; a fake container stands in for
CodeClash's DockerEnvironment.
"""
from __future__ import annotations

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    HarnessAdapter,
    Usage,
    git_diff,
)
from spikes.spike_codeclash_decoupling import ContainerLike, HarnessPlayerCore


class FakeContainer:
    """In-memory stand-in for CodeClash's DockerEnvironment."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)

    def read_file(self, path: str) -> str:
        return self.files[path]

    def write_file(self, path: str, content: str) -> None:
        self.files[path] = content


class EditingFakeAdapter(HarnessAdapter):
    """Adapter that edits the bot file on disk like a real headless CLI would."""

    name = "fake-edit"

    def run(self, req: AdapterRequest) -> AdapterResult:
        from pathlib import Path

        bot = Path(req.repo_path) / req.bot_file
        bot.write_text(bot.read_text().replace('"up"', '"down"'))
        return AdapterResult(
            status=AdapterStatus.OK,
            diff=git_diff(req.repo_path),
            log="edited",
            usage=Usage(tokens=1, seconds=0.0, turns=1),
            model="fake-model-x",
        )


class NoEditFakeAdapter(HarnessAdapter):
    name = "fake-noop"

    def run(self, req: AdapterRequest) -> AdapterResult:
        return AdapterResult(
            status=AdapterStatus.NO_EDIT, diff="", log="noop", usage=Usage()
        )


def test_fake_container_satisfies_protocol():
    assert isinstance(FakeContainer({}), ContainerLike)


def test_edit_turn_writes_edit_back_to_container():
    container = FakeContainer({"main.py": 'def move(): return "up"\n'})
    core = HarnessPlayerCore(
        adapter=EditingFakeAdapter(),
        container=container,
        bot_file="main.py",
        goal="Make the snake go down.",
    )
    result = core.edit_turn()
    assert result.status == AdapterStatus.OK
    # The edit reached the container — this is the decoupling proof.
    assert container.files["main.py"] == 'def move(): return "down"\n'
    # Model tag is captured for thesis-integrity leaderboard labeling.
    assert result.model == "fake-model-x"


def test_no_edit_leaves_container_untouched():
    original = 'def move(): return "up"\n'
    container = FakeContainer({"main.py": original})
    core = HarnessPlayerCore(
        adapter=NoEditFakeAdapter(),
        container=container,
        bot_file="main.py",
        goal="do nothing",
    )
    result = core.edit_turn()
    assert result.status == AdapterStatus.NO_EDIT
    assert container.files["main.py"] == original


def test_core_has_no_model_or_arena_imports():
    """The decoupling guarantee: the editing core module must not import any
    codeclash arena/model machinery at module load."""
    import spikes.spike_codeclash_decoupling as mod

    src = open(mod.__file__).read()
    # codeclash is only imported lazily inside make_codeclash_player, never top-level
    top_level = [
        line
        for line in src.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert not any("codeclash" in line for line in top_level), top_level
