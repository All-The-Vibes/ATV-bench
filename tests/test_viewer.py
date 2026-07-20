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
LIVE_VIEW = Path(__file__).parent.parent / "src" / "atv_bench" / "view" / "live.html"


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


def test_javascript_url_neutralized_in_href():
    """Santa round-1: a javascript: pr_url/logs_url must never become a live href.
    Defense-in-depth in the viewer even though the schema also rejects it."""
    from playwright.sync_api import sync_playwright
    fixture = {"schema_version": 1, "updated_at": "2026-07-15T18:00:00Z", "rows": [{
        "rank": 1, "elo": 1600, "rated": True, "match_count": 40,
        "ci": {"lo": 1570, "hi": 1630}, "identity": "attacker", "harness_name": "claude-code",
        "fingerprint_summary": "", "fingerprint_gstack": False,
        "details": {"skills": [], "mcps": [], "plugins": [], "unknown": []},
        "bot_sha256": "a" * 64, "fingerprint_probe_version": "1.0.0",
        "pr_url": "javascript:alert(document.cookie)",
        "logs_url": "javascript:alert(1)",
    }]}
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(f"window.__FIXTURE__ = {json.dumps(fixture)};")
        page.goto(VIEW.as_uri())
        page.wait_for_timeout(150)
        hrefs = page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
        browser.close()
    assert not errors, f"JS errors: {errors}"
    for h in hrefs:
        assert h is None or not h.lower().startswith("javascript:"), f"live javascript href: {h}"


def test_rendered_dom_has_no_rank_when_unverified():
    """Section 6 typed-rank guard at the DOM boundary.

    When the published doc is UNVERIFIED (verified=false), the board must render the
    integrity-gate reframe and NO rank language — not #1, not a medal, not "leads" /
    "beats" / "edged out". Adversarial non-regex variants are checked so the guard
    catches the meaning, not a single pattern.
    """
    fixture = {
        "schema_version": 1, "updated_at": "2026-07-15T18:00:00Z",
        "verified": False,
        "rows": [
            {"rank": 1, "elo": 1812, "rated": True, "match_count": 64,
             "ci": {"lo": 1782, "hi": 1842}, "identity": "octocat",
             "harness_name": "claude-code", "fingerprint_summary": "",
             "fingerprint_gstack": True,
             "details": {"skills": ["gstack"], "mcps": [], "plugins": [], "unknown": []},
             "bot_sha256": "a" * 64, "fingerprint_probe_version": "1.0.0",
             "pr_url": "https://x/1", "logs_url": "https://x/l"},
            {"rank": 2, "elo": 1650, "rated": True, "match_count": 40,
             "ci": {"lo": 1620, "hi": 1680}, "identity": "hubot",
             "harness_name": "copilot-cli", "fingerprint_summary": "",
             "fingerprint_gstack": False,
             "details": {"skills": [], "mcps": [], "plugins": [], "unknown": []},
             "bot_sha256": "b" * 64, "fingerprint_probe_version": "1.0.0",
             "pr_url": "https://x/2", "logs_url": "https://x/l"},
        ],
    }
    text, html = _render(fixture)
    low = text.lower()
    # The reframe must be present.
    assert "integrity" in low
    assert "not a ranked number" in low or "gated" in low
    # Adversarial rank-shaped phrasings must be ABSENT from the rendered DOM.
    for phrase in ["#1", "#2", "edged out", "leads", "beats", "🥇", "🥈", "🥉", "🏆"]:
        assert phrase.lower() not in low, f"rank phrasing leaked when unverified: {phrase!r}"
    import re as _re
    assert not _re.search(r"#\s*\d+", text), "numeric rank token rendered when unverified"


def _render_live(board_fixture):
    """Render the Act-3 live board (live.html) with an injected board doc.

    live.html listens for an SSE `board` event; tests inject the same doc via
    window.__BOARD_FIXTURE__ so we can exercise the render path without a live match.
    """
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(f"window.__BOARD_FIXTURE__ = {json.dumps(board_fixture)};")
        page.goto(LIVE_VIEW.as_uri())
        page.wait_for_timeout(150)
        text = page.inner_text("body")
        html = page.content()
        browser.close()
    assert not errors, f"JS errors: {errors}"
    return text, html


def test_live_board_no_rank_when_unverified():
    """Section 6 typed-rank guard at the Act-3 live-board DOM boundary.

    The live board (src/atv_bench/view/live.html, served by live_server.py) must mirror
    index.html: when the board doc is UNVERIFIED (verified=false) it renders the
    integrity-gate reframe and NO rank language — not #1/#2, no medal, no
    "leads"/"beats"/"edged out". This is the porous-guard fix: live.html previously had
    no verified gate at all and only avoided leaking by being fed verified-by-omission
    demo data.
    """
    board = {
        "verified": False,
        "rows": [
            {"rank": 1, "elo": 1812, "identity": "ATV-StarterKit",
             "harness_name": "claude-code", "fingerprint_summary": ""},
            {"rank": 2, "elo": 1650, "identity": "ATV-Phoenix",
             "harness_name": "copilot-cli", "fingerprint_summary": ""},
        ],
        "insights": ["ATV-StarterKit leads ATV-Phoenix by 162 ELO"],
    }
    text, html = _render_live(board)
    low = text.lower()
    # The reframe must be present.
    assert "integrity" in low
    assert "not a ranked number" in low or "gated" in low
    # Adversarial rank-shaped phrasings must be ABSENT from the rendered DOM.
    for phrase in ["#1", "#2", "edged out", "leads", "beats", "🥇", "🥈", "🥉", "🏆"]:
        assert phrase.lower() not in low, f"rank phrasing leaked when unverified: {phrase!r}"
    import re as _re
    assert not _re.search(r"#\s*\d+", text), "numeric rank token rendered when unverified"


def test_live_board_shows_rank_when_verified():
    """Sanity contrast: a verified (or verified-by-omission) board still shows ranks."""
    board = {
        "rows": [
            {"rank": 1, "elo": 1812, "identity": "ATV-StarterKit",
             "harness_name": "claude-code", "fingerprint_summary": ""},
            {"rank": 2, "elo": 1650, "identity": "ATV-Phoenix",
             "harness_name": "copilot-cli", "fingerprint_summary": ""},
        ],
        "insights": ["a decisive match"],
    }
    text, _ = _render_live(board)
    assert "#1" in text
    assert "ATV-StarterKit" in text


def test_bundled_view_matches_canonical():
    """The wheel bundles a copy of the viewer at atv_bench/view/index.html so `board`
    renders clone-free. It MUST stay byte-identical to the canonical
    leaderboard/view/index.html, or an installed tool renders a stale board. This guard
    fails if the two drift; re-copy when you edit the viewer.
    """
    canonical = Path(__file__).parent.parent / "leaderboard" / "view" / "index.html"
    bundled = Path(__file__).parent.parent / "src" / "atv_bench" / "view" / "index.html"
    assert bundled.exists(), "bundled viewer copy is missing"
    assert bundled.read_text() == canonical.read_text(), (
        "bundled viewer drifted from canonical; re-copy "
        "leaderboard/view/index.html -> src/atv_bench/view/index.html"
    )
