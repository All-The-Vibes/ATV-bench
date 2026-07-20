# Phoenix versus hve-core v4 calibration plan

Frozen on Monday, July 20, 2026, before launching any v4 calibration attempt.

## Why this cell exists

The v3 30-credit cell repeatedly ended while a harness was replacing `main.py`.
It measured completion under an uncalibrated budget and could not produce paired
quality observations.

V4 therefore starts with a **public, non-scored completion-feasibility calibration**.
No held-out games may run until both harnesses can reliably leave valid artifacts.

## Frozen calibration cell

| Field | Value |
|---|---|
| Phoenix commit | `233e8e1e968bbc0b1dc446d7830efa82489bf118` |
| hve-core commit | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` |
| Copilot CLI | `GitHub Copilot CLI 1.0.72-1` |
| Model | explicit `gpt-5.4` |
| Phase | `calibration` |
| Candidate budgets | 60, then 90 AI credits |
| Attempts per budget | 2 |
| Required pass rate | 100% for Phoenix, hve-core, and paired validity |
| Harness wall timeout | 1,200 seconds |
| Tool compatibility shim | identical frontmatter-only `tools: ['*']` |
| Held-out games | prohibited |

The 30-credit cell is not retested because v3 already established it as infeasible.

## Frozen implementation identities

| Component | SHA-256 |
|---|---|
| `scripts/compare_phoenix_hve.py` | `a53c279abd6ed4e87188ab85a5fe7ca45d16890a8cda9133ff559cc3a45054b0` |
| `src/atv_bench/comparison.py` | `1e92551c20dab7e9527004016da2f03f6b7e548c4ecb886fc96793b56906fe5b` |
| `src/atv_bench/arena/engine.py` | `c6eeb8ceea85af433ba8274c7c16c8d7c3444a070b60b17d61cb5ed1047839f3` |
| `src/atv_bench/arena/referee.py` | `e6fa3c9f06971cafa715b6d1a121e5f4966c10a2b0b86c7c84c28f9d7b7be87e` |

Any identity change starts a new calibration cell.

## Calibration decision

At each budget, run two fresh paired attempts. An attempt passes only when both
harnesses have:

1. a complete exact-model JSONL receipt;
2. a successful terminal harness execution;
3. `main.py` present;
4. successful compilation;
5. a passing smoke test.

Select the smallest budget with two passing paired attempts. Stop immediately after
selection. If neither 60 nor 90 passes, declare the Lightcycles task contract
unsuitable for a scored comparison and do not launch evaluation.

Calibration artifacts and failures are preserved but never scored as harness quality.

## Evaluation policy after calibration

Only after calibration passes will a separate evaluation plan be frozen.

- Primary estimand: **end-to-end task success**.
  - Phoenix valid / hve invalid = `+1`.
  - Phoenix invalid / hve valid = `-1`.
  - both invalid = `0`.
  - both valid = held-out game score difference.
- Secondary estimand: **conditional artifact quality** using only both-valid trials.
- Reliability rates and paired discordance are always published separately.
- Minimum evaluation attempts: 5.
- Maximum attempts: 8.
- Stop for futility when the primary estimand can no longer reach five eligible
  trials within the frozen maximum.
- Winner/equivalence intervals must clear the configured `±0.05` margin.

