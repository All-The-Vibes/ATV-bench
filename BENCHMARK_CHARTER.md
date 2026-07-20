# ATV-Bench Benchmark Charter

Status: pre-alpha
Effective date: July 19, 2026

## Purpose

ATV-Bench evaluates coding-agent harnesses with reproducible tasks, fresh independent trials,
trusted grading, explicit resource budgets, and uncertainty-aware reporting.

## Product separation

ATV-Bench maintains distinct tracks:

1. **ATV League**: public bot competition and online Elo for entertainment/community signal.
2. **ATV Controlled**: same model, task, tools, environment, and budget; isolates harness effects.
3. **ATV Systems**: each harness uses its preferred full stack; measures complete-system performance.
4. **ATV Resilience**: injected failures and recovery/verification behavior.

League scores never enter Controlled or Systems rankings.

## Primary estimand

The primary Controlled metric is:

> Probability that a fresh harness execution solves a previously unseen task under a fixed,
> declared resource budget.

One trial means one new harness process, one clean immutable task snapshot, one policy/budget,
one produced artifact, and one trusted grader result.

Tests, games, simulations, tool calls, and iterative rounds are nested observations. They do not
increase the harness trial count.

## Trust tiers

| Tier | Meaning | Rankable |
|---|---|---:|
| Local self-attested | Developer-run without trusted runner evidence | No |
| Community reproducible | Public bundle another user can replay | No |
| Official attested | Executed and graded by the official trusted runner | Yes |
| Independently reproduced | Official result reproduced by a separate operator | Yes, marked |

## Winner rule

ATV-Bench names a Controlled-track winner only when:

1. The effect exceeds a preregistered practical-equivalence margin.
2. The confidence interval excludes zero and the equivalence region.
3. Direction is stable under task resampling.
4. The result is not explained solely by crashes, no-edits, or infrastructure faults.
5. Direction persists across at least two immutable model policies.
6. No unresolved contamination or grader-validity incident exists.

Otherwise the report says `inconclusive`, `equivalent`, `more reliable but not higher-quality`, or
`better in this category/budget only`.

## Minimum official evidence

Every official result binds:

- benchmark/task/protocol/runner versions;
- immutable harness source and runtime digest;
- task image, prompt, base-tree, tool, network, and budget policy digests;
- requested and gateway-resolved model identity;
- complete normalized trajectory and usage;
- output-tree and artifact digests;
- trusted grader digest and result;
- accepted/excluded trial set and analysis configuration.

## Current status

No current ATV-Bench harness result is official or rankable under this charter. The existing
community board is ATV League. Local harness comparisons are experimental case studies.
