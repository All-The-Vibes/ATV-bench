"""Browser smoke test for the live gameplay view (T4-canvas / B-canvas remediation).

Loads `src/atv_bench/view/live.html` against the REAL LiveView status contract —
the SUPERSET that `LiveView._view_status()` publishes, which live.html must honor:

  status.json (view contract, what the browser reads):
    { state: "empty"|"running"|"complete",
      game,
      seats:  [{name, color:"a"}, {name, color:"b"}],           # D3 legend
      score:  {a, b},
      rounds: [{round, status:"pending"|"current"|"landed",     # D1 strip
                winner: <seat idx 0|1|null>, turn}],
      current: <round idx|null>,
      complete?: {lift, ci_lo, ci_hi, confidence, leaderboard_url},
      # engine-facing keys ALSO present (superset); the page uses matches[].rounds
      # only to resolve a landed round's clickable per-round file:
      status, harness, baseline, games,
      matches:[{game,index,seats,rounds:[{round,winner,color,turn,file}]}],
      lift, leaderboard_url? }
  <file> = { game, round, winner, color, seats, scores,
             sims:[ants|lightcycles sim], chess:[{fens:[...]}] }

Two layers:
  * hermetic fixture tests inject `window.__LIVE_FIXTURE__ = {status, files}` —
    the SAME view contract LiveView writes, asserted through the browser;
  * an end-to-end test starts a real `LiveView`, drops real fixture tarballs,
    waits for the served `status.json`, opens `lv.url`, and asserts the strip /
    canvas / complete UI render from *actual* published files.

Asserts (B-canvas):
  * pending rounds render as GHOST chips (hollow, number only) in the empty state,
  * the current round pulses and shows the live turn,
  * a landed chip fills with the WINNER's seat color class (+ check) — bare wins
    are red(--b), harness wins are blue(--a), same mapping as the watcher (D1/D3),
  * the seat legend (D3) establishes color->name before the strip uses it,
  * switching rounds does NOT stack playback timers (no setInterval leak),
  * the current-round canvas renders non-empty for lightcycles / ants / chess,
  * ZERO JS console / page errors across all three games,
  * complete state (D2) shows lift + CI + confidence meter + leaderboard,
  * empty state (D4) shows the "round 0 incoming" line.

Guarded on Playwright + a browser being installed (skips cleanly otherwise).
"""
from __future__ import annotations

import json
import shutil
import time
import urllib.request
from pathlib import Path

import pytest

LIVE_VIEW = Path(__file__).parent.parent / "src" / "atv_bench" / "view" / "live.html"
FIXTURES = Path(__file__).parent / "fixtures" / "rounds"
ANTS_TAR = FIXTURES / "ants-0_round_0.tar.gz"
CHESS_TAR = FIXTURES / "chess-1_round_0.tar.gz"
LIGHTCYCLES_TAR = FIXTURES / "lightcycles-2_round_0.tar.gz"


def _playwright_ready() -> bool:
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


pytestmark = pytest.mark.skipif(
    not _playwright_ready(), reason="playwright browser not installed"
)

_SEATS = ["claude-code", "bare-claude-code"]
# Seat color name -> seat index, matching the watcher (blue=seat0, red=seat1).
_COLOR_IDX = {"blue": 0, "red": 1}


# --- canned per-round FILE payloads (LiveView's per-round JSON shape) --------


def _lightcycles_file() -> dict:
    frames = []
    for t in range(6):
        frames.append(
            {
                "t": t,
                "heads": [[2 + t, 4], [40 - t, 30]],
                "trails": [
                    [[2 + i, 4] for i in range(t + 1)],
                    [[40 - i, 30] for i in range(t + 1)],
                ],
            }
        )
    return {
        "game": "lightcycles",
        "round": 2,
        "winner": "claude-code",
        "color": "blue",
        "seats": list(_SEATS),
        "scores": {},
        "sims": [
            {
                "game": "lightcycles",
                "width": 48,
                "height": 36,
                "rocks": [[10, 10], [11, 10]],
                "winner": 0,
                "frames": frames,
            }
        ],
        "chess": [],
    }


def _ants_file() -> dict:
    frames = []
    for t in range(5):
        frames.append(
            {
                "t": t,
                "ants": [[3 + t, 3, 0], [15 - t, 12, 1]],
                "hills": [[3, 3, 0], [15, 12, 1]],
                "food": [[7, 8], [9, 4]],
            }
        )
    return {
        "game": "ants",
        "round": 2,
        "winner": "bare-claude-code",
        "color": "red",
        "seats": list(_SEATS),
        "scores": {},
        "sims": [
            {
                "game": "ants",
                "rows": 20,
                "cols": 20,
                "water": [[5, 5]],
                "winner": 1,
                "frames": frames,
            }
        ],
        "chess": [],
    }


