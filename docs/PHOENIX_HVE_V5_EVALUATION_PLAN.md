# Phoenix versus hve-core v5 evaluation plan

Frozen on Monday, July 20, 2026, after full-path calibration passed and before any
v5 evaluation attempt.

## Calibration evidence

Two non-scored attempts both passed:

- both harnesses produced valid artifacts;
- all model receipts and terminal executions passed;
- both side-swapped public games finished without bot forfeits;
- no public game reached `MATCH_TIMEOUT`.

Selected budget: **60 AI credits**.

Calibration artifact SHA-256:
`642d9892d13dc0ae2e9136d660f6e83c7b5740be8e0bcf04779530e57431a311`.

Public calibration outcomes were deliberately not used for scoring.

## Frozen execution cell

| Field | Value |
|---|---|
| Phoenix commit | `233e8e1e968bbc0b1dc446d7830efa82489bf118` |
| hve-core commit | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` |
| Copilot CLI | `GitHub Copilot CLI 1.0.72-1` |
| Model | explicit `gpt-5.4` |
| AI-credit budget | 60 |
| Harness timeout | 1,200 seconds |
| Board profile | `compact` |
| Maximum turns | 40 |
| Per-turn timeout | 3.0 seconds |
| Per-match timeout | 60.0 seconds |
| Seeds per attempt | 5, each side-swapped |
| Primary attempts | 5 |
| Maximum attempts | 8 |

## Frozen implementation identities

| Component | SHA-256 |
|---|---|
| `scripts/compare_phoenix_hve.py` | `227a4da5a6e3c50031d4d0e0e63799f1afac2ece4f53e26ffdd6126da59a4366` |
| `scripts/summarize_phoenix_hve_v2.py` | `1d3f45fb59c0d8b613b1d4380ac544df68ed6a64828bade0f35d46e2deeead55` |
| `src/atv_bench/comparison.py` | `183f93036d947601ad3ea7b5c3ec50a31d6720a342b31147f24a5f8525301ed2` |
| `src/atv_bench/arena/engine.py` | `c6eeb8ceea85af433ba8274c7c16c8d7c3444a070b60b17d61cb5ed1047839f3` |
| `src/atv_bench/arena/referee.py` | `4a2523cb6335562f4ecd1e1d9a8ac252b5fef3e42ed97a7cc133773f860ca60c` |

Any identity or runtime-policy change starts a new cell.

## Seed commitment

Salted canonical seed-plan SHA-256:

`63b828639791cf920072346388c290513be02dcdef1a1b6f4432556f8f34ae7b`

The plan contains five primary and three reserve balanced seed sets. It is revealed
with the local result package after execution.

## Estimands

Primary: **end-to-end task success**.

- Phoenix valid / hve-core invalid = `+1`.
- Phoenix invalid / hve-core valid = `-1`.
- both invalid = `0`.
- both valid = held-out normalized game score difference.

Secondary: **conditional artifact quality**, using only attempts where both artifacts
and evaluator runtime are valid.

Reliability, completed games, match timeouts, bot forfeits, and exact sign tests are
reported separately.

## Decision and stopping rules

- At least 5 primary-eligible attempts are required.
- At most 8 attempts may run.
- Reserve attempts replace identity/infrastructure-invalid attempts only.
- Valid unfavorable attempts are never replaced.
- Evaluator `MATCH_TIMEOUT` invalidates the attempt; it is never scored as a draw.
- Stop for futility when five primary-eligible attempts can no longer be reached.
- Phoenix superiority requires the primary bootstrap interval entirely above `+0.05`.
- hve-core superiority requires it entirely below `-0.05`.
- Equivalence requires it entirely inside `[-0.05, +0.05]`.
- Otherwise the decision is inconclusive.

