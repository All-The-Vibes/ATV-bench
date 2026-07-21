"""Build a VERIFIED board, serve it over http, drive it in a real browser, screenshot it.

Section 8: the verified board surface carries the harness-LIFT headline + every honesty
affordance (fingerprint chips, budget vector, unknown[] ledger, verified banner). This
helper builds such a board from the demo store (with lift/theta/budget threaded in),
serves it locally (fetch() needs http, not file://), opens it in Chromium via Playwright,
asserts the affordances render in the DOM, and writes a PNG.

Usage:
    python scripts/screenshot_verified_board.py [--out screenshots/verified_board.png]

Doubles as a live smoke test: it fails if the verified board does not render the
lift-headline / chips / budget / ledger / banner, so `atv-bench board` regressions are
caught against a real browser, not just fixture-injection unit tests.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _verified_board_doc(store_dir: str, updated_at: str) -> dict:
    """A verified demo board with Section-5.5 lift/theta + Section-4 budgets threaded in."""
    from atv_bench.demo import build_demo_store
    from atv_bench.match_record import BudgetVector
    from atv_bench.leaderboard import build_leaderboard_doc
    from atv_bench.store import LeagueStore, _to_match_result

    build_demo_store(store_dir)
    store = LeagueStore(store_dir)
    submissions, _ = store.load_submissions_quarantined()
    match_records, _ = store.load_matches_quarantined()
    matches = []
    seen: set[str] = set()
    for m in match_records:
        mid = m.get("match_id")
        if isinstance(mid, str) and mid:
            if mid in seen:
                continue
            seen.add(mid)
        try:
            matches.append(_to_match_result(m))
        except (ValueError, KeyError):
            continue

    # Section-5.5 lift results per harness name (over the bare model), with CI + theta.
    class _Lift:
        def __init__(self, lift, lo, hi, theta):
            self.lift, self.lo, self.hi, self.theta = lift, lo, hi, theta

    lifts = {
        "claude-code": _Lift(0.62, 0.41, 0.83, 0.28),
        "copilot-cli": _Lift(0.34, 0.12, 0.56, 0.11),
    }
    # Section-4 budget vectors, keyed by identity.
    budgets = {
        "ada-demo": BudgetVector(tokens=128000, tool_calls=240, wall_time_s=512.0),
        "grace-demo": BudgetVector(tokens=96000, tool_calls=160, wall_time_s=380.0),
        "linus-demo": BudgetVector(tokens=64000, tool_calls=90, wall_time_s=210.0),
    }
    # G5/G6/G9 gap-fill: the publication-gate report + winner verdict + trust tier, computed
    # by the real gates module so the board proves the NEW gated/clustered numbers render.
    from atv_bench.gates import (
        GateThresholds, evaluate_quality_gates, decide_contrast,
    )
    from atv_bench.stats import direction_stability
    import numpy as _np
    # A demo stats bundle the gates evaluate against (all-clear synthetic corpus).
    _stats = {"infrastructure_error_rate": 0.0, "eligible_n": 60,
              "min_trials_per_cell": 6, "referee_nondeterminism_rate": 0.0}
    _gate_report = evaluate_quality_gates(_stats, thresholds=GateThresholds())
    # Winner verdict for the top contrast (claude-code lift 0.62 CI [0.41,0.83] over bare).
    _draws = list(_np.random.default_rng(0).normal(0.62, 0.10, 2000))
    _ds = direction_stability(_draws)
    _verdict = decide_contrast(diff=0.62, lo=0.41, hi=0.83, margin=0.05,
                               direction_stability=_ds, n_policies=2)
    quality_gates = {
        "passed": _gate_report.passed,
        "publishable": _gate_report.passed,
        "trust_tier": "attested" if _gate_report.passed else "local-self-attested",
        "rankable": _gate_report.passed,
        "failures": [dict(f) for f in _gate_report.failures],
        "top_contrast_verdict": _verdict["verdict"],
        "direction_stability": round(_ds, 4),
    }
    return build_leaderboard_doc(
        matches, submissions, updated_at=updated_at, verified=True,
        lifts=lifts, budgets=budgets, quality_gates=quality_gates,
    )


def _serve(site: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}/index.html"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "screenshots" / "verified_board.png"))
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    from atv_bench.leaderboard import validate_leaderboard
    from atv_bench.publish import _find_view_html

    store_dir = tempfile.mkdtemp(prefix="atv-verified-store-")
    site = Path(tempfile.mkdtemp(prefix="atv-verified-site-"))
    doc = _verified_board_doc(store_dir, "2026-07-20T12:00:00Z")
    validate_leaderboard(doc)
    (site / "leaderboard.json").write_text(json.dumps(doc, indent=2))
    view = _find_view_html()
    assert view is not None, "viewer HTML not found"
    (site / "index.html").write_text(view.read_text())

    httpd, url = _serve(site)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_timeout(250)
        text = page.inner_text("body").lower()
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=args.out, full_page=True)
        browser.close()
    httpd.shutdown()

    assert not errors, f"JS errors on the verified board: {errors}"
    for needle in ("lift", "verified", "integrity", "unknown", "token", "nested", "theta"):
        assert needle in text, f"verified board missing affordance: {needle!r}"
    print(f"✓ verified board rendered lift-headline + chips + budget + ledger + banner")
    print(f"  screenshot: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
