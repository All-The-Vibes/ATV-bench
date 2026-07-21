# ATV-Phoenix vs hve-core local case study

Status as of July 21, 2026:

- bounded Lightcycles v5: **inconclusive**;
- preregistered 20-task suite: **practically equivalent; no winner**.

## Preregistered 20-task result

The follow-up study used 20 distinct public synthetic tasks, four from each of
five categories, with five paired attempts nested inside every task. The task—not
the attempt—was the independent cluster.

All 100 paired attempts were both-reliable and model-attested under isolated OCI
execution with an endpoint-allowlisted Copilot proxy.

| Metric | Result |
|---|---:|
| Phoenix macro score | 0.815000 |
| hve-core macro score | 0.823571 |
| Phoenix minus hve-core | -0.008571 |
| Task-bootstrap 95% interval | [-0.022857, 0.000000] |
| Reliability | both 100/100 |

The full interval lies inside the preregistered `[-0.05, +0.05]` practical-
equivalence region. All formal gates passed.

> **Final task-suite decision: practically equivalent. No winner.**

Eighteen task means tied. hve-core had higher means on
`debugging.10-premature-rounding` and `recovery.03-queue-order`; Phoenix had no
higher task mean. This narrow result still does not measure overall harness
richness or sophistication because the suite consists of easy deterministic
repair, retrieval, recovery, and arithmetic fixtures.

See `docs/proof/phoenix-hve-task-v1/TASK_EVALUATION_RESULT.md`.

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

## Calibrated bounded v5 result

V5 passed two complete non-scored calibrations before evaluation:

- 60 AI credits;
- compact boards;
- 40-turn cap;
- 3-second per-turn timeout;
- 60-second per-match timeout;
- no calibration forfeits or match timeouts.

All five hidden-seed evaluation attempts were both-valid, model-attested, and
evaluator-valid.

| Trial | Phoenix wins | hve-core wins | Draws | Trial outcome |
|---|---:|---:|---:|---|
| primary-1 | 5 | 0 | 5 | Phoenix |
| primary-2 | 2 | 2 | 6 | Tie |
| primary-3 | 1 | 2 | 7 | hve-core |
| primary-4 | 2 | 3 | 5 | hve-core |
| primary-5 | 0 | 0 | 10 | Tie |
| **Total** | **10** | **7** | **33** | hve-core 2 trials, Phoenix 1, ties 2 |

Primary end-to-end and secondary conditional-quality results are identical because
both harnesses produced valid artifacts in all five trials:

- mean Phoenix-minus-hve score difference: **+0.06**;
- trial-bootstrap 95% interval: **[-0.08, +0.28]**;
- exact two-sided sign-test p-value: **1.0**;
- artifact validity: Phoenix **5/5**, hve-core **5/5**;
- forfeits: **0**;
- evaluator match timeouts: **0**.

### Final v5 conclusion

> **Inconclusive.** hve-core won more independent trials (2 versus 1), while Phoenix
> won more nested games (10 versus 7) and had a small positive mean score difference.
> The uncertainty interval crosses zero and both practical-margin boundaries.

There is no statistically supported task-contract winner. If a descriptive
trial-count leader must be named, it is **hve-core**; that is not the formal benchmark
decision and is not an overall harness ranking.
