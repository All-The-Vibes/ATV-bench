# atv-quickstart — TDD Implementation Proof

Evidence for the 6-phase `atv-bench quickstart` experience. All work done TDD-first
(RED → GREEN) in the `feat/quickstart` worktree, with every command sandbox-verified.

## Phase status

| Phase | What | Status | Tests | Evidence |
|---|---|---|---|---|
| 0 | `atv-bench quickstart` subcommand + graceful degradation | ✅ (branch) | `test_quickstart_cli.py` | `quickstart_cli_run.png` |
| 1 | Keyboard harness dropdown (questionary) — Copilot CLI / Codex / Claude Code | ✅ **new** | `test_harness_selection.py` (9) | `phase1_tests.png` |
| 2 | 1–3 game bot-build & compete orchestration | ✅ (branch) | `test_quickstart_engine.py` (16) | `quickstart_cli_run.png` |
| 3 | Persistent corpus + ELO + local leaderboard | ✅ (branch) | `test_viewer.py`, engine | `scorecard_browser.png`, `board_browser.png` |
| 4 | Live browser playback (SSE) | ✅ (branch) | `test_viewer.py` (15) | `live_playback_browser.png` |
| 5 | Terminal summary (lift + per-game + gate verdict) | ✅ (branch) | `test_quickstart_cli.py` | `quickstart_cli_run.png` |
| 6 | ATV-BENCH gold-medal first-run banner | ✅ **new** | `test_banner.py` (9) | `phase6_tests.png`, `phase6_banner.png` |

**Net-new this session:** Phase 1 (`harness_selection.py`) and Phase 6 (`banner.py`), each
TDD-first. Phases 0/2/3/4/5 were already implemented on the `feat/quickstart` branch (a
santa-loop-verified POC carrying the full scientific engine); this session verified them green
and sandbox-ran the real commands.

## Test results

- Phase 1 + Phase 6 + CLI wiring: **all green** (`phase1_tests.png`, `phase6_tests.png`)
- Combined phase suites: **80 passed** (`all_phases_summary.png`)
- Full hermetic suite: **1049 passed, 17 skipped, 0 failed**

## Sandbox command verification

- `atv-bench quickstart --help` — command registered under the single `atv-bench` binary.
- `atv-bench quickstart --harness claude-code --home <fake> --model ... --game lightcycles
  --game chess --repeats 3 --store <tmp> --yes` — ran end-to-end, EXIT 0, produced:
  `matches.jsonl`, `rating_matches.jsonl`, `scorecard.html`, `_board/index.html`,
  `leaderboard.json`, `quickstart_result.json`. (`quickstart_cli_run.png`)
- Harness dropdown: activates when >1 harness detected + TTY; explicit `--harness` and
  `--yes`/`--json`/no-TTY skip the TUI (fail-closed on cancel).
- Banner: shows once on first run, writes `~/.atv-bench/.banner_shown_v1`, silent thereafter;
  suppressed in `--json` and non-TTY output (verified no contamination). (`phase6_banner.png`)
- Live SSE playback: `serve_live_match(open_browser=False)` served a real match page; browser
  loaded the live canvas and transitioned to the Act-3 leaderboard. (`live_playback_browser.png`)

## Decisions honored (user-confirmed)

1. `atv-bench quickstart` **subcommand** (single binary) — not a separate `atv-quickstart` script.
2. Default **attempts live**, fails with actionable hint; demo behind `--fallback-to-demo`.
3. Default **3-game** quick trio, results labeled **PROVISIONAL** (below sufficiency gate).
4. `questionary` + `rich` added to **base** deps, **lazy-imported**.
