"""TDD for the repo-harness fingerprint reader (real GitHub-repo layout).

The standard probes read a machine's ~/.claude / ~/.copilot config. A GitHub REPO that
ships a harness (ATV-Phoenix, hve-core) uses a different layout: top-level skills/,
plugins/<p>/{skills,agents}, .copilot-plugin/skills, .github/skills. This reader
fingerprints that HONESTLY — no gstack unless it's actually there, model=unknown unless
the repo declares one — so the leaderboard reflects the repo, not the machine that ran it.
"""
from __future__ import annotations

from pathlib import Path

from atv_bench.fingerprint import probe
from atv_bench.fingerprint.probe import FINGERPRINT_SCHEMA_KEYS


def _mk(root: Path, rel: str) -> None:
    (root / rel).mkdir(parents=True, exist_ok=True)


def test_hve_like_repo_has_no_gstack_and_real_plugins(tmp_path):
    # a repo with plugins/security, plugins/ado — and NO gstack anywhere
    for plug in ("security", "ado", "jira"):
        _mk(tmp_path, f"plugins/{plug}/skills/{plug}-skill")
        _mk(tmp_path, f"plugins/{plug}/agents/{plug}-agent")
    m = probe.probe_repo(tmp_path, harness_name="microsoft/hve-core")
    assert m["gstack"] is False, "must NOT claim gstack when the repo has none"
    assert set(m["plugins"]) == {"security", "ado", "jira"}
    assert "security-skill" in m["nested_skills"]
    assert m["model"] == "unknown"  # repo declares no model
    assert set(m) == set(FINGERPRINT_SCHEMA_KEYS) | {"harness_name"}


def test_phoenix_like_repo_top_level_skills(tmp_path):
    _mk(tmp_path, "skills/phoenix-goal")
    _mk(tmp_path, "skills/phoenix-heal")
    _mk(tmp_path, ".copilot-plugin/skills/phoenix-setup")
    m = probe.probe_repo(tmp_path, harness_name="all-the-vibes/ATV-Phoenix")
    assert "phoenix-goal" in m["skills"]
    assert "phoenix-setup" in m["nested_skills"]  # under .copilot-plugin
    assert m["gstack"] is False


def test_gstack_only_when_actually_present(tmp_path):
    _mk(tmp_path, "plugins/gstack/skills/gstack")
    m = probe.probe_repo(tmp_path, harness_name="x/y")
    assert m["gstack"] is True  # honest: it IS here


def test_repo_reader_is_leak_safe(tmp_path):
    # a secret-shaped skill dir name must be scrubbed, not emitted
    _mk(tmp_path, "skills/ghp_1234567890abcdefghijklmnopqrstuvwxyzAB")
    _mk(tmp_path, "skills/clean-skill")
    m = probe.probe_repo(tmp_path, harness_name="x/y")
    assert "clean-skill" in m["skills"]
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" not in m["skills"]
    assert any(u["field"] == "skills" for u in m["unknown"])


def test_harness_name_carried(tmp_path):
    _mk(tmp_path, "skills/a")
    m = probe.probe_repo(tmp_path, harness_name="microsoft/hve-core")
    assert m["harness_name"] == "microsoft/hve-core"