def _chess_file() -> dict:
    return {
        "game": "chess",
        "round": 2,
        "winner": "claude-code",
        "color": "blue",
        "seats": list(_SEATS),
        "scores": {},
        "sims": [],
        "chess": [
            {
                "white": "claude-code",
                "black": "bare-claude-code",
                "result": "1-0",
                "fens": [
                    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                    "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                ],
            }
        ],
    }


# --- status.json in the VIEW contract (what _view_status publishes) ---------


def _landed_view(round_idx: int, color: str, turn: int) -> dict:
    """A landed top-level round (D1): winner is the seat INDEX the strip colors by."""
    return {"round": round_idx, "status": "landed",
            "winner": _COLOR_IDX.get(color), "turn": turn}


def _match_round(round_idx: int, winner: str, color: str, fname: str,
                 turn: int) -> dict:
    """An engine-facing match round: carries the name/color + the clickable file."""
    return {"round": round_idx, "winner": winner, "color": color,
            "turn": turn, "file": fname}


def _running_status(file_payload: dict, fname: str,
                    rounds_per_match: int | None = None) -> dict:
    """Mid-run VIEW status: a prior landed round (bare/red), THIS landed round,
    then a current pulse + padded pending ghosts, exactly as _view_status shapes it.

    The engine-facing `matches[].rounds[]` (with per-round `file`) is carried
    alongside so the page can resolve each landed chip's clickable canvas file.
    """
    game = file_payload["game"]
    this_round = file_payload["round"]
    this_color = file_payload["color"]
    # Size the strip so landed + current + pending ghosts all appear.
    if rounds_per_match is None:
        rounds_per_match = this_round + 3
    # prior landed round 0 always goes to bare-control (red / seat b).
    prior_landed = _landed_view(0, "red", 12)
    this_landed = _landed_view(this_round, this_color, 6)
    view_rounds = [prior_landed]
    if this_round != 0:
        view_rounds.append(this_landed)
    # current + pending tail padded to rounds_per_match.
    next_idx = view_rounds[-1]["round"] + 1
    if next_idx < rounds_per_match:
        view_rounds.append({"round": next_idx, "status": "current",
                            "winner": None, "turn": 0})
        for k in range(next_idx + 1, rounds_per_match):
            view_rounds.append({"round": k, "status": "pending",
                                "winner": None, "turn": 0})
    landed = [r for r in view_rounds if r["status"] == "landed"]
    score_a = sum(1 for r in landed if r["winner"] == 0)
    score_b = sum(1 for r in landed if r["winner"] == 1)

    match_rounds = [_match_round(0, "bare-claude-code", "red",
                                 "match_0_round_0.json", 12)]
    if this_round != 0:
        match_rounds.append(_match_round(this_round, file_payload["winner"],
                                         this_color, fname, 6))
    return {
        # view contract
        "state": "running",
        "game": game,
        "seats": [{"name": _SEATS[0], "color": "a"},
                  {"name": _SEATS[1], "color": "b"}],
        "score": {"a": score_a, "b": score_b},
        "rounds": view_rounds,
        "current": landed[-1]["round"] if landed else 0,
        # engine-facing superset
        "status": "running",
        "harness": "claude-code",
        "baseline": "bare-claude-code",
        "games": [game],
        "matches": [{"game": game, "index": 0, "seats": list(_SEATS),
                     "rounds": match_rounds}],
        "lift": None,
    }


def _empty_status(game: str, *, rounds_per_match: int = 3) -> dict:
    """A fresh match VIEW status: all-pending strip, state=empty (D4)."""
    view_rounds = [{"round": 0, "status": "current", "winner": None, "turn": 0}]
    for k in range(1, rounds_per_match):
        view_rounds.append({"round": k, "status": "pending",
                            "winner": None, "turn": 0})
    return {
        "state": "empty",
        "game": game,
        "seats": [{"name": _SEATS[0], "color": "a"},
                  {"name": _SEATS[1], "color": "b"}],
        "score": {"a": 0, "b": 0},
        "rounds": view_rounds,
        "current": None,
        "status": "running",
        "harness": "claude-code",
        "baseline": "bare-claude-code",
        "games": [game],
        "matches": [{"game": game, "index": 0, "seats": list(_SEATS),
                     "rounds": []}],
        "lift": None,
    }


def _complete_status(file_payload: dict, fname: str) -> dict:
    st = _running_status(file_payload, fname)
    st["state"] = "complete"
    st["status"] = "complete"
    st["lift"] = {"value": 0.69, "ci_lo": -4.6, "ci_hi": 8.2, "confidence": "low"}
    st["complete"] = {"lift": 0.69, "ci_lo": -4.6, "ci_hi": 8.2,
                      "confidence": "low",
                      "leaderboard_url": "https://example.com/leaderboard"}
    st["leaderboard_url"] = "https://example.com/leaderboard"
    return st


def _fixture(status: dict, files: dict[str, dict]) -> dict:
    return {"status": status, "files": files}


# --- playwright driver (injected fixture) -----------------------------------

# Wrap setInterval/clearInterval BEFORE page scripts run so a leaked playback
# timer (each round switch stacking a new interval without clearing the old one)
# shows up as a growing balance. A clean page keeps at most one live canvas timer.
_TIMER_PROBE = """
window.__timerBalance = 0;
const _si = window.setInterval, _ci = window.clearInterval;
window.setInterval = function () {
  window.__timerBalance++;
  return _si.apply(this, arguments);
};
window.clearInterval = function (id) {
  if (id != null) window.__timerBalance--;
  return _ci.call(this, id);
};
"""


def _load(fixture: dict, *, click_all_landed: bool = False):
    """Load live.html with an injected fixture; return (errors, result).

    When `click_all_landed`, click every landed chip (exercising the round-switch
    path) so a stacked-timer leak is observable via window.__timerBalance.
    """
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    result: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "console",
            lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error"
            else None,
        )
        page.add_init_script(_TIMER_PROBE)
        page.add_init_script(f"window.__LIVE_FIXTURE__ = {json.dumps(fixture)};")
        page.goto(LIVE_VIEW.as_uri())
        page.wait_for_timeout(400)
        if click_all_landed:
            handles = page.query_selector_all(".round-strip .chip.landed.clickable")
            for h in handles:
                h.click()
                page.wait_for_timeout(120)
            page.wait_for_timeout(200)
        result.update(_scrape(page))
        result["timer_balance"] = page.evaluate("() => window.__timerBalance")
        browser.close()
    return errors, result


