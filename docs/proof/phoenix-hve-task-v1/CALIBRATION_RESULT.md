# Phoenix vs hve-core task calibration result

Completed on **Tuesday, July 21, 2026** against the previously committed
calibration plan.

## Decision

**Selected budget: 30 AI credits per harness execution.**

Both frozen candidate cells passed:

| Candidate | Held-out tasks complete | Phoenix reliable/artifact-valid | hve-core reliable/artifact-valid | Status |
|---:|---:|---:|---:|---|
| 30 | 5/5 | 5/5 | 5/5 | passed |
| 60 | 5/5 | 5/5 | 5/5 | passed |

The preregistered rule selects the smallest passing candidate, so the formal
20-task evaluation must use `30`.

## Evidence seals

- Calibration plan digest:
  `8ed128ece69b17c17548530129eed4a095dc0dd9a96b968f9a77979f3afe19a4`
- Sealed calibration summary:
  `c8c3a0a719a61bfb0560460da37de9a8e548c3ddd40102240aac769f548f41e7`
- Calibration summary file SHA-256:
  `e36729bf38bc183362b250a3def11272346783bc65f8598c4270f9dd234395ac`

The cell contains 10 paired task attempts and 20 isolated harness executions.
Quality scores and task pass/fail outcomes were not used to select the budget.

## Transient infrastructure event

The first pass through the 30-credit repair task stopped fail-closed when one
hve-core OCI run did not return verified execution evidence. The hidden grader
was not loaded, no attempt was checkpointed or scored, and Docker cleanup
confirmed the harness container, proxy, and both networks absent. Resuming the
same frozen cell produced a fully valid paired attempt; all five 30-credit tasks
then passed.

This event is reported as execution history, not counted as a benchmark outcome.

## Claim boundary

This result proves only completion feasibility for the selected budget under
the pinned OCI cell. It does not indicate which harness is better.
