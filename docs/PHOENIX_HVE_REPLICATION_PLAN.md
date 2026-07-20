# Phoenix versus hve-core replication plan

Frozen on Monday, July 20, 2026, before launching the replication trials.

## Claim

This replication may support only one of:

- `phoenix_better_on_this_task_contract`;
- `hve_better_on_this_task_contract`;
- `practically_equivalent_on_this_task_contract`;
- `inconclusive`.

It cannot establish a global harness winner or a harness-sophistication ranking.

## Frozen execution cell

| Field | Frozen value |
|---|---|
| Phoenix source commit | `233e8e1e968bbc0b1dc446d7830efa82489bf118` |
| hve-core source commit | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` |
| Copilot CLI | `GitHub Copilot CLI 1.0.72-1` |
| Requested model | `gpt-5.4` |
| Model-selection source | explicit CLI argument |
| Model rationale | reproduce the historical cell, not claim it is universally best |
| Harness build timeout | 1,200 seconds |
| AI-credit ceiling | 30 per harness execution |
| Held-out move deadline | 3.0 seconds per turn |
| Held-out seeds per trial | 5, each played with sides swapped |
| Primary fresh trials | 5 |
| Maximum reserve attempts | 3, infrastructure/noncomparable replacement only |
| Tool compatibility policy | identical frontmatter-only `tools: ['*']` shim |
| Network isolation | not technically enforced; explicit limitation |

## Frozen implementation identities

| Component | SHA-256 |
|---|---|
| `scripts/compare_phoenix_hve.py` | `182fcf6b100bef0596113a40d031914d3db5555e0008ddf09d79f68f85ad2b2c` |
| `src/atv_bench/comparison.py` | `1e92551c20dab7e9527004016da2f03f6b7e548c4ecb886fc96793b56906fe5b` |
| `src/atv_bench/arena/engine.py` | `c6eeb8ceea85af433ba8274c7c16c8d7c3444a070b60b17d61cb5ed1047839f3` |
| `src/atv_bench/arena/referee.py` | `e6fa3c9f06971cafa715b6d1a121e5f4966c10a2b0b86c7c84c28f9d7b7be87e` |

Any change to these identities starts a new replication cell; trials cannot be pooled.

## Seed commitment

The canonical UTF-8 JSON seed plan uses sorted keys and compact separators. Its
SHA-256 commitment is:

`228ccbc555d8fdac7ceace194233461fb5b62fd5e2a6c647f1c992e822c9a60e`

The plan contains five primary and three reserve balanced seed sets. Each set covers
every Lightcycles width and height class exactly once. The salt and seed plan are
revealed with the local result bundle after execution.

This commitment prevents post-result seed substitution. It does not create a trusted
hidden-test boundary because the local runner lacks hard filesystem isolation.

## Inclusion policy

A trial is comparable only when both sides satisfy all of the following:

1. exact source, runner, evaluator, CLI, model, prompt, budget, and timeout identity;
2. checksummed schema-v2 evidence with explicit local/non-rankable/unofficial labels;
3. one complete JSONL receipt per harness:
   - zero malformed lines;
   - at least one model-bearing event;
   - exactly one observed model;
   - exact match to `gpt-5.4`;
   - exactly one successful terminal result;
4. successful harness execution;
5. candidate file present, compilable, and smoke-test valid;
6. identical compatibility-shim policy.

Noncomparable attempts are preserved and reported. A reserve set may replace only an
infrastructure/noncomparable attempt; it cannot replace an unfavorable scored result.

## Analysis policy

The independent unit is one fresh paired harness execution. Nested games never
increase trial count.

For each comparable trial:

`score_difference = (Phoenix points - hve-core points) / nested games`

where a win is 1 point, draw 0.5, and task-contract forfeits remain losses.

After at least five comparable trials:

- Phoenix superiority requires the trial-bootstrap 95% interval to lie entirely
  above `+0.05`;
- hve-core superiority requires it to lie entirely below `-0.05`;
- equivalence requires the full interval to lie inside `[-0.05, +0.05]`;
- otherwise the decision is inconclusive.

The report also publishes:

- exact two-sided sign test over decisive trials;
- completed-game-only Phoenix/HVE/draw totals;
- timeout/crash/other forfeit decomposition;
- artifact and execution reliability separately;
- all exclusions and replacement reasons.

## Pre-result attempt ledger

At `2026-07-20T20:20:17.9032121Z`, before inspecting a result from `primary-1`,
unrelated local pytest sweeps were discovered running concurrently with that attempt.
The sweeps were stopped, but `primary-1` was prospectively marked
`excluded_infrastructure_contamination`. Its artifacts remain preserved, and
`reserve-1` replaces it. This decision cannot be reversed based on its outcome.

After `primary-4`, the operator stopped the cell before obtaining a `primary-5`
result or launching reserves. Three uncontaminated attempts had produced Phoenix
valid artifacts in two attempts and hve-core valid artifacts in zero; a fourth
attempt produced neither artifact. The fixed 30-credit budget repeatedly ended
sessions during destructive file replacement. At that point five comparable trials
were no longer reachable, and continuing would only repeat an uncalibrated task
contract.

The v3 cell is therefore closed as a **calibration failure**. Its defensible output is
a Phoenix-favoring completion-reliability signal, not a conditional bot-quality
winner. A replacement cell must first pass a non-scored budget/feasibility calibration
for both harnesses and preregister separate reliability and conditional-quality
estimands.
