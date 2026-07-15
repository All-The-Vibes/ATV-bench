"""Tests for the safe reader (eng T5 + santa round-1 findings).

count_child_files must (a) refuse to count a symlinked child that escapes the config
root, and (b) surface per-child read failures as structured errors rather than
silently undercounting.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atv_bench.fingerprint import reader
from atv_bench.fingerprint import probe as fp


def test_count_child_files_returns_count_and_errors(tmp_path):
    root = tmp_path / ".claude"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "a.md").write_text("x")
    (agents / "b.md").write_text("y")
    count, errors = reader.count_child_files(agents, root, suffix=".md")
    assert count == 2
    assert errors == []


def test_count_child_files_refuses_symlink_escape(tmp_path):
    root = tmp_path / ".claude"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "real.md").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "sk-proj-leaked-agent.md"
    secret.write_text("secret")
    try:
        (agents / "sk-proj-leaked-agent.md").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unsupported")
    count, errors = reader.count_child_files(agents, root, suffix=".md")
    # the escaping symlink is NOT counted and IS reported
    assert count == 1
    assert any(reason == reader.REASON_SYMLINK_ESCAPE for _n, reason in errors)


def test_probe_surfaces_agent_symlink_escape_in_unknown(tmp_path):
    """Santa round-1 (both reviewers): agent-file symlink escape must land in
    unknown[], and the escaping name must never reach the manifest."""
    home = tmp_path / ".claude"
    agents = home / "agents"
    agents.mkdir(parents=True)
    (agents / "planner.md").write_text("plan")
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.md").write_text("secret")
    try:
        (agents / "sk-proj-escape.md").symlink_to(outside / "target.md")
    except OSError:
        pytest.skip("symlinks unsupported")
    result = fp.probe_claude_code(home)
    # escaping agent file not counted; reason surfaced; name never in manifest
    assert result.manifest["custom_agents_count"] == 1
    assert "sk-proj-escape" not in json.dumps(result.manifest)
    reasons = {u["reason"] for u in result.manifest["unknown"]}
    assert reader.REASON_SYMLINK_ESCAPE in reasons
