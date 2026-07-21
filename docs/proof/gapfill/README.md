# Gap-fill e2e proof — porting PR16's data-science rigor onto PR17's spine

This directory holds the real (non-fixture) evidence that the gap-fill work runs end-to-end.

## `verified_board_with_gates.png`

A **real browser screenshot** (Playwright → Chromium, 1280×1400) of the verified
leaderboard, produced by `scripts/screenshot_verified_board.py`. The script:

1. builds a verified board from the demo store with Section-5.5 lift/theta + Section-4
   budgets threaded in,
2. computes the **new** gap-fill numbers from the *real* modules — `gates.evaluate_quality_gates`,
   `stats.direction_stability`, `gates.decide_contrast` — and threads them onto the doc as a
   schema-validated `quality_gates` block (verdict `A_wins`, `trust_tier=attested`,
   `direction_stability=1.0`),
3. serves the board over http, opens it in Chromium, asserts the DOM affordances render
   (lift headline, fingerprint chips, budget vector, unknown[] ledger, verified banner) with
   **zero JS errors**, and writes the PNG.

It fails closed: a board that does not render the affordances aborts the screenshot.

## `bundle_reproduce.json`

Output of the **G7 content-addressed bundle + offline reproduce** path:

- `content_id` = sha256 over canonical JSON,
- `verify_intact = true` — `verify_bundle` recomputed the id **and** re-ran `compute_lift`
  offline, and the published scalars matched,
- `verify_tampered = false` — a 1-character mutation of the `content_id` is rejected,
- G9 fail-closed defaults: `trust_tier = local-self-attested`, `track = league`,
  `rankable = false`.

## Reproduce

```bash
source .venv/bin/activate
python scripts/screenshot_verified_board.py --out docs/proof/gapfill/verified_board_with_gates.png
python -m pytest tests/test_gates.py tests/test_lift_clustered.py tests/test_stats.py \
                 tests/test_scheduler.py tests/test_bundle.py tests/test_leaderboard_schema.py -q
```
