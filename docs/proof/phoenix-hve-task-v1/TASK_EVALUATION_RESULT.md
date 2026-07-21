# Phoenix vs hve-core: 20-task clustered result

Completed on **Tuesday, July 21, 2026** under the separately committed
preregistration.

## Formal decision

> **Practically equivalent on this exact 20-task suite. No winner.**

| Metric | Result |
|---|---:|
| Eligible task clusters | 20/20 |
| Paired attempts | 100 |
| Phoenix macro score | 0.815000 |
| hve-core macro score | 0.823571 |
| Phoenix minus hve-core | -0.008571 |
| Task-bootstrap 95% interval | [-0.022857, 0.000000] |
| Practical-equivalence region | [-0.05, 0.05] |
| Phoenix reliability | 100/100 |
| hve-core reliability | 100/100 |

The complete interval lies inside the preregistered equivalence region. All
portfolio, binding, infrastructure-validity, informative-coverage,
category-sensitivity, and score/reliability-consistency gates passed.

## What actually differed

Eighteen of twenty task means were tied.

| Task | Phoenix | hve-core | Difference |
|---|---:|---:|---:|
| `pilot.debugging.10-premature-rounding` | 0.428571 | 0.542857 | -0.114286 |
| `pilot.recovery.03-queue-order` | 0.942857 | 1.000000 | -0.057143 |

`pilot.greenfield.08-join-segments` had one Phoenix-higher attempt and one
hve-higher attempt, which canceled at the task-mean level.

Descriptively, the 100 nested attempt pairs were Phoenix-higher 1, hve-higher
4, and tied 95. These counts are **not** treated as independent inference.

## Category sensitivity

| Category | Phoenix | hve-core | Difference |
|---|---:|---:|---:|
| context-retrieval | 0.571429 | 0.571429 | 0.000000 |
| debugging | 0.614286 | 0.642857 | -0.028571 |
| greenfield | 0.975000 | 0.975000 | 0.000000 |
| recovery | 0.914286 | 0.928571 | -0.014286 |
| repair | 1.000000 | 1.000000 | 0.000000 |

Removing any one category preserved the practical-equivalence decision.

## Execution integrity

- exact task-selection digest:
  `5b2fdc11722d266ebf6443975fabdd5867787b36ec33f5b6d1b8390df54b665a`
- experiment digest:
  `df75a8992e6af166efb6e4692ee1c7c06d03dbad3fe333767755869c9769ed81`
- preregistration SHA-256:
  `5b1f76f336cbbea8cadc4e366c140c7723150a6866461cc46cf38aae58a5fe43`
- aggregate JSON SHA-256:
  `4eca8de6e6d7ca86e2c7deea50131465de874ff1aa62995bd673de64f38826d5`
- raw evidence manifest:
  7,598 files, 85,162,204 bytes,
  canonical SHA-256
  `f1ff562833bcfe942c9dcd7f7c4132d97c635c1af42a4f3b9aad1744012ce29a`

Every completed attempt was revalidated in a final zero-execution resume sweep:
100 checkpoints resumed, zero executions launched, and all referenced grades,
artifacts, receipts, raw logs, and OCI evidence rehashed successfully.

Several infrastructure runs stopped fail-closed before grading and were retried
under the unchanged schedule. They are not benchmark observations. All scored
attempts had valid model receipts, successful container cleanup, and both
harnesses reliable.

## Interpretation

The earlier Lightcycles pilot could be described as hve-core leading 2 trials
to 1, but it was formally inconclusive. This broader task study gives the more
defensible answer: **neither harness won; their completion-adjusted performance
was practically equivalent on these easy public synthetic tasks.**

This does not mean the harnesses are equally sophisticated. The suite mostly
tests deterministic file repair, retrieval, and arithmetic. It does not measure
Phoenix's full orchestration richness, subagent design, or production feature
breadth.

## Claim boundary

This result is local, self-attested, unofficial, and non-rankable. It applies
only to the pinned commits, model, budget, OCI runtime, and exact 20 tasks. It
does not establish a global harness winner.
