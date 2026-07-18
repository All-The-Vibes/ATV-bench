# Demo E2E Fix Plan — Two-Harness Head-to-Head Tron + Live Feed + Insights

## Demo narrative (what the user shows tomorrow)
1. Select two harnesses to compete (ATV-StarterKit = claude-code, ATV-Phoenix = copilot-cli).
2. Watch a **live Tron game feed** of the two going head-to-head.
3. Show the **leaderboard with insights** from the gstack plan.

## E2E audit results (tested against current `main`, PR8 merged; PR9/PR10 still open)
| Act | Status | Gap |
|-----|--------|-----|
| 1. Select two harnesses | ✅ works | `atv-bench harnesses`/`games` list them |
| 2. Head-to-head Tron | ⚠️ engine supports it, no CLI | `run_match(source_a, source_b)` + `TronEngine` already play two arbitrary bots; arena entrypoint only plays bot-vs-anchor. No command to pit two selected harnesses. |
| 3. Live game feed | ❌ missing | `run_match` returns one final JSON line; no turn-by-turn rendering. |
| 4. Leaderboard | ✅ renders | `board --demo` shows ranked rows + fingerprint chips. |
| 4b. Insights | ❌ missing | No insights panel tying fingerprint tags → ranking. |

Baseline: 424 hermetic tests pass. No regressions to introduce.

## Fixes (all TDD, hermetic, no Docker/live needed)

### F1 — `arena/render.py`: pure ASCII frame renderer
`render_frame(state, engine, *, label_a, label_b) -> str`. Deterministic. Renders board
grid with both trails/heads + a turn/score header. Pure, no I/O. **Tests first.**

### F2 — `run_match(..., observer=None)` streaming hook
Add optional `observer(state)` callback invoked with each GameState (initial + each tick).
Backward compatible (default None = current behavior). **Tests: observer receives full
turn sequence; absence unchanged.**

### F3 — CLI `demo-match` command
`atv-bench demo-match [--a-bot PATH] [--b-bot PATH] [--a-name] [--b-name] [--live/--no-live] [--board/--no-board]`
- Runs two local bots head-to-head via `run_match` with a live frame observer (throttled;
  `--no-live` for tests/CI = no sleeps).
- Ships two bundled sample bots (greedy survivors) so the demo works with zero args.
- After the match, records the result into a temp store, builds the board, prints the
  ranked table + an **insights** summary. `--no-board` skips for a pure match view.
- **Tests via typer CliRunner with `--no-live --no-board`**: exit 0, prints frames +
  outcome; with `--board`: prints leaderboard + insights lines.

### F4 — Insights generator `leaderboard.py::build_insights(rows) -> list[str]`
Pure heuristic insights from board rows (e.g. "gstack harnesses average +X ELO",
"top harness runs N skills"). **Tests first** on synthetic rows.

## Verification
- `uv run pytest -m "not live and not integration"` all green (424 + new).
- Run `atv-bench demo-match --no-live` in CLI; screenshot board via agent-browser.
- Confirm no CI workflow breakage (hermetic job only touched by new tests).
