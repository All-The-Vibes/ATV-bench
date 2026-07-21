"""Capture browser screenshots of the live Tron match (Act 2 feed + Act 3 board).

Starts the real SSE live-match server (`atv_bench.arena.live_server.serve_live_match`,
open_browser=False so it runs headless), drives a real Chromium via Playwright to the
match page, lets the deterministic two-bot match play to completion, and screenshots:

  - live_match_midgame.png  — the Tron feed mid-match (both trails on the canvas)
  - live_match_result.png   — the terminal result + settled leaderboard board (Act 3)

This is genuine browser execution of the shipped demo surface, not a mock: the frames
come from the same engine/referee the arena uses to adjudicate.

Usage: python scripts/capture_live_match.py [out_dir]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from atv_bench.arena import live_server

ROOT = Path(__file__).resolve().parent.parent
BOTS = ROOT / "src" / "atv_bench" / "arena" / "sample_bots"


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]).resolve() if argv else (ROOT / "docs" / "proof" / "live-browser")
    out_dir.mkdir(parents=True, exist_ok=True)

    a_bot = str(BOTS / "greedy_survivor.py")
    b_bot = str(BOTS / "wall_hugger.py")

    # Slug-valid identities so the settled board ranks them (invalid names quarantine).
    # Headless server: starts, returns immediately (does not block, no system browser).
    srv = live_server.serve_live_match(
        a_bot=a_bot, b_bot=b_bot,
        a_name="greedy-harness", b_name="wallhugger-bare",
        seed=7, turn_delay=0.22, open_browser=False, verified=True,
    )
    url = f"{srv.url}/"
    print(f"live server up at {url}", flush=True)

    shots: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 980})
            page.goto(url, wait_until="domcontentloaded")
            # Wait until the feed is connected and a few turns have painted trails.
            page.wait_for_function(
                "() => { const h = document.getElementById('hud');"
                " return h && !/Connecting/.test(h.textContent); }",
                timeout=30_000,
            )
            time.sleep(2.5)  # let both trails extend across the canvas
            mid = out_dir / "live_match_midgame.png"
            page.screenshot(path=str(mid), full_page=True)
            shots.append(str(mid))
            print(f"captured {mid}  (hud={page.locator('#hud').inner_text()[:60]!r})", flush=True)
            # Wait for the terminal result banner + settled leaderboard board.
            page.wait_for_selector("#board.show", timeout=60_000)
            page.wait_for_function(
                "() => { const b = document.getElementById('banner');"
                " return b && b.textContent.trim().length > 0; }",
                timeout=60_000,
            )
            time.sleep(1.0)
            res = out_dir / "live_match_result.png"
            page.screenshot(path=str(res), full_page=True)
            shots.append(str(res))
            print(f"captured {res}  (banner={page.locator('#banner').inner_text()[:60]!r})", flush=True)
            browser.close()
    finally:
        srv.stop()

    print(f"\n=== captured {len(shots)} screenshots into {out_dir}", flush=True)
    return 0 if len(shots) == 2 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
