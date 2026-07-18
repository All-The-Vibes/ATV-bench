"""TDD for players.py — HarnessPlayerCore promoted with snapshot capture + build-once.

Uses a FAKE container (a dir-backed tree) and a FAKE adapter (writes files, optionally
commits) so the edit turn is fully unit-testable with zero Docker/CodeClash.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    HarnessAdapter,
    Usage,
)
from atv_bench.capture import CaptureRejected
from atv_bench.players import HarnessPlayerCore


class DirContainer:
    """A fake ContainerLike backed by a host dir (a stand-in for the Docker workdir)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read_tree(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                out[p.relative_to(self.root).as_posix()] = p.read_text()
        return out

    def write_tree(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)


class FakeAdapter(HarnessAdapter):
    """Writes a fixed edit to the repo; can commit or leave in place; counts invocations."""

    name = "fake"

    def __init__(self, *, new_content="def get_move(o):\n    return 'S'\n",
                 commit=False, extra_files=None, model="fake-model-1", no_edit=False):
        self.new_content = new_content
        self.commit = commit
        self.extra_files = extra_files or {}
        self.model = model
        self.no_edit = no_edit
        self.calls = 0

    def run(self, req: AdapterRequest) -> AdapterResult:
        self.calls += 1
        repo = Path(req.repo_path)
        if not self.no_edit:
            (repo / req.bot_file).write_text(self.new_content)
            for rel, content in self.extra_files.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(content)
            if self.commit:
                subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
                subprocess.run(
                    ["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                     "commit", "-qm", "edit"], cwd=repo, check=True)
        status = AdapterStatus.NO_EDIT if self.no_edit else AdapterStatus.OK
        return AdapterResult(status=status, diff="", log="", usage=Usage(), model=self.model)


def _core(container, adapter, **kw):
    return HarnessPlayerCore(
        adapter=adapter, container=container, bot_file="main.py",
        goal="win", model="m", budget=Budget(max_seconds=30), **kw,
    )


def test_captures_committed_edit(tmp_path):
    """CRITICAL: adapter commits its edit → captured tree written back to container."""
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "def get_move(o):\n    return 'N'\n"})
    adapter = FakeAdapter(commit=True)
    core = _core(c, adapter)
    result = core.edit_turn()
    assert result.status == AdapterStatus.OK
    assert "return 'S'" in c.read_tree()["main.py"]


def test_captures_in_place_unstaged_edit(tmp_path):
    """CRITICAL: adapter rewrites tracked file in place (no stage/commit) → captured."""
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "def get_move(o):\n    return 'N'\n"})
    adapter = FakeAdapter(commit=False)
    core = _core(c, adapter)
    core.edit_turn()
    assert "return 'S'" in c.read_tree()["main.py"]


def test_multi_file_writeback(tmp_path):
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "old\n"})
    adapter = FakeAdapter(extra_files={"strategy.py": "WEIGHT = 3\n"})
    core = _core(c, adapter)
    core.edit_turn()
    tree = c.read_tree()
    assert "strategy.py" in tree
    assert tree["strategy.py"] == "WEIGHT = 3\n"


def test_no_edit_is_forfeit_not_crash(tmp_path):
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "unchanged\n"})
    adapter = FakeAdapter(no_edit=True)
    core = _core(c, adapter)
    result = core.edit_turn()
    assert result.status == AdapterStatus.NO_EDIT
    # container unchanged, no exception
    assert c.read_tree()["main.py"] == "unchanged\n"


def test_planted_secret_in_captured_tree_is_rejected(tmp_path):
    """CRITICAL: adapter writes a .env secret → capture allowlist rejects it."""
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "old\n"})
    adapter = FakeAdapter(extra_files={
        ".env": "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyzAB\n"})
    core = _core(c, adapter)
    with pytest.raises(CaptureRejected):
        core.edit_turn()


def test_build_once_across_multiple_triggers(tmp_path):
    """ENG-4/gap#9: the harness builds exactly once; N round-triggers reuse the artifact."""
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "def get_move(o):\n    return 'N'\n"})
    adapter = FakeAdapter(commit=False)
    core = _core(c, adapter, player_id="p1", game="lightcycles", prompt_version="edit@1")
    core.edit_turn()
    core.edit_turn()
    core.edit_turn()
    assert adapter.calls == 1  # built once, cached thereafter
    assert "return 'S'" in c.read_tree()["main.py"]


def test_provenance_diff_captured(tmp_path):
    c = DirContainer(tmp_path / "ctr")
    c.write_tree({"main.py": "def get_move(o):\n    return 'N'\n"})
    adapter = FakeAdapter(commit=True)
    core = _core(c, adapter)
    result = core.edit_turn()
    # diff is provenance/display; the materialized tree is authoritative (ENG-3)
    assert core.last_diff and "return 'S'" in core.last_diff
