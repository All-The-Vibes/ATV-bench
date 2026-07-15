# ATV-bench — Implementation Plan (locked)

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
| ELO / standings | `cli/rank.py`, `analysis/matrix.py`, `analysis/metrics` | reuse; read its output |
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
 │ atv_bench.leaderboard│◀──────│ CodeClash rank.py (ELO/win-rate)    │
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

## Network isolation: Portkey gateway (LOCKED, v1) — supersedes `--network none`

`--network none` was WRONG (Codex #1): the harness needs network to reach its
model. Corrected design: harness containers join an internal Docker network
(`atv-net`) with **no default egress**; their only reachable host is a **Portkey
gateway** (reuses CodeClash's existing `model_class: portkey`). Harness CLIs route
model calls via `HTTPS_PROXY=http://portkey-gw:8787`.

```
harness-runner (--network atv-net, no direct internet)
   HTTPS_PROXY → portkey-gw ──▶ allowlisted: api.anthropic.com / Copilot / BYOK
                              └▶ everything else: DROP
```

One choke point retires four risks:
- **Isolation** — opponent-code / arbitrary-web fetches blocked; inference flows.
- **#8 model tag** — gateway sees real model per call → authoritative tagging (not
  CLI-output parsing).
- **#9 prompt fairness** — gateway logs the *effective* prompt each harness sent →
  fairness auditable, not just "identical goal string".
- **#10 rate limits** — centralized retry/backoff.

Verify each CLI honors `HTTPS_PROXY` (claude/copilot do via standard env); BYOK +
Copilot inference reachable through Portkey provider routing. Config-mount caveat
(Codex #3): mount config **read-write to a per-run COPY**, not ro to the user's real
dir — CLIs write session/lock/token-refresh files. Copy-in, throwaway, so the real
config is never mutated and reproducibility holds.

## Harness execution: containerized runner (LOCKED, v1)

Each harness edit turn runs in an ephemeral `--rm` container that bind-mounts the
user's REAL harness config read-only. Isolation + reproducibility + the actual
harness being benchmarked. Anti-cheat restored via `--network none` during edit.

```
HarnessPlayerCore.edit_turn()  (host orchestrator)
  1. materialize bot → /tmp/atv-run-<uuid>/repo (git init + commit)
  2. docker run --rm --network atv-net \
       -e HTTPS_PROXY=http://portkey-gw:8787 \
       atv-bench/harness-runner:<harness> \
       -v /tmp/atv-run-<uuid>/repo:/work:rw \
       --mount type=bind,src=/tmp/atv-run-<uuid>/cfg,dst=/root/.claude  (COPY of real cfg, rw)
       -e ANTHROPIC_API_KEY / COPILOT_*  (auth via env, never baked)
       -e ATV_GOAL="<identical goal>"  \
       entrypoint: cd /work && <headless CLI invocation>
  3. host reads `git diff` from the shared /work mount → AdapterResult
  4. write diff into the CodeClash game container (existing copy path)
```

- One `--rm` container per edit turn. Per-harness base image with CLI preinstalled.
- Config via per-run **COPY** of the user's real config (`~/.claude`, `~/.copilot`,
  skills/MCP dirs), mounted rw so CLIs can write session/lock/token files (Codex #3);
  copy is thrown away after the turn → real config never mutated, reproducibility holds.
- Egress isolation via Portkey gateway (above), not `--network none`.
- Auth by env var only. Contract unchanged: still returns `AdapterResult`; only the
  transport swaps (subprocess → `docker run`). Tested `HarnessPlayerCore` stays; we
  swap `adapter.run`'s transport behind the same interface.

### Declarative harness manifest (LOCKED)
Per-harness matrix lives in one YAML (`harnesses/<name>.yaml`), read by
`atv_bench.runner`:
```yaml
name: claude-code
image: atv-bench/harness-runner:claude-code
mounts:
  - {src: "~/.claude", dst: "/root/.claude", ro: true}
auth_env: [ANTHROPIC_API_KEY]
invoke: 'claude -p "$ATV_GOAL" --permission-mode acceptEdits --output-format json'
```
Adding a harness = add a manifest entry (a config PR — evangelism path). DRY, keeps
the per-harness mount/auth/invoke differences in one readable place.

## Test plan (locked)

Existing (DONE): `test_contract.py` (4), `test_spike_codeclash_decoupling.py` (4),
live spikes (2). 8 hermetic + 2 live green.

New coverage required:
- **runner (real-container tests):** actual `docker run` in the suite — assert
  config mount is genuinely **read-only** (write attempt fails), `--network none`
  really blocks egress, no auth baked in image, edit turn produces a real diff.
  Slower + gated (needs Docker + prebuilt images + auth secrets in CI; not on every
  push — a dedicated `integration` CI job). Chosen over argv-only for isolation
  fidelity (mounts/network are security-critical).
- **integration.register (unit):** `get_agent` returns `HarnessPlayer` for the 3 keys.
- **config.build_config (unit):** valid pvp YAML; **identical goal prompt to both
  players** (thesis fairness — CRITICAL test).
- **elo (unit):** win/loss/draw math; byok pinned 1500; forfeit=loss+flagged.
- **leaderboard (unit):** row carries underlying model + comparison_mode; same-model
  vs model+harness labeling correct.
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
  hours (breaks the wedge). Run edit turns concurrently with a bounded worker pool
  (`ATV_MAX_CONCURRENCY`, default ~ min(8, cores)); reuse CodeClash's sim thread
  pool for the compete phase. Cap prevents Docker fork-bomb + LLM rate-limit trips.
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

## Phasing (locked)
- **Phase 1 — prove the loop (host):** byok adapter, register(), config builder,
  players core (done), ELO from match results, leaderboard.json, CLI `run`. Runs a
  real battlesnake match host-side, emits an ELO number + leaderboard. Ships the wedge.
- **Phase 2 — isolation + real harness:** containerized Portkey runner, harness
  manifests + 3 images, config-copy, model/prompt capture, real-container security
  tests. Turns the number into a trustworthy number.
- **Phase 3 — surface:** leaderboard view on the reused viewer, recs.md, lightcycles
  (Tron) as game #2, static export, bounded concurrency tuning.

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

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (optional) |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found | 16 raised; 4 fixed in-plan (network, config-rw, model-tag, prompt), 3 hardened (register, ELO dir, concurrency), rest acknowledged/phased |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 6 decisions locked (dashboard scope, register seam, containerized runner, manifest, test strategy, concurrency) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not run (minimal UI) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **CODEX:** caught the load-bearing `--network none` contradiction; drove the Portkey-gateway isolation redesign that also retired model-tagging, prompt-fairness, and rate-limit risks.
- **CROSS-MODEL:** review + Codex agreed the benchmark loop should be proven before Docker hardening → Phase 1 (host loop) then Phase 2 (containerized Portkey runner).
- **UNRESOLVED:** 0.
- **VERDICT:** ENG CLEARED — spikes done, scope locked, architecture corrected via outside voice. Ready to implement Phase 1.

