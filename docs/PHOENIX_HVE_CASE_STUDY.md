# ATV-Phoenix vs hve-core local case study

Status as of July 20, 2026: **inconclusive**.

## Claim boundary

This was one local Windows case study using GitHub Copilot CLI, a runner-selected
`gpt-5.4` model identifier, and one synthetic Lightcycles task. It is:

- local and self-attested;
- non-rankable and unofficial;
- not protocol-v1 OCI evidence;
- not a general harness-sophistication ranking.

The historical runner passed `--model gpt-5.4` to both Copilot processes. It did not
preserve evidence proving whether the experiment-level model choice came from an
explicit operator decision or the runner's then-hard-coded default.

## Historical descriptive result

Five historical both-artifact-valid trials produced:

| Result | Count |
|---|---:|
| Phoenix trial wins | 3 |
| hve-core trial wins | 0 |
| Tied trials | 2 |
| Phoenix nested-game wins | 33 |
| hve-core nested-game wins | 6 |
| Draws | 5 |

Those game totals are not independent samples. One fresh paired harness execution is
the trial unit.

## Reliability versus completed gameplay

Twenty-six of Phoenix's 33 wins were HVE forfeits recorded by the historical referee
as `CRASH`. Instrumented replay on representative affected seeds from all affected
trials showed the HVE-generated bot exceeding the 3-second per-turn deadline with no
Python exception output. The old referee conflated timeout, EOF, and invalid response
under `CRASH`; the current referee records `TIMEOUT` separately.

Among games with no recorded forfeit:

| Result | Count |
|---|---:|
| Phoenix wins | 7 |
| hve-core wins | 6 |
| Draws | 5 |

The historical evidence therefore shows a Phoenix-favoring **end-to-end reliability
signal**, not clear tactical domination.

## Why there is no formal winner

### Finalized evidence contract

The historical trial documents predate the finalized v2 contract. They omit required
explicit fields including non-rankable/unofficial status, trust tier, fresh paired
trial identity, equal compatibility-shim confirmation, and tracked-tree listing
digests.

Two model receipts are also incomplete:

- r4 HVE JSONL is front-truncated;
- r5 Phoenix JSONL is front-truncated.

The current strict summarizer consequently includes **0 of 5 required** fresh trials.

### Statistical gate

Analyzing all five historical trials descriptively yields:

- mean score difference: `+0.406667`;
- trial-bootstrap 95% interval: `[+0.040000, +0.773333]`;
- configured practical superiority margin: `+0.050000`;
- exact two-sided sign-test p-value over decisive trials: `0.25`.

The lower interval bound (`0.04`) does not clear the superiority margin (`0.05`).
The five-trial/margin policy was introduced after the first valid trial completed, so
it is an analysis rule rather than a prospectively preregistered rule.

## Defensible conclusion

> The result is inconclusive. Historical evidence favors Phoenix on end-to-end
> Lightcycles artifact reliability, while completed-game tactical outcomes are nearly
> even. No global harness winner is established.

Five new fresh trials under the hardened explicit-model, complete-receipt runner are
required before reconsidering a task-contract-specific winner.

## Hardened v3 replication outcome

A hardened replication cell was launched later on July 20, 2026. The first attempt
was prospectively excluded when unrelated local test load was discovered before
result inspection. The next three uncontaminated attempts produced:

| Attempt | Phoenix artifact | hve-core artifact | Quality game |
|---|---:|---:|---:|
| primary-2 | valid | invalid | not run |
| primary-3 | valid | invalid | not run |
| primary-4 | invalid | invalid | not run |

Both harnesses had complete `gpt-5.4` JSONL receipts and successful terminal
executions. Invalid artifacts occurred because sessions ended during destructive
replacement of `main.py`; hve-core failed to leave an artifact in all three clean
attempts, while Phoenix succeeded in two.

The remaining attempts were stopped because the frozen 30-credit cell could no
longer reach five paired-valid trials and was repeatedly terminating mid-edit.

Therefore:

- Phoenix completion reliability: **2/3** clean attempts;
- hve-core completion reliability: **0/3** clean attempts;
- paired-valid quality trials: **0**;
- v3 verdict: **calibration failure**.

The next cell must run non-scored completion calibration first. Its primary
end-to-end estimand must count one-sided invalid artifacts as task failures, while
conditional bot quality remains a separately reported secondary estimand.
