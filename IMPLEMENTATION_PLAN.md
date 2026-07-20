<!-- /autoplan restore point: ~/.gstack/projects/atv-bench/master-autoplan-restore-20260715-103015.md -->

> ## ⚠️ SUPERSEDED — historical design only
> This July 15, 2026 document records the earlier local-runner and Community League
> design. It is not the specification for official harness benchmarking. The current
> product boundaries, trial unit, trust model, statistics, and launch gates are defined
> by [`docs/HARNESS_BENCHMARKING_BLUEPRINT.md`](docs/HARNESS_BENCHMARKING_BLUEPRINT.md),
> [`docs/HARNESS_BENCHMARKING_TEST_PLAN.md`](docs/HARNESS_BENCHMARKING_TEST_PLAN.md),
> [`docs/PRODUCTS_AND_TRACKS.md`](docs/PRODUCTS_AND_TRACKS.md), and
> [`BENCHMARK_CHARTER.md`](BENCHMARK_CHARTER.md). Where this historical plan conflicts
> with those July 19, 2026 documents, the newer documents control.

# ATV-bench — Implementation Plan (local-harness v1, retained for context)

Source design: `~/.gstack/projects/atv-bench/sschofield-main-design-20260715-013403.md`
Spikes: `spikes/SPIKE_REPORT.md` (both PASS). Branch: master. Date: 2026-07-15.

## Scope (locked in review Step 0)
- **Dashboard:** reuse CodeClash `viewer/app.py` + `replay/serve.py` + `cli/rank.py`;
  ATV-bench adds only a **model-tag / leaderboard layer** (the thesis-integrity
  piece the viewer lacks). No custom dashboard subsystem in v1.
- **Games:** BattleSnake + **lightcycles** (this IS Tron — CodeClash ships it).
  No new arena, no substitute needed.
- **Adapters:** claude-code (proven), byok (anchor), copilot-cli (mechanism proven,
  needs entitled account).

## What already exists (reuse, do not rebuild)
| Need | CodeClash asset | ATV-bench action |
|---|---|---|
| Game arenas | `arenas/battlesnake`, `arenas/lightcycles` (+Dockerfiles) | reuse as-is |
| Match engine | `arenas/arena.py` `CodeArena.run_round` (model-agnostic) | reuse untouched |
| Edit seam | `agents/player.py` `Player.run()`, `agents/get_agent()` | register 3 adapters |
| ELO / standings | `analysis/matrix.py`, `analysis/metrics`, `RoundStats` | compute ELO from low-level match results (NOT rank.py text) |
| Web viewer / replay | `viewer/app.py`, `replay/serve.py` | reuse; add leaderboard view |
| Adapter contract | `src/atv_bench/adapters/contract.py` (DONE, tested) | reuse |
| Decoupling core | `spikes/spike_codeclash_decoupling.py::HarnessPlayerCore` (DONE) | promote to src/ |

## Architecture

```
 atv-bench run --game battlesnake --a claude-code --b byok --model <M> --rounds N
        │
        ▼
 ┌─────────────────────┐   emits    ┌───────────────────────────┐
 │ atv_bench.config     │──────────▶│ CodeClash pvp YAML config  │
 │  (build_config)      │           │  agent: claude-code / byok │
 └─────────────────────┘           │  prompts.game_description  │
        │                           └───────────┬───────────────┘
        │ registers adapters                     │ codeclash run
        ▼                                         ▼
 ┌─────────────────────┐        ┌────────────────────────────────────┐
 │ atv_bench.players    │◀──────│ PvpTournament: edit → compete loop  │
 │  HarnessPlayer(Player)│ run() │  edit: player.run() → harness CLI   │
 │   → HarnessPlayerCore │       │  compete: arena.run_round (no model)│
 └─────────────────────┘        └───────────────┬────────────────────┘
        │ AdapterResult(model tag)               │ logs + RoundStats
        ▼                                         ▼
 ┌─────────────────────┐        ┌────────────────────────────────────┐
 │ atv_bench.leaderboard│◀──────│ ELO from RoundStats (not rank.py)   │
 │  + model tagging     │        └────────────────────────────────────┘
 │  → leaderboard.json  │                        │
 │  + recs.md (heuristic)│                       ▼
 └──────────┬──────────┘             CodeClash viewer/replay (reused)
            ▼
   ATV leaderboard view (model + "model+harness" labels)
```

