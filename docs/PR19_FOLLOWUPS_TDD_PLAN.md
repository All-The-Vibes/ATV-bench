# PR #19 Follow-ups — TDD Implementation Plan

Branch: `feat/pr19-followups` (off `origin/harness-final-merge`)
Method: strict TDD (RED → GREEN → REFACTOR), one follow-up per commit group.
Goal: eliminate every deferred follow-up in PR #19's description so the benchmark line
ships with zero "tracked, not merge-blocking" debt. Separate PR + santa-loop review.

The four follow-ups (verbatim from PR #19):
1. Executor↔lift seam: join the live-match path to `matches.jsonl` so results flow into `compute_lift` end-to-end.
2. Register `BareModelAdapter` in `ADAPTERS` so the ~0-lift negative control runs on real match data, not synthetic thetas.
3. Wire `scheduler.py` (G1) and `gates.py` (G5/G6) into the live pipeline (currently library+tests).
4. Commit Wave C live-match evidence; downgrade unproven `live=True` flags otherwise.

Design principle throughout: **YAGNI** — connect existing, tested library functions with the
smallest honest seam. Do NOT build a speculative "tournament engine." Every new public function
is justified by a failing test first.

---

## Follow-up 1 — Executor↔lift seam

### Current state (cited)
- `runner.build_match_record()` (`src/atv_bench/runner.py:112`) returns a `MatchRecord`
  (`match_record.py:97`) with `outcome: dict` whose `outcome["winner"]` is a **harness key**
  (`runner.py:194`), and `players: list[PlayerRecord]` each carrying `harness`, `model`, `verified`.
- `LeagueStore.append_match(match: dict)` (`store.py:359`) requires keys `_MATCH_KEYS`
  (includes `player_a, player_b, outcome, match_id`) and dedups on `match_id`.
- `rating.matches_from_records(records)` (`rating.py:262`) builds `RatingMatch` rows from dicts
  carrying `harness_a/harness_b/model_a/model_b/score_a`.
- `lift.compute_lift(matches: list[RatingMatch], baselines, ...)` (`lift.py:243`) consumes those rows.

### The gap
Nothing converts a finished `MatchRecord` into (a) a `matches.jsonl` store dict, nor (b) a
`RatingMatch`. A local `atv-bench run` returns the record but never persists it, so
`compute_lift` can only be exercised on synthetic rows. There is no single end-to-end path
`live match → matches.jsonl → compute_lift`.

### Acceptance criteria
- AC1.1: A pure function `match_record_to_rating_row(rec) -> dict` exists that maps a
  `MatchRecord` to a dict with `harness_a, harness_b, model_a, model_b, score_a`, deriving
  `score_a` from `outcome["winner"]` (1.0 if winner==players[0].harness, 0.0 if winner==players[1].harness,
  0.5 on `"tie"`). Verified-flag and forfeits handled explicitly.
- AC1.2: `matches_from_records([match_record_to_rating_row(rec)])` returns a `RatingMatch` list
  that `compute_lift` accepts without error.
- AC1.3: An end-to-end test: build two synthetic `MatchRecord`s (harness vs its bare baseline),
  convert → persist to a temp `LeagueStore.matches.jsonl` → reload via `store.load_matches()` →
  `matches_from_records` → `compute_lift` returns a `LiftResult` for the harness with a finite
  point estimate. No synthetic thetas — the number is derived from the persisted match rows.
- AC1.4: The CLI `run` path gains an opt-in `--persist <store>` (default off, preserving hermetic
  Phase-1 behavior) that appends the converted row via `append_match`. Off by default → no behavior
  change to existing tests.

### Tests (RED first)
- `tests/test_executor_lift_seam.py`:
  - `test_match_record_to_rating_row_winner_a/_b/_tie` (unit, AC1.1)
  - `test_round_trip_record_to_lift` (AC1.2/1.3 end-to-end, hermetic)
  - `test_run_persist_flag_appends_match` (AC1.4, monkeypatch the live match to a canned outcome)

---

## Follow-up 2 — Register BareModelAdapter in ADAPTERS

### Current state (cited)
- `ADAPTERS` (`adapters/contract.py`) registers only `ClaudeCodeAdapter`, `CopilotCliAdapter`.
- `BareModelAdapter` (`lift.py:103`) is a **wrapper** (`inner: Any`, `run(req)` forces
  `bare_run_env()`), with no `name`/`available()` — so it is not a leaf adapter.

### Design decision
`BareModelAdapter` cannot be a plain `ADAPTERS[key]()` entry (it needs an inner adapter). Register
it via a **composition factory** keyed `"bare:<inner>"` so the negative control resolves like any
other harness name, and the ~0-lift control runs on real match data.

### Acceptance criteria
- AC2.1: `BareModelAdapter` gains `name` and `available()` (delegates to `inner.available()`), so it
  structurally satisfies the `HarnessAdapter` protocol (`contract.py`).
- AC2.2: A resolver `resolve_adapter(key)` handles `"bare:claude-code"` → `BareModelAdapter(ClaudeCodeAdapter())`
  and plain keys unchanged. Unknown inner → actionable error.
- AC2.3: A registered bare control participates in a `matches_from_records → compute_lift` run and
  yields a lift point estimate **near 0** for the bare-vs-bare contrast (negative control), computed
  from match rows, not synthetic thetas.
- AC2.4: `ADAPTERS` (or a sibling `COMPOSABLE`/resolver) exposes the bare control by name; a registry
  test asserts round-trip resolution and that `manifest_is_bare` holds for its produced env.

### Tests (RED first)
- `tests/test_bare_adapter_registry.py`:
  - `test_bare_adapter_satisfies_protocol` (AC2.1)
  - `test_resolve_bare_composite` + `test_resolve_unknown_inner_errors` (AC2.2)
  - `test_bare_control_zero_lift` (AC2.3, hermetic, synthetic match rows for a model vs its bare self)

---

## Follow-up 3 — Wire scheduler.py (G1) + gates.py (G5/G6) into the live pipeline

### Current state (cited)
- `scheduler.build_paired_schedule(harnesses, games, *, seed, repeats)` (`scheduler.py:43`) →
  `list[Match]`, side-balanced. Unit-tested, never called by CLI.
- `gates.evaluate_quality_gates(stats, thresholds)` + `gates.decide_contrast(...)` (`gates.py:52,139`)
  — never consulted before rating/board publish.
- Live entry `cli.run()` runs exactly ONE match; `cli.rate()`/board render never gate.

### The gap
No orchestration builds a schedule, and no publish/board path enforces gates.

### Acceptance criteria
- AC3.1: A new `cli` command `plan-schedule --harnesses ... --games ... --repeats N [--seed S] [--json]`
  calls `build_paired_schedule` and emits the planned matches (JSON). Deterministic under seed.
- AC3.2: The rating/board path gains a gate check: a function `gate_corpus(records, thresholds) ->
  QualityGateReport` extracts corpus stats and calls `evaluate_quality_gates`; `rate`/`board` accept
  `--enforce-gates` that refuses to publish (non-zero exit + actionable message) when gates fail.
- AC3.3: `decide_contrast` is invoked when producing pairwise verdicts in the ratings doc (or a new
  `contrast` command), turning (diff, CI) into A_wins/B_wins/equivalent/inconclusive.
- AC3.4: Tests prove the functions are **called from the pipeline** (not just exist): a failing-gates
  corpus makes `rate --enforce-gates` exit non-zero; a passing corpus publishes.

### Tests (RED first)
- `tests/test_pipeline_scheduler_gates.py`:
  - `test_plan_schedule_cli_deterministic` (AC3.1, CliRunner)
  - `test_rate_enforce_gates_blocks_thin_corpus` / `test_rate_publishes_when_gates_pass` (AC3.2/3.4)
  - `test_contrast_decision_surfaced` (AC3.3)

---

## Follow-up 4 — Wave C live-match evidence / downgrade unproven live=True

### Current state (cited)
- `games.py` marks 20 arenas `live=True`; `tests/test_wave_c_arenas.py` asserts `len(live_keys())==20`
  but only checks the hardcoded flag (tautological). `_e2e/FINAL_MATRIX.json` is referenced but not
  committed.

### Resolution (evidence-first — pairs with Workflow 1)
Workflow 1's live 22-arena matrix produces real per-arena `verdict.json` + `_e2e/matrix.json`. Commit
that as the Wave C evidence artifact and make the live-set claim **derive from proof**, not a hardcoded
list.

### Acceptance criteria
- AC4.1: Commit the matrix proof to `docs/proof/wave-c/matrix.json` (per-arena pass/fail + timings,
  from Workflow 1).
- AC4.2: A test asserts every arena marked `live=True` in `games.py` has a corresponding **PASS** row
  in the committed proof (or is explicitly listed as upstream-blocked). Any `live=True` without a PASS
  → test fails (forces a downgrade to `live=False`).
- AC4.3: If any currently-`live=True` arena did not PASS in the real matrix, downgrade it to
  `live=False` with an inline citation to the proof, and update `test_wave_c_arenas.py` counts.

### Tests (RED first)
- `tests/test_wave_c_evidence.py`:
  - `test_every_live_arena_has_passing_proof` (AC4.2/4.3, reads the committed proof JSON)

---

## Execution order & review
1. FU2 (smallest, unblocks FU1's bare control) → FU1 → FU3 → FU4 (depends on Workflow-1 matrix output).
2. Each follow-up: RED commit (failing tests) then GREEN commit (impl), conventional messages.
3. Full hermetic suite green locally (CI-exact venv) after each.
4. Open PR off `feat/pr19-followups`; run `/santa-loop` dual-review to convergence; ensure all CI
   checks green, no merge conflicts, before requesting the code-owner approval.
