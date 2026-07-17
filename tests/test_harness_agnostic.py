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


def test_claude_code_live_others_planned():
    assert hz.is_live("claude-code")
    assert not hz.is_live("copilot-cli")
    assert not hz.is_live("codex")


def test_detect_harness_finds_live_config(tmp_path):
    # No config dir → nothing detected.
    assert hz.detect_harness(home=tmp_path) is None
    # Create the claude-code config root → detected.
    (tmp_path / ".claude").mkdir()
    assert hz.detect_harness(home=tmp_path) == "claude-code"


def test_detect_ignores_planned_harness_config(tmp_path):
    # A planned harness's config dir must NOT be detected — we can't fingerprint it yet.
    (tmp_path / ".copilot").mkdir()
    assert hz.detect_harness(home=tmp_path) is None


def test_config_root_for_uses_registry(tmp_path):
    assert hz.config_root_for("claude-code", home=tmp_path) == tmp_path / ".claude"
    assert hz.config_root_for("copilot-cli", home=tmp_path) == tmp_path / ".copilot"


def test_assert_probeable_rejects_unknown_and_planned():
    with pytest.raises(ValueError, match="unknown harness"):
        hz.assert_probeable("bogus")
    with pytest.raises(ValueError, match="planned"):
        hz.assert_probeable("copilot-cli")
    hz.assert_probeable("claude-code")  # does not raise


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


def test_probe_fails_closed_on_planned_harness(tmp_path):
    with pytest.raises(ValueError, match="planned"):
        fp.probe(home=tmp_path, harness="copilot-cli")


def test_probe_fails_closed_on_unknown_harness(tmp_path):
    with pytest.raises(ValueError, match="unknown harness"):
        fp.probe(home=tmp_path, harness="bogus")


# --- CLI surface ----------------------------------------------------------------------

def test_harnesses_command_lists_live_and_planned():
    result = runner.invoke(app, ["harnesses"])
    assert result.exit_code == 0
    assert "claude-code" in result.output
    assert "copilot-cli" in result.output
    assert "[live]" in result.output
    assert "[planned]" in result.output


def test_harnesses_json():
    result = runner.invoke(app, ["harnesses", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    keys = {h["key"]: h for h in payload}
    assert keys["claude-code"]["live"] is True
    assert keys["copilot-cli"]["live"] is False


def test_fingerprint_planned_harness_fails_closed(tmp_path):
    result = runner.invoke(app, ["fingerprint", "--harness", "copilot-cli", "--home", str(tmp_path)])
    assert result.exit_code == 2
    assert "planned" in result.output


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