### Registration seam (the whole integration)
`codeclash/agents/__init__.py::get_agent` maps `config['agent']` → Player class.
ATV-bench registers three via a shim (no fork edit — monkeypatch/register at runtime
or a thin `atv_bench.integration.register()` that extends the dict):
```
"copilot-cli" -> HarnessPlayer(CopilotCliAdapter)
"claude-code" -> HarnessPlayer(ClaudeCodeAdapter)
"byok"        -> HarnessPlayer(ByokAdapter)
```
`HarnessPlayer.run()` → `HarnessPlayerCore.edit_turn()` (already built + tested).

## Network isolation: Portkey LLM-gateway (LOCKED, v1) — corrected

`--network none` was WRONG (harness needs network to reach its model) AND a plain
forward proxy is ALSO wrong: as `HTTPS_PROXY`, Portkey sees only `CONNECT host:443`
+ TLS ciphertext — it CANNOT read the model name or prompt (autoplan Codex+eng,
CRITICAL). Corrected design:

- Harness containers join an **`internal: true`** Docker network (no NAT egress) whose
  only reachable host is a **dual-homed Portkey gateway** running as an **LLM gateway
  (API base-URL), not a forward proxy**. Each harness's provider base-URL env points at
  Portkey (e.g. `ANTHROPIC_BASE_URL`, `COPILOT_PROVIDER_BASE_URL`). Only harnesses that
  support base-URL override get gateway-level model/prompt capture.
- **Per-harness capture caveat (LOCKED):** claude-code may NOT expose a clean base-URL
  override. For any harness that can't route through the gateway API, we fall back to
  **CLI-output parsing** for the model tag (contract already parses `modelUsage`) and
  drop the "authoritative gateway prompt capture" claim for that harness. Model/prompt
  capture is **best-effort per harness**, not a universal guarantee.
- Egress mechanism is explicit owned work (Codex): `internal: true` network + gateway
  is the only allowed route; a test runs `curl https://example.com` from inside the
  harness container and asserts FAIL while the gateway host succeeds. CodeClash's own
  `internet_control.py` iptables covers only the GAME container, not our sibling runner
  — we build + test this ourselves.

## Harness execution: containerized runner (LOCKED, v1)

Each harness edit turn runs in an ephemeral `--rm` container that bind-mounts the
user's REAL harness config read-only. Isolation + reproducibility + the actual
harness being benchmarked. Anti-cheat restored via `--network none` during edit.

```
HarnessPlayerCore.edit_turn()  (host orchestrator)
  1. materialize bot → /tmp/atv-run-<uuid>/repo (git init + commit)
  2. docker run --rm --name atv-<uuid> --network atv-net-internal \
       -e ANTHROPIC_BASE_URL=http://portkey-gw:8787 (LLM gateway, not proxy) \
       atv-bench/harness-runner:<harness> \
       -v /tmp/atv-run-<uuid>/repo:/work:rw \
       --mount type=bind,src=/tmp/atv-run-<uuid>/cfg,dst=/root/.claude  (SCRUBBED profile copy, rw)
       -e ANTHROPIC_API_KEY / COPILOT_*  (auth via env)
       -e ATV_GOAL="<identical goal>"  \
       entrypoint: cd /work && <headless CLI invocation>
  3. host reads edit via SNAPSHOT diff: git diff <init_sha>..HEAD + staged + untracked
     (NOT plain `git diff` — CodeClash/CLIs commit; see edit-detection fix below)
  4. write diff into the CodeClash game container (existing copy path)
     try/finally: force-remove atv-<uuid> container + rmtree temp dir on any exit
  3. host reads `git diff` from the shared /work mount → AdapterResult
  4. write diff into the CodeClash game container (existing copy path)
```

