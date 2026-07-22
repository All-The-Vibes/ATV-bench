# `quickstart` — one-command harness evaluation UX

## Goal (verbatim intent)

A user `uv tool install`s ATV-bench, then runs **`atv-bench quickstart`**, which:
1. **Infers their harness** (claude-code / copilot-cli), produces the **fingerprint**, shows it back.
2. **Asks their preferred model** via an **arrow-key list** of models routable through their harness auth
   (Copilot OAuth / Claude Code / Codex).
3. **Automatically runs the harness + the bare model** across the **20 live games** in the **isolated arena**.
4. **Updates the leaderboard**, prints a **link** to it, and shows the **overall + per-game score** with a
   **thoughtful, scientific assessment** (lift over bare, CIs, quality gates — per the plan's Section 5.5).

## What already exists (recon-verified, cited) — REUSE, don't rebuild

| Capability | Symbol (file:line) | Notes |
|---|---|---|
| Harness auto-detect | `harnesses.detect_harness(home)` → key\|None (harnesses.py:138) | Ambiguity guard in cli.py |
| Config root per harness | `harnesses.config_root_for(key, home)` (harnesses.py:114) | e.g. ~/.claude |
| Fingerprint probe | `fingerprint.probe.probe(home, harness)` → ProbeResult(manifest,log) (probe.py:712) | 13-key leak-safe manifest; reads configured **model** (probe.py:215) |
| Render fingerprint | `cli._render_full_assessment(manifest, key)` / `_render_consent(manifest)` (cli.py:169/127) | reuse the human render |
| Harness→binary (incl bare) | `runner.harness_binary_for(key)` (runner.py:31) | `bare:<inner>`→inner binary |
| Plan schedule | `scheduler.build_paired_schedule(harnesses, games, seed, repeats)` → list[Match] (scheduler.py:43) | side-balanced |
| 20 live games | `games.live_keys()` (games.py:192) | exactly 20 |
| Execute ONE live match | `runner.run_live_match(cfg, output_dir, homes)` (runner.py:377) | Docker arena + adjudication |
| Match→rating row | `runner.match_record_to_rating_row(rec)` / `persist_rating_row_from_record` (runner.py:48/125) | fail-closed |
| Bradley-Terry ratings | `rating.build_ratings_doc(rows,...)` (rating.py:279) | clustered CIs |
| Lift over bare | `lift.compute_lift(matches, baselines, ...)` → {harness: LiftResult(lift,lo,hi,n_boot)} (lift.py:243) | Section 5.5 |
| Quality gates G5/G6 | `gates.evaluate_quality_gates` / `decide_contrast` (gates.py:52/139) | + pipeline.corpus_stats/gate_corpus |
| Build local board + link | `cli.board` → `publish.build_site(store)` + local HTTP server (cli.py:642) | `http://127.0.0.1:<port>/` |
| e2e per-arena executor | `scripts/e2e_arena_matrix.py` | reference loop for running a real match per arena |

## What must be BUILT (the gaps)

1. **Model catalog + routing awareness** (`src/atv_bench/models.py`, NEW)
   - Curated per-harness routable model lists + the harness's **currently-configured model** (from the fingerprint
     manifest) surfaced as the default/first choice.
   - `available_models_for(harness) -> list[ModelChoice]` where ModelChoice = (id, label, is_current).
   - Honest disclaimer: the upstream CLI is authoritative; a model not listed may still route. A `--model` flag
     bypasses the picker (non-interactive / CI).
   - **Codex:** now a first-class runnable harness — see unit 0 (codex execution adapter). Its routable models
     (o-series / gpt-*) are catalogued alongside claude-code and copilot-cli.

0. **Codex execution adapter** (`adapters/contract.py`, extend) — DECISION: build it so codex runs matches.
   - `CodexCliAdapter(HarnessAdapter)` with `name="codex"`, `available()` = `shutil.which("codex")`, and `run(req)`
     driving the codex CLI headless (mirror ClaudeCodeAdapter/CopilotCliAdapter: build a `codex` command with the
     goal + a non-interactive/auto-approve flag + JSON output, parse the used model out of the response). Register
     in `ADAPTERS` so `BUILDER_HARNESSES` and `resolve_adapter`/`harness_binary_for` pick it up automatically
     (`bare:codex` then works for free via the existing composition). Add `_HARNESS_BINARY["codex"]="codex"`.
   - Tests: `tests/test_codex_adapter.py` — name/available/run-shape via an injected fake runner (no real CLI),
     model parse from a canned codex JSON payload, registry membership, `bare:codex` resolves.


2. **Interactive arrow-key selector** (`src/atv_bench/interactive.py`, NEW; dep: `questionary`)
   - `select_model(choices, *, non_interactive=False) -> str`. Falls back to a numbered `typer.prompt` when stdin
     is not a TTY or questionary is unavailable, and honors an explicit `--model`.

3. **Per-game aggregation** (`src/atv_bench/pergame.py`, NEW)
   - `per_game_scores(rows, harness, baseline) -> list[GameScore]` — for each game key present in the corpus,
     filter rows to that game, fit `build_ratings_doc` (or a direct 2-player score) + `compute_lift`, and return
     `GameScore(game, n, win_rate, lift, lo, hi, decisive)`. Overall = pooled `compute_lift`.
   - Fail-closed: a game with too few trials for a defensible CI reports `insufficient` rather than a phantom number.

