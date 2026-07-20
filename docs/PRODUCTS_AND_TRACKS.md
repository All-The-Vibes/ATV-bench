# ATV-bench Products and Tracks

ATV-bench separates four products because they answer different questions. Their
results must never be merged into one ranking.

## ATV League

- **Question:** How did a submitted frozen bot perform in the community arena?
- **Unit:** A refereed bot match.
- **Allowed score:** Online Elo and match history.
- **Trust:** The arena outcome is trusted; the harness fingerprint is self-attested.
- **Claim limit:** No conclusion about general harness quality.

## ATV Controlled

- **Question:** What is the effect of harness behavior when model, task, tools,
  environment, budget, and schedule are held fixed?
- **Unit:** One fresh harness execution in one clean task workspace.
- **Primary metric:** Task success across previously unseen tasks.
- **Analysis:** Paired, task-clustered effects with uncertainty and a preregistered
  practical-equivalence margin.
- **Winner rule:** Declare a winner only when the confidence interval clears the
  practical margin and operational quality gates pass. Otherwise report equivalent or
  inconclusive.

## ATV Systems

- **Question:** How well does a complete preferred agent system perform at a stated
  cost and latency?
- **Unit:** One fresh full-system execution.
- **Allowed variation:** Model, tools, subagents, memory, plugins, and routing.
- **Claim limit:** This is system performance, not a causal harness-only effect.

## ATV Resilience

- **Question:** How safely and effectively does a system recover from failures?
- **Unit:** One fresh execution with a preregistered injected fault.
- **Metrics:** Recovery, verification, rollback, retry discipline, silent-failure
  avoidance, task success, cost, and latency.

## Trust tiers

1. **Local self-attested:** development only; never officially ranked.
2. **Community reproducible:** public bundle and replay; visibly unofficial.
3. **Official attested:** benchmark-run, signed, and eligible for reports.
4. **Independently reproduced:** an official bundle rerun by a separate operator.

Every command, bundle, report, and UI must display both the product track and trust
tier. Official and self-hosted results must not share a rank table.

## Independent trial rule

The hierarchy is:

```text
task
└── fresh harness trial
    └── generated artifact
        └── tests, games, simulations, or rounds
```

Tests, games, and rounds improve measurement of one generated artifact. They do not
increase the number of independent harness trials.