- One `--rm` container per edit turn (deterministic `--name atv-<uuid>`; try/finally
  force-remove + temp-dir rmtree on crash; startup sweep of stale `atv-*`). Per-harness
  base image with CLI preinstalled.
- Config via a per-run **SCRUBBED PROFILE** copy (Codex+eng CRITICAL): copy only auth +
  model settings; **strip `mcpServers`, `hooks`, and non-essential skills** before mount
  — else the harness imports ambient MCP tools/hooks that execute code, egress outside
  the gateway, and make "identical goal" fairness meaningless. Test asserts the copied
  profile contains no MCP/hook entries. Copy discarded after turn (real config untouched).
- Egress isolation via internal Docker network + Portkey LLM-gateway (above).
- **Edit detection = SNAPSHOT, not `git diff`** (Codex+eng CRITICAL): CodeClash's
  `post_run_hook` commits, and CLIs may `git add`/`commit`; plain `git diff` (worktree
  vs HEAD) then reads empty → false NO_EDIT/forfeit. Capture base tree SHA at seed, then
  detect `git diff <base>..HEAD` + staged + `--porcelain` untracked. Fix lives in
  `contract.git_diff` + `HarnessPlayerCore.edit_turn` (drop the `edited != original`
  gate). Add a test where the fake adapter commits.
- Auth by env var where the CLI supports it; where claude-code uses subscription/session
  tokens, document that a real token enters the sandbox and rely on the network DROP.
- Contract unchanged in shape: still returns `AdapterResult`; transport swaps
  (subprocess → `docker run`).

### Declarative harness manifest (LOCKED)
Per-harness matrix lives in one YAML (`harnesses/<name>.yaml`), read by
`atv_bench.runner`:
```yaml
name: claude-code
image: atv-bench/harness-runner:claude-code
profile_include: [auth, model_settings]   # scrubbed: NO mcpServers/hooks
base_url_env: ANTHROPIC_BASE_URL          # gateway routing (empty if unsupported)
auth_env: [ANTHROPIC_API_KEY]
invoke: 'claude -p "$ATV_GOAL" --permission-mode acceptEdits --output-format json'
```
Adding a harness = add a manifest entry (a config PR — evangelism path). DRY, keeps
the per-harness mount/auth/invoke differences in one readable place.

## Test plan (locked)

Existing (DONE): `test_contract.py` (4), `test_spike_codeclash_decoupling.py` (4),
live spikes (2). 8 hermetic + 2 live green.

New coverage required:
- **runner (argv unit tests, EVERY push):** assert the constructed `docker run` argv —
  `--network atv-net-internal` present, scrubbed-profile mount, base-URL env set, NO
  literal `-e ANTHROPIC_API_KEY=<value>` baked, invoke string correct. Fast tripwire so
  a mount-mode/network/secret regression can't land on main between gated runs.
- **runner (real-container tests, GATED job):** actual `docker run` — config mount
  behaves, egress DROP verified (`curl https://example.com` fails, gateway host
  succeeds), scrubbed profile has no MCP/hooks, snapshot edit detection catches a
  committed edit. Needs Docker + images + secrets; dedicated `integration` CI job.
- **integration.register (unit):** `get_agent` returns `HarnessPlayer` for the 3 keys;
  asserts ATV drives the tournament IN-PROCESS (register() only works in-process — hard
  architectural constraint, no CLI-subprocess path).
- **config.build_config (unit):** valid pvp YAML; **identical goal prompt to both
  players** (thesis fairness — CRITICAL test).
- **elo (unit):** win/loss/draw math; byok pinned 1500; forfeit=loss+flagged with a
  reason enum (TIMEOUT|INVALID_DIFF|NO_OP|MODEL_UNREACHABLE|AUTH_FAILED|CRASH).
- **leaderboard (unit):** row keyed `(game, harness, model)`; game is a required facet;
  same-model and model+harness rows never mixed in one ranked table.
