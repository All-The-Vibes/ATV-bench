# Governance

## Roles

Critical responsibilities require a bus factor of at least two:

- release maintainers;
- task-review maintainers;
- runner/security maintainers;
- statistics reviewers;
- appeals/reproduction reviewers.

No person may approve their own task, adapter, security-sensitive runner change, or score appeal.

## Repository controls required for official releases

- protected default branch;
- required CI, security, task-audit, and reproducibility checks;
- CODEOWNERS review enforced;
- administrators do not bypass required review except documented emergency recovery;
- third-party Actions pinned to full commit SHAs;
- protected official-evaluation environment;
- signed release tags and published changelog.

If these controls are not active, ATV-Bench may publish development artifacts but not official
benchmark rankings.

## Task governance

Every task requires:

1. Author conflict disclosure.
2. Independent technical reviewer.
3. Oracle pass.
4. No-op failure.
5. Existing regression pass.
6. Alternative-correct-solution pass.
7. Exploit/mutation audit.
8. Deterministic grader replay.
9. License and redistribution review.
10. Contamination classification.

Task authors do not receive official harness results before release.

## Harness submissions

- Local runs are unlimited and unranked.
- Official submissions are limited to two per harness per benchmark release.
- One leaderboard row identifies an immutable harness/runtime digest, adapter version, and model policy.
- Scores never accumulate by GitHub login across changing harness versions.
- Reruns are allowed only for confirmed infrastructure faults.
- Top-three and a random 10% of official submissions receive manual audit.

## Conflicts

Maintainers disclose employment, funding, ownership, or substantial contribution relationships with
submitted harnesses. A conflicted maintainer cannot access private tasks for that submission, grade
it, approve it, or decide its appeal.

## Appeals

- Appeal window: 14 calendar days from publication.
- Initial response target: five business days.
- One independent reviewer and one final appeal.
- Outcomes: upheld, rerun, invalidated, benchmark defect, adapter defect, or infrastructure defect.

Every outcome is added to the public incident/retraction log.

## Releases

- Benchmark releases are immutable.
- Task corrections create a new semantic version and leaderboard.
- Historical scores are never silently rewritten.
- Private tasks rotate on a declared cadence.
- Major claims require one independent reproduction.
