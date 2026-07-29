# atv-quickstart — Implementation Plan

> Target UX (user's words): install `atv-bench` with `uv`/`pip` per the README, run
> **`atv-quickstart`** → keyboard dropdown to pick the harness under test (Copilot CLI /
> Codex / Claude Code) → build the bot and compete over 1–3 games → **watch it live in the
> browser** → get an **ELO** score → get a **rank in the live leaderboard** → read a
> **brief terminal summary**. Plus a fancy **ATV-BENCH gold-medal banner** on install/first run.

## 1. Executive Summary

**Current state on `main`:** a single `atv-bench` entry point exposes `run --demo` (canned
replay), `doctor`, `fingerprint`, `board`, `rate`, `play`. The scoring/playback/leaderboard
primitives all exist but are wired only as **separate CLI verbs**:
- Live browser playback: `live_server.py` (SSE) + `view/live.html`, invoked today by `demo-match --browser`.
- ELO / ratings: `elo.py`, `rating.py`, `rate` command.
- Leaderboard + publish: `leaderboard.py`, `publish.build_site`, `store.py`, `board` command.
- Harness registry: `harnesses.HARNESSES` (`claude-code`, `copilot-cli`, `codex`) + `fingerprint/probe.py`.

**What's missing:** the orchestration layer that chains them into one command, plus two new
UX surfaces — the **keyboard harness picker** and the **gold-medal banner**. `questionary`
(the dropdown lib) is already a dependency but **unused**; `rich` is **not** yet a dependency.

**Important existing asset:** a `feat/quickstart` worktree (santa-loop-verified POC) already
contains `src/atv_bench/quickstart.py` (multi-game orchestrator), `interactive.py`
(`select_model` keyboard picker), `models.py` (model catalog), and 5 test files
(`test_quickstart_cli.py`, `test_quickstart_engine.py`, `test_interactive_select.py`,
`test_harness_lift.py`, `test_harness_agnostic.py`). It is **unmerged and drifted from `main`**.
Much of Phases 1–2 is therefore **port + rebase**, not build-from-scratch — but every ported
file must be re-tested against current `main`.

**The gap in one sentence:** the engine parts exist and a POC already stitches some of them;
quickstart productionizes that into `atv-quickstart` with a harness picker, live playback,
persistent ELO/rank, terminal summary, and a first-run banner.

### Resolved decisions (user-confirmed 2026-07-23)
1. **Entry point:** `atv-bench quickstart` **subcommand** (single binary, consistent with
   `fingerprint`/`run`/`board`). No separate `atv-quickstart` console script. README/docs
   advertise `atv-bench quickstart`.
2. **Live vs demo default:** bare `atv-bench quickstart` **attempts a live** harness-vs-baseline
   match; on missing Docker / `[run]` extra / auth it **exits with actionable guidance**. The
   canned/demo path requires explicit `--fallback-to-demo`.
3. **Default games:** **3 games**, terminal summary + board label results as **provisional**
   (below the ~30-match sufficiency gate); suggest `--repeats` / more games for a credible ELO.
4. **Base deps:** `questionary` + `rich` (+ prompt_toolkit) added to **base** deps,
   **lazy-imported** so non-interactive paths stay fast.

---

## 2. Phased Plan (each phase ships something runnable)

### Phase 0 — `atv-bench quickstart` subcommand + graceful degradation
*Shippable: `atv-bench quickstart --help` works; missing `[run]`/Docker degrades cleanly.*
- Add `quickstart` as a subcommand on the existing Typer `app` in `src/atv_bench/cli.py`
  (single binary — no separate console script). Delegates to shared orchestration in
  `src/atv_bench/quickstart.py`.
- Options: `--harness --model --games/--repeats --fallback-to-demo --store --yes --json`.
- Reuse `_probe_or_exit()`, `runner`/`preflight` fail-closed checks.
- Stable exit codes: `0` ok · `2` usage · `5` docker-missing · `9` codeclash-missing.
- **Default = attempt live**; on missing `[run]` extra (CodeClash ImportError) / Docker / auth,
  print the corrected reinstall guidance (`atv-bench[run] @ git+...` — see the run-extra fix)
  and exit `5`/`9`, unless `--fallback-to-demo` is passed.

### Phase 1 — Harness keyboard dropdown (questionary)
*Shippable: arrow-key harness selection.*
- Add `questionary>=2.0` to **base** deps (already present transitively; make explicit). Lazy-import.
- New `src/atv_bench/harness_selection.py`:
  - `_detect_harness_status(key)` → annotates each option using `harnesses.harness_config_present(key)`
    + `fingerprint/probe.py` runtime detection: `(configured · cli found)` / `(⚠ config missing)` / `(cli not on PATH)`.
  - `select_harness(choices, preselected=None, non_interactive=None)` — mirrors `interactive.select_model`:
    preselected returns verbatim; non-interactive returns first installed / raises on empty;
    interactive → `questionary.select()` with options **Copilot CLI / Codex / Claude Code**;
    cancellation (`None`) → `ValueError("harness selection cancelled")` (fail-closed).
- Wire into quickstart: picker shows once, before the model picker, only when `harness is None`,
  `>1` harness available, and stdin is a TTY.

### Phase 2 — Model dropdown + 1–3 game bot-build & compete
*Shippable: full non-visual eval producing scores.*
- **Port from `feat/quickstart` worktree** (re-test against `main`): `interactive.py`,
  `models.py`, `quickstart.py`.
- `run_quickstart_eval(harness, model, games, repeats, store_dir, progress_cb) -> QuickstartResult`
  — loop 1–3 games (default 3), run **harness vs `bare:harness` baseline** each game, collect a
  `MatchRecord` per game into the corpus. Opponent identity identical in live and demo paths.
- Executor wraps `runner.run_live_match()` (live) or `play.run_local_match()` (fallback) —
  never the frozen canned recording.
- Score via `rating.py` → per-game wins + overall harness-over-bare lift.

### Phase 3 — Persistent store + ELO + local leaderboard rank
*Shippable: `atv-bench board` reflects quickstart matches; rows carry ELO + rank.*
- Fix the throwaway-store defect: thread `store_dir` through `live_server.serve_live_match()`
  and `LiveMatchServer`. Extract `_record_and_build_board(...)` so matches `append_match()` +
  `add_submission()` to a **persistent** `league/` store instead of a deleted temp dir.
- Recompute via `leaderboard.build_leaderboard_from_store` → `elo.compute_leaderboard`; return
  the recomputed doc to both the SSE `board` event and the terminal.
- `--store` default stays throwaway (backward-compatible) unless quickstart passes `league/`.
  Dedup by `match_id` to prevent ELO double-count.

### Phase 4 — Live browser playback wiring
*Shippable: quickstart opens the real match in the browser.*
- From the eval loop, call `serve_live_match(..., store_dir=league_dir)` for (at least) the
  first game → user watches SSE-streamed play + live leaderboard.
- **Persist the match before emitting the `board` SSE event** (race fix).
- `--json`/headless skips the browser and emits machine-readable `QuickstartResult`
  (leaderboard rows + lift) so CI stays deterministic. `live.html` needs no change.

### Phase 5 — Terminal summary
*Shippable: one-line ELO-delta + rank narrative.*
- New `src/atv_bench/summary.py::summarize_match_impact(board_before, board_after, a, b)`.
- Emit e.g. `  claude-code  +42 ELO → #3   ·   bare:claude-code  −18 ELO → #6`.
- Integrity gate: when corpus is `verified=false`, **omit the hard rank** →
  `(match recorded; rankings pending verification)`. Keep within ~100 cols.

### Phase 6 — ATV-BENCH gold-medal banner
*Shippable: polished first-run greeting.*
- Constraint: wheels can't run code on `pip install`, so the banner shows on **first run** of
  `atv-quickstart`/`atv-bench`, not literally during install. Document this framing.
- Add `rich>=13` to base deps. New `src/atv_bench/banner.py`:
  - `render_banner()` — `rich.Panel` + `rich.Text`, "ATV-BENCH" wordmark in gold (`#FFD700`) + 🥇.
  - `first_run_check()` — sentinel `~/.atv-bench/.banner_shown_v1`.
- Trigger from a Typer `@app.callback()` (and the `atv-quickstart` entrypoint): show only when
  TTY + first-run + not `ATV_BENCH_SKIP_BANNER=1` + not `--json`. Fail silent on `rich` import
  error / unwritable home / non-TTY — never block the command.

---

## 3. TDD Acceptance Criteria (write tests first, RED → GREEN)

**Phase 0**
- `test_quickstart_cli.py::test_help_lists_options` — `--harness/--model/--games/--repeats/--fallback-to-demo/--store/--yes/--json` in `--help`.
- `::test_quickstart_subcommand_registered` — `atv-bench quickstart` appears in `atv-bench --help`.
- `::test_exit_code_docker_missing` → exit `5`, hint names `[run]` extra + `run --demo`.
- `::test_exit_code_codeclash_missing` → exit `9`, remediation uses `atv-bench[run] @ git+...`.
- `::test_usage_error_ambiguous_noninteractive` — `>1` harness, no TTY, no `--harness` → exit `2`.

**Phase 1**
- `test_harness_selection.py`: `test_preselected_bypasses_picker`, `test_non_interactive_picks_first`,
  `test_single_harness_auto_selected`, `test_multiple_harnesses_shows_picker` (asserts the 3
  options + status annotations), `test_cancel_raises`, `test_empty_choices_raises`,
  `test_questionary_import_failure_falls_back`.

**Phase 2** (port worktree tests, re-green on `main`)
- `test_quickstart_engine.py::test_default_three_games` (loop yields 3 records; cap at 3).
- `::test_per_game_scoring_and_lift` (deterministic mocked executor).
- `::test_opponent_is_bare_baseline` (opponent B == `BARE_PREFIX` every match).
- `test_interactive_select.py` (preselected / non-interactive / current-default / cancellation).
- `::test_fallback_uses_real_play_not_recording`.

**Phase 3**
- `test_quickstart_persist.py::test_match_appended_to_store` (N rows, schema v2, `verified=false`).
- `::test_elo_recomputed_from_store`, `::test_no_double_count` (dedup by `match_id`),
  `::test_default_store_is_throwaway` (backward compat).

**Phase 4** (Docker-gated `@pytest.mark.integration`)
- `test_quickstart_e2e.py::test_json_mode_skips_browser` (emits rows+lift, no server).
- `::test_persist_before_board_event` (ordering).

**Phase 5**
- `test_summary.py::test_delta_and_rank_rendered`, `::test_unverified_omits_rank`, `::test_fits_terminal_width`.

**Phase 6**
- `test_banner.py::test_renders_gold_and_medal` (contains `ATV-BENCH`, gold style, 🥇).
- `::test_suppressed_on_non_tty`, `::test_suppressed_by_env`, `::test_suppressed_in_json_mode`.
- `::test_shown_once` (sentinel), `::test_readonly_home_no_crash`, `::test_rich_import_failure_no_crash`.

---

## 4. Reuse Map

| Need | Existing module / function |
|---|---|
| Command surface, `_probe_or_exit`, `_serve_and_open` | `cli.py` |
| Preflight (Docker/CodeClash) fail-closed | `runner.preflight_or_raise()` |
| Harness registry + config detection | `harnesses.HARNESSES`, `harness_config_present()`, `detect_harness()` |
| CLI runtime detection | `fingerprint/probe.py` (`_HARNESS_BINARY`, readers) |
| Model picker + TTY pattern | `interactive.py::select_model` *(worktree)* |
| Model catalog | `models.py` *(worktree)* |
| Multi-game loop / corpus | `quickstart.py::run_quickstart_eval` *(worktree)* |
| Live match execution | `runner.run_live_match()`, `config` PvP build |
| Local real play (fallback) | `play.py::run_local_match()` |
| Bare baseline opponent | `adapters.contract` `BARE_PREFIX` |
| Lift / scoring | `rating.py` |
| Store I/O | `store.py::LeagueStore` |
| ELO + leaderboard | `elo.py::compute_leaderboard`, `leaderboard.py` |
| Site publish | `publish.py::build_site` |
| Live SSE server + board | `live_server.py`, `view/live.html` |
| Match schema v2 | `match_record.py` |

**Net-new:** `quickstart.py` orchestration (P0/P2), `harness_selection.py` (P1), `summary.py` (P5), `banner.py` (P6).

---

## 5. Decisions — RESOLVED (2026-07-23)
1. **Entry point:** `atv-bench quickstart` subcommand (single binary). ✅
2. **Live vs demo default:** attempt live, fail with actionable hint; demo behind `--fallback-to-demo`. ✅
3. **Default games/repeats:** 3 games, labeled provisional; suggest more for a credible ELO. ✅
4. **Base deps:** accept `questionary` + `rich` (+ prompt_toolkit), lazy-imported. ✅
5. **Opponent identity:** `bare:harness` baseline in both live and demo paths (honest comparison). ✅ (default)

## 6. Risks
- **Worktree drift** — `feat/quickstart` predates current `main`; cherry-pick per-file and re-green.
- **Throwaway-store regression** — `_build_board` refactor must keep `--store`-absent behavior identical.
- **ELO double-count / SSE race** — dedup by `match_id`; persist before board event.
- **Provisional ratings** — `verified=false` must never show a hard rank in browser or terminal.
- **Banner in restricted envs** — read-only home / missing `rich` / non-TTY must fail silent.
- **`[run]` extra** — the install currently doesn't pull CodeClash; the quickstart live path
  depends on the separate run-extra install fix (`docs/plans/fix-run-extra-install.md`) landing.
