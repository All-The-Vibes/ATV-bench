"""Capture real screenshots of live.html in each of the three reviewed states.

Reuses the smoke test's fixture builders and the window.__LIVE_FIXTURE__
injection path so the page renders exactly as it does under test — then waits
for the render before shooting. Blank screenshots => the page is broken, which
is the whole point of a visual gate.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_live_html_smoke as S  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path("/tmp/liveview-shots")
OUT.mkdir(parents=True, exist_ok=True)
LIVE_VIEW = S.LIVE_VIEW

# Build a landed-round file payload from the smoke test's real ants fixture so
# mid/complete states show an actual canvas, not an empty stage.
ants_file = S._ants_file()


def _running(game: str):
    # A strip mid-match: round 0 landed (harness win = blue), 1 current, 2 pending.
    fp = ants_file
    st = S._running_status(fp, "match_0_round_0.json")
    st["rounds"] = [
        {"round": 0, "status": "landed", "winner": "claude-code", "color": "a", "turn": 40},
        {"round": 1, "status": "current", "winner": None, "turn": 12},
        {"round": 2, "status": "pending", "winner": None, "turn": 0},
    ]
    st["score"] = {"a": 1, "b": 0}
    return S._fixture(st, {"match_0_round_0.json": fp})


def _shot(page, fixture, name):
    page.add_init_script(f"window.__LIVE_FIXTURE__ = {json.dumps(fixture)};")
    page.goto(LIVE_VIEW.as_uri())
    page.wait_for_timeout(700)
    txt = page.inner_text("body")
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print(f"{name}: {len(txt)} chars body text; strip chips="
          f"{page.eval_on_selector_all('.round-strip .chip', 'els => els.length')}")


def main():
    game = "ants"
    empty = S._fixture(S._empty_status(game), {})
    running = _running(game)
    complete_fp = ants_file
    complete = S._fixture(S._complete_status(complete_fp, "match_0_round_0.json"),
                          {"match_0_round_0.json": complete_fp})
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for fx, name in [(empty, "empty"), (running, "mid-round"), (complete, "complete")]:
            page = b.new_page()
            _shot(page, fx, name)
            page.close()
        b.close()


if __name__ == "__main__":
    main()
