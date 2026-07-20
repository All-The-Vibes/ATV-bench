# Phoenix versus hve-core v4 evaluation plan

Frozen on Monday, July 20, 2026, after calibration passed and before launching any
v4 evaluation attempt.

## Calibration prerequisite

Two independent non-scored 60-credit attempts both produced valid Phoenix and
hve-core artifacts with complete model receipts and successful terminal executions.

- selected budget: **60 AI credits**;
- calibration decision: `calibrated`;
- calibration artifact SHA-256:
  `f32ea3a755247b8759fc0afa99dfaba3cda84212f9e6804ee3751ab01032a2df`.

No 90-credit calibration is permitted because the smallest passing budget must be
selected.

## Frozen execution cell

| Field | Value |
|---|---|
| Phoenix commit | `233e8e1e968bbc0b1dc446d7830efa82489bf118` |
| hve-core commit | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` |
| Copilot CLI | `GitHub Copilot CLI 1.0.72-1` |
| Model | explicit `gpt-5.4` |
| AI-credit budget | 60 |
| Harness wall timeout | 1,200 seconds |
| Move deadline | 3.0 seconds |
| Held-out seeds per attempt | 5, each side-swapped |
| Primary attempts | 5 |
| Maximum attempts | 8 |
| Tool compatibility shim | identical frontmatter-only `tools: ['*']` |
| Network isolation | not technically enforced; explicit limitation |

## Frozen implementation identities

| Component | SHA-256 |
|---|---|
| `scripts/compare_phoenix_hve.py` | `a53c279abd6ed4e87188ab85a5fe7ca45d16890a8cda9133ff559cc3a45054b0` |
| `scripts/summarize_phoenix_hve_v2.py` | `d5aeac6292e0d8e796ca09746e3bd08d587611f11431115b813bb32fd6d8ecd3` |
| `src/atv_bench/comparison.py` | `1e92551c20dab7e9527004016da2f03f6b7e548c4ecb886fc96793b56906fe5b` |
| `src/atv_bench/arena/engine.py` | `c6eeb8ceea85af433ba8274c7c16c8d7c3444a070b60b17d61cb5ed1047839f3` |
| `src/atv_bench/arena/referee.py` | `e6fa3c9f06971cafa715b6d1a121e5f4966c10a2b0b86c7c84c28f9d7b7be87e` |

Any identity or budget change starts a new cell.

## Seed commitment

The salted canonical seed-plan commitment is:

`54bb8624e2dc7922e22431656cb9a5791a248a706006f1b655bd31ac01818adc`

The plan contains five primary and three reserve balanced sets. Each set contains
every Lightcycles width and height class exactly once. The seed plan is revealed
with the local result bundle after execution.

This commitment prevents post-result substitution but is not a trusted hidden-test
boundary because local filesystem isolation is not enforced.

## Primary and secondary estimands

### Primary: end-to-end task success

Every identity-valid paired execution contributes:

- Phoenix valid / hve-core invalid: `+1`;
- Phoenix invalid / hve-core valid: `-1`;
- both invalid: `0`;
- both valid: normalized held-out game score difference.

This estimand measures whether the harness produces a useful artifact under the
frozen task, model, and budget.

### Secondary: conditional artifact quality

Only attempts where both artifacts are valid contribute. This answers which generated
bot performs better, conditional on both harnesses completing.

Artifact-validity rates and paired discordant outcomes are published separately.

## Decision and stopping rules

- Minimum primary attempts: 5.
- Maximum attempts: 8.
- Reserve sets replace identity-invalid/infrastructure attempts only.
- An unfavorable valid attempt is never replaced.
- Stop for futility if the chosen primary estimand can no longer reach five eligible
  attempts within the frozen maximum.
- Phoenix superiority requires the primary trial-bootstrap 95% interval entirely
  above `+0.05`.
- hve-core superiority requires it entirely below `-0.05`.
- Equivalence requires it entirely inside `[-0.05, +0.05]`.
- Otherwise report inconclusive.

Conditional quality, reliability, completed-game results, forfeits, exact sign tests,
and all exclusions remain visible regardless of the primary decision.

