# Phoenix vs hve-core task calibration preregistration

Frozen on **Tuesday, July 21, 2026**, before any calibration outcome.

## Purpose

This phase selects only an execution budget. It is non-scored, non-rankable,
and cannot support a harness-quality claim.

## Frozen cell

- Benchmark implementation commit: `a526a8d`
- Calibration plan digest:
  `8ed128ece69b17c17548530129eed4a095dc0dd9a96b968f9a77979f3afe19a4`
- Full plan file SHA-256:
  `6ca6b150ff9eb4fdd8634f3ca5a41d37bd128df2457d8dc82629616ca8be1081`
- Model: explicit `gpt-5.4`
- Candidate budgets, in order: `30`, `60` AI credits
- Harness timeout: `900` seconds
- Randomization seed: `20260721`
- Runtime: isolated OCI container per harness
- Network: internal-only harness network; egress only through a CONNECT proxy
  allowlisting the four GitHub Copilot API hosts
- Token transport: one-shot `GITHUB_ASKPASS` FIFO over `docker exec` stdin;
  no OAuth token in Docker argv, environment, or inspect metadata

## Held-out tasks

Exactly one task outside the selected 20 from each category:

| Category | Task | Task digest |
|---|---|---|
| context-retrieval | `pilot.context-retrieval.08-compliance-mode` | `278bdf0c0aeea2f9ff4af0b0defa1d5447e78446d82497e8ecba3917d49e95ef` |
| debugging | `pilot.debugging.08-retry-counter` | `0333dba4370b8ae3d040f2ca31800292aa4e43a818b6b9ba9077a2206a1b7bfe` |
| greenfield | `pilot.greenfield.10-median-value` | `1190934c102bc8a4e097035efea5030b139dc620ffe1f1895a0f9ef7d2f31e4b` |
| recovery | `pilot.recovery.09-cursor-position` | `77ce92fd59514dea88c2d09a1f6f8086760ac3ff32be5972c1a9d6cffb6e49cd` |
| repair | `pilot.repair.09-retry-limit` | `0aaf933c65fda19781d14a807f4365e566049b6074e4942e1e20eadfa08f0978` |

## Selection rule

Execute all frozen candidate cells. Select the smallest ascending budget for
which **both** harnesses are reliable and artifact-valid on **every** held-out
task. Calibration quality scores and task pass/fail outcomes are not used.

If neither candidate passes, no evaluation budget is selected and the formal
20-task study does not launch.

## Bound images

| Runtime | Image ID | Build-spec SHA-256 |
|---|---|---|
| Phoenix | `sha256:b918b4dfcc06d4ce97d9af55cdd8e8f407e0bc36475f905348660db07ea0a534` | `e7c5ad4b47278d3286d459927ff9cd2537faaed5d8c46a2ecbf950cadf9fe010` |
| hve-core | `sha256:497a1f29dd234c3fa538817fc7efb5eb9cc3df473f286ed47d38e815f1df6b20` | `a2afd332372228ca610c8470d078b9a0e85862085c3f5619188f8746f14ffe51` |
| CONNECT proxy | `sha256:dc7bc0755dad0b0c5b5bab7379c329a7cfda6df99c6db41048a617c3a3c93592` | `6c540f01c0b0b74ab6c43b63abc08f81ae4ad395b2715a55218ac9a532bac3c4` |

The harness sources remain pinned to Phoenix
`233e8e1e968bbc0b1dc446d7830efa82489bf118` and hve-core
`5c15a03c78da2408527693e0fc3b3e387bf99cb2`.

## Claim boundary

Passing calibration establishes only that the selected budget can execute both
harnesses across this five-category feasibility set. It does not estimate a
win rate, quality difference, or production-sophistication ranking.