- **recs (unit):** heuristic fires on forfeit / timeout / missing-survival-edit.
- **cli (E2E, gated):** `run → elo → leaderboard` on a stubbed 1-round match.

CI: fast hermetic job (unit + `-m "not live"`) on every push; `integration` job
(real containers + live) gated/scheduled with Docker + secrets.

## Components to build (v1)
1. **`byok` adapter** — Copilot `COPILOT_PROVIDER_*` env mode (no GitHub auth). ~1 file.
2. **`atv_bench.runner`** — containerized edit turn: reads harness manifest, builds
   `docker run --rm --network atv-net` (Portkey egress) with per-run config COPY +
   auth env, runs the headless invocation, returns `AdapterResult` from the git diff.
   Swaps behind the existing adapter interface (subprocess → docker transport).
3. **`harnesses/*.yaml` + Dockerfiles** — 3 manifests (claude-code, copilot-cli,
   byok) + 3 `harness-runner` base images with each CLI preinstalled.
4. **`atv_bench.players`** — promote `HarnessPlayerCore` from spikes/ to src/; add
   `HarnessPlayer(Player)` production wrapper + `_DockerContainerShim`.
5. **`atv_bench.integration`** — runtime `register()` injecting 3 adapters into
   `get_agent` (no fork edit).
6. **`atv_bench.config`** — `build_config(...)` → pvp YAML; identical
   `prompts.game_description` for both players (fairness); per-game bot file.
7. **`atv_bench.elo`** — read CodeClash rank output → ELO, byok pinned 1500;
   win=1/loss=0/draw=0.5; per-game win defs; forfeit=loss+flag.
8. **`atv_bench.leaderboard`** — leaderboard.json (row: harness, underlying model,
   comparison_mode) + static export.
9. **`atv_bench.recs`** — heuristic recs.md from match logs + diffs.
10. **`atv_bench.cli`** — `atv-bench run/leaderboard/serve` (typer).
11. **Leaderboard view** — extend viewer with model-tag columns + comparison label.

## Comparison modes (thesis integrity)
- PRIMARY same-model/different-harness: `--a claude-code --b byok --model <same>`.
- SECONDARY cross-stack: `--a copilot-cli --b claude-code` → row labeled "model+harness".
- Every leaderboard row records underlying model; runner refuses to omit it.