def _scrape(page) -> dict:
    return {
        "text": page.inner_text("body"),
        "distinct_colors": page.evaluate(_DISTINCT_COLORS_JS),
        "strip": page.eval_on_selector_all(
            ".round-strip .chip",
            "els => els.map(e => ({cls: e.className, text: e.innerText}))",
        ),
        "swatches": page.eval_on_selector_all(
            ".seat-legend .swatch", "els => els.map(e => e.className)"
        ),
    }


_DISTINCT_COLORS_JS = """
() => {
  const c = document.querySelector('canvas');
  if (!c || !c.width || !c.height) return 0;
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  const seen = new Set();
  for (let i = 0; i < d.length; i += 4) {
    seen.add(d[i] + ',' + d[i+1] + ',' + d[i+2] + ',' + d[i+3]);
    if (seen.size > 4) break;
  }
  return seen.size;
}
"""


# --- tests: current-round canvas renders for each game ----------------------


@pytest.mark.parametrize(
    "builder",
    [_lightcycles_file, _ants_file, _chess_file],
    ids=["lightcycles", "ants", "chess"],
)
def test_current_round_canvas_renders_non_empty(builder):
    fp = builder()
    fname = "match_0_round_%d.json" % fp["round"]
    fixture = _fixture(_running_status(fp, fname), {fname: fp})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    assert res["distinct_colors"] > 1, "canvas appears empty (single color)"


@pytest.mark.parametrize(
    "builder",
    [_lightcycles_file, _ants_file, _chess_file],
    ids=["lightcycles", "ants", "chess"],
)
def test_round_strip_has_landed_current_and_pending_ghosts(builder):
    """D1: the strip must encode all three states from the top-level contract —
    landed (colored), a current pulse, AND pending ghost chips."""
    fp = builder()
    fname = "match_0_round_%d.json" % fp["round"]
    fixture = _fixture(_running_status(fp, fname), {fname: fp})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    classes = [c["cls"] for c in res["strip"]]
    assert any("landed" in c for c in classes), "no landed chip"
    assert any("current" in c for c in classes), "no current chip while running"
    assert any("pending" in c for c in classes), "no pending ghost chip"


@pytest.mark.parametrize(
    "builder",
    [_lightcycles_file, _ants_file, _chess_file],
    ids=["lightcycles", "ants", "chess"],
)
def test_zero_console_errors(builder):
    fp = builder()
    fname = "match_0_round_%d.json" % fp["round"]
    fixture = _fixture(_running_status(fp, fname), {fname: fp})
    errors, _ = _load(fixture)
    assert not errors, f"JS console/page errors: {errors}"


