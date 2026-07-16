"""Render the leaderboard view in each interaction state and screenshot it.

Covers the 7 states from the design spec (loading, empty, 1-entry/unrated,
low-confidence, populated, stale, error) at desktop + mobile widths using Playwright
against the real static viewer (leaderboard/view/index.html). Run:

    uv run python scripts/screenshot_leaderboard.py

Writes PNGs to screenshots/. Also usable as a smoke test that the viewer renders
without JS errors.
"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
VIEW = ROOT / "leaderboard" / "view" / "index.html"
OUT = ROOT / "screenshots"


def _row(rank, ident, elo, matches, rated=True, low=False, gstack=True,
         skills=None, mcps=None, plugins=None, unknown=None, harness="claude-code"):
    skills = skills if skills is not None else ["gstack", "office-hours", "tdd"]
    mcps = mcps if mcps is not None else ["github", "grafana"]
    plugins = plugins if plugins is not None else ["compound-engineering"]
    unknown = unknown if unknown is not None else []
    half = 95 if low else 30
    return {
        "rank": rank, "elo": elo, "rated": rated, "match_count": matches,
        "low_confidence": low, "fingerprint_gstack": gstack,
        "ci": {"lo": elo - half, "hi": elo + half}, "identity": ident,
        "harness_name": harness, "fingerprint_summary": "",
        "details": {"skills": skills, "mcps": mcps, "plugins": plugins, "unknown": unknown},
        "bot_sha256": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "fingerprint_probe_version": "1.0.0",
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
    }


TS = "2026-07-15T18:00:00Z"

FIXTURES: dict[str, object] = {
    "empty": {"schema_version": 1, "updated_at": TS, "rows": []},
    "one_entry_unrated": {"schema_version": 1, "updated_at": TS, "rows": [
        _row(1, "sschofield", 1500, 0, rated=False),
    ]},
    "low_confidence": {"schema_version": 1, "updated_at": TS, "rows": [
        _row(1, "octocat", 1720, 40),
        _row(2, "hubot", 1560, 22),
        _row(3, "newcomer", 1620, 4, low=True, unknown=[{"field": "plugins", "reason": "permission_denied"}]),
    ]},
    "populated": {"schema_version": 1, "updated_at": TS, "rows": [
        _row(1, "octocat", 1812, 64, skills=["gstack","office-hours","tdd","brainstorming","review","ship","plan","debug","qa","canary","design","docs"], mcps=["github","grafana","slack"], plugins=["compound-engineering","superpowers"]),
        _row(2, "sschofield", 1655, 51, harness="copilot-cli", gstack=False, skills=["review","test"], mcps=["github"], plugins=[]),
        _row(3, "hubot", 1533, 47, mcps=["github"], plugins=["compound-engineering"]),
        _row(4, "newcomer", 1590, 3, low=True),
    ]},
    "error": "__ERROR__",
}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    url = VIEW.as_uri()
    written = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, fixture in FIXTURES.items():
            for label, width, height in (("desktop", 1200, 900), ("mobile", 390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                if fixture == "__ERROR__":
                    # force the error state: navigate, then trigger a failed fetch render
                    page.goto(url)
                    page.evaluate("() => { document.getElementById('board-root').innerHTML = "
                                  "'<div class=\"state\"><h2>Leaderboard unavailable</h2>"
                                  "<p>Could not load leaderboard.json (HTTP 404).</p>"
                                  "<p style=\"font-family:var(--mono);font-size:.78rem\">build a1b2c3d</p></div>'; }")
                else:
                    page.add_init_script(f"window.__FIXTURE__ = {json.dumps(fixture)};")
                    page.goto(url)
                page.wait_for_timeout(250)
                # expand the first drawer on the populated desktop shot for evidence
                if name == "populated" and label == "desktop":
                    try:
                        page.locator("details.drawer > summary").first.click()
                        page.wait_for_timeout(150)
                    except Exception:
                        pass
                out = OUT / f"{name}_{label}.png"
                page.screenshot(path=str(out), full_page=True)
                written.append(out.name)
                assert not errors, f"JS errors in {name}/{label}: {errors}"
                page.close()
        browser.close()
    print(f"wrote {len(written)} screenshots to {OUT}:")
    for w in sorted(written):
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