## Performance (locked)
- **Bounded concurrent edit turns.** ELO run = ~10 matches × ~15 rounds × 2
  harnesses; each edit turn = container + real LLM call (~23s observed). Serial =
  hours (breaks the wedge). Run edit turns concurrently with a bounded worker pool at
  the **cross-match** scheduling layer (our orchestrator), NOT the 2-agent edit phase
  (CodeClash already pools that — don't double-nest). `ATV_MAX_CONCURRENCY` default
  **3** for live agent CLIs; configurable. Cap prevents Docker fork-bomb + rate-limit trips.
- Watch: each `--rm` container is cold-start overhead; keep base images small.

## Outside-voice (Codex) resolutions
- **#1 `--network none` contradiction** — FIXED: Portkey gateway isolation (above).
- **#3 ro config breaks CLIs** — FIXED: per-run rw COPY, discarded after turn.
- **#7 monkeypatch registration fragile** — HARDENED: register via a proper entry
  point, not import-time monkeypatch. `atv-bench run` is our CLI; it imports
  codeclash and calls `register()` BEFORE constructing the tournament, and we add a
  test asserting `get_agent("claude-code")` resolves. If CodeClash resolves agents in
  a subprocess, register inside that process via an env-driven plugin hook.
- **#8 model tag loss / #9 prompt fairness** — FIXED: Portkey gateway logs real model
  + effective prompt per call (authoritative, not CLI-output parsing).
- **#11/#12 ELO under-specified + wrong dependency direction** — RESOLVED: compute
  ELO from **low-level match results** (`RoundStats`/logs), NOT by wrapping
  `rank.py` text output. byok is a strict anchor (1500, excluded from updates).
  Single scoring policy: win=1/loss=0/draw=0.5; forfeit/timeout/invalid-diff/crash/
  no-op all = loss + flagged. Written before implementation.
- **#10 concurrency overpromised** — TEMPERED: default `ATV_MAX_CONCURRENCY` low
  (~3) for live agent CLIs, not min(8,cores); make it configurable, tune up empirically.
- **#13/#14 dashboard + Docker images understated** — ACKNOWLEDGED: 3 authenticated
  runner images (CI build, pinned versions, no baked secrets) is a top v1 risk, called
  out as its own workstream; leaderboard view kept minimal (model-tag columns only).
- **#16 front-loading Docker before proving the loop** — SEQUENCING (see Phasing):
  Phase 1 proves the benchmark loop end-to-end on the host path (already spiked);
  Phase 2 swaps in the containerized Portkey runner. Value proven before hardening.

## Phasing (locked, autoplan-revised)
- **Phase 1 — prove the loop (host) + on-ramp:** byok adapter, register() (in-process),
  config builder, players core, ELO from match results, leaderboard.json, CLI `run`.
  PLUS `atv-bench run --demo` (canned match → ELO in <60s, zero Docker/Portkey/auth —
  the documented first step) and `atv-bench doctor` (preflight: Docker up? images? gateway?
  auth env?). DX on-ramp so the tool survives the setup cliff.
- **Phase 1.5 — variance gate (BOTH MODELS, GATES Phase 2):** A/A self-play control —
  run the SAME harness against itself, measure ELO spread. If self-play variance >= the
  gap between two real harnesses, the benchmark is noise: STOP and fix (more matches,
  seed control, or narrow the claim) before funding containerization. Report the
  signal-to-noise ratio. This is the credibility gate.
- **Phase 2 — isolation + real harness:** containerized runner (internal net + Portkey
  LLM-gateway), harness manifests + 3 images, scrubbed-profile copy, snapshot edit
  detection, model/prompt capture (best-effort per harness), argv + real-container
  security tests, `validate-harness` + Dockerfile.harness-template (real contribution path).
- **Phase 3 — surface:** leaderboard view (row-key `(game, harness, model)`, game as
  required facet, same-model / cross-stack segmented into separate sections — never
  mixed in one ranked column, flagged + low-N rows visibly marked) on the reused viewer;
  recs.md; lightcycles (Tron) game #2; static export; concurrency tuning.

## NOT in scope (v1)
- Custom dashboard subsystem — reuse viewer (Step 0 decision).
- Copilot ADE-app headless integration — CLI path only; fast-follow.
- CI-native league (Approach C) — v2.
- Cost-adjusted ELO — usage captured + displayed, not scored (design defer).
- New arenas beyond battlesnake + lightcycles.
- Trained recommender — heuristic only.

## Distribution
- `uvx atv-bench` / `uv run` quick-start (mirror CodeClash). Docker for arenas +
  harness-runner images + Portkey gateway (compose).
- CI: fast hermetic job every push; gated `integration` job (real containers + live)
  builds/publishes package + arena + runner images with pinned CLI versions, no baked
  secrets.
- Static leaderboard export = shareable artifact (no hosted service v1).

## Worktree parallelization
| Lane | Steps | Modules | Depends on |
|---|---|---|---|
| A | T1 byok, T3 config, T4 elo | adapters/, config, elo | — |
| B | T2 register, T5 players | integration, players | — |
| C | T6 runner, T7 images | runner, harnesses/, docker/ | A (adapter iface) |
| D | T8 leaderboard, T9 recs, T10 viewer | leaderboard, view | A (elo) |

Launch A + B in parallel. Then C and D in parallel (both need A). T11 tests ride
alongside each lane. Conflict flag: T6 and T13 both touch `runner.py` — sequential.

## Failure modes (new codepaths)
- **runner container** — model endpoint unreachable through Portkey → clean ERROR,
  scored+flagged (test: real-container). Silent-failure risk if diff-capture reads a
  stale mount → guard with explicit git-diff exit check.
- **register()** — resolves in a subprocess where our patch didn't run → `get_agent`
  KeyError. Covered by resolve test (T2). CRITICAL if untested → gated by T2.
- **ELO** — draw/forfeit miscount skews ratings silently. Covered by T4 scoring tests.
- **config copy** — copy fails / partial → harness runs with broken config, looks like
  a harness loss. Guard: verify copy integrity before run; flag on failure.

## Decision Audit Trail (autoplan)

| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | Eng | Delete all `--network none`; internal net + Portkey LLM-gateway | Mechanical | P1 | Both models CRITICAL: harness needs network; forward proxy can't read TLS |
| 2 | Eng | Portkey as API base-URL (not HTTPS_PROXY); model/prompt capture best-effort per harness | Mechanical | P5 | Both CRITICAL: proxy sees only ciphertext; claim was false |
| 3 | Eng | Snapshot edit detection (base..HEAD + staged + untracked), not `git diff` | Mechanical | P1 | Both CRITICAL, code-verified: CodeClash commits in post_run_hook |
| 4 | Eng | Scrubbed profile copy (strip mcpServers/hooks/skills), not raw ~/.claude | Mechanical | P1 | Both CRITICAL: ambient MCP/hooks break isolation+fairness+token safety |
| 5 | Eng | register() requires in-process tournament (hard constraint) | Mechanical | P5 | Both: no plugin hook in CodeClash; monkeypatch only works in-process |
| 6 | Eng | Fix manifest ro:true→scrubbed copy; concurrency 8→3; purge rank.py refs | Mechanical | P5 | Internal contradictions flagged by both |
| 7 | Eng | Add argv unit tests + keep real-container (USER CHALLENGE #2 → accepted) | User Challenge | P1 | User accepted: fast per-push tripwire for security regressions |
| 8 | CEO | Insert Phase 1.5 A/A variance gate before Phase 2 (USER CHALLENGE #1 → accepted) | User Challenge | P1 | User accepted: prove signal>noise before funding containerization |
| 9 | DX | Add `run --demo` + `doctor` on-ramp to Phase 1 | Taste (auto) | P1 | Internal tool dies at setup cliff; reuses host loop |
| 10 | Design | Leaderboard row-key (game,harness,model); segment modes; mark flagged/low-N | Taste (auto) | P5 | Schema the static-export contract depends on; prevents over-read |
| 11 | DX | `validate-harness` + Dockerfile template → Phase 2 | Taste (auto) | P1 | Makes the evangelism contribution path real, not aspirational |
| 12 | Eng/DX | Forfeit reason enum surfaced to user | Taste (auto) | P1 | Diagnosable losses; already internal for scoring |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_found | Codex+Claude: phasing backwards (→Phase 1.5 gate accepted), games-as-proxy premise, recs-as-real-value; variance is the credibility risk |
| Codex Review | `/codex review` | Independent 2nd opinion | 2 | issues_found | 1st: network contradiction. 2nd (autoplan): Portkey-proxy-can't-read-TLS, config drags MCP/hooks, git-diff misses commits — all fixed |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 6 decisions locked + autoplan fixed 6 mechanical CRITICALs |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | issues_found | Leaderboard row-key + mode segmentation + flagged/low-N states (auto-folded) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | issues_found | On-ramp cliff → demo+doctor; contribution path → validate-harness (auto-folded) |

- **CODEX:** across two runs, caught the network contradiction and then the deeper Portkey-as-forward-proxy feasibility flaw (can't read TLS). Both drove real redesigns.
- **CROSS-MODEL:** Claude subagents + Codex converged independently on all 4 CRITICAL technical fixes (network, proxy, git-diff, config-scrub) and on the variance-gate strategy — high-confidence signal.
- **CROSS-PHASE THEME:** "trust before surface" appeared in CEO (variance), Eng (isolation correctness), and DX (diagnosable failures) independently.
- **UNRESOLVED:** 0. Two user challenges both accepted.
- **VERDICT:** ENG CLEARED — plan corrected via 4-phase dual-voice review; 6 mechanical CRITICALs fixed, 2 user challenges accepted, taste items folded. Ready to implement Phase 1.

