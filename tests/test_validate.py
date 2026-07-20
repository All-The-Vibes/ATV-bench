"""Contributor validation tools (devex T6): validate-harness / validate-game.

The ecosystem path must be a real command, not tribal knowledge. A new harness or
game contribution is validated locally before it is PR'd: the harness reader must
produce a schema-valid, leak-safe fingerprint; a game bot must pass shape checks and
its own required canary test.
"""
from __future__ import annotations

from atv_bench.validate import (
    validate_game_bot,
    validate_harness_fingerprint,
)


def _clean_fp():
    return {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["gstack"], "nested_skills": [], "tools": [], "mcps": ["github"],
        "plugins": [], "custom_agents_count": 0,
        "cli_version": {"binary": "claude", "version": "1.0.0", "path": "/x", "sha256": "unknown"},
        "unknown_runtime": [], "unknown": [], "probe_version": "1.0.0",
    }


def test_validate_harness_accepts_clean_fingerprint():
    report = validate_harness_fingerprint(_clean_fp())
    assert report["ok"] is True
    assert report["errors"] == []


def test_validate_harness_rejects_missing_schema_key():
    fp = _clean_fp()
    del fp["skills"]
    report = validate_harness_fingerprint(fp)
    assert report["ok"] is False
    assert any("skills" in e for e in report["errors"])


def test_validate_harness_rejects_leaky_value():
    fp = _clean_fp()
    fp["skills"] = ["ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"]
    report = validate_harness_fingerprint(fp)
    assert report["ok"] is False
    assert any("leak" in e.lower() or "secret" in e.lower() for e in report["errors"])


def test_validate_harness_rejects_unknown_reason_not_in_schema_enum():
    """Santa round-1 (Reviewer B): validate-harness was looser than the locked schema
    — it accepted an unknown[].reason the leaderboard schema rejects."""
    fp = _clean_fp()
    fp["unknown"] = [{"field": "plugins", "reason": "totally_made_up_reason"}]
    report = validate_harness_fingerprint(fp)
    assert report["ok"] is False
    assert any("reason" in e.lower() for e in report["errors"])


def test_validate_harness_accepts_valid_unknown_reason():
    fp = _clean_fp()
    fp["unknown"] = [{"field": "plugins", "reason": "permission_denied"}]
    report = validate_harness_fingerprint(fp)
    assert report["ok"] is True


def test_validate_game_bot_accepts_small_text_file(tmp_path):
    bot = tmp_path / "main.py"
    bot.write_text("def move(s):\n    return 'up'\n")
    report = validate_game_bot(str(bot))
    assert report["ok"] is True


def test_validate_game_bot_rejects_oversize(tmp_path):
    bot = tmp_path / "main.py"
    bot.write_text("x = '" + "A" * (300 * 1024) + "'\n")
    report = validate_game_bot(str(bot))
    assert report["ok"] is False
    assert any("size" in e.lower() or "bytes" in e.lower() for e in report["errors"])


def test_validate_game_bot_rejects_missing(tmp_path):
    report = validate_game_bot(str(tmp_path / "nope.py"))
    assert report["ok"] is False
