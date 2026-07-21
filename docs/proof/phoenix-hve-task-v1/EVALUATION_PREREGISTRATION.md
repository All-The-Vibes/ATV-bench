# Phoenix vs hve-core 20-task evaluation preregistration

Frozen on **Tuesday, July 21, 2026**, before any formal evaluation attempt.

## Frozen identity

- Experiment digest:
  `df75a8992e6af166efb6e4692ee1c7c06d03dbad3fe333767755869c9769ed81`
- Preregistration SHA-256:
  `5b1f76f336cbbea8cadc4e366c140c7723150a6866461cc46cf38aae58a5fe43`
- Experiment file SHA-256:
  `515bdcbb574c00c1bd4161f146397793c9b0cc24b59326690b9218fc36bf48b0`
- Task-selection SHA-256:
  `5b2fdc11722d266ebf6443975fabdd5867787b36ec33f5b6d1b8390df54b665a`
- Schedule SHA-256:
  `1df9ce09c1ab05f44d688ec2e4c505023e0711e264b76e7ed44bc21ea3c672c4`
- Sample-size assumption seal:
  `f3eb3109eff64e23bc9bef0ae319243c6ad74ec3443556f2d99132798af4598c`

## Execution cell

| Field | Frozen value |
|---|---|
| Phoenix commit | `233e8e1e968bbc0b1dc446d7830efa82489bf118` |
| hve-core commit | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` |
| Model | explicit `gpt-5.4` |
| Copilot Linux package | `1.0.71-0`; build `b551bd5896` |
| Host-observed Copilot banner | `GitHub Copilot CLI 1.0.72-1` |
| Budget | 30 AI credits, selected by sealed calibration |
| Timeout | 900 seconds per harness |
| Independent task clusters | 20 |
| Nested paired attempts | 5 per task |
| Total paired attempts | 100 |
| Harness executions | 200 |
| Execution backend | OCI, one container per harness |
| Network | internal-only harness network through a four-host Copilot CONNECT allowlist |

Phoenix image:
`sha256:b918b4dfcc06d4ce97d9af55cdd8e8f407e0bc36475f905348660db07ea0a534`.

hve-core image:
`sha256:497a1f29dd234c3fa538817fc7efb5eb9cc3df473f286ed47d38e815f1df6b20`.

CONNECT proxy image:
`sha256:dc7bc0755dad0b0c5b5bab7379c329a7cfda6df99c6db41048a617c3a3c93592`.

## Schedule

The 100 cells are deterministically category-interleaved. Each task gets a
3/2 harness-first split; the complete schedule is exactly 50 Phoenix-first and
50 hve-first, with 2/2 base-order balance inside every category.

Resume skips only sealed, evidence-rehashed completed cells and preserves the
original schedule indices.

## Primary estimand

End-to-end completion-adjusted task score:

- a reliable harness receives its hidden-grader score;
- an unreliable harness receives zero;
- the five paired attempts are averaged inside each task;
- every task receives equal macro weight;
- bootstrap resampling is over the 20 tasks only.

The conditional score among both-reliable attempts is descriptive and cannot
name a winner.

## Frozen inference

- 10,000 task-bootstrap samples
- bootstrap seed `20260721`
- 95% interval
- superiority/equivalence margin `±0.05`
- reliability sign-test alpha `0.05`
- superiority requires at least 90% informative pairs suite-wide and at least
  4/5 per task with at least one reliable harness
- equivalence requires at least 90% both-reliable pairs suite-wide and at least
  4/5 per task with both harnesses reliable
- category-sensitivity and score/reliability-consistency gates must pass
- any failed gate yields `inconclusive`

## Sample-size boundary

Twenty task clusters can support only a large, consistent effect on this exact
suite. At a task-effect SD of `0.15`, a true `+0.10` effect would require about
71 independent tasks for 80% superiority power. More task families—not more
repetitions—are needed for broader or subtle claims.

## Claim boundary

This study can report only performance on these exact 20 public, synthetic,
machine-reviewed tasks under this pinned cell. It cannot establish overall
harness richness, production sophistication, or a global winner.
