# Phoenix versus hve-core v5 proof index

Generated from the calibrated bounded evaluation completed on July 20, 2026.

## Result

- Formal decision: **inconclusive**
- Primary estimand: end-to-end task success
- Trial outcomes: hve-core 2, Phoenix 1, ties 2
- Nested games: Phoenix 10, hve-core 7, draws 33
- Mean Phoenix-minus-hve score: `+0.06`
- Trial-bootstrap 95% interval: `[-0.08, +0.28]`
- Exact two-sided sign-test p-value: `1.0`
- Artifact validity: both harnesses `5/5`
- Bot forfeits: `0`
- Evaluator match timeouts: `0`

hve-core is the descriptive independent-trial leader. Phoenix is the descriptive
nested-game and mean-score leader. Because those summaries disagree and the interval
does not pass superiority or equivalence gates, no formal winner is claimed.

## Scope

This is local, self-attested, non-rankable evidence for one `gpt-5.4` compact-board
Lightcycles contract. It is not protocol-v1 OCI attestation and not an overall harness
ranking.

`evidence-index.json` binds the local aggregate, summary, revealed seed plan, and every
trial's comparison/checksum manifest by exact size and SHA-256. Raw logs and generated
candidate files remain in the local report bundle and are addressed by each trial's
`checksums.json`.

