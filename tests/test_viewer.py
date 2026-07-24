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
LIVE_VIEW = Path(__file__).parent.parent / "src" / "atv_bench" / "view" / "live_match.html"


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


# ---------------------------------------------------------------------------
# Section 8: the VERIFIED board surface must render the harness-LIFT results and
# every honesty affordance in the DOM, driven through real Chromium.
#
# The verified fixture below is the target doc shape the board must consume: each
# row carries the Section-5.5 harness lift (lift over the bare model) + its CI, the
# Section-4 schema-v2 fingerprint fields (tools / nested_skills), a Section-4
# BudgetVector (tokens / tool_calls / wall_time_s), the unknown[] ledger, and a
# secondary bundle theta. `verified: true` selects the ranked (non-gated) view.
# ---------------------------------------------------------------------------

def _verified_board_fixture():
    return {
        "schema_version": 1,
        "updated_at": "2026-07-18T12:00:00Z",
        "verified": True,
        "rows": [
            {
                "rank": 1, "elo": 1812, "rated": True, "match_count": 64,
                "ci": {"lo": 1782, "hi": 1842},
                "lift": 0.62, "lift_ci": {"lo": 0.41, "hi": 0.83},
                "theta": 0.28,
                "identity": "octocat", "harness_name": "claude-code",
                "fingerprint_summary": "", "fingerprint_gstack": True,
                "budget": {"tokens": 128000, "tool_calls": 240, "wall_time_s": 512.0},
                "details": {
                    "skills": ["gstack"], "nested_skills": ["gstack:land", "gstack:plan"],
                    "tools": ["Bash", "Edit", "WebFetch"],
                    "mcps": ["github"], "plugins": [],
                    "unknown": [{"field": "agents", "reason": "not_readable"}],
                },
                "bot_sha256": "a" * 64, "fingerprint_probe_version": "1.0.0",
                "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l",
            },
            {
                "rank": 2, "elo": 1650, "rated": True, "match_count": 48,
                "ci": {"lo": 1620, "hi": 1680},
                "lift": 0.34, "lift_ci": {"lo": 0.12, "hi": 0.56},
                "theta": 0.11,
                "identity": "hubot", "harness_name": "copilot-cli",
                "fingerprint_summary": "", "fingerprint_gstack": False,
                "budget": {"tokens": 64000, "tool_calls": 90, "wall_time_s": 210.0},
                "details": {
                    "skills": [], "nested_skills": [],
                    "tools": ["Bash"],
                    "mcps": [], "plugins": [],
                    "unknown": [],
                },
                "bot_sha256": "b" * 64, "fingerprint_probe_version": "1.0.0",
                "pr_url": "https://x/2", "logs_url": "https://x/l",
            },
        ],
    }


def test_board_renders_lift_rows():
    """Section 5.5 headline: each row shows the harness LIFT over the bare model, with CI."""
    text, _ = _render(_verified_board_fixture())
    low = text.lower()
    assert "lift" in low, "LIFT metric not rendered on the board"
    # Point lifts (formatted with sign) must appear as the per-harness headline number.
    assert "+0.62" in text, "row 1 harness lift not rendered"
    assert "+0.34" in text, "row 2 harness lift not rendered"
    # Each lift carries its confidence interval.
    assert "+0.41" in text and "+0.83" in text, "row 1 lift CI not rendered"
    assert "+0.12" in text and "+0.56" in text, "row 2 lift CI not rendered"


def test_board_shows_fingerprint_chips():
    """Schema-v2 (Section 4) tools + nested_skills fingerprint chips render per row."""
    text, _ = _render(_verified_board_fixture())
    low = text.lower()
    # tools count/list and nested_skills count/list surfaced as chips.
    assert "tool" in low, "tools fingerprint chip not rendered"
    assert "nested" in low, "nested_skills fingerprint chip not rendered"
    # At least one concrete schema-v2 value is visible.
    assert "3 tools" in low or "bash" in low, "tools chip has no substance"
    assert "2 nested" in low or "gstack:land" in low, "nested_skills chip has no substance"


def test_board_shows_budget_column():
    """Section 4 BudgetVector (tokens / tool-calls / wall-time) renders per row."""
    text, _ = _render(_verified_board_fixture())
    low = text.lower()
    assert "token" in low, "budget tokens not rendered"
    assert "tool" in low, "budget tool-calls not rendered"
    # wall-time surfaced (seconds). Accept a formatted duration.
    assert "512" in text, "budget wall-time not rendered"
    # A concrete token figure must be visible (formatted with or without a separator).
    assert "128,000" in text or "128000" in text or "128k" in low, "token figure not rendered"


def test_board_shows_unknown_ledger():
    """The unknown[] ledger is visible in the rendered DOM (an honesty affordance,
    not buried in a collapsed drawer)."""
    text, _ = _render(_verified_board_fixture())
    low = text.lower()
    assert "unknown" in low, "unknown[] ledger label not visible"
    # The concrete ledger entry must render (field + reason), visible without interaction.
    assert "agents" in low, "unknown[] entry field not visible"
    assert "not_readable" in low or "not readable" in low, "unknown[] entry reason not visible"


def test_board_shows_verified_banner():
    """A verified board shows the positive integrity/verified banner — the counterpart
    to Section 6's unranked integrity gate."""
    text, _ = _render(_verified_board_fixture())
    low = text.lower()
    assert "verified" in low, "verified banner not rendered on a verified board"
    assert "integrity" in low, "integrity framing absent from verified banner"
    # It must be the POSITIVE banner, never the gated reframe.
    assert "not a ranked number" not in low, "verified board leaked the unranked reframe"


def test_board_lift_is_headline_not_theta():
    """LIFT must be the lead metric: it appears before / above the secondary bundle theta
    in the rendered DOM."""
    text, _ = _render(_verified_board_fixture())
    low = text.lower()
    assert "lift" in low and "theta" in low, "both lift and theta must render"
    assert low.index("lift") < low.index("theta"), "lift must lead theta as the headline metric"
    # The lift point estimate precedes the theta value in document order for row 1.
    assert text.index("+0.62") < text.index("0.28"), "row 1 lift must render above its theta"


def test_verified_board_end_to_end_real_data(tmp_path):
    """End-to-end: the SERVER builds a verified board from real artifacts (demo store +
    Section-5.5 lift results + Section-4 budgets threaded through build_leaderboard_doc),
    it validates against the locked schema, and every honesty affordance renders in the
    live DOM through real Chromium. This exercises the real data flow, not an injected
    fixture — the counterpart to scripts/screenshot_verified_board.py.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from screenshot_verified_board import _verified_board_doc
    from atv_bench.leaderboard import validate_leaderboard

    doc = _verified_board_doc(str(tmp_path / "store"), "2026-07-20T12:00:00Z")
    validate_leaderboard(doc)
    # The server actually populated lift/theta/budget/tools/nested from real inputs.
    top = doc["rows"][0]
    assert "lift" in top and "lift_ci" in top and "theta" in top, "server did not thread lift"
    assert "budget" in top and top["budget"]["tokens"], "server did not thread budget"
    assert top["details"]["tools"] and top["details"]["nested_skills"], "no schema-v2 chips"

    text, _ = _render(doc)
    low = text.lower()
    for needle in ("lift", "verified", "integrity", "unknown", "token", "nested", "theta"):
        assert needle in low, f"verified board missing affordance: {needle!r}"
    assert low.index("lift") < low.index("theta"), "lift must lead theta"
    assert "not a ranked number" not in low, "verified board leaked the unranked reframe"


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
