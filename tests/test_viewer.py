"""Viewer smoke test: the static leaderboard renders each state without JS errors.

Guarded on Playwright + its browser being installed (skips cleanly otherwise), so
the hermetic suite stays fast. This is the automated companion to
scripts/screenshot_leaderboard.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

VIEW = Path(__file__).parent.parent / "leaderboard" / "view" / "index.html"


def _playwright_ready():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _playwright_ready(), reason="playwright browser not installed")


def _render(fixture):
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(f"window.__FIXTURE__ = {json.dumps(fixture)};")
        page.goto(VIEW.as_uri())
        page.wait_for_timeout(150)
        text = page.inner_text("body")
        html = page.content()
        browser.close()
    assert not errors, f"JS errors: {errors}"
    return text, html


def test_populated_renders_rank_elo_identity():
    fixture = {"schema_version": 1, "updated_at": "2026-07-15T18:00:00Z", "rows": [{
        "rank": 1, "elo": 1812, "rated": True, "match_count": 64,
        "ci": {"lo": 1782, "hi": 1842}, "identity": "octocat", "harness_name": "claude-code",
        "fingerprint_summary": "", "fingerprint_gstack": True,
        "details": {"skills": ["gstack"], "mcps": ["github"], "plugins": [], "unknown": []},
        "bot_sha256": "a" * 64, "fingerprint_probe_version": "1.0.0",
        "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l",
    }]}
    text, _ = _render(fixture)
    assert "#1" in text
    assert "1812" in text
    assert "octocat" in text
    assert "Last updated" in text  # staleness affordance always visible


def test_empty_state_shows_cta():
    text, _ = _render({"schema_version": 1, "updated_at": "2026-07-15T18:00:00Z", "rows": []})
    assert "Be the first" in text
    assert "atv-bench submit" in text


def test_low_confidence_demoted_and_marked():
    fixture = {"schema_version": 1, "updated_at": "2026-07-15T18:00:00Z", "rows": [
        {"rank": 1, "elo": 1700, "rated": True, "match_count": 40, "ci": {"lo": 1670, "hi": 1730},
         "identity": "stable", "harness_name": "claude-code", "fingerprint_summary": "",
         "fingerprint_gstack": True, "details": {"skills": [], "mcps": [], "plugins": [], "unknown": []},
         "bot_sha256": "a"*64, "fingerprint_probe_version": "1.0.0", "pr_url": "https://x/1", "logs_url": "https://x/l"},
        {"rank": 2, "elo": 1650, "rated": True, "match_count": 3, "ci": {"lo": 1555, "hi": 1745},
         "identity": "shaky", "harness_name": "claude-code", "fingerprint_summary": "",
         "fingerprint_gstack": True, "details": {"skills": [], "mcps": [], "plugins": [], "unknown": []},
         "bot_sha256": "b"*64, "fingerprint_probe_version": "1.0.0", "pr_url": "https://x/2", "logs_url": "https://x/l"},
    ]}
    text, html = _render(fixture)
    assert "low confidence" in text.lower()
    assert "provisional" in text.lower()  # demoted below a divider
