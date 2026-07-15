"""Tests for submit preflight + CLI (devex T3, T7, eng T4).

`submit` never reports a self-scored result; it opens a PR carrying bot + fingerprint.
Before doing anything it runs a 7-check preflight so failures are diagnosable up front
rather than mid-push. --dry-run runs preflight + shows the plan without opening a PR.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.submit import (
    PREFLIGHT_CHECKS,
    PreflightCheck,
    build_submission,
    run_preflight,
)
from atv_bench.errors import AtvError


def test_seven_preflight_checks_defined():
    # gh installed / gh authed / repo exists / fork exists / branch clean /
    # leak-scan clean / bot-shape valid
    assert len(PREFLIGHT_CHECKS) == 7
    ids = {c.id for c in PREFLIGHT_CHECKS}
    assert ids == {
        "gh_installed", "gh_authed", "repo_exists", "fork_exists",
        "branch_clean", "leak_scan", "bot_shape",
    }


def test_preflight_runs_all_checks_and_reports():
    # a fake runner that passes everything
    def always_ok(check: PreflightCheck) -> tuple[bool, str]:
        return True, "ok"

    report = run_preflight(runner=always_ok)
    assert report["passed"] is True
    assert len(report["results"]) == 7
    assert all(r["ok"] for r in report["results"])


def test_preflight_surfaces_first_failure_with_actionable_error():
    def fail_auth(check: PreflightCheck) -> tuple[bool, str]:
        if check.id == "gh_authed":
            return False, "not logged in"
        return True, "ok"

    report = run_preflight(runner=fail_auth)
    assert report["passed"] is False
    failing = [r for r in report["results"] if not r["ok"]]
    assert len(failing) == 1
    assert failing[0]["id"] == "gh_authed"
    # the failing check carries an actionable fix + docs link
    assert failing[0]["fix"]
    assert failing[0]["docs_url"].startswith("https://")


def test_build_submission_shape():
    fingerprint = {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["gstack"], "mcps": [], "plugins": [], "custom_agents_count": 0,
        "unknown": [], "probe_version": "1.0.0",
    }
    sub = build_submission(
        bot_path=_write_bot(),
        fingerprint=fingerprint,
        identity="octocat",
        game="battlesnake",
    )
    assert sub["identity"] == "octocat"
    assert sub["game"] == "battlesnake"
    assert len(sub["bot_sha256"]) == 64
    assert sub["fingerprint"] == fingerprint
    # never carries a self-reported result
    assert "result" not in sub and "elo" not in sub and "win" not in sub


def test_build_submission_rejects_leaky_fingerprint():
    # a fingerprint that somehow still contains a secret-shaped value is refused
    bad = {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"], "mcps": [],
        "plugins": [], "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0",
    }
    with pytest.raises(AtvError):
        build_submission(bot_path=_write_bot(), fingerprint=bad,
                         identity="octocat", game="battlesnake")


# --- helpers ---

_BOT_SRC = "def move(state):\n    return 'up'\n"


def _write_bot(tmp=[None]):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    p = d / "main.py"
    p.write_text(_BOT_SRC)
    return str(p)
