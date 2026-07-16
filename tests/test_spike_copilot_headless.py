"""Spike 1 (live): headless harness CLI edits, no TTY, token via env.

These tests invoke the REAL CLIs and are marked `live` (deselected by default).
Run with:  uv run pytest -m live -s

They encode the design's assignment acceptance criteria:
  - a harness CLI, given a repo + goal, edits bot.py with zero keystrokes
  - runs with no controlling TTY (stdin closed)
  - produces a git diff we can capture

claude-code is the primary (known entitled here). copilot-cli is validated for
the *mechanism*; if the local account hits an org policy wall, the test records
POLICY_DENIED as an expected fallback-ladder outcome rather than failing — the
headless mechanism is still proven (non-interactive, no TTY, clean exit).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterStatus,
    Budget,
    ClaudeCodeAdapter,
    CopilotCliAdapter,
)


def _make_bot_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bot"
    repo.mkdir()
    (repo / "bot.py").write_text('def move(state):\n    return "up"\n')
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=atv", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.mark.live
@pytest.mark.skipif(not ClaudeCodeAdapter.available(), reason="claude CLI not installed")
def test_claude_code_headless_edit(tmp_path):
    repo = _make_bot_repo(tmp_path)
    adapter = ClaudeCodeAdapter()
    req = AdapterRequest(
        repo_path=str(repo),
        goal="Edit bot.py so the move() function returns 'down' instead of 'up'. Edit the file directly.",
        budget=Budget(max_seconds=180),
    )
    # stdin closed => proves no TTY needed
    result = adapter.run(req)
    assert result.status == AdapterStatus.OK, result.log
    assert '"down"' in (repo / "bot.py").read_text()
    assert result.diff.strip(), "expected a non-empty diff"
    assert result.model != "unknown", "expected model tag for leaderboard labeling"


@pytest.mark.live
@pytest.mark.skipif(not CopilotCliAdapter.available(), reason="copilot CLI not installed")
def test_copilot_cli_headless_mechanism(tmp_path):
    repo = _make_bot_repo(tmp_path)
    adapter = CopilotCliAdapter()
    req = AdapterRequest(
        repo_path=str(repo),
        goal="Edit bot.py so move() returns 'down' instead of 'up'. Edit the file directly.",
        budget=Budget(max_seconds=180),
    )
    result = adapter.run(req)
    # The headless MECHANISM is what this spike validates: non-interactive, no TTY,
    # clean exit, env-token auth. Either it edited (OK) or org policy blocked it
    # (POLICY_DENIED, a documented fallback-ladder outcome). Both prove mechanism;
    # a TTY/crash failure would not.
    assert result.status in {
        AdapterStatus.OK,
        AdapterStatus.POLICY_DENIED,
        AdapterStatus.NO_EDIT,
    }, result.log
    if result.status == AdapterStatus.OK:
        assert '"down"' in (repo / "bot.py").read_text()
