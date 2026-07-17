"""CRITICAL fingerprint completeness + runtime-surface tests (Lane A, Eng Decision #4).

The fingerprint is the MOAT (Premise 5). A surface silently dropped is a benchmark-
integrity bug. These tests build a fixture config carrying EVERY required surface and
assert each is either PRESENT in the manifest OR recorded in unknown[] with a reason —
a silent drop FAILS the test.

Required surface (locked): model, plugins, tools, MCPs, skills, nested_skills,
custom agents, plus runtime honesty (CLI version/path/hash + unknown_runtime[]).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atv_bench.fingerprint import probe
from atv_bench.fingerprint.probe import FINGERPRINT_SCHEMA_KEYS


REQUIRED_SURFACES = (
    "model", "plugins", "tools", "mcps", "skills", "nested_skills",
    "custom_agents_count",
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _surface_accounted(m: dict, surface: str, unknown_fields: set[str]) -> bool:
    """A surface is accounted for if it's populated, is a present count, or in unknown[].

    custom_agents_count is a COUNT (0 is a legitimate value, not a silent drop). The
    completeness contract is 'never silently DROP a surface' — a zero count is a read
    that returned zero, which is honest.
    """
    if surface == "custom_agents_count":
        return isinstance(m.get(surface), int)
    return bool(m.get(surface)) or surface in unknown_fields


def _full_claude_fixture(root: Path) -> None:
    """A ~/.claude with every surface populated, incl. nested plugin skills + tools."""
    _write(root / "settings.json", json.dumps({
        "model": "claude-opus-4-8",
        "permissions": {
            "allow": ["Bash", "Read", "Edit"],
            "deny": ["WebFetch"],
        },
    }))
    _write(root / ".mcp.json", json.dumps({
        "mcpServers": {"github": {"command": "x"}, "grafana": {"url": "y"}}
    }))
    (root / "skills" / "tdd").mkdir(parents=True)
    (root / "skills" / "office-hours").mkdir(parents=True)
    # nested skills under a plugin
    (root / "plugins" / "compound-engineering" / "skills" / "ce-plan").mkdir(parents=True)
    (root / "plugins" / "compound-engineering" / "skills" / "ce-debug").mkdir(parents=True)
    (root / "agents").mkdir(parents=True)
    _write(root / "agents" / "reviewer.md", "x")
    _write(root / "agents" / "planner.md", "y")


def test_schema_keys_include_tools_and_nested_skills_and_runtime():
    for k in ("tools", "nested_skills", "cli_version", "unknown_runtime"):
        assert k in FINGERPRINT_SCHEMA_KEYS, f"schema missing {k}"


def test_claude_manifest_has_exactly_the_schema_keys(tmp_path):
    _full_claude_fixture(tmp_path)
    m = probe.probe_claude_code(tmp_path).manifest
    assert set(m) == set(FINGERPRINT_SCHEMA_KEYS)


def test_claude_completeness_every_surface_present_or_accounted(tmp_path):
    _full_claude_fixture(tmp_path)
    m = probe.probe_claude_code(tmp_path).manifest
    unknown_fields = {u["field"] for u in m["unknown"]}
    for surface in REQUIRED_SURFACES:
        assert _surface_accounted(m, surface, unknown_fields), \
            f"surface {surface!r} silently dropped (not in manifest nor unknown[])"


def test_claude_nested_skills_are_captured(tmp_path):
    _full_claude_fixture(tmp_path)
    m = probe.probe_claude_code(tmp_path).manifest
    assert "ce-plan" in m["nested_skills"]
    assert "ce-debug" in m["nested_skills"]
    # top-level skills stay in skills, not nested
    assert "tdd" in m["skills"]
    assert "ce-plan" not in m["skills"]


def test_claude_tools_captured_with_source(tmp_path):
    _full_claude_fixture(tmp_path)
    m = probe.probe_claude_code(tmp_path).manifest
    # tools is a list of {name, source, enabled} leak-safe entries
    assert m["tools"], "tools surface empty despite permissions in settings"
    names = {t["name"] for t in m["tools"]}
    assert "Bash" in names
    for t in m["tools"]:
        assert t["source"] in ("permission", "builtin", "mcp", "plugin", "unknown")
        assert isinstance(t["enabled"], bool)
    # a denied tool is captured as enabled=False
    deny = [t for t in m["tools"] if t["name"] == "WebFetch"]
    assert deny and deny[0]["enabled"] is False


def test_runtime_surface_is_honest(tmp_path):
    _full_claude_fixture(tmp_path)
    m = probe.probe_claude_code(tmp_path).manifest
    # cli_version is a dict {version, path, sha256} or records unknown_runtime honestly
    assert isinstance(m["cli_version"], dict)
    assert isinstance(m["unknown_runtime"], list)


def test_copilot_completeness(tmp_path):
    # minimal copilot fixture with nested skills + mcp + plugins
    _write(tmp_path / "settings.json", json.dumps({
        "model": "claude-opus-4.8",
        "enabledPlugins": {"superpowers@github": True},
    }))
    _write(tmp_path / "mcp-config.json", json.dumps({
        "mcpServers": {"github-mcp-server": {"url": "x"}}
    }))
    skill = tmp_path / "installed-plugins" / "github" / "superpowers" / "skills" / "brainstorming"
    skill.mkdir(parents=True)
    m = probe.probe_copilot_cli(tmp_path).manifest
    assert set(m) == set(FINGERPRINT_SCHEMA_KEYS)
    unknown_fields = {u["field"] for u in m["unknown"]}
    for surface in REQUIRED_SURFACES:
        # Copilot has no top-level skills dir — the "skills" concept maps entirely to
        # nested_skills, so treat either populated as accounting for the skill surface.
        if surface == "skills":
            accounted = bool(m["skills"]) or bool(m["nested_skills"]) or "skills" in unknown_fields
        else:
            accounted = _surface_accounted(m, surface, unknown_fields)
        assert accounted, f"{surface} dropped"
    assert "brainstorming" in m["nested_skills"]