4. **The eval orchestrator** (`src/atv_bench/quickstart.py`, NEW — the engine, pure/testable)
   - `run_quickstart_eval(harness, model, *, games, repeats, store, homes, execute=run_live_match, progress=...)
     -> QuickstartResult`:
     a. `plan = build_paired_schedule([harness, f"bare:{harness}"], games, seed, repeats)`
     b. for each planned Match: execute in isolation → `match_record_to_rating_row` → `persist_rating_row_from_record`
        into the store corpus. Robust to a single arena failing (record it, continue) — surface an infra-error rate.
     c. build ratings + `compute_lift` (overall) + `per_game_scores` (per game).
     d. run `gate_corpus` (G5/G6) → mark the result credible / provisional (fail-closed messaging, never a silent
        pass on a thin corpus).
     e. build the local board via `build_site` → return the link + a `QuickstartResult` (overall lift+CI,
        per-game breakdown, gate verdict, board path/url).
   - The `execute` and `progress` seams are injected so the engine is hermetically testable WITHOUT Docker (stub
     executor returns canned outcomes; real path uses `run_live_match`).

5. **The `quickstart` CLI command** (`cli.py`, thin wrapper over the engine)
   - `atv-bench quickstart [--harness K] [--model M] [--games N|--all] [--repeats R] [--store DIR] [--yes] [--json]`
   - Flow: detect harness (or `--harness`) → fingerprint + `_render_full_assessment` → confirm → pick model
     (arrow list, or `--model`) → run engine with a live progress bar (per-game PASS/FAIL) → print the scientific
     summary table (overall lift ±CI, per-game rows, gate verdict) + the board link.
   - Non-interactive: `--model` + `--yes` makes it fully scriptable (CI); `--json` emits the QuickstartResult.
   - Time/cost guard: default runs a **fast 3-game taste** (`--games` defaults to a curated quick trio, ~6 matches,
     minutes). `--all` runs all 20 live games (~40 matches, hours + real API cost) for the full scientific eval;
     quickstart warns on the estimated wall-clock/cost and requires confirmation (or `--yes`) before the `--all`
     live run. `--games <key>...` picks explicit arenas.

## Scientific assessment (what the scores MEAN — per IMPLEMENTATION_PLAN §5.5)

- **Overall score** = `lift(H, M) = θ(M+H) − θ(M bare)` with a clustered bootstrap CI. Because both players share
  the base model, the model term cancels → a **pure harness effect**, comparable across models.
- **Per-game score** = the same contrast restricted to one arena: win-rate + per-game lift (or "insufficient N").
- **Credibility** = the G5/G6 gates: eligible-N, per-cell trials, infra-error rate, referee determinism. A run that
  fails a gate is reported **provisional**, not published as a ranked number (fail-closed — the whole ethos).
- **Decisiveness** = `decide_contrast` (CI excludes the equivalence margin + stable bootstrap sign) → the board
  shows "harness helps / no measurable effect / inconclusive", never an over-claimed win.

## TDD test plan (RED→GREEN, per unit)

- `tests/test_models_catalog.py`: available_models_for lists routable models; surfaces the fingerprint's current
  model as `is_current`; unknown harness → empty; codex flagged run-unsupported.
- `tests/test_interactive_select.py`: non-interactive fallback picks `--model`; numbered fallback parses input;
  questionary path stubbed.
- `tests/test_pergame_scores.py`: per-game filter + fit yields a GameScore per game; thin game → `insufficient`;
  overall pooled lift matches compute_lift on the full corpus.
- `tests/test_quickstart_engine.py`: run_quickstart_eval with a STUB executor (no Docker) over a few games →
  persists rows, computes overall+per-game, runs gates, returns a QuickstartResult with a board path; a failing
  arena is recorded and raises the infra-error rate (gate goes provisional); deterministic under seed.
- `tests/test_quickstart_cli.py` (CliRunner): `quickstart --harness claude-code --model sonnet --games dummy --yes
  --json` with the engine's executor monkeypatched → emits the JSON result + board link; `--model` bypasses the
  picker; a detected-codex path exits with the run-unsupported message.

## Isolation

Reuses the existing arena isolation (`--network none`, read-only, non-root, cap-drop — proven in
`docs/proof/isolation/`). The engine calls `run_live_match`, which runs each bot in that sandbox. No new isolation
code; the quickstart just orchestrates.

## Leaderboard + link

- **Local (default):** engine writes the corpus to the store, `build_site` renders `_board/{leaderboard.json,
  index.html}`, quickstart serves it on `http://127.0.0.1:<port>/` and prints the URL (same as `board`). The
  per-game breakdown is written to a `quickstart_result.json` beside the board and shown in the terminal.
- **Hosted (optional, later):** publishing to `all-the-vibes.github.io/ATV-bench` is gated behind the fork→PR→Action
  flow (`submit --live`). Quickstart prints how to publish; it does not silently push. (Out of scope for v1 of the
  command; a `--publish` flag can wire `submit` later.)

## Delivery

- Branch `feat/quickstart` off `origin/main`. TDD per unit. Full hermetic suite green (CI-exact venv).
- Agent team: parallel unit builders (models, interactive, pergame) → engine (depends on the three) → CLI (depends
  on engine) → docs. Then a live smoke of `quickstart --games dummy` end-to-end, and `/santa-loop` review to green.
- Open PR; santa-loop dual-review; ensure CI green + no conflicts before requesting approval.
