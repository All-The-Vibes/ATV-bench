# Legitimacy and CodeClash alignment audit

Audit date: **July 18, 2026**
Audited default branch: `main` at `8f71eaf663e4b758703ff98542e67c65a5f3ea7f`

## Verdict

ATV-bench is a legitimate, MIT-licensed, early-stage engineering prototype with real
security work around untrusted submitted bots. It is **not currently a reproduction of
the CodeClash paper, a scientifically validated harness benchmark, or a production path
that executes arbitrary harnesses**.

No evidence of a scam, fabricated paper, or malicious dependency was found. The risk is
overclaiming methodology and maturity, not hidden provenance.

## Paper legitimacy

The cited paper is real:

- arXiv: <https://arxiv.org/abs/2511.00839>
- official implementation: <https://github.com/CodeClash-ai/CodeClash>
- ICML 2026 poster: <https://icml.cc/virtual/2026/poster/63924>
- OpenReview: <https://openreview.net/forum?id=SW2D390ePP>

The paper's main experiment evaluates 8 models across 6 arenas, 10 tournaments per model
pair and arena, and 15 rounds per tournament: 25,200 rounds. Each round is an
**edit → compete → feedback** cycle with persistent per-player codebases. Rankings use a
Bradley–Terry maximum-likelihood fit converted to Elo (base 1200, slope 400), with
parametric and non-parametric bootstrap checks.

The paper also has artifact concerns: no frozen GitHub release/tag was found during this
audit, some linked analysis directories were missing, and one appendix sample-count
statement conflicts with the main experiment count. Reproduction should pin a commit and
verify the released artifacts.

## ATV-bench repository signals

Positive:

- public source, MIT license, explicit CodeClash citation;
- trusted referee rather than bot-asserted outcomes;
- strong workflow isolation intent for untrusted submissions;
- substantial hermetic tests and adversarial regression tests;
- README already labels fingerprint correlations as directional.

Maturity concerns observed on July 18, 2026:

- repository created July 15, 2026;
- one effective contributor;
- no tags or releases;
- zero public stars and forks at audit time;
- live leaderboard contained zero rows;
- hard-coded test-count badge was stale;
- default-branch CI passed on Linux, while the suite had Windows-specific failures;
- open PR #14 had no clean install path because its `codeclash` extra was not resolvable.

These are normal early-project risks, not evidence of fraud.

## Requirement matrix

| CodeClash/paper property | ATV-bench `main` |
|---|---|
| Trusted arena outcome | Implemented for a separate Lightcycles referee |
| Harness is executed by benchmark | Missing; workflow executes a prebuilt bot |
| Persistent codebase per player | Missing |
| Edit phase every round | Missing |
| Competition feedback before next edit | Missing |
| Multiple tournaments and repeated simulations | Missing |
| Positional balancing/shuffling | Missing |
| Multiple arenas | One live arena |
| Immutable trajectories and round logs | Missing |
| Bradley–Terry MLE Elo and bootstrap uncertainty | Contradicted by sequential 1500/K=32 Elo |
| Arbitrary harness integration | Process adapter added; local and self-attested only |
| Verified harness/model provenance | Missing |

## Claims that are safe now

- “CodeClash-inspired community bot league”
- “trusted Lightcycles referee”
- “self-attested harness fingerprint”
- “experimental online league Elo”
- “generic local process adapter for headless harness commands”

Do not claim:

- paper reproduction or paper-aligned scores;
- measured harness performance when only a submitted bot ran;
- statistically meaningful harness-feature correlations;
- verified model or harness identity;
- “works for any harness” in a trusted, comparable, publishable sense.

## Path to paper-aligned harness benchmarking

1. Pin a specific official CodeClash commit or vendor an auditable fork.
2. Add a separate versioned `paper-v2` profile rather than changing the community league.
3. Execute each harness in a persistent isolated player workspace every round.
4. Feed prior competition logs into the next edit phase.
5. Capture runner, harness, model, prompt, policy, base-tree, output-tree and artifact
   digests.
6. Run balanced repeated tournaments, not one fixed bot-versus-anchor game.
7. Publish immutable trajectory bundles.
8. Fit paper-compatible Bradley–Terry Elo with documented bootstrap uncertainty.
9. Keep online league Elo separate and clearly named.
10. Require the same black-box conformance suite for process, JSONL, OCI and CI adapters.

The detailed current-state gap analysis, target architecture, security boundary, statistical
design, phased roadmap, and launch gates are in
[`HARNESS_BENCHMARKING_BLUEPRINT.md`](HARNESS_BENCHMARKING_BLUEPRINT.md).
