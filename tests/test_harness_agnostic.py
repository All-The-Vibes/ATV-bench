"""Harness-agnostic surface tests.

ATV-bench benchmarks the *harness*, so the CLI must never present as a claude-code-only
tool. These tests lock in: the harness registry (single source of truth), the generic
`probe()` dispatcher (auto-detect + fail-closed on planned/unknown), the `harnesses`
command, and — as a regression guard — that no user-facing command help string names a
specific harness.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from atv_bench import harnesses as hz
from atv_bench.cli import app
from atv_bench.fingerprint import probe as fp

runner = CliRunner()


# --- registry -------------------------------------------------------------------------

def test_default_harness_is_live():
    assert hz.is_live(hz.DEFAULT_HARNESS)
    assert hz.DEFAULT_HARNESS in hz.live_keys()


def test_all_v1_harnesses_live():
    assert hz.is_live("claude-code")
    assert hz.is_live("copilot-cli")
    assert hz.is_live("codex")


def test_detect_harness_finds_live_config(tmp_path):
    # No config dir → nothing detected.
    assert hz.detect_harness(home=tmp_path) is None
    # Create the claude-code config root → detected.
    (tmp_path / ".claude").mkdir()
    assert hz.detect_harness(home=tmp_path) == "claude-code"


def test_detect_finds_codex_now_live(tmp_path):
    # codex is now live, so its config dir IS detected.
    (tmp_path / ".codex").mkdir()
    assert hz.detect_harness(home=tmp_path) == "codex"


def test_config_root_for_uses_registry(tmp_path):
    assert hz.config_root_for("claude-code", home=tmp_path) == tmp_path / ".claude"
    assert hz.config_root_for("copilot-cli", home=tmp_path) == tmp_path / ".copilot"


def test_assert_probeable_rejects_unknown_accepts_live():
    with pytest.raises(ValueError, match="unknown harness"):
        hz.assert_probeable("bogus")
    hz.assert_probeable("claude-code")  # does not raise
    hz.assert_probeable("copilot-cli")  # live — does not raise
    hz.assert_probeable("codex")        # now live — does not raise


# --- generic probe() dispatcher -------------------------------------------------------

def _fixture_home(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    (home / "skills" / "gstack").mkdir(parents=True)
    return home


def test_probe_defaults_to_claude_code(tmp_path):
    home = _fixture_home(tmp_path)
    result = fp.probe(home=home)
    assert result.manifest["harness"] == "claude-code"
    assert "gstack" in result.manifest["skills"]


def test_probe_explicit_harness_matches_default(tmp_path):
    home = _fixture_home(tmp_path)
    result = fp.probe(home=home, harness="claude-code")
    assert result.manifest["harness"] == "claude-code"


def test_probe_codex_now_live(tmp_path):
    """codex is live: probe() dispatches to its reader instead of failing closed."""
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.5"\n')
    result = fp.probe(home=home, harness="codex")
    assert result.manifest["harness"] == "codex"
    assert result.manifest["model"] == "gpt-5.5"


def test_probe_fails_closed_on_unknown_harness(tmp_path):
    with pytest.raises(ValueError, match="unknown harness"):
        fp.probe(home=tmp_path, harness="bogus")


# --- CLI surface ----------------------------------------------------------------------

def test_harnesses_command_lists_live():
    result = runner.invoke(app, ["harnesses"])
    assert result.exit_code == 0
    assert "claude-code" in result.output
    assert "copilot-cli" in result.output
    assert "codex" in result.output
    assert "[live]" in result.output


def test_harnesses_json():
    result = runner.invoke(app, ["harnesses", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    keys = {h["key"]: h for h in payload}
    assert keys["claude-code"]["live"] is True
    assert keys["copilot-cli"]["live"] is True
    assert keys["codex"]["live"] is True


def test_fingerprint_codex_now_live_probes(tmp_path):
    """codex is live: an explicit probe against a real ~/.codex succeeds (no fail-closed)."""
    home = tmp_path / ".codex"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "config.toml").write_text('model = "gpt-5.5"\n')
    result = runner.invoke(app, ["fingerprint", "--json", "--harness", "codex", "--home", str(home)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["harness"] == "codex"
    assert payload["model"] == "gpt-5.5"


def test_fingerprint_unknown_harness_fails_closed(tmp_path):
    result = runner.invoke(app, ["fingerprint", "--harness", "bogus", "--home", str(tmp_path)])
    assert result.exit_code == 2
    assert "unknown harness" in result.output


def test_no_command_help_names_a_specific_harness():
    """Regression guard: the top-level help + every command's help must stay harness-
    agnostic — no 'claude-code'/'claude code' in any user-facing help string. (The
    `harnesses` listing itself names harnesses at runtime, which is expected and is
    output, not help text.)"""
    top = runner.invoke(app, ["--help"])
    assert "claude-code" not in top.output.lower()
    assert "claude code" not in top.output.lower()
    for cmd in ("fingerprint", "submit", "validate-harness", "doctor", "games", "board"):
        out = runner.invoke(app, [cmd, "--help"]).output.lower()
        assert "claude-code" not in out, f"{cmd} --help names claude-code"
        assert "claude code" not in out, f"{cmd} --help names claude code"