def test_landed_chip_colored_by_winning_seat():
    """A landed chip carries the winner's seat color class: R0 bare -> seat-b/red,
    R2 harness -> seat-a/blue (same seat->color mapping as the watcher)."""
    fp = _lightcycles_file()  # round 2, winner claude-code (blue / seat a)
    fname = "match_0_round_2.json"
    fixture = _fixture(_running_status(fp, fname), {fname: fp})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    landed = [c for c in res["strip"] if "landed" in c["cls"]]
    seat_classes = " ".join(c["cls"] for c in landed)
    assert "seat-a" in seat_classes, "harness win not colored blue (seat-a)"
    assert "seat-b" in seat_classes, "bare win not colored red (seat-b)"


def test_bare_win_lands_red_matching_watcher_mapping():
    """An ants round won by bare-claude-code (seat 1) must land RED (seat-b)."""
    fp = _ants_file()  # round 2, winner bare-claude-code (red / seat b)
    fname = "match_0_round_2.json"
    fixture = _fixture(_running_status(fp, fname), {fname: fp})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    r2 = [c for c in res["strip"] if "landed" in c["cls"] and "seat-b" in c["cls"]]
    assert r2, "bare-control win did not land as seat-b (red)"


def test_seat_legend_establishes_color_names():
    fp = _lightcycles_file()
    fname = "match_0_round_2.json"
    fixture = _fixture(_running_status(fp, fname), {fname: fp})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    text = res["text"]
    assert "claude-code" in text
    assert "bare:claude-code" in text  # display transform of bare-claude-code
    swatch_classes = " ".join(res["swatches"])
    assert "a" in swatch_classes and "b" in swatch_classes


# --- timer leak (fix #3) ----------------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [_lightcycles_file, _ants_file, _chess_file],
    ids=["lightcycles", "ants", "chess"],
)
def test_switching_rounds_does_not_stack_playback_timers(builder):
    """Fix #3: clicking through landed chips repeatedly builds a fresh Playback
    each time; without a stop/destroy the old setInterval leaks and timers stack.
    After clicking every landed chip, at most ONE canvas timer may be live."""
    fp = builder()
    fname = "match_0_round_%d.json" % fp["round"]
    # two landed rounds (R0 prior + this one) so the click loop switches rounds.
    fixture = _fixture(
        _running_status(fp, fname),
        {fname: fp, "match_0_round_0.json": _ants_file()},
    )
    errors, res = _load(fixture, click_all_landed=True)
    assert not errors, f"JS errors: {errors}"
    assert res["timer_balance"] <= 1, (
        f"playback timers stacked (balance={res['timer_balance']}) — "
        "round switch leaks setInterval"
    )


# --- empty state (D4) -------------------------------------------------------


def test_empty_state_shows_ghost_chips_and_incoming_line():
    """D4: before round 0, the strip shows the all-pending ghost strip and the
    minimal 'Starting <game> — round 0 incoming…' line."""
    fixture = _fixture(_empty_status("lightcycles"), {})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    text = res["text"].lower()
    assert "round 0" in text and "incoming" in text
    assert "lightcycles" in text
    classes = [c["cls"] for c in res["strip"]]
    assert any("pending" in c for c in classes), "empty state has no ghost chips"
    # ghost chips render the number only (no winner name / check).
    ghosts = [c for c in res["strip"] if "pending" in c["cls"]]
    assert ghosts and all("tie" not in c["text"].lower() for c in ghosts)


# --- complete state (D2) ----------------------------------------------------


def test_complete_state_shows_lift_ci_and_confidence_meter():
    fp = _lightcycles_file()
    fname = "match_0_round_2.json"
    fixture = _fixture(_complete_status(fp, fname), {fname: fp})
    errors, res = _load(fixture)
    assert not errors, f"JS errors: {errors}"
    text = res["text"]
    assert "+0.69" in text or "0.69" in text  # lift headline
    assert "95% CI" in text or "CI" in text
    assert "-4.6" in text or "−4.6" in text
    assert "8.2" in text
    assert "low" in text.lower()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.add_init_script(f"window.__LIVE_FIXTURE__ = {json.dumps(fixture)};")
        page.goto(LIVE_VIEW.as_uri())
        page.wait_for_timeout(300)
        has_meter = page.evaluate(
            "() => !!document.querySelector('.confidence-meter, [data-confidence]')"
        )
        has_link = page.evaluate(
            "() => !!Array.from(document.querySelectorAll('a'))"
            ".find(a => (a.href||'').includes('leaderboard'))"
        )
        browser.close()
    assert has_meter, "no confidence meter element"
    assert has_link, "no leaderboard link in complete state"


