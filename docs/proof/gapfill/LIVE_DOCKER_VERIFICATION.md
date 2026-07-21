# Live-Docker Wave A verification (2026-07-21)

Goal: verify the two/three live-Docker Wave A tests that intermittently showed red in the
full suite.

## Tests
- `tests/test_wave_a_games.py::test_wave_a_fake_match_scores[gomoku-...]`
- `tests/test_wave_a_games.py::test_wave_a_fake_match_scores[dummy-...]`
- `tests/test_e2e_live.py::test_live_aa_selfplay_lightcycles` (flagship "honest proof")

## Findings

**gomoku / dummy** — pass reliably (real CodeClash Docker match → scored non-forfeit
RoundStats). Confirmed passing both in isolation and in the combined run.

**lightcycles** — was **reproducibly failing** (not a parallelism flake; xdist is not even
installed — the suite runs sequentially). Root cause traced to vendored + upstream defaults:

- mini-swe-agent `DockerEnvironment.config.timeout` defaults to **30s**.
- CodeClash `ClashDockerEnvironment.execute(cmd)` is called with `timeout=None`, so it
  inherits that 30s.
- lightcycles' `engine.py -r 10` adjudication runs inside Docker and exceeds 30s on this
  host, so it was killed with `RuntimeError: Command failed with exit code -1: ... timed out
  after 30 seconds`.
- Critically, the match had **already completed and scored 3-3** ("both submissions compiled
  successfully") before the adjudication command timed out — i.e. an honest, fully-played
  match was being turned into a FALSE forfeit by the adjudicator's default exec timeout. A
  trust bug, not a real outcome.

Proven pre-existing: the failure is entirely in the live-match path
(`test_e2e_live.py` / `runner.py` / `codeclash_env.py` / `vendor/CodeClash`); the gap-fill
commits touch none of those.

## Fix

`integration.register()` now wraps `ClashDockerEnvironment.execute` to substitute a generous
default (900s, tunable via `ATV_ARENA_EXEC_TIMEOUT`) when the caller passes `timeout=None`;
an explicit timeout is always respected; `unregister()` restores the original. A genuinely
hung bot is still bounded (new default + CodeClash's 10h `container_timeout`).

Hermetic coverage: `tests/test_arena_timeout.py` (5 cases: default ≥600s, env-tunable,
garbage-env fallback, patch-applied via register, explicit-timeout-respected).

## Verification runs (real Docker + live claude CLI, GITHUB_TOKEN set)

| Run | Result |
|---|---|
| lightcycles alone, before fix (loaded Docker) | FAIL — 30s timeout |
| lightcycles alone, before fix (idle Docker) | FAIL — 30s timeout (reproducible) |
| lightcycles alone, after fix | **PASS** (252s) |
| all 3 live-Docker Wave A together, after fix | **PASS** (3 passed, 424s) |