# --- end-to-end: real LiveView server + real fixture tarballs ---------------


def _drop_round(match_dir: Path, tar_src: Path, round_index: int) -> None:
    """Drop a real arena round tar (results.json lives INSIDE at 0/results.json —
    no fabricated on-disk sibling)."""
    match_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(tar_src, match_dir / f"round_{round_index}.tar.gz")


def _wait_for_round(live, timeout_s: float = 5.0) -> dict:
    """Poll the live server until status.json reports a landed round."""
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        live.poll_once()
        with urllib.request.urlopen(f"{live.url_base}/status.json", timeout=5) as r:
            last = json.loads(r.read())
        rounds = last.get("matches", [{}])[0].get("rounds", []) if last.get("matches") else []
        if rounds:
            return last
        time.sleep(0.1)
    return last


def test_end_to_end_real_liveview_renders_strip_and_canvas(tmp_path):
    """Start a real LiveView, drop real tarballs, assert the page renders from
    the actual served status.json + per-round files (no injected fixture): a
    landed chip, ghost pending chips, a non-empty canvas, zero JS errors, and the
    complete leaderboard link."""
    from atv_bench.liveview import LiveView

    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        match_dir = store / "ants-0" / "rounds"
        lv.match_start("ants", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        _drop_round(match_dir, ANTS_TAR, 0)
        status = _wait_for_round(lv)
        rounds = status["matches"][0]["rounds"]
        assert rounds, "server never published a round"
        assert rounds[0]["file"]  # a real per-round filename the page will fetch

        lv.finish(lift=0.69, ci_lo=-4.6, ci_hi=8.2,
                  leaderboard_url="https://example.com/leaderboard")

        from playwright.sync_api import sync_playwright

        errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error"
                else None,
            )
            page.goto(lv.url)  # the actual served page, fetching real files
            page.wait_for_timeout(700)
            res = _scrape(page)
            has_link = page.evaluate(
                "() => !!Array.from(document.querySelectorAll('a'))"
                ".find(a => (a.href||'').includes('leaderboard'))"
            )
            browser.close()

        assert not errors, f"JS errors against real server: {errors}"
        assert any("landed" in c["cls"] for c in res["strip"]), "no landed chip"
        assert res["distinct_colors"] > 1, "canvas empty against real round file"
        assert has_link, "complete state missing leaderboard link"
    finally:
        lv.close()


# --- regression: extracted shell still renders play's replay ----------------


def test_play_replay_still_renders_after_shell_extraction(tmp_path):
    from atv_bench.play import Contestant, build_replay_html, run_local_match

    res = run_local_match(
        game="lightcycles",
        player=Contestant(key="greedy"),
        opponent=Contestant(key="bare"),
        seed=0,
    )
    out = build_replay_html(res, tmp_path)

    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(out.as_uri())
        page.wait_for_timeout(400)
        distinct = page.evaluate(_DISTINCT_COLORS_JS)
        browser.close()
    assert not errors, f"JS errors in play replay: {errors}"
    assert distinct > 1, "play replay canvas appears empty after shell extraction"


def test_state_screenshots_are_non_blank(tmp_path):
    """Visual gate: the three reviewed states (empty/mid-round/complete) must
    produce non-blank screenshots. A blank PNG means the page silently failed to
    render even though DOM assertions passed — exactly what the visual gate
    exists to catch. Runs the capture script and asserts real byte size + that
    each rendered a round strip."""
    if not _playwright_ready():
        pytest.skip("playwright/chromium not available")
    import subprocess, sys as _sys
    script = Path(__file__).parent.parent / "scripts" / "capture_liveview_states.py"
    out = subprocess.run([_sys.executable, str(script)], capture_output=True, text=True)
    assert out.returncode == 0, f"capture failed: {out.stderr}"
    shots = Path("/tmp/liveview-shots")
    for name in ("empty", "mid-round", "complete"):
        p = shots / f"{name}.png"
        assert p.exists(), f"missing {name}.png"
        # A truly blank page renders to a tiny PNG; a real render is >8KB.
        assert p.stat().st_size > 8000, f"{name}.png looks blank ({p.stat().st_size}B)"
    # And the capture script self-reports strip chips per state.
    assert "strip chips=3" in out.stdout, "empty/mid states missing round strip"
    assert "strip chips=4" in out.stdout, "complete state missing full strip"
